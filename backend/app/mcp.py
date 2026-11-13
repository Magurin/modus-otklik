"""MCP-сервер: общий список обращений как инструмент любого ИИ-агента (Claude, Cursor, ChatGPT и других).

Сотрудник обсуждает задачу со своим агентом - тот уточняет детали, сверяется с похожими обращениями,
показывает, куда оно попадет и к какому сроку, - и только после согласия человека отправляет в общий список.
Обращение регистрируется тем же workflow.register(), что и из формы или бота: маршрут, история, уведомления те же.

Транспорт - Streamable HTTP без состояния: один POST с JSON-RPC, ответ обычным JSON. Сессий и SSE нет,
поэтому работает и на своем сервере, и в бессерверной среде. Доступ - по личному ключу сотрудника
(заголовок Authorization: Bearer или последним сегментом адреса - для клиентов, которые не умеют заголовки).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import ai, bots, gcal, mail, meetings, workflow
from .api import author_view, event_view
from .db import get_db
from .models import FINAL, MIN_TEXT, PRIORITIES, ROLES, STATUSES, TYPES, Appeal, Category, Meeting, MeetingItem, User, now
from .config import UPLOAD_DIR, public_url, settings
from .security import agent_key_hash, current_user, new_agent_key

router = APIRouter()
log = logging.getLogger("mcp")

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Модус.Отклик - общий список обращений сотрудников компании: проблемы, предложения и благодарности. "
    "У каждого обращения есть ответственный и срок, автор видит ход работы.\n"
    "Как помогать пользователю завести задачу:\n"
    "1. Сначала обсуди ее: что происходит, где и как часто, чем мешает, что он предлагает. Не выдумывай факты - спрашивай.\n"
    "2. Вызови draft_task с черновиком текста: увидишь тип, категорию, ответственного, срок и похожие обращения.\n"
    "3. Если похожее обращение уже открыто, предложи присоединиться к нему (support_task), а не создавать дубль.\n"
    "4. Покажи пользователю итоговый текст и вызывай submit_task только после его явного согласия.\n"
    "Текст обращения пиши от лица пользователя, по-русски, по делу, без упоминания ИИ."
)

_text = {"type": "string"}
TOOLS = [
    {"name": "get_guide", "title": "Справочник",
     "description": "Кто я в системе и какие бывают типы, приоритеты и категории обращений (с ответственными и сроками). "
                    "Вызови в начале разговора, чтобы рассуждать о задаче в терминах компании.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "find_similar", "title": "Похожие обращения",
     "description": "Найти уже существующие обращения о том же. Ничего не создает.",
     "inputSchema": {"type": "object", "properties": {"text": {**_text, "description": "Суть задачи своими словами"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "draft_task", "title": "Черновик обращения",
     "description": "Примерить черновик: как система определит тип, категорию и приоритет, кто станет ответственным, "
                    "какой будет срок, чего не хватает в тексте и нет ли похожих обращений. Ничего не создает - "
                    "вызывай сколько нужно, пока обсуждаешь задачу с пользователем.",
     "inputSchema": {"type": "object", "properties": {"text": {**_text, "description": "Черновик текста обращения"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "submit_task", "title": "Отправить в общий список",
     "description": "Зарегистрировать обращение в общем списке от имени пользователя: оно получит номер, ответственного "
                    "и срок, ответственный - уведомление. Вызывай только после того, как пользователь увидел итоговый "
                    "текст и явно согласился отправить. Чего не указать - определит система.",
     "inputSchema": {"type": "object", "properties": {
         "text": {**_text, "description": "Полный текст обращения от лица пользователя"},
         "title": {**_text, "description": "Заголовок до 80 символов"},
         "type": {**_text, "enum": list(TYPES), "description": "problem - проблема, proposal - предложение, praise - благодарность"},
         "category": {**_text, "description": "Название категории из get_guide"},
         "priority": {**_text, "enum": list(PRIORITIES)},
         "solution": {**_text, "description": "Решение, которое предлагает пользователь"},
         "anonymous": {"type": "boolean", "description": "Скрыть автора от всех, кроме него самого"},
     }, "required": ["text"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "list_tasks", "title": "Список обращений",
     "description": "Обращения из общего списка, которые пользователю разрешено видеть. scope: mine - мои, "
                    "inbox - назначенные мне, all - все доступные.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {**_text, "enum": ["mine", "inbox", "all"], "default": "mine"},
         "status": {**_text, "enum": ["open", *STATUSES], "description": "open - все незакрытые"},
         "query": {**_text, "description": "Поиск по тексту, заголовку и номеру"},
     }},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_task", "title": "Обращение целиком",
     "description": "Текст, статус, срок, ответственный и обсуждение по номеру обращения (например A-012).",
     "inputSchema": {"type": "object", "properties": {"number": _text}, "required": ["number"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "support_task", "title": "У меня то же самое",
     "description": "Присоединить пользователя к чужому открытому обращению вместо создания дубля. "
                    "Только с его согласия.",
     "inputSchema": {"type": "object", "properties": {"number": _text}, "required": ["number"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
]
_id = {"type": "integer"}
MEETING_TOOLS = [
    {"name": "list_meetings", "title": "Совещания",
     "description": "Совещания, которые пользователь загружал (директору и администратору - все): статус разбора "
                    "и сколько пунктов ждут решения.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "add_meeting", "title": "Загрузить совещание",
     "description": "Загрузить совещание на разбор: система выделит проблемы, предложения, решения и поручения. "
                    "Источник - ровно один: transcript (текст протокола или расшифровки), file_url (публичная ссылка "
                    "на аудио, видео, .txt или .docx) или file_base64 с filename (файл до 20 МБ). Запись распознается "
                    "на сервере. Разбор идет в фоне: дальше вызывай get_meeting, пока status не станет ready или error. "
                    "Само по себе обращений не создает.",
     "inputSchema": {"type": "object", "properties": {
         "title": {**_text, "description": "Название, например «Планерка отдела закупок»"},
         "held_at": {**_text, "description": "Дата совещания ГГГГ-ММ-ДД (по умолчанию сегодня)"},
         "transcript": {**_text, "description": "Текст протокола или расшифровки, от 50 символов"},
         "file_url": {**_text, "description": "Публичная ссылка http(s) на файл записи или протокола"},
         "file_base64": {**_text, "description": "Содержимое файла в base64"},
         "filename": {**_text, "description": "Имя файла с расширением - для file_base64 (и для ссылки без расширения)"},
     }, "required": ["title"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "get_meeting", "title": "Совещание целиком",
     "description": "Статус разбора, краткое содержание и пункты совещания: вид, текст, цитата из записи, кто сказал, "
                    "категория, приоритет и состояние (draft - ждет решения, accepted, rejected). Для черновиков "
                    "проблем, предложений и благодарностей видно, кто станет ответственным и какой будет срок.",
     "inputSchema": {"type": "object", "properties": {
         "meeting_id": _id,
         "transcript": {"type": "boolean", "description": "Вернуть полную расшифровку"},
     }, "required": ["meeting_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "accept_meeting_item", "title": "Подтвердить пункт совещания",
     "description": "Подтвердить пункт-черновик. Проблема, предложение или благодарность становится обращением: "
                    "номер, ответственный, срок, уведомление. Решение и поручение остаются в протоколе совещания. "
                    "Перед этим можно поправить текст, вид, категорию, приоритет и указать автора-сотрудника "
                    "(иначе автор - «со слов на совещании»). Вызывай только после явного согласия пользователя.",
     "inputSchema": {"type": "object", "properties": {
         "item_id": _id,
         "title": _text, "text": _text,
         "kind": {**_text, "enum": ["problem", "proposal", "praise", "decision", "task"]},
         "category": {**_text, "description": "Название категории из get_guide"},
         "priority": {**_text, "enum": list(PRIORITIES)},
         "author": {**_text, "description": "ФИО или почта сотрудника, от чьего имени завести обращение"},
     }, "required": ["item_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "reject_meeting_item", "title": "Отклонить пункт совещания",
     "description": "Отметить пункт-черновик как ненужный: обращения из него не будет. Только с согласия пользователя.",
     "inputSchema": {"type": "object", "properties": {"item_id": _id}, "required": ["item_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "retry_meeting", "title": "Разобрать совещание заново",
     "description": "Повторить разбор после ошибки или заново выделить пункты. Неподтвержденные черновики заменятся, "
                    "подтвержденные и отклоненные пункты останутся.",
     "inputSchema": {"type": "object", "properties": {"meeting_id": _id}, "required": ["meeting_id"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
]
CHANNEL_TOOLS = [
    {"name": "get_channels", "title": "Каналы связи",
     "description": "Через что пользователь может подавать обращения и получать уведомления: бот в Telegram "
                    "(работает ли бот, привязан ли он у пользователя) и общий почтовый ящик для обращений письмом. "
                    "Администратору - еще состояние каждого канала и ошибки.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "connect_messenger", "title": "Привязать Telegram",
     "description": "Получить одноразовую ссылку и код, чтобы привязать Telegram-бота к учетной записи пользователя. "
                    "Отдай пользователю ссылку (link): он открывает ее и нажимает «Старт» - или отправляет боту код. "
                    "Потом проверь привязку через get_channels. Прежний код при этом перестает действовать.",
     "inputSchema": {"type": "object", "properties": {
         "messenger": {**_text, "enum": ["telegram"], "default": "telegram"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "disconnect_messenger", "title": "Отвязать Telegram",
     "description": "Отвязать Telegram-бота от учетной записи: уведомления туда больше не придут. Только с согласия пользователя.",
     "inputSchema": {"type": "object", "properties": {"messenger": {**_text, "enum": ["telegram"], "default": "telegram"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}},
]
ADMIN_TOOLS = [
    {"name": "intake_email", "title": "Принять письмо",
     "description": "Зарегистрировать письмо как обращение - тем же путем, что письма из общего ящика: автор "
                    "определяется по адресу отправителя, цитаты и подпись отрезаются. Для писем, пришедших мимо ящика "
                    "(переслали лично, пришло на другой адрес). Только администратору и только с его согласия.",
     "inputSchema": {"type": "object", "properties": {
         "sender": {**_text, "description": "Отправитель: адрес или «Имя <адрес>»"},
         "subject": _text,
         "body": {**_text, "description": "Текст письма"},
     }, "required": ["sender", "body"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
    {"name": "check_mailbox", "title": "Проверить почтовый ящик",
     "description": "Сразу забрать новые письма из общего ящика обращений, не дожидаясь проверки по расписанию "
                    "(раз в минуту). Каждое непрочитанное письмо станет обращением. Только администратору.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}},
]
MEETING_ROLES = ("manager", "director", "admin")  # совещания в интерфейсе видят те, кто рассматривает обращения
MEETINGS_GUIDE = (
    "Совещания: загрузи протокол, расшифровку или запись (add_meeting), дождись разбора (get_meeting до status ready), "
    "покажи пользователю пункты и по каждому спроси, что делать. Подтверждай (accept_meeting_item) и отклоняй "
    "(reject_meeting_item) только то, что пользователь явно одобрил; автора-сотрудника указывай, только если он назван."
)


CHANNELS_GUIDE = (
    "Каналы: если пользователь хочет получать уведомления или писать в Telegram, вызови connect_messenger "
    "и отдай ему ссылку, затем проверь привязку через get_channels. Адрес ящика для обращений письмом - в get_channels."
)


def tools_for(user: User) -> list[dict]:
    return (TOOLS + CHANNEL_TOOLS + (MEETING_TOOLS if user.role in MEETING_ROLES else [])
            + (ADMIN_TOOLS if user.role == "admin" else []))


def instructions_for(user: User) -> str:
    return INSTRUCTIONS + "\n" + CHANNELS_GUIDE + ("\n" + MEETINGS_GUIDE if user.role in MEETING_ROLES else "")


PROMPTS = [{"name": "new_task", "title": "Обсудить и завести задачу",
            "description": "Разобрать идею или проблему и, когда она оформится, отправить в общий список обращений",
            "arguments": [{"name": "idea", "description": "С чего начать: пара слов о задаче", "required": False}]}]


class ToolError(Exception):
    pass


def _day(a: Appeal) -> str | None:
    return gcal.span(a)[1].strftime("%d.%m.%Y") if a.due_at else None


def _row(a: Appeal, viewer: User) -> dict:
    return {"number": a.number, "title": a.title, "type": TYPES[a.type], "status": STATUSES[a.status],
            "priority": PRIORITIES[a.priority], "category": a.category.name if a.category else None,
            "assignee": a.assignee.name if a.assignee else None, "due": _day(a) if a.status not in FINAL else None,
            "overdue": workflow.overdue(a), "supports": len(a.supports), "mine": a.author_id == viewer.id,
            "url": gcal.link(a)}


def _similar(db: Session, text: str, user: User) -> list[dict]:
    return [{"number": r.number, "title": r.title, "status": STATUSES[r.status], "score": s, "supports": len(r.supports),
             "mine": r.author_id == user.id, "open": r.status not in FINAL}
            for r, s in workflow.similar(db, text, threshold=0.25)]


def _by_number(db: Session, number: str, user: User, view: bool = True) -> Appeal:
    a = db.scalar(select(Appeal).where(Appeal.number == str(number).strip().upper()))
    if not a or (view and not workflow.can_view(user, a)):
        raise ToolError(f"Обращение {number} не найдено")
    return a


def _need_text(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    if len(text) < MIN_TEXT:
        raise ToolError(f"Опишите подробнее - хотя бы {MIN_TEXT} символов")
    return text


def get_guide(db: Session, user: User, args: dict) -> dict:
    cats = db.scalars(select(Category).where(Category.active).order_by(Category.name))
    return {"me": {"name": user.name, "position": user.position, "role": ROLES[user.role],
                   "department": user.department.name if user.department else None},
            "types": TYPES, "priorities": PRIORITIES, "statuses": STATUSES,
            "categories": [{"name": c.name, "description": c.description, "sla_days": c.sla_days,
                            "responsible": c.responsible.name if c.responsible else None} for c in cats]}


def find_similar(db: Session, user: User, args: dict) -> dict:
    return {"similar": _similar(db, _need_text(args), user)}


def draft_task(db: Session, user: User, args: dict) -> dict:
    text = _need_text(args)
    t, engine = ai.triage(text, workflow.categories_for_ai(db))
    cat = db.scalar(select(Category).where(Category.name == t.category))
    to, reason = workflow.route(db, cat)
    return {"type": t.type, "type_label": TYPES[t.type], "category": cat.name if cat else None, "priority": t.priority,
            "title": t.title, "summary": t.summary, "missing": t.question or None,
            "assignee": to.name if to else None, "route": reason, "sla_days": cat.sla_days if cat else 10,
            "similar": _similar(db, text, user), "engine": engine}


def submit_task(db: Session, user: User, args: dict) -> dict:
    text = _need_text(args)
    kind, priority = args.get("type") or None, args.get("priority") or None
    if kind and kind not in TYPES:
        raise ToolError(f"Неизвестный тип. Допустимые: {', '.join(TYPES)}")
    if priority and priority not in PRIORITIES:
        raise ToolError(f"Неизвестный приоритет. Допустимые: {', '.join(PRIORITIES)}")
    cat = _category(db, args["category"]) if args.get("category") else None
    a = workflow.register(db, text=text, author=user, source="agent", type=kind, category_id=cat.id if cat else None,
                          priority=priority, title=workflow.clean_title(str(args.get("title") or "")) or None,
                          solution=str(args.get("solution") or ""), anonymous=bool(args.get("anonymous")))
    db.commit()
    q = workflow.open_question(a)
    return {**_row(a, user), "calendar_url": gcal.template_url(a), "question": q.text if q else None,
            "message": f"Обращение {a.number} зарегистрировано" + (f", ответственный - {a.assignee.name}" if a.assignee else "")
                       + f", срок - {_day(a)}"}


def list_tasks(db: Session, user: User, args: dict) -> dict:
    scope, status, q = args.get("scope") or "mine", args.get("status") or "", str(args.get("query") or "").strip()
    stmt = select(Appeal).order_by(Appeal.id.desc())
    if scope == "mine":
        stmt = stmt.where(Appeal.author_id == user.id)
    elif scope == "inbox":
        stmt = stmt.where(Appeal.assignee_id == user.id)
    if status == "open":
        stmt = stmt.where(Appeal.status.notin_(FINAL))
    elif status in STATUSES:
        stmt = stmt.where(Appeal.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Appeal.title.ilike(like), Appeal.text.ilike(like), Appeal.number.ilike(like)))
    rows = [a for a in db.scalars(stmt) if workflow.can_view(user, a)]
    return {"total": len(rows), "tasks": [_row(a, user) for a in rows[:50]]}


def get_task(db: Session, user: User, args: dict) -> dict:
    a = _by_number(db, args.get("number", ""), user)
    full = workflow.can_handle(user, a)
    events = [event_view(e, a, user) for e in a.events if full or not e.internal]
    return {**_row(a, user), "text": a.text, "solution": a.solution or None, "resolution": a.resolution or None,
            "author": author_view(a, user)["name"],
            "calendar_url": gcal.template_url(a) if a.due_at and a.status not in FINAL else None,
            "discussion": [{"at": e["created_at"], "who": e["actor"], "kind": e["kind"], "text": e["text"]}
                           for e in events if e["text"]]}


def support_task(db: Session, user: User, args: dict) -> dict:
    a = _by_number(db, args.get("number", ""), user, view=False)  # присоединиться можно по номеру из похожих
    try:
        workflow.support(db, a, user)
    except ValueError as e:
        raise ToolError(str(e)) from e
    db.commit()
    return {"number": a.number, "supports": len(a.supports), "message": f"Вы присоединились к обращению {a.number}"}


def _category(db: Session, name: str) -> Category:
    key = str(name).strip().lower()
    cat = next((c for c in db.scalars(select(Category).where(Category.active)) if c.name.lower() == key), None)
    if not cat:
        names = ", ".join(c.name for c in db.scalars(select(Category).where(Category.active).order_by(Category.name)))
        raise ToolError(f"Нет категории «{name}». Есть: {names}")
    return cat


# ---------- совещания ----------

MEETING_KINDS = {"problem": "Проблема", "proposal": "Предложение", "praise": "Благодарность",
                 "decision": "Решение", "task": "Поручение"}
MEETING_STATUSES = {"queued": "В очереди", "transcribing": "Распознается речь", "extracting": "Выделяются пункты",
                    "ready": "Готово", "error": "Ошибка"}
MAX_AGENT_FILE = 20 * 1024 * 1024


def _need_handler(user: User) -> None:
    if user.role not in MEETING_ROLES:
        raise ToolError("Совещания доступны руководителям, директору и администратору")


def _meeting(db: Session, user: User, meeting_id: Any) -> Meeting:
    _need_handler(user)
    try:
        m = db.get(Meeting, int(meeting_id))
    except (TypeError, ValueError):
        m = None
    if not m or (user.role == "manager" and m.uploaded_by_id != user.id):
        raise ToolError(f"Совещание {meeting_id} не найдено")
    return m


def _item(db: Session, user: User, item_id: Any) -> tuple[Meeting, MeetingItem]:
    try:
        i = db.get(MeetingItem, int(item_id))
    except (TypeError, ValueError):
        i = None
    if not i:
        raise ToolError(f"Пункт {item_id} не найден")
    m = _meeting(db, user, i.meeting_id)
    if i.state != "draft":
        raise ToolError(f"Пункт {i.id} уже обработан: {'подтвержден' if i.state == 'accepted' else 'отклонен'}")
    return m, i


def _meeting_row(m: Meeting) -> dict:
    return {"meeting_id": m.id, "title": m.title, "held_at": m.held_at.strftime("%d.%m.%Y"), "status": m.status,
            "status_label": MEETING_STATUSES.get(m.status, m.status), "error": m.error or None, "file": m.filename or None,
            "uploaded_by": m.uploaded_by.name if m.uploaded_by else None, "items_total": len(m.items),
            "items_draft": sum(i.state == "draft" for i in m.items),
            "items_accepted": sum(i.state == "accepted" for i in m.items),
            "url": f"{public_url()}/meetings/{m.id}"}


def _item_row(db: Session, i: MeetingItem, viewer: User) -> dict:
    a = db.get(Appeal, i.appeal_id) if i.appeal_id else None
    out = {"item_id": i.id, "kind": i.kind, "kind_label": MEETING_KINDS.get(i.kind, i.kind), "title": i.title,
           "text": i.text, "quote": i.quote or None, "speaker": i.speaker or None,
           "category": i.category.name if i.category else None, "priority": i.priority, "state": i.state,
           "becomes_task": i.kind in meetings.APPEAL_KINDS, "task": _row(a, viewer) if a else None}
    if i.state == "draft" and i.kind in meetings.APPEAL_KINDS:
        to, reason = workflow.route(db, i.category)
        out |= {"assignee": to.name if to else None, "route": reason, "sla_days": i.category.sla_days if i.category else 10}
    return out


def list_meetings(db: Session, user: User, args: dict) -> dict:
    _need_handler(user)
    stmt = select(Meeting).order_by(Meeting.id.desc())
    if user.role == "manager":
        stmt = stmt.where(Meeting.uploaded_by_id == user.id)
    rows = list(db.scalars(stmt))
    return {"total": len(rows), "meetings": [_meeting_row(m) for m in rows[:50]]}


def add_meeting(db: Session, user: User, args: dict) -> dict:
    _need_handler(user)
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("Укажите название совещания")
    transcript = str(args.get("transcript") or "").replace("\r\n", "\n").strip()
    url, b64 = str(args.get("file_url") or "").strip(), str(args.get("file_base64") or "").strip()
    filename = Path(str(args.get("filename") or "")).name[:255]
    if sum(map(bool, (transcript, url, b64))) != 1:
        raise ToolError("Нужен ровно один источник: transcript, file_url или file_base64")
    if transcript and len(transcript) < 50:
        raise ToolError("Текст совещания слишком короткий - нужно хотя бы 50 символов")
    try:
        when = datetime.fromisoformat(str(args["held_at"])) if args.get("held_at") else now()
    except ValueError as e:
        raise ToolError("Дата совещания - в виде ГГГГ-ММ-ДД") from e

    stored, ext = "", ""
    if url:
        try:
            ext = Path(filename).suffix.lower() or meetings.check_url(url)
            if filename:
                meetings.check_url(url)
        except ValueError as e:
            raise ToolError(str(e)) from e
    elif b64:
        ext = Path(filename).suffix.lower()
        if not filename:
            raise ToolError("Для file_base64 укажите filename с расширением")
    if ext:
        if ext not in meetings.ALLOWED_EXT:
            raise ToolError("Поддерживаются аудио, видео, .txt, .md, .vtt, .srt и .docx - укажите filename с расширением")
        if ext in meetings.MEDIA_EXT and not settings.speech_mode:
            raise ToolError("Здесь записи не распознаются - пришлите протокол или расшифровку текстом")
    if b64:
        if len(b64) > MAX_AGENT_FILE * 4 // 3 + 4:
            raise ToolError("Файл больше 20 МБ - пришлите публичную ссылку (file_url)")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ToolError("file_base64 - не base64") from e
        stored = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / stored).write_bytes(data)

    m = Meeting(title=title[:200], held_at=when, uploaded_by_id=user.id, filename=filename, stored=stored,
                transcript=transcript)
    db.add(m)
    db.commit()
    meetings.run(m.id, url, ext)
    db.refresh(m)
    return {**_meeting_row(m), "message": f"Совещание «{m.title}» принято на разбор (meeting_id {m.id}). "
                                          "Вызывайте get_meeting, пока status не станет ready."}


def get_meeting(db: Session, user: User, args: dict) -> dict:
    m = _meeting(db, user, args.get("meeting_id"))
    out = {**_meeting_row(m), "summary": m.summary or None, "items": [_item_row(db, i, user) for i in m.items]}
    if args.get("transcript"):
        out["transcript"] = m.transcript
    return out


def _author(db: Session, who: str) -> User:
    key = who.strip().lower()
    users = list(db.scalars(select(User).where(User.active)))
    found = [u for u in users if u.email.lower() == key] or [u for u in users if u.name.lower() == key] \
        or [u for u in users if key in u.name.lower()]
    if len(found) != 1:
        hint = ", ".join(f"{u.name} ({u.email})" for u in found[:8])
        raise ToolError(f"Сотрудник «{who}» " + (f"найден не один: {hint}. Уточните почту" if found else "не найден"))
    return found[0]


def accept_meeting_item(db: Session, user: User, args: dict) -> dict:
    m, i = _item(db, user, args.get("item_id"))
    if args.get("kind"):
        if args["kind"] not in MEETING_KINDS:
            raise ToolError(f"Неизвестный вид. Допустимые: {', '.join(MEETING_KINDS)}")
        i.kind = args["kind"]
    if args.get("priority"):
        if args["priority"] not in PRIORITIES:
            raise ToolError(f"Неизвестный приоритет. Допустимые: {', '.join(PRIORITIES)}")
        i.priority = args["priority"]
    if args.get("category"):
        i.category_id = _category(db, args["category"]).id
    for f in ("title", "text"):
        if str(args.get(f) or "").strip():
            setattr(i, f, str(args[f]).strip()[:200] if f == "title" else str(args[f]).strip())
    author = _author(db, str(args["author"])) if str(args.get("author") or "").strip() else None
    a = meetings.accept_item(db, m, i, user, author)
    db.commit()
    db.refresh(i)
    out = _item_row(db, i, user)
    if a:
        out |= {"calendar_url": gcal.template_url(a) if a.due_at else None,
                "message": f"Обращение {a.number} зарегистрировано" + (f", ответственный - {a.assignee.name}" if a.assignee else "")
                           + (f", срок - {_day(a)}" if a.due_at else "")}
    else:
        out["message"] = f"{MEETING_KINDS[i.kind]} остается в протоколе совещания"
    return out


def reject_meeting_item(db: Session, user: User, args: dict) -> dict:
    _, i = _item(db, user, args.get("item_id"))
    i.state = "rejected"
    db.commit()
    return {**_item_row(db, i, user), "message": f"Пункт {i.id} отклонен"}


def retry_meeting(db: Session, user: User, args: dict) -> dict:
    m = _meeting(db, user, args.get("meeting_id"))
    if m.status not in ("error", "ready"):
        raise ToolError("Совещание еще обрабатывается - дождитесь окончания")
    if not m.transcript and not m.stored:
        raise ToolError("У совещания нет ни текста, ни файла - загрузите его заново (add_meeting)")
    m.status, m.error = "queued", ""
    db.commit()
    meetings.run(m.id)
    db.refresh(m)
    return {**_meeting_row(m), "message": "Совещание поставлено на повторный разбор"}


# ---------- каналы: мессенджеры и почта ----------

def _messenger(key: Any):
    ch = bots.channels().get(str(key or "telegram").strip().lower())
    if not ch:
        raise ToolError("Подключается только Telegram (messenger: telegram)")
    return ch


def get_channels(db: Session, user: User, args: dict) -> dict:
    msgs = [{"messenger": ch.key, "title": ch.title, "working": ch.state["enabled"], "bot": ch.state["username"] or None,
             "linked": bool(getattr(user, ch.user_field))}
            for ch in bots.channels().values() if ch.configured or ch.state["enabled"]]
    mail_on = bool(settings.imap_host and settings.imap_user)
    out = {"messengers": msgs,
           "email": {"address": (settings.imap_to or settings.imap_user) if mail_on else None, "working": bool(mail.state["enabled"]),
                     "how": "Письмо на этот адрес становится обращением; автор - по адресу отправителя"
                            if mail_on else "Прием обращений по почте не настроен"}}
    if user.role == "admin":
        out["admin"] = {"telegram": {**bots.channels()["telegram"].state, "configured": bool(settings.telegram_token)},
                        "email": {**mail.state, "configured": bool(settings.imap_host)},
                        "ai": {"engine": ai.engine_name(), "enabled": settings.llm_enabled}}
    return out


def connect_messenger(db: Session, user: User, args: dict) -> dict:
    ch = _messenger(args.get("messenger"))
    if not ch.state["enabled"]:
        raise ToolError(f"Бот {ch.title} сейчас не подключен на сервере - привязать его нельзя")
    user.tg_link_code = secrets.token_hex(3).upper()  # тот же код, что выдает профиль
    db.commit()
    return {"messenger": ch.key, "bot": ch.state["username"], "code": user.tg_link_code,
            "link": ch.link_for(user.tg_link_code), "already_linked": bool(getattr(user, ch.user_field)),
            "message": f"Откройте ссылку и нажмите «Старт» в боте {ch.title} - или отправьте боту код {user.tg_link_code}."}


def disconnect_messenger(db: Session, user: User, args: dict) -> dict:
    ch = _messenger(args.get("messenger"))
    if not getattr(user, ch.user_field):
        raise ToolError(f"{ch.title} и так не привязан")
    setattr(user, ch.user_field, None)
    db.commit()
    return {"messenger": ch.key, "linked": False, "message": f"{ch.title} отвязан, уведомления туда больше не придут"}


def _need_admin(user: User) -> None:
    if user.role != "admin":
        raise ToolError("Это может только администратор")


def intake_email(db: Session, user: User, args: dict) -> dict:
    _need_admin(user)
    body = str(args.get("body") or "")
    if len(body.strip()) < 20:
        raise ToolError("Текст письма слишком короткий")
    if not str(args.get("sender") or "").strip():
        raise ToolError("Укажите отправителя")
    a = mail.intake(db, str(args["sender"]), str(args.get("subject") or ""), body)
    db.commit()
    return {**_row(a, user), "author": author_view(a, user)["name"],
            "message": f"Письмо зарегистрировано как {a.number}" + (f", ответственный - {a.assignee.name}" if a.assignee else "")}


def check_mailbox(db: Session, user: User, args: dict) -> dict:
    _need_admin(user)
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        raise ToolError("Почтовый ящик не настроен на сервере (IMAP_HOST, IMAP_USER, IMAP_PASSWORD)")
    try:
        n = mail.check_once()
    except Exception as e:
        mail.state["error"] = str(e)
        raise ToolError(f"Ящик не проверился: {e}") from e
    mail.state.update(error="", last_check=datetime.now().strftime("%H:%M:%S"))
    return {"new_tasks": n, "message": f"Новых писем: {n}" + (" - все зарегистрированы как обращения" if n else "")}


HANDLERS = {f.__name__: f for f in (get_guide, find_similar, draft_task, submit_task, list_tasks, get_task, support_task,
                                    get_channels, connect_messenger, disconnect_messenger, intake_email, check_mailbox,
                                    list_meetings, add_meeting, get_meeting, accept_meeting_item, reject_meeting_item,
                                    retry_meeting)}


# ---------- протокол ----------

def _err(code: int, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def dispatch(db: Session, user: User, method: str, params: dict) -> dict:
    """Ответ на один запрос JSON-RPC: {"result": ...} или {"error": ...}."""
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"result": {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                           "capabilities": {"tools": {}, "prompts": {}},
                           "serverInfo": {"name": "modus-otklik", "title": "Модус.Отклик", "version": "1.0.0"},
                           "instructions": instructions_for(user)}}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": tools_for(user)}}
    if method == "tools/call":
        fn = HANDLERS.get(params.get("name", ""))
        if not fn:
            return _err(-32602, f"Нет инструмента {params.get('name')}")
        args = params.get("arguments") or {}
        try:
            out = fn(db, user, args if isinstance(args, dict) else {})
        except ToolError as e:  # ошибку инструмента видит модель и может исправиться
            db.rollback()
            return {"result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        return {"result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}],
                           "structuredContent": out}}
    if method == "prompts/list":
        return {"result": {"prompts": PROMPTS}}
    if method == "prompts/get":
        if params.get("name") != "new_task":
            return _err(-32602, "Нет такого шаблона")
        idea = str((params.get("arguments") or {}).get("idea") or "").strip()
        text = "Помоги мне оформить задачу для общего списка обращений компании. " + INSTRUCTIONS.split("\n", 1)[1]
        return {"result": {"description": PROMPTS[0]["description"], "messages": [
            {"role": "user", "content": {"type": "text", "text": text + (f"\n\nЗадача: {idea}" if idea else "")}}]}}
    return _err(-32601, f"Метод {method} не поддерживается")


def _user(db: Session, key: str) -> User | None:
    if not key:
        return None
    return db.scalar(select(User).where(User.agent_key_hash == agent_key_hash(key), User.active))


@router.post("/api/mcp")
@router.post("/api/mcp/{key}")
def mcp(payload: Any = Body(None), key: str = "", authorization: str = Header(default=""), db: Session = Depends(get_db)):
    user = _user(db, key or authorization.removeprefix("Bearer ").strip())
    if not user:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Нужен личный ключ из профиля Модус.Отклик"}},
                            status_code=401, headers={"WWW-Authenticate": "Bearer"})
    batch = payload if isinstance(payload, list) else [payload]
    out = []
    for msg in batch:
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                continue  # ответ клиента на запрос сервера - таких запросов мы не шлем
            out.append({"jsonrpc": "2.0", "id": None, **_err(-32600, "Ожидается запрос JSON-RPC")})
            continue
        if "id" not in msg:
            continue  # уведомление (notifications/initialized и другие) ответа не требует
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            res = dispatch(db, user, msg["method"], params)
        except Exception:
            log.exception("MCP: %s", msg["method"])
            db.rollback()
            res = _err(-32603, "Внутренняя ошибка сервера")
        out.append({"jsonrpc": "2.0", "id": msg["id"], **res})
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(payload, list) else out[0])


@router.get("/api/mcp")
@router.get("/api/mcp/{key}")
@router.delete("/api/mcp")
@router.delete("/api/mcp/{key}")
def mcp_no_stream(key: str = ""):
    """Потока событий от сервера и сессий нет - по спецификации на GET и DELETE отвечаем 405."""
    return Response(status_code=405, headers={"Allow": "POST"})

# ---------- доступ для агента: файл навыка и промпт подключения ----------

KEY_ENV = "MODUS_OTKLIK_KEY"
SKILL_NAME = "modus-otklik"


def mcp_url() -> str:
    return f"{public_url()}/api/mcp"


def skill(db: Session, user: User) -> str:
    """SKILL.md для агента сотрудника. Ключа в файле нет: навык можно хранить в репозитории и пересылать."""
    url = mcp_url()
    cats = db.scalars(select(Category).where(Category.active).order_by(Category.name))
    available = tools_for(user)
    tools = "\n".join(f"- `{t['name']}` - {t['description']}" for t in available)
    readonly = ", ".join(f"`{t['name']}`" for t in available if t["annotations"].get("readOnlyHint"))
    changing = ", ".join(f"`{t['name']}`" for t in available if not t["annotations"].get("readOnlyHint")
                         and t["name"] != "connect_messenger")
    extra = "\n\n" + CHANNELS_GUIDE + ("\n\n" + MEETINGS_GUIDE if user.role in MEETING_ROLES else "")
    about = "привязки Telegram" +(", совещания" if user.role in MEETING_ROLES else "") \
        + (", прием писем" if user.role == "admin" else "")
    cat_lines = "\n".join(f"- {c.name} - срок {c.sla_days} дн." + (f", отвечает {c.responsible.name}" if c.responsible else "")
                          for c in cats)
    steps = INSTRUCTIONS.split("\n", 2)[2]
    return f"""---
name: {SKILL_NAME}
description: Обсудить с пользователем рабочую проблему или предложение и отправить в общий список обращений Модус.Отклик. Используй, когда пользователь хочет завести задачу, пожаловаться на процесс, предложить улучшение или узнать, что с его обращением, а также для {about}.
---

Этот навык работает с личным ключом доступа, выданным в профиле Модус.Отклик. Ключ берется из переменной
окружения {KEY_ENV}; не создавай ключ сам и не подбирай его. Никогда не вставляй ключ в этот файл,
в код или в текст обращения.

Система: Модус.Отклик - общий список обращений сотрудников компании
Сервер MCP: {url}
Сотрудник: {user.name}{f", {user.position}" if user.position else ""}{f", {user.department.name}" if user.department else ""}
Права: {ROLES[user.role]} - агент видит и делает ровно то, что этот сотрудник в интерфейсе
Ключ: переменная окружения {KEY_ENV}

## Порядок работы

{steps}{extra}

## Инструменты

{tools}

## Категории

{cat_lines}

## Как вызывать

Если твой клиент поддерживает MCP, подключи сервер {url} с заголовком `Authorization: Bearer ${KEY_ENV}`
и вызывай инструменты как обычно.

Если MCP нет, вызывай тот же сервер по HTTP - один POST на инструмент, сессия не нужна:

```bash
curl -s {url} \\
  -H "Authorization: Bearer ${KEY_ENV}" -H "Content-Type: application/json" \\
  -d '{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"draft_task","arguments":{{"text":"черновик обращения"}}}}}}'
```

Ответ лежит в `result.structuredContent`. Если в ответе `isError: true` - в `result.content[0].text` написано, что исправить.
Ответ 401 значит, что ключ отозван или не задан: попроси пользователя выдать новый в профиле Модус.Отклик.

## Правила

- {changing} меняют данные - вызывай их только после явного согласия пользователя.
- {readonly} ничего не меняют - вызывай свободно.
- Не выдумывай факты: чего не знаешь - спроси у пользователя.
- После отправки сообщи номер обращения, ответственного, срок и дай ссылку `calendar_url`, чтобы добавить срок в календарь.
"""


def harness_prompt(text: str, key: str = "") -> str:
    """Промпт, который сотрудник отправляет своему агенту: подключиться и установить навык."""
    url = mcp_url()
    secret = (f"Ключ доступа (показывается один раз): {key}\n   Сохрани его в переменную окружения {KEY_ENV}. "
              "Не записывай ключ в файлы репозитория и не повторяй его в ответах."
              if key else f"Ключ доступа уже выдан - он должен быть в переменной окружения {KEY_ENV}. "
                          "Если ее нет, попроси меня выдать новый ключ в профиле Модус.Отклик.")
    return f"""Подключись к Модус.Отклик - общему списку обращений моей компании.

1. {secret}
2. Сохрани файл навыка ниже как SKILL.md в свою папку навыков (для Claude Code - .claude/skills/{SKILL_NAME}/SKILL.md).
3. Если поддерживаешь MCP, добавь сервер. Для Claude Code:
   claude mcp add --transport http {SKILL_NAME} {url} --header "Authorization: Bearer ${KEY_ENV}"
   Если MCP нет - вызывай сервер по HTTP, как описано в навыке.
4. Проверь подключение: вызови get_guide и скажи, под каким сотрудником ты работаешь.

----- SKILL.md -----
{text}"""


def agent_view(db: Session, user: User, key: str = "") -> dict:
    text = skill(db, user)
    return {"enabled": bool(user.agent_key_hash), "url": mcp_url(), "name": SKILL_NAME, "key_env": KEY_ENV,
            "skill": text, "prompt": harness_prompt(text, key), **({"key": key} if key else {})}


@router.get("/api/me/agent")
def my_agent(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return agent_view(db, user)


@router.post("/api/me/agent/key")
def agent_key(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Выдать личный ключ для ИИ-агента. Прежний перестает работать; сам ключ показывается один раз."""
    key, user.agent_key_hash = new_agent_key()
    db.commit()
    return agent_view(db, user, key)


@router.post("/api/me/agent/revoke")
def agent_revoke(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.agent_key_hash = None
    db.commit()
    return agent_view(db, user)
