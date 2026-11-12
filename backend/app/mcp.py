"""MCP-сервер: общий список обращений как инструмент любого ИИ-агента (Claude, Cursor, ChatGPT и других).

Сотрудник обсуждает задачу со своим агентом - тот уточняет детали, сверяется с похожими обращениями,
показывает, куда оно попадет и к какому сроку, - и только после согласия человека отправляет в общий список.
Обращение регистрируется тем же workflow.register(), что и из формы или бота: маршрут, история, уведомления те же.

Транспорт - Streamable HTTP без состояния: один POST с JSON-RPC, ответ обычным JSON. Сессий и SSE нет,
поэтому работает и на своем сервере, и в бессерверной среде. Доступ - по личному ключу сотрудника
(заголовок Authorization: Bearer или последним сегментом адреса - для клиентов, которые не умеют заголовки).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import ai, gcal, workflow
from .api import author_view, event_view
from .db import get_db
from .models import FINAL, MIN_TEXT, PRIORITIES, ROLES, STATUSES, TYPES, Appeal, Category, User
from .config import public_url
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
    cat = None
    if args.get("category"):
        name = str(args["category"]).strip().lower()
        cat = next((c for c in db.scalars(select(Category).where(Category.active)) if c.name.lower() == name), None)
        if not cat:
            names = ", ".join(c.name for c in db.scalars(select(Category).where(Category.active).order_by(Category.name)))
            raise ToolError(f"Нет категории «{args['category']}». Есть: {names}")
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


HANDLERS = {f.__name__: f for f in (get_guide, find_similar, draft_task, submit_task, list_tasks, get_task, support_task)}


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
                           "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": TOOLS}}
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
    tools = "\n".join(f"- `{t['name']}` - {t['description']}" for t in TOOLS)
    cat_lines = "\n".join(f"- {c.name} - срок {c.sla_days} дн." + (f", отвечает {c.responsible.name}" if c.responsible else "")
                          for c in cats)
    steps = INSTRUCTIONS.split("\n", 2)[2]
    return f"""---
name: {SKILL_NAME}
description: Обсудить с пользователем рабочую проблему или предложение и отправить в общий список обращений Модус.Отклик. Используй, когда пользователь хочет завести задачу, пожаловаться на процесс, предложить улучшение или узнать, что с его обращением.
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

{steps}

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

- Отправляй обращение (`submit_task`) и присоединяйся к чужому (`support_task`) только после явного согласия пользователя.
- `draft_task`, `find_similar`, `list_tasks`, `get_task`, `get_guide` ничего не меняют - вызывай свободно.
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
