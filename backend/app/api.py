from __future__ import annotations

import secrets
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import ai, bots, mail, meetings, orgsync, workflow
from .config import UPLOAD_DIR, settings
from .db import get_db
from .models import (FINAL, MIN_TEXT, PRIORITIES, ROLES, SOURCES, STATUSES, TYPES, Appeal, Attachment, Category, Department,
                     Digest, Event, Meeting, MeetingItem, Notification, OrgSync, User, now)
from .security import admin_only, check_init_data, check_password, current_user, handler, make_token, overseer

router = APIRouter(prefix="/api")
MAX_FILE = 20 * 1024 * 1024
MAX_MEETING_FILE = 1024 * 1024 * 1024


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def bad(msg: str, code: int = 400):
    raise HTTPException(code, msg)


# ---------- представления ----------

def user_brief(u: User | None) -> dict | None:
    if not u:
        return None
    return {"id": u.id, "name": u.name, "position": u.position, "role": u.role, "is_absent": u.is_absent,
            "department": u.department.name if u.department else None}


def user_full(u: User) -> dict:
    return {**user_brief(u), "email": u.email, "department_id": u.department_id, "deputy_id": u.deputy_id,
            "telegram": bool(u.tg_chat_id), "max": bool(u.max_user_id), "active": u.active, "role_label": ROLES[u.role],
            "synced": sorted(u.ext_ids or {})}


def author_view(a: Appeal, viewer: User) -> dict:
    if a.anonymous and a.author_id != viewer.id:
        return {"name": "Анонимно", "anonymous": True}
    if a.author:
        return {"id": a.author.id, "name": a.author.name, "position": a.author.position,
                "department": a.author.department.name if a.author.department else None, "anonymous": a.anonymous}
    return {"name": a.author_label or "Автор не указан", "external": True}


def appeal_row(a: Appeal, viewer: User) -> dict:
    return {
        "id": a.id, "number": a.number, "type": a.type, "title": a.title, "status": a.status, "priority": a.priority,
        "source": a.source, "category": a.category.name if a.category else None, "category_id": a.category_id,
        "department": a.department.name if a.department and not (a.anonymous and a.author_id != viewer.id) else None,
        "author": author_view(a, viewer), "assignee": user_brief(a.assignee), "created_at": iso(a.created_at),
        "updated_at": iso(a.updated_at), "due_at": iso(a.due_at), "closed_at": iso(a.closed_at),
        "overdue": workflow.overdue(a), "supports": len(a.supports), "mine": a.author_id == viewer.id,
        "summary": a.ai_summary, "ai_filled": a.ai_filled,
    }


def event_view(e: Event, a: Appeal, viewer: User) -> dict:
    actor = e.actor.name if e.actor else None
    if e.actor_id and a.anonymous and e.actor_id == a.author_id and viewer.id != a.author_id:
        actor = "Автор"
    if not e.actor_id:
        actor = "ИИ-помощник" if e.data.get("by_ai") else "Система"
    return {"id": e.id, "kind": e.kind, "text": e.text, "data": e.data, "internal": e.internal, "actor": actor,
            "actor_id": None if actor == "Автор" else e.actor_id,
            # роль нужна для цвета аватара; у анонимного автора и у системы ее не показываем
            "actor_role": e.actor.role if e.actor and actor != "Автор" else None,
            "created_at": iso(e.created_at)}


def appeal_detail(db: Session, a: Appeal, viewer: User) -> dict:
    can_handle = workflow.can_handle(viewer, a)
    is_author = a.author_id == viewer.id
    events = [event_view(e, a, viewer) for e in a.events if can_handle or not e.internal]
    open_q = workflow.open_question(a)
    sim = [{"id": r.id, "number": r.number, "title": r.title, "status": r.status, "score": s,
            "can_open": workflow.can_view(viewer, r)}
           for r, s in workflow.similar(db, f"{a.title}. {a.text}", exclude_id=a.id)]
    return {
        **appeal_row(a, viewer), "text": a.text, "solution": a.solution, "resolution": a.resolution,
        "first_response_at": iso(a.first_response_at), "events": events,
        "attachments": [{"id": f.id, "filename": f.filename, "size": f.size} for f in a.attachments],
        "similar": sim, "meeting_id": a.meeting_id if can_handle else None,
        "open_question": {"text": open_q.text, "by_ai": bool(open_q.data.get("by_ai"))} if open_q else None,
        "supported": any(s.user_id == viewer.id for s in a.supports), "rating": a.rating,
        "can": {"handle": can_handle, "answer": is_author and open_q is not None, "comment": can_handle or is_author,
                "support": not is_author and a.status not in FINAL,
                "rate": is_author and a.status == "done" and not a.rating,
                "transitions": sorted(workflow.TRANSITIONS[a.status], key=list(STATUSES).index) if can_handle else []},
    }


def get_appeal(db: Session, appeal_id: int, user: User, handle: bool = False) -> Appeal:
    a = db.get(Appeal, appeal_id)
    if not a or not workflow.can_view(user, a):
        bad("Обращение не найдено", 404)
    if handle and not workflow.can_handle(user, a):
        bad("Обращение рассматривает другой сотрудник", 403)
    return a


# ---------- вход и профиль ----------

class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not u or not u.active or not check_password(body.password, u.password_hash):
        bad("Неверная почта или пароль", 401)
    return {"token": make_token(u), "user": user_full(u)}


@router.get("/auth/demo-users")
def demo_users(db: Session = Depends(get_db)):
    """Список учетных записей для быстрого входа на демонстрации (отключается DEMO_LOGIN=0)."""
    if not settings.demo_login:
        return []
    order = {"director": 0, "manager": 1, "employee": 2, "admin": 3}
    users = sorted(db.scalars(select(User).where(User.active)), key=lambda u: (order[u.role], u.id))
    return [{"email": u.email, "name": u.name, "position": u.position, "role": u.role} for u in users]


@router.post("/auth/demo-login")
def demo_login(body: dict, db: Session = Depends(get_db)):
    if not settings.demo_login:
        bad("Быстрый вход отключен", 403)
    u = db.scalar(select(User).where(User.email == str(body.get("email", "")).lower(), User.active))
    if not u:
        bad("Пользователь не найден", 404)
    return {"token": make_token(u), "user": user_full(u)}


class InitDataIn(BaseModel):
    init_data: str


def telegram_id(init_data: str) -> str:
    """Кто открыл Mini App: Telegram подписывает данные запуска токеном бота, подделать их нельзя."""
    if not settings.telegram_token:
        bad("Telegram-бот не подключен", 404)
    who = check_init_data(init_data, settings.telegram_token)
    if not who or not who.get("id"):
        bad("Данные Telegram не прошли проверку. Закройте приложение и откройте заново из бота", 401)
    return str(who["id"])


@router.post("/auth/telegram")
def telegram_login(body: InitDataIn, db: Session = Depends(get_db)):
    """Вход в Mini App без пароля - для тех, кто уже привязал Telegram к учетной записи."""
    tg_id = telegram_id(body.init_data)
    u = db.scalar(select(User).where(User.tg_chat_id == tg_id, User.active))
    if not u:
        bad("Этот Telegram еще не привязан к учетной записи", 404)
    return {"token": make_token(u), "user": user_full(u)}


@router.post("/me/messengers/telegram/webapp")
def telegram_link(body: InitDataIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Привязать Telegram, из которого открыт Mini App, к вошедшему сотруднику - без кода."""
    tg_id = telegram_id(body.init_data)
    for other in db.scalars(select(User).where(User.tg_chat_id == tg_id, User.id != user.id)):
        other.tg_chat_id = None  # один Telegram - одна учетная запись
    fresh = user.tg_chat_id != tg_id
    user.tg_chat_id, user.tg_link_code = tg_id, None
    db.commit()
    if fresh:
        bots.channels()["telegram"].push(tg_id, f"Здравствуйте, {user.name}! Telegram привязан.\n\n{bots.HELP}", bots.menu_buttons(user))
    return user_full(user)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_full(user)


@router.get("/me/messengers")
def my_messengers(user: User = Depends(current_user)):
    """Мессенджеры, в которых можно писать боту: работает ли бот и привязан ли он у этого сотрудника."""
    # мессенджеры без токена сотруднику не показываем: подключать там нечего
    return [{"key": ch.key, "title": ch.title, "enabled": ch.state["enabled"], "bot": ch.state["username"],
             "linked": bool(getattr(user, ch.user_field))}
            for ch in bots.channels().values() if ch.configured or ch.state["enabled"]]


@router.post("/me/messengers/{key}/code")
def messenger_code(key: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Одноразовый код привязки: сотрудник отправляет его боту или открывает ссылку с кодом."""
    ch = bots.channels().get(key)
    if not ch:
        bad("Неизвестный мессенджер", 404)
    if not ch.state["enabled"]:
        bad(f"Бот {ch.title} сейчас не подключен")
    user.tg_link_code = secrets.token_hex(3).upper()
    db.commit()
    return {"code": user.tg_link_code, "bot": ch.state["username"], "link": ch.link_for(user.tg_link_code)}


@router.post("/me/messengers/{key}/unlink")
def messenger_unlink(key: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ch = bots.channels().get(key)
    if not ch:
        bad("Неизвестный мессенджер", 404)
    setattr(user, ch.user_field, None)
    db.commit()
    return {"ok": True}


@router.get("/meta")
def meta(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cats = db.scalars(select(Category).where(Category.active).order_by(Category.name)).all()
    out = {
        "statuses": STATUSES, "types": TYPES, "priorities": PRIORITIES, "sources": SOURCES, "roles": ROLES,
        "final": sorted(FINAL),
        "categories": [{"id": c.id, "name": c.name, "description": c.description,
                        "responsible": c.responsible.name if c.responsible else None, "sla_days": c.sla_days} for c in cats],
        "departments": [{"id": d.id, "name": d.name} for d in db.scalars(select(Department).order_by(Department.name))],
        "ai": ai.engine_name(),
        "speech": settings.speech_mode,  # local, api (звук режет браузер, распознает OpenAI) или "" - только текст
    }
    if user.role != "employee":
        hs = db.scalars(select(User).where(User.active, User.role != "employee").order_by(User.name)).all()
        out["handlers"] = [user_brief(u) for u in hs]
        out["employees"] = [user_brief(u) for u in db.scalars(select(User).where(User.active).order_by(User.name))]
    return out


# ---------- обращения ----------

class AssistIn(BaseModel):
    text: str


@router.post("/appeals/assist")
def assist(body: AssistIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Подсказка при заполнении формы: тип, категория, приоритет, заголовок и похожие обращения."""
    text = body.text.strip()
    if len(text) < 20:
        bad("Опишите подробнее - хотя бы пару предложений")
    t, engine = ai.triage(text, workflow.categories_for_ai(db))
    cat = db.scalar(select(Category).where(Category.name == t.category))
    sim = [{"id": r.id, "number": r.number, "title": r.title, "status": r.status, "score": s, "supports": len(r.supports),
            "mine": r.author_id == user.id}
           for r, s in workflow.similar(db, text, threshold=0.25) if r.status not in ("rejected", "closed")]
    return {"type": t.type, "category_id": cat.id if cat else None, "priority": t.priority, "title": t.title,
            "summary": t.summary, "question": t.question, "engine": engine, "similar": sim,
            "responsible": cat.responsible.name if cat and cat.responsible else None}


def save_upload(f: UploadFile, limit: int) -> tuple[str, int]:
    ext = Path(f.filename or "").suffix.lower()[:10]
    stored = f"{uuid.uuid4().hex}{ext}"
    size = 0
    with (UPLOAD_DIR / stored).open("wb") as out:
        while chunk := f.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                (UPLOAD_DIR / stored).unlink(missing_ok=True)
                bad(f"Файл «{f.filename}» больше {limit // 1024 // 1024} МБ", 413)
            out.write(chunk)
    return stored, size


@router.post("/appeals")
def create_appeal(text: str = Form(...), type: str = Form(""), category_id: int | None = Form(None),
                  priority: str = Form(""), title: str = Form(""), solution: str = Form(""),
                  anonymous: bool = Form(False), files: list[UploadFile] = File(default=[]),
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    if len(text.strip()) < MIN_TEXT:
        bad(f"Опишите подробнее - хотя бы {MIN_TEXT} символов")
    if type and type not in TYPES:
        bad("Неизвестный тип обращения")
    if priority and priority not in PRIORITIES:
        bad("Неизвестный приоритет")
    a = workflow.register(db, text=text, author=user, source="web", type=type or None, category_id=category_id,
                          priority=priority or None, title=workflow.clean_title(title) or None, solution=solution,
                          anonymous=anonymous)
    for f in files:
        if f.filename:
            stored, size = save_upload(f, MAX_FILE)
            db.add(Attachment(appeal_id=a.id, filename=Path(f.filename).name[:255], stored=stored, size=size))
    db.commit()
    return appeal_detail(db, a, user)


@router.get("/appeals")
def list_appeals(scope: str = "mine", status: str = "", type: str = "", category_id: int | None = None,
                 department_id: int | None = None, assignee_id: int | None = None, source: str = "",
                 overdue: bool = False, q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """scope: mine - мои обращения; inbox - назначенные мне; all - все, что мне разрешено видеть."""
    stmt = select(Appeal).order_by(Appeal.id.desc())
    if scope == "mine":
        stmt = stmt.where(Appeal.author_id == user.id)
    elif scope == "inbox":
        stmt = stmt.where(Appeal.assignee_id == user.id)
    if status == "open":
        stmt = stmt.where(Appeal.status.notin_(FINAL))
    elif status:
        stmt = stmt.where(Appeal.status == status)
    if type:
        stmt = stmt.where(Appeal.type == type)
    if category_id:
        stmt = stmt.where(Appeal.category_id == category_id)
    if department_id:
        stmt = stmt.where(Appeal.department_id == department_id)
    if assignee_id:
        stmt = stmt.where(Appeal.assignee_id == assignee_id)
    if source:
        stmt = stmt.where(Appeal.source == source)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Appeal.title.ilike(like), Appeal.text.ilike(like), Appeal.number.ilike(like),
                              Appeal.resolution.ilike(like)))
    rows = [a for a in db.scalars(stmt) if workflow.can_view(user, a)]
    if department_id:  # не раскрываем подразделение анонимных авторов через фильтр
        rows = [a for a in rows if not a.anonymous or a.author_id == user.id]
    if overdue:
        rows = [a for a in rows if workflow.overdue(a)]
    return [appeal_row(a, user) for a in rows]


@router.get("/appeals/{appeal_id}")
def read_appeal(appeal_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return appeal_detail(db, get_appeal(db, appeal_id, user), user)


class TextIn(BaseModel):
    text: str
    internal: bool = False


@router.post("/appeals/{appeal_id}/comment")
def comment(appeal_id: int, body: TextIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_appeal(db, appeal_id, user)
    can_handle = workflow.can_handle(user, a)
    if not (can_handle or a.author_id == user.id):
        bad("Комментировать могут автор и те, кто рассматривает обращение", 403)
    if not body.text.strip():
        bad("Пустой комментарий")
    workflow.comment(db, a, user, body.text, body.internal)
    db.commit()
    return appeal_detail(db, a, user)


class StatusIn(BaseModel):
    status: str
    text: str = ""


@router.post("/appeals/{appeal_id}/status")
def change_status(appeal_id: int, body: StatusIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_appeal(db, appeal_id, user, handle=True)
    try:
        workflow.set_status(db, a, body.status, user, body.text)
    except ValueError as e:
        bad(str(e))
    db.commit()
    return appeal_detail(db, a, user)


@router.post("/appeals/{appeal_id}/clarify")
def clarify(appeal_id: int, body: TextIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_appeal(db, appeal_id, user, handle=True)
    if a.status in FINAL:
        bad("Обращение уже закрыто")
    if not body.text.strip():
        bad("Напишите вопрос автору")
    if not a.author_id:
        bad("У обращения нет автора в системе - уточнить можно только лично")
    workflow.ask(db, a, user, body.text)
    db.commit()
    return appeal_detail(db, a, user)


@router.post("/appeals/{appeal_id}/answer")
def answer(appeal_id: int, body: TextIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_appeal(db, appeal_id, user)
    if a.author_id != user.id:
        bad("Ответить на уточнение может только автор", 403)
    if not body.text.strip():
        bad("Пустой ответ")
    workflow.answer(db, a, user, body.text)
    db.commit()
    return appeal_detail(db, a, user)


class RateIn(BaseModel):
    ok: bool
    text: str = ""


@router.post("/appeals/{appeal_id}/rate")
def rate(appeal_id: int, body: RateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Автор подтверждает результат или возвращает обращение в работу."""
    a = get_appeal(db, appeal_id, user)
    if a.author_id != user.id:
        bad("Оценить результат может только автор", 403)
    try:
        workflow.rate(db, a, user, body.ok, body.text)
    except ValueError as e:
        bad(str(e))
    db.commit()
    return appeal_detail(db, a, user)


class AssignIn(BaseModel):
    user_id: int


@router.post("/appeals/{appeal_id}/assign")
def assign(appeal_id: int, body: AssignIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = get_appeal(db, appeal_id, user, handle=True)
    to = db.get(User, body.user_id)
    if not to or not to.active or to.role == "employee":
        bad("Ответственным может быть руководитель, директор или администратор")
    if to.id == a.assignee_id:
        bad("Этот сотрудник уже отвечает за обращение")
    workflow.assign(db, a, user, to)
    db.commit()
    return appeal_detail(db, a, user)


class EditIn(BaseModel):
    type: str | None = None
    category_id: int | None = None
    priority: str | None = None
    due_days: int | None = None


@router.patch("/appeals/{appeal_id}")
def edit(appeal_id: int, body: EditIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Ответственный поправляет то, что определил ИИ или автор. Смена категории переназначает ответственного."""
    a = get_appeal(db, appeal_id, user, handle=True)
    changes = []
    if body.type and body.type != a.type:
        if body.type not in TYPES:
            bad("Неизвестный тип")
        changes.append(f"тип: {TYPES[a.type]} -> {TYPES[body.type]}")
        a.type = body.type
    if body.priority and body.priority != a.priority:
        if body.priority not in PRIORITIES:
            bad("Неизвестный приоритет")
        changes.append(f"приоритет: {PRIORITIES[a.priority]} -> {PRIORITIES[body.priority]}")
        a.priority = body.priority
    if body.due_days:
        a.due_at = now() + timedelta(days=max(1, min(body.due_days, 365)))
        a.escalated_at = a.reminded_at = None
        changes.append(f"срок перенесен на {a.due_at:%d.%m.%Y}")
    new_cat = None
    if body.category_id and body.category_id != a.category_id:
        new_cat = db.get(Category, body.category_id)
        if not new_cat:
            bad("Категория не найдена")
        changes.append(f"категория: {a.category.name if a.category else 'нет'} -> {new_cat.name}")
        a.category_id = new_cat.id
    if not changes:
        bad("Нечего менять")
    a.ai_filled = False
    workflow.add_event(db, a, "edited", user, "; ".join(changes))
    if new_cat:
        to, reason = workflow.route(db, new_cat)
        if to and to.id != a.assignee_id:
            a.assignee_id = to.id
            workflow.add_event(db, a, "routed", None, f"Назначен ответственный: {to.name} ({reason})")
            workflow.notify(db, to.id, a, f"Вам передано обращение {a.number}: {a.title}", kind="assigned")
    db.commit()
    return appeal_detail(db, a, user)


@router.post("/appeals/{appeal_id}/support")
def support(appeal_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """«У меня то же самое». Доступно по номеру из подсказки похожих, даже если полный текст сотруднику не виден."""
    a = db.get(Appeal, appeal_id)
    if not a:
        bad("Обращение не найдено", 404)
    try:
        workflow.support(db, a, user)
    except ValueError as e:
        bad(str(e))
    db.commit()
    return {"ok": True, "number": a.number, "supports": len(a.supports)}


@router.get("/attachments/{att_id}")
def download(att_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    f = db.get(Attachment, att_id)
    if not f:
        bad("Файл не найден", 404)
    get_appeal(db, f.appeal_id, user)
    path = UPLOAD_DIR / f.stored
    if not path.exists():
        bad("Файл не найден", 404)
    return FileResponse(path, filename=f.filename)


# ---------- уведомления и лента улучшений ----------

@router.get("/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(40)).all()
    return {"unread": sum(not n.read for n in rows),
            "items": [{"id": n.id, "text": n.text, "appeal_id": n.appeal_id, "read": n.read, "created_at": iso(n.created_at)} for n in rows]}


@router.post("/notifications/read")
def notifications_read(user: User = Depends(current_user), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.read.is_(False))):
        n.read = True
    db.commit()
    return {"ok": True}


@router.get("/improvements")
def improvements(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Что сделано по обращениям - видно всем сотрудникам, без авторов. Показывает, что писать не бесполезно."""
    rows = db.scalars(select(Appeal).where(Appeal.status == "done").order_by(Appeal.closed_at.desc()).limit(50)).all()
    return [{"number": a.number, "title": a.title, "type": a.type, "category": a.category.name if a.category else None,
             "resolution": a.resolution, "closed_at": iso(a.closed_at), "supports": len(a.supports)} for a in rows]


# ---------- аналитика ----------

def visible(db: Session, user: User) -> list[Appeal]:
    return [a for a in db.scalars(select(Appeal).order_by(Appeal.id)) if workflow.can_view(user, a)]


def hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600


def theme_view(title: str, items: list[Appeal], note: str = "") -> dict:
    return {
        "title": title, "note": note,
        "count": len(items) + sum(len(a.supports) for a in items),  # сколько раз об этом сказали
        "open": sum(a.status not in FINAL for a in items),
        "departments": sorted({a.department.name for a in items if a.department and not a.anonymous}),
        "appeals": [{"id": a.id, "number": a.number, "title": a.title, "status": a.status} for a in items],
    }


def themes_rules(rows: list[Appeal]) -> list[dict]:
    """Повторяющиеся темы без модели - только то, что можно утверждать наверняка:
    почти дословные повторы, обращения, к которым присоединились коллеги, и категории, куда пишут из разных отделов."""
    pool = [a for a in rows if a.type != "praise"]
    out, used = [], set()
    for g in ai.clusters([f"{a.title}. {a.text}" for a in pool], threshold=0.3):
        items = [pool[i] for i in g]
        used.update(a.id for a in items)
        out.append(theme_view(items[0].title, items, "Несколько обращений почти об одном и том же"))
    for a in pool:
        if a.supports and a.id not in used:
            used.add(a.id)
            out.append(theme_view(a.title, [a], f"К обращению присоединились коллеги: {len(a.supports)}"))
    by_cat: dict[str, list[Appeal]] = defaultdict(list)
    for a in pool:
        if a.category and a.status not in FINAL:
            by_cat[a.category.name].append(a)
    for name, items in by_cat.items():
        deps = {a.department_id for a in items if a.department_id}
        if len(items) >= 2 and len(deps) >= 2 and not all(a.id in used for a in items):
            out.append(theme_view(name, items, "Об этом пишут из разных подразделений"))
    return sorted(out, key=lambda t: (-t["count"], -t["open"]))


def themes(db: Session, rows: list[Appeal]) -> tuple[list[dict], str]:
    """Темы от модели (если сводка строилась моделью), иначе по правилам."""
    last = db.scalar(select(Digest).order_by(Digest.id.desc()))
    if last and last.engine != "rules" and last.themes:
        by_num = {a.number: a for a in rows}
        out = []
        for th in last.themes:
            items = [by_num[n] for n in th.get("numbers", []) if n in by_num]
            if len(items) >= 2:
                out.append(theme_view(th.get("title", ""), items, th.get("note", "")))
        if out:
            return sorted(out, key=lambda t: (-t["count"], -t["open"])), last.engine
    return themes_rules(rows), "rules"


@router.get("/analytics")
def analytics(user: User = Depends(handler), db: Session = Depends(get_db)):
    rows = visible(db, user)
    t = now()
    total = len(rows)
    closed = [a for a in rows if a.status in FINAL]
    responded = [a for a in rows if a.first_response_at]
    over = [a for a in rows if workflow.overdue(a, t)]
    no_resp = [a for a in rows if not a.first_response_at and a.status not in FINAL
               and hours(a.created_at, t) > settings.first_response_hours]

    def count(key):
        c = Counter(key(a) for a in rows)
        return [{"key": k, "count": v} for k, v in c.most_common() if k]

    by_cat: dict[str, dict] = defaultdict(lambda: {"total": 0, "open": 0, "overdue": 0})
    for a in rows:
        d = by_cat[a.category.name if a.category else "Без категории"]
        d["total"] += 1
        d["open"] += a.status not in FINAL
        d["overdue"] += workflow.overdue(a, t)

    by_handler: dict[str, dict] = defaultdict(lambda: {"open": 0, "overdue": 0, "closed": 0, "resp": []})
    for a in rows:
        if not a.assignee:
            continue
        d = by_handler[a.assignee.name]
        d["open"] += a.status not in FINAL
        d["closed"] += a.status in FINAL
        d["overdue"] += workflow.overdue(a, t)
        if a.first_response_at:
            d["resp"].append(hours(a.created_at, a.first_response_at))

    weeks = []
    for i in range(7, -1, -1):
        start = (t - timedelta(days=t.weekday(), weeks=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        weeks.append({"week": start.strftime("%d.%m"),
                      "created": sum(start <= a.created_at < end for a in rows),
                      "closed": sum(bool(a.closed_at) and start <= a.closed_at < end for a in rows)})

    return {
        "total": total, "open": total - len(closed), "closed": len(closed),
        "closed_share": round(len(closed) / total * 100) if total else 0,
        "with_result_share": round(sum(bool(a.resolution) for a in closed) / len(closed) * 100) if closed else 0,
        "implemented": sum(a.status == "done" for a in rows),
        "avg_first_response_h": round(sum(hours(a.created_at, a.first_response_at) for a in responded) / len(responded), 1) if responded else None,
        "avg_resolution_d": round(sum(hours(a.created_at, a.closed_at) for a in closed if a.closed_at) / 24 / max(1, sum(bool(a.closed_at) for a in closed)), 1) if closed else None,
        "overdue": [appeal_row(a, user) for a in sorted(over, key=lambda a: a.due_at)],
        "no_response": [appeal_row(a, user) for a in no_resp],
        "by_type": count(lambda a: a.type), "by_status": [{"key": s, "count": sum(a.status == s for a in rows)} for s in STATUSES],
        "by_source": count(lambda a: a.source),
        "by_department": count(lambda a: a.department.name if a.department and not a.anonymous else None),
        "by_category": sorted(({"key": k, **v} for k, v in by_cat.items()), key=lambda x: -x["total"]),
        "by_handler": sorted(({"key": k, "open": v["open"], "closed": v["closed"], "overdue": v["overdue"],
                               "avg_response_h": round(sum(v["resp"]) / len(v["resp"]), 1) if v["resp"] else None}
                              for k, v in by_handler.items()), key=lambda x: -x["open"]),
        "weeks": weeks, "themes": themes(db, rows)[0], "themes_engine": themes(db, rows)[1],
    }


def digest_facts(rows: list[Appeal]) -> str:
    t = now()
    lines = [f"Сегодня {t:%d.%m.%Y}. Всего обращений: {len(rows)}, открыто: {sum(a.status not in FINAL for a in rows)}."]
    lines.append("\nОткрытые обращения (номер | тип | приоритет | статус | категория | поступило дней назад | срок | "
                 "сколько еще сотрудников сообщили о том же | суть):")
    for a in rows:
        if a.status in FINAL:
            continue
        due = workflow.due_phrase(a, t)
        lines.append(f"{a.number} | {TYPES[a.type]} | {PRIORITIES[a.priority]} | {STATUSES[a.status]} | "
                     f"{a.category.name if a.category else '-'} | {(t - a.created_at).days} | {due} | "
                     f"{len(a.supports)} | {a.title}: {a.text[:300]}")
    lines.append("\nРеализовано за последние 30 дней:")
    for a in rows:
        if a.status == "done" and a.closed_at and (t - a.closed_at).days <= 30:
            lines.append(f"- {a.number} {a.title}: {a.resolution[:200]}")
    return "\n".join(lines)


def digest_rules(rows: list[Appeal]) -> str:
    t = now()
    over = [a for a in rows if workflow.overdue(a, t)]
    high = [a for a in rows if a.priority == "high" and a.status not in FINAL and a not in over]
    done = [a for a in rows if a.status == "done" and a.closed_at and (t - a.closed_at).days <= 30]
    out = []
    if over or high:
        out.append("**Требует решения**")
        out += [f"- {a.number} {workflow.due_phrase(a, t)}: {a.title}" for a in over[:5]]
        out += [f"- {a.number}, высокий приоритет: {a.title}" for a in high[:5]]
    th = themes_rules(rows)[:3]
    if th:
        out.append("\n**Повторяющиеся темы**")
        out += [f"- {x['title']} - сказали {x['count']} раз ({', '.join(i['number'] for i in x['appeals'])})" for x in th]
    if done:
        out.append("\n**Сделано за месяц**")
        out += [f"- {a.number}: {a.title}" for a in done[:5]]
    return "\n".join(out) or "Открытых вопросов, требующих внимания, нет."


@router.get("/analytics/digest")
def get_digest(refresh: bool = False, user: User = Depends(overseer), db: Session = Depends(get_db)):
    last = db.scalar(select(Digest).order_by(Digest.id.desc()))
    if last and not refresh:
        return {"text": last.text, "engine": last.engine, "created_at": iso(last.created_at)}
    rows = visible(db, user)
    out, engine = ai.digest(digest_facts(rows))
    d = Digest(text=out.text if out else digest_rules(rows), engine=engine,
               themes=[t.model_dump() for t in out.themes] if out else [])
    db.add(d)
    db.commit()
    return {"text": d.text, "engine": d.engine, "created_at": iso(d.created_at)}


# ---------- совещания ----------

def meeting_row(m: Meeting) -> dict:
    return {"id": m.id, "title": m.title, "held_at": iso(m.held_at), "status": m.status, "error": m.error,
            "filename": m.filename, "uploaded_by": m.uploaded_by.name if m.uploaded_by else None, "engine": m.engine,
            "items_total": len(m.items), "items_draft": sum(i.state == "draft" for i in m.items),
            "items_accepted": sum(i.state == "accepted" for i in m.items), "created_at": iso(m.created_at)}


def item_view(db: Session, i: MeetingItem) -> dict:
    a = db.get(Appeal, i.appeal_id) if i.appeal_id else None
    return {"id": i.id, "kind": i.kind, "title": i.title, "text": i.text, "quote": i.quote, "speaker": i.speaker,
            "category_id": i.category_id, "category": i.category.name if i.category else None, "priority": i.priority,
            "state": i.state, "appeal": {"id": a.id, "number": a.number, "status": a.status} if a else None}


@router.post("/meetings")
def create_meeting(title: str = Form(...), held_at: str = Form(""), transcript: str = Form(""),
                   source_name: str = Form(""), file: UploadFile | None = File(None),
                   user: User = Depends(handler), db: Session = Depends(get_db)):
    """source_name - имя исходной записи, если ее уже распознал браузер и прислал готовую расшифровку."""
    if not title.strip():
        bad("Укажите название совещания")
    has_file = file is not None and bool(file.filename)
    transcript = transcript.replace("\r\n", "\n")  # браузер передает переводы строк формы как \r\n
    if not has_file and len(transcript.strip()) < 50:
        bad("Приложите запись совещания или вставьте текст протокола")
    stored, filename = "", Path(source_name).name[:255]
    if has_file:
        ext = Path(file.filename).suffix.lower()
        if ext not in meetings.ALLOWED_EXT:
            bad("Поддерживаются аудио, видео, .txt и .docx")
        if ext in meetings.MEDIA_EXT and settings.speech_mode == "api":
            bad("Запись распознается по частям прямо в браузере - обновите страницу и загрузите ее снова")
        if ext in meetings.MEDIA_EXT and settings.speech_mode != "local":
            bad("Здесь записи не распознаются - загрузите протокол или расшифровку (.txt, .docx)")
        stored, _ = save_upload(file, MAX_MEETING_FILE)
        filename = Path(file.filename).name[:255]
    try:
        when = datetime.fromisoformat(held_at) if held_at else now()
    except ValueError:
        when = now()
    m = Meeting(title=title.strip()[:200], held_at=when, uploaded_by_id=user.id, filename=filename, stored=stored,
                transcript="" if has_file else transcript.strip())
    db.add(m)
    db.commit()
    run_meeting(db, m)
    return meeting_row(m)


@router.post("/meetings/transcribe")
def transcribe_part(audio: UploadFile = File(...), prev: str = Form(""), user: User = Depends(handler)):
    """Распознать один кусок записи (WAV до ~2 мин) через OpenAI. Куски режет и склеивает браузер:
    так запись любой длины проходит через лимит тела запроса на Vercel и не нужен ffmpeg на сервере."""
    if settings.speech_mode != "api":
        bad("Распознавание через API выключено", 409)
    data = audio.file.read(meetings.MAX_API_PART + 1)
    if not data:
        bad("Пустой фрагмент записи")
    if len(data) > meetings.MAX_API_PART:
        bad("Фрагмент записи слишком большой", 413)
    try:
        return {"text": meetings.transcribe_api(data, Path(audio.filename or "part.wav").name, prev)}
    except meetings.SpeechError as e:
        bad(str(e), 502)


def run_meeting(db: Session, m: Meeting) -> None:
    """На своем сервере совещание разбирается в фоне. В бессерверной среде фоновых потоков нет - разбираем в запросе."""
    if settings.serverless:
        meetings.process(m.id)
        db.refresh(m)
    else:
        meetings.start(m.id)


@router.get("/meetings")
def list_meetings(user: User = Depends(handler), db: Session = Depends(get_db)):
    stmt = select(Meeting).order_by(Meeting.id.desc())
    if user.role == "manager":
        stmt = stmt.where(Meeting.uploaded_by_id == user.id)
    return [meeting_row(m) for m in db.scalars(stmt)]


def get_meeting(db: Session, meeting_id: int, user: User) -> Meeting:
    m = db.get(Meeting, meeting_id)
    if not m or (user.role == "manager" and m.uploaded_by_id != user.id):
        bad("Совещание не найдено", 404)
    return m


@router.get("/meetings/{meeting_id}")
def read_meeting(meeting_id: int, user: User = Depends(handler), db: Session = Depends(get_db)):
    m = get_meeting(db, meeting_id, user)
    return {**meeting_row(m), "summary": m.summary, "transcript": m.transcript, "items": [item_view(db, i) for i in m.items]}


@router.post("/meetings/{meeting_id}/retry")
def retry_meeting(meeting_id: int, user: User = Depends(handler), db: Session = Depends(get_db)):
    m = get_meeting(db, meeting_id, user)
    if m.status not in ("error", "ready"):
        bad("Совещание еще обрабатывается")
    m.status, m.error = "queued", ""
    db.commit()
    run_meeting(db, m)
    return meeting_row(m)


class ItemIn(BaseModel):
    title: str | None = None
    text: str | None = None
    kind: str | None = None
    category_id: int | None = None
    priority: str | None = None
    author_id: int | None = None


@router.post("/meetings/items/{item_id}/accept")
def accept_item(item_id: int, body: ItemIn, user: User = Depends(handler), db: Session = Depends(get_db)):
    """Подтвердить пункт: проблема или предложение становится обращением и уходит по маршруту."""
    i = db.get(MeetingItem, item_id)
    if not i:
        bad("Пункт не найден", 404)
    m = get_meeting(db, i.meeting_id, user)
    if i.state != "draft":
        bad("Пункт уже обработан")
    for f in ("title", "text", "kind", "category_id", "priority"):
        v = getattr(body, f)
        if v:
            setattr(i, f, v)
    if i.kind in ("problem", "proposal", "praise"):
        author = db.get(User, body.author_id) if body.author_id else None
        label = "" if author else f"Со слов на совещании{': ' + i.speaker if i.speaker else ''}"
        a = workflow.register(db, text=i.text, author=author, author_label=label, source="meeting", type=i.kind,
                              category_id=i.category_id, priority=i.priority, title=i.title or None,
                              meeting_id=m.id, registrar=user)
        if i.quote:
            workflow.add_event(db, a, "comment", None, f"Из расшифровки совещания «{m.title}» ({m.held_at:%d.%m.%Y}): «{i.quote}»",
                               internal=True)
        i.appeal_id = a.id
    i.state = "accepted"  # решения и поручения остаются в протоколе совещания
    db.commit()
    return item_view(db, i)


@router.post("/meetings/items/{item_id}/reject")
def reject_item(item_id: int, user: User = Depends(handler), db: Session = Depends(get_db)):
    i = db.get(MeetingItem, item_id)
    if not i:
        bad("Пункт не найден", 404)
    get_meeting(db, i.meeting_id, user)
    if i.state != "draft":
        bad("Пункт уже обработан")
    i.state = "rejected"
    db.commit()
    return item_view(db, i)


# ---------- администрирование ----------

class CategoryIn(BaseModel):
    name: str | None = None
    description: str | None = None
    responsible_id: int | None = None
    sla_days: int | None = None
    active: bool | None = None


@router.get("/admin/categories")
def admin_categories(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "description": c.description, "responsible_id": c.responsible_id,
             "sla_days": c.sla_days, "active": c.active,
             "route": workflow.route(db, c)[0].name if workflow.route(db, c)[0] else None,
             "route_reason": workflow.route(db, c)[1]}
            for c in db.scalars(select(Category).order_by(Category.name))]


@router.post("/admin/categories")
def admin_category_create(body: CategoryIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if not (body.name or "").strip():
        bad("Укажите название")
    if db.scalar(select(Category).where(Category.name == body.name.strip())):
        bad("Такая категория уже есть")
    c = Category(name=body.name.strip(), description=body.description or "", responsible_id=body.responsible_id,
                 sla_days=body.sla_days or 10)
    db.add(c)
    db.commit()
    return {"id": c.id}


@router.patch("/admin/categories/{cat_id}")
def admin_category_edit(cat_id: int, body: CategoryIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    c = db.get(Category, cat_id)
    if not c:
        bad("Категория не найдена", 404)
    data = body.model_dump(exclude_unset=True)
    if "sla_days" in data and not (1 <= (data["sla_days"] or 0) <= 365):
        bad("Срок рассмотрения - от 1 до 365 дней")
    for k, v in data.items():
        setattr(c, k, v)
    db.commit()
    return {"ok": True}


class UserIn(BaseModel):
    role: str | None = None
    is_absent: bool | None = None
    deputy_id: int | None = None
    active: bool | None = None


@router.get("/admin/users")
def admin_users(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return [user_full(u) for u in db.scalars(select(User).order_by(User.name))]


@router.patch("/admin/users/{user_id}")
def admin_user_edit(user_id: int, body: UserIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        bad("Пользователь не найден", 404)
    data = body.model_dump(exclude_unset=True)
    if data.get("role") and data["role"] not in ROLES:
        bad("Неизвестная роль")
    if data.get("deputy_id") == u.id:
        bad("Нельзя назначить заместителем самого себя")
    if u.id == user.id and (data.get("role", "admin") != "admin" or data.get("active") is False):
        bad("Нельзя снять права администратора с самого себя")
    for k, v in data.items():
        setattr(u, k, v)
    db.commit()
    return user_full(u)


@router.get("/admin/channels")
def admin_channels(user: User = Depends(admin_only)):
    return {
        "ai": {"engine": ai.engine_name(), "enabled": settings.llm_enabled},
        "telegram": {**bots.channels()["telegram"].state, "configured": bool(settings.telegram_token)},
        "max": {**bots.channels()["max"].state, "configured": bool(settings.max_token)},
        "email": {**mail.state, "configured": bool(settings.imap_host), "address": settings.imap_user},
        "whisper": {"model": settings.whisper_model},
    }


class EmailIn(BaseModel):
    sender: str
    subject: str = ""
    body: str


@router.post("/admin/intake/email")
def simulate_email(body: EmailIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Имитация входящего письма - тот же код, что обрабатывает письма из ящика. Для демонстрации без почтового сервера."""
    if len(body.body.strip()) < 20:
        bad("Текст письма слишком короткий")
    a = mail.intake(db, body.sender, body.subject, body.body)
    db.commit()
    return appeal_row(a, user)


# ---------- оргструктура из 1С, Битрикс24, файла ----------

@router.get("/admin/org")
def org_state(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    runs = db.scalars(select(OrgSync).order_by(OrgSync.id.desc()).limit(15))
    linked = Counter(k for u in db.scalars(select(User).where(User.active)) for k in (u.ext_ids or {}))
    return {**orgsync.status(), "linked": linked, "template": orgsync.TEMPLATE.format(
                until=(now() + timedelta(days=10)).strftime("%d.%m.%Y")),
            "runs": [{"id": r.id, "source": r.source, "label": orgsync.SOURCES[r.source], "ok": r.ok, "stats": r.stats,
                      "error": r.error, "changes": r.changes[:30], "actor": r.actor.name if r.actor else "по расписанию",
                      "created_at": iso(r.created_at)} for r in runs]}


@router.post("/admin/org/sync/{source}")
def org_sync(source: str, apply: bool = False, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    if source not in ("1c", "b24"):
        bad("Неизвестный источник", 404)
    try:
        return orgsync.run(db, source, user, dry_run=not apply)
    except orgsync.SyncError as e:
        bad(str(e))


@router.post("/admin/org/import")
async def org_import(file: UploadFile = File(...), apply: bool = Form(False), user: User = Depends(admin_only),
                     db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        bad("Файл больше 5 МБ")
    try:
        return orgsync.apply(db, orgsync.parse_file(file.filename or "", data), user, dry_run=not apply)
    except orgsync.SyncError as e:
        bad(str(e))
