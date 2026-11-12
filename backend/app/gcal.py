"""Google Календарь: обращение лежит в календаре полосой на весь срок - от регистрации до дедлайна.

Два уровня:
- ссылка «добавить в календарь» (template_url) - работает всегда, без настроек и без доступа к аккаунту;
- подключенный календарь (OAuth, нужны GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET) - события создаются и
  обновляются сами: перенесли срок - полоса удлинилась, передали другому - событие переехало, закрыли - отмечено.

Синхронизация привязана к сессии базы, а не к отдельным обработчикам: что бы ни изменило обращение
(форма, бот, почта, ИИ-агент, контроль сроков), после фиксации изменений календарь догоняет базу.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session

from .config import public_url, settings
from .db import SessionLocal
from .models import FINAL, STATUSES, Appeal, Notification, User

log = logging.getLogger("gcal")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SCOPE = "openid email https://www.googleapis.com/auth/calendar.events"

_tokens: dict[int, tuple[str, float]] = {}  # id сотрудника -> (токен доступа, до какого времени годен)
_lock = threading.Lock()                    # синхронизации идут по одной: не создаем два события на одно обращение


class CalendarError(Exception):
    """Ошибка с понятным текстом для пользователя."""


class Revoked(Exception):
    """Сотрудник отозвал доступ в настройках Google - календарь считаем отключенным."""


def _http() -> httpx.Client:
    return httpx.Client(timeout=15)


def redirect_uri() -> str:
    return f"{public_url()}/api/calendar/google/callback"


# ---------- что показываем в календаре ----------

def span(a: Appeal) -> tuple[date, date]:
    """Первый и последний день полосы в часовом поясе компании: от регистрации до срока (или до закрытия)."""
    shift = timedelta(hours=settings.tz_hours)
    start = (a.created_at + shift).date()
    last = a.closed_at if a.status in FINAL and a.closed_at else a.due_at
    return start, max(start, (last + shift).date()) if last else start


def link(a: Appeal) -> str:
    return f"{public_url()}/appeals/{a.id}"


def template_url(a: Appeal) -> str:
    """Ссылка, которая открывает в Google Календаре готовое событие на весь срок - остается нажать «Сохранить»."""
    start, last = span(a)
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"{a.number} {a.title}",
        "dates": f"{start:%Y%m%d}/{last + timedelta(days=1):%Y%m%d}",  # конец в формате Google - не включительно
        "details": f"{a.text[:600]}\n\nСрок: {last:%d.%m.%Y}\n{link(a)}",
    })


def event_body(a: Appeal, role: str) -> dict:
    start, last = span(a)
    done = a.status in FINAL
    who = "Вы отвечаете за это обращение" if role == "assignee" else "Ваше обращение"
    when = f"Закрыто {last:%d.%m.%Y}" if done else f"Срок: {last:%d.%m.%Y}"
    return {
        "summary": f"{'✓ ' if done else ''}{a.number} {a.title}",
        "description": f"{who}. Статус: {STATUSES[a.status]}. {when}\n\n{a.text[:1500]}\n\n{link(a)}",
        "start": {"date": start.isoformat()},
        "end": {"date": (last + timedelta(days=1)).isoformat()},
        "transparency": "transparent",  # полоса срока не делает сотрудника «занятым» на эти дни
        "status": "confirmed",          # если событие удалили в календаре руками - вернется
        "source": {"title": "Модус.Отклик", "url": link(a)},
    }


# ---------- OAuth ----------

def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })


def connect(db: Session, user: User, code: str) -> None:
    """Обменять код возврата на постоянный доступ и разложить по календарю открытые обращения сотрудника."""
    try:
        with _http() as c:
            r = c.post(TOKEN_URL, data={"code": code, "client_id": settings.google_client_id,
                                        "client_secret": settings.google_client_secret,
                                        "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise CalendarError("Google не подтвердил доступ - попробуйте подключить еще раз") from e
    if not data.get("refresh_token"):
        raise CalendarError("Google не выдал постоянный доступ - подключите календарь еще раз")
    email = ""
    try:  # ответ пришел напрямую от Google по TLS - подпись id_token перепроверять незачем
        email = jwt.decode(data.get("id_token", ""), options={"verify_signature": False}).get("email", "")
    except Exception:
        pass
    user.gcal = {"refresh_token": data["refresh_token"], "email": email}
    if data.get("access_token"):
        _tokens[user.id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    db.commit()
    ids = db.scalars(select(Appeal.id).where(or_(Appeal.author_id == user.id, Appeal.assignee_id == user.id),
                                             Appeal.status.notin_(FINAL))).all()
    schedule(set(ids))


def disconnect(db: Session, user: User) -> None:
    """Убрать события из календаря, отозвать доступ и забыть его."""
    key = str(user.id)
    for a in db.scalars(select(Appeal).where(or_(Appeal.author_id == user.id, Appeal.assignee_id == user.id))):
        if key in (a.gcal or {}):
            try:
                _delete(user, a.gcal[key]["id"])
            except Exception:  # не получилось убрать - отключение все равно доводим до конца
                log.warning("Не удалось убрать событие %s из календаря", a.number)
            a.gcal = {k: v for k, v in a.gcal.items() if k != key}
    try:
        with _http() as c:
            c.post(REVOKE_URL, data={"token": (user.gcal or {}).get("refresh_token", "")})
    except httpx.HTTPError:
        pass
    user.gcal = None
    _tokens.pop(user.id, None)


def _access(u: User) -> str:
    tok = _tokens.get(u.id)
    if tok and tok[1] > time.time() + 60:
        return tok[0]
    with _http() as c:
        r = c.post(TOKEN_URL, data={"client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                    "refresh_token": u.gcal["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code in (400, 401):  # invalid_grant: доступ отозван или истек
        raise Revoked()
    r.raise_for_status()
    data = r.json()
    _tokens[u.id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def _call(u: User, method: str, event_id: str = "", body: dict | None = None) -> httpx.Response:
    token = _access(u)
    with _http() as c:
        return c.request(method, f"{EVENTS_URL}/{event_id}" if event_id else EVENTS_URL,
                         headers={"Authorization": f"Bearer {token}"}, json=body)


def _upsert(u: User, event_id: str | None, body: dict) -> str:
    if event_id:
        r = _call(u, "PATCH", event_id, body)
        if r.status_code not in (404, 410):  # события уже нет в календаре - создадим заново
            r.raise_for_status()
            return event_id
    r = _call(u, "POST", body=body)
    r.raise_for_status()
    return r.json()["id"]


def _delete(u: User, event_id: str) -> None:
    r = _call(u, "DELETE", event_id)
    if r.status_code not in (404, 410):
        r.raise_for_status()


# ---------- синхронизация ----------

def sync_appeal(db: Session, a: Appeal) -> None:
    """Привести события в календарях автора и ответственного к текущему состоянию обращения."""
    want: dict[int, str] = {}
    if a.author_id:
        want[a.author_id] = "author"
    if a.assignee_id:
        want[a.assignee_id] = "assignee"
    state = dict(a.gcal or {})
    for uid in {int(k) for k in state} | set(want):
        key, cur = str(uid), state.get(str(uid))
        u = db.get(User, uid)
        connected = bool(u and u.active and u.gcal)
        if not cur and not connected:
            continue
        try:
            if uid not in want or not connected:  # обращение передали другому или календарь отключен
                if connected:
                    _delete(u, cur["id"])
                state.pop(key, None)
                continue
            body = event_body(a, want[uid])
            h = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            if cur and cur.get("h") == h:
                continue  # в календаре уже то же самое - Google не дергаем
            state[key] = {"id": _upsert(u, cur["id"] if cur else None, body), "h": h}
        except Revoked:
            u.gcal = None
            _tokens.pop(uid, None)
            state.pop(key, None)
            db.add(Notification(user_id=uid, text="Google Календарь отключился: доступ отозван. Подключите его заново в профиле"))
        except (httpx.HTTPError, KeyError, ValueError):
            log.exception("Google Календарь: %s не синхронизировано для сотрудника %s", a.number, uid)
    if state != (a.gcal or {}):
        a.gcal = state


def sync_ids(ids: set[int]) -> None:
    with _lock, SessionLocal() as db:
        db.info["gcal_skip"] = True  # собственные записи синхронизации новую синхронизацию не запускают
        for appeal_id in sorted(ids):
            a = db.get(Appeal, appeal_id)
            if a:
                sync_appeal(db, a)
                db.commit()


def schedule(ids: set[int]) -> None:
    if not ids or not settings.google_enabled:
        return
    if settings.background:  # свой сервер: сеть не задерживает ответ пользователю
        threading.Thread(target=_safe_sync, args=(ids,), daemon=True, name="gcal").start()
    else:                    # бессерверная среда и тесты: фоновых потоков нет
        _safe_sync(ids)


def _safe_sync(ids: set[int]) -> None:
    try:
        sync_ids(ids)
    except Exception:
        log.exception("Google Календарь: синхронизация")


@event.listens_for(SessionLocal, "after_flush")
def _collect(session, _ctx) -> None:
    if session.info.get("gcal_skip") or not settings.google_enabled:
        return
    ids = {o.id for o in list(session.new) + list(session.dirty) if isinstance(o, Appeal) and o.id}
    if ids:
        session.info.setdefault("gcal_ids", set()).update(ids)


@event.listens_for(SessionLocal, "after_commit")
def _flush_out(session) -> None:
    schedule(session.info.pop("gcal_ids", set()))


@event.listens_for(SessionLocal, "after_rollback")
def _forget(session) -> None:
    session.info.pop("gcal_ids", None)
