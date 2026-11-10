"""Жизненный цикл обращения: регистрация, маршрутизация, статусы, уведомления, контроль сроков.

Все каналы (форма, Telegram, почта, совещания) создают обращения только через register() -
поэтому маршрутизация, история и уведомления у них одинаковые.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ai
from .models import FINAL, STATUSES, Appeal, Category, Event, Notification, User, now

log = logging.getLogger("workflow")

# какие переходы статусов разрешены ответственному
TRANSITIONS = {
    "new": {"review", "clarify", "in_progress", "rejected", "closed"},
    "review": {"clarify", "in_progress", "done", "rejected", "closed"},
    "clarify": {"review", "in_progress", "rejected", "closed"},
    "in_progress": {"clarify", "done", "rejected", "closed"},
    "done": {"in_progress"},      # вернуть в работу, если результат не устроил
    "rejected": {"review"},       # пересмотреть отказ
    "closed": {"review"},
}
NEEDS_TEXT = {"done": "Опишите, что сделано", "rejected": "Объясните причину отказа - автор ее увидит"}


def categories_for_ai(db: Session) -> list[tuple[str, str]]:
    return [(c.name, c.description) for c in db.scalars(select(Category).where(Category.active).order_by(Category.name))]


def next_number(db: Session) -> str:
    last = db.scalar(select(func.max(Appeal.id))) or 0
    return f"A-{last + 1:03d}"


def add_event(db: Session, appeal: Appeal, kind: str, actor: User | None = None, text: str = "",
              data: dict | None = None, internal: bool = False, at: datetime | None = None) -> Event:
    ev = Event(appeal=appeal, actor_id=actor.id if actor else None, kind=kind, text=text, data=data or {},
               internal=internal, created_at=at or now())
    db.add(ev)
    appeal.updated_at = ev.created_at
    return ev


def notify(db: Session, user_id: int | None, appeal: Appeal | None, text: str) -> None:
    if not user_id:
        return
    db.add(Notification(user_id=user_id, appeal_id=appeal.id if appeal else None, text=text))
    from . import bots
    bots.push_user(db.get(User, user_id), text)


def route(db: Session, category: Category | None) -> tuple[User | None, str]:
    """Кому отдать обращение. Возвращает ответственного и объяснение для истории.

    Цепочка: ответственный за категорию -> его заместитель, если он отсутствует ->
    руководитель его подразделения -> дежурный администратор. Обращение без ответственного не остается.
    """
    steps: list[str] = []
    user = category.responsible if category else None
    if category and not user:
        steps.append(f"у категории «{category.name}» не назначен ответственный")
    seen: set[int] = set()
    while user and user.id not in seen:
        seen.add(user.id)
        if user.active and not user.is_absent:
            reason = f"по категории «{category.name}»" if not steps else "; ".join(steps)
            return user, reason
        steps.append(f"{user.name} отсутствует")
        if user.deputy:
            steps.append(f"передано заместителю ({user.deputy.name})")
            user = user.deputy
            continue
        head = db.get(User, user.department.head_id) if user.department and user.department.head_id else None
        if head and head.id not in seen:
            steps.append(f"заместитель не назначен, передано руководителю подразделения ({head.name})")
            user = head
            continue
        break
    fallback = db.scalar(select(User).where(User.role == "admin", User.active, User.is_absent.is_(False)).order_by(User.id))
    steps.append("передано администратору системы для ручного назначения")
    return fallback, "; ".join(steps)


def register(db: Session, *, text: str, author: User | None = None, author_label: str = "", source: str = "web",
             type: str | None = None, category_id: int | None = None, priority: str | None = None,
             title: str | None = None, solution: str = "", anonymous: bool = False, meeting_id: int | None = None,
             registrar: User | None = None, triage: ai.Triage | None = None) -> Appeal:
    """Зарегистрировать обращение из любого канала. Чего не указал человек - определяет ИИ."""
    text = text.strip()
    ai_filled = False
    summary = ""
    need = not (type and category_id and priority and title)
    if need and triage is None:
        triage, _ = ai.triage(text, categories_for_ai(db))
    if triage:
        summary = triage.summary
        if need:
            ai_filled = not (type and category_id)
            type = type or triage.type
            priority = priority or triage.priority
            title = title or triage.title
            if not category_id:
                cat = db.scalar(select(Category).where(Category.name == triage.category))
                category_id = cat.id if cat else None
    category = db.get(Category, category_id) if category_id else None
    created = now()
    appeal = Appeal(
        number=next_number(db), type=type or "problem", title=(title or text[:80]).strip()[:200], text=text,
        solution=solution.strip(), category_id=category.id if category else None, priority=priority or "medium",
        source=source, author_id=author.id if author else None, author_label=author_label, anonymous=anonymous,
        department_id=author.department_id if author else None, meeting_id=meeting_id, created_at=created,
        updated_at=created, ai_summary=summary, ai_filled=ai_filled,
        due_at=created + timedelta(days=category.sla_days if category else 10),
    )
    db.add(appeal)
    db.flush()
    who = registrar or author
    add_event(db, appeal, "created", who, data={"source": source}, at=created)
    assignee, reason = route(db, category)
    if assignee:
        appeal.assignee_id = assignee.id
        add_event(db, appeal, "routed", None, f"Назначен ответственный: {assignee.name} ({reason})",
                  data={"assignee_id": assignee.id}, at=created)
        notify(db, assignee.id, appeal, f"Новое обращение {appeal.number}: {appeal.title}")
    if triage and triage.question and author:
        # ИИ заметил, что без уточнения работу не начать: спрашиваем автора сразу, не дожидаясь ответственного
        add_event(db, appeal, "question", None, triage.question, data={"by_ai": True}, at=created)
        notify(db, author.id, appeal, f"{appeal.number}: чтобы быстрее взять в работу, уточните. {triage.question}")
    return appeal


def can_view(user: User, a: Appeal) -> bool:
    if user.role in ("director", "admin"):
        return True
    if a.author_id == user.id or a.assignee_id == user.id:
        return True
    if user.role == "manager":
        if a.category and a.category.responsible_id == user.id:
            return True
        # руководитель видит обращения своего подразделения, кроме анонимных
        if a.department_id and a.department_id == user.department_id and not a.anonymous:
            return True
    return False


def can_handle(user: User, a: Appeal) -> bool:
    """Может менять статус, назначать, фиксировать решение."""
    if user.role in ("director", "admin"):
        return True
    return user.role == "manager" and (a.assignee_id == user.id or bool(a.category and a.category.responsible_id == user.id))


def set_status(db: Session, a: Appeal, status: str, actor: User, text: str = "") -> None:
    if status not in STATUSES:
        raise ValueError("Неизвестный статус")
    if status == a.status:
        raise ValueError("Обращение уже в этом статусе")
    if status not in TRANSITIONS[a.status]:
        raise ValueError(f"Из статуса «{STATUSES[a.status]}» нельзя перейти в «{STATUSES[status]}»")
    text = text.strip()
    if status in NEEDS_TEXT and not text:
        raise ValueError(NEEDS_TEXT[status])
    old = a.status
    a.status = status
    mark_response(a)
    if status in FINAL:
        a.closed_at = now()
        if text:
            a.resolution = text
    else:
        a.closed_at = None
    add_event(db, a, "status", actor, text, data={"from": old, "to": status})
    msg = f"{a.number}: статус «{STATUSES[status]}»" + (f". {text}" if text else "")
    notify(db, a.author_id, a, msg)
    for s in a.supports:
        notify(db, s.user_id, a, msg)


def mark_response(a: Appeal) -> None:
    if not a.first_response_at:
        a.first_response_at = now()


def ask(db: Session, a: Appeal, actor: User, question: str) -> None:
    old = a.status
    a.status = "clarify"
    mark_response(a)
    add_event(db, a, "question", actor, question.strip(), data={"from": old})
    notify(db, a.author_id, a, f"{a.number}: нужен ваш ответ. {question.strip()}")
    if a.author_id:
        from . import bots
        author = db.get(User, a.author_id)
        if author and bots.linked(author):
            author.tg_pending_appeal_id = a.id  # следующий текст в боте станет ответом


def answer(db: Session, a: Appeal, actor: User, text: str) -> None:
    add_event(db, a, "answer", actor, text.strip())
    if a.status == "clarify":
        a.status = "review"
        add_event(db, a, "status", None, "Автор ответил на уточнение", data={"from": "clarify", "to": "review"})
    notify(db, a.assignee_id, a, f"{a.number}: автор ответил на уточнение")
    if a.author_id:
        author = db.get(User, a.author_id)
        if author and author.tg_pending_appeal_id == a.id:
            author.tg_pending_appeal_id = None


def assign(db: Session, a: Appeal, actor: User, to: User) -> None:
    a.assignee_id = to.id
    add_event(db, a, "assigned", actor, f"Ответственный: {to.name}", data={"assignee_id": to.id})
    notify(db, to.id, a, f"Вам передано обращение {a.number}: {a.title}")


def similar(db: Session, text: str, exclude_id: int | None = None, limit: int = 4, threshold: float = 0.2) -> list[tuple[Appeal, float]]:
    """Похожие обращения - чтобы не плодить дубли и видеть повторяющиеся проблемы."""
    rows = list(db.scalars(select(Appeal).where(Appeal.type != "praise")))
    rows = [r for r in rows if r.id != exclude_id]
    scores = ai.similarity(text, [f"{r.title}. {r.text}" for r in rows])
    ranked = sorted(zip(rows, scores), key=lambda x: -x[1])
    return [(r, round(s, 2)) for r, s in ranked[:limit] if s >= threshold]


def overdue(a: Appeal, at: datetime | None = None) -> bool:
    return bool(a.due_at and a.status not in FINAL and (at or now()) > a.due_at)


def check_deadlines(db: Session) -> int:
    """Эскалация: просроченное обращение поднимается к директору, ответственный получает напоминание."""
    t = now()
    rows = db.scalars(select(Appeal).where(Appeal.status.notin_(FINAL), Appeal.due_at < t, Appeal.escalated_at.is_(None))).all()
    directors = db.scalars(select(User).where(User.role == "director", User.active)).all()
    for a in rows:
        a.escalated_at = t
        add_event(db, a, "escalated", None, "Срок рассмотрения истек - обращение поднято к руководству")
        notify(db, a.assignee_id, a, f"{a.number}: срок рассмотрения истек")
        for d in directors:
            if d.id != a.assignee_id:
                notify(db, d.id, a, f"{a.number} просрочено: {a.title}")
    return len(rows)


def clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()[:200]
