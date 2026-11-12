"""Боты в мессенджерах: Telegram и MAX. Сотрудник пишет или надиктовывает обращение, проверяет черновик,
получает номер и уведомления; ответственный разбирает входящие кнопками прямо в чате.

Логика диалога одна на оба мессенджера (Dialog), различается только транспорт (Channel).
Привязка к учетной записи - по одноразовому коду из профиля: посторонний написать не может.
Оба бота работают через long polling, внешний адрес и вебхук не нужны.
Незавершенный шаг диалога (черновик, ожидание текста) хранится в User.bot_state и переживает перезапуск.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import UPLOAD_DIR, settings
from .db import session_scope
from .models import FINAL, MIN_TEXT, STATUSES, TYPES, Appeal, Attachment, Category, Event, User, now

log = logging.getLogger("bots")

MAX_FILE = 20 * 1024 * 1024
# Кнопки под сообщением: строки из пар (подпись, действие). Действие «web:/путь» открывает приложение.
Buttons = list[list[tuple[str, str]]]

HELP = ("Напишите или надиктуйте голосовым, что мешает работе или что можно улучшить. Можно приложить фото или файл.\n\n"
        "Я покажу черновик, а после подтверждения зарегистрирую обращение и передам ответственному.")
ASK_TEXT = "Опишите, что мешает работе или что можно улучшить - текстом или голосом. Можно сразу с фото."
SHORT = "Опишите чуть подробнее: что происходит и чему это мешает."


@dataclass
class Incoming:
    """Входящее событие, одинаковое для всех мессенджеров."""
    chat_id: str
    text: str = ""
    start_code: str = ""            # код из ссылки запуска бота
    audio_url: str = ""             # голосовое: откуда скачать
    audio_headers: dict = field(default_factory=dict)
    transcript: str = ""            # если мессенджер сам расшифровал голосовое
    file_url: str = ""              # фото или документ
    file_name: str = ""
    file_size: int = 0
    callback: str = ""              # нажатая кнопка
    message_id: str = ""            # сообщение с кнопкой: его можно обновить на месте


class Channel:
    """Транспорт одного мессенджера. Наследник реализует _me, _send, _updates."""
    key = ""            # telegram | max
    title = ""
    user_field = ""     # поле User, где хранится id собеседника
    pause = 0.5         # пауза между исходящими сообщениями

    def __init__(self) -> None:
        self.state = {"enabled": False, "username": "", "error": "", "link": ""}
        self._out: queue.Queue[tuple[str, str, Buttons, str]] = queue.Queue()
        self._started = False

    configured = False

    def push(self, chat_id: str, text: str, buttons: Buttons | None = None, edit_id: str = "") -> None:
        """Поставить сообщение в очередь (не блокирует запрос и не падает без сети).
        edit_id - обновить уже отправленное сообщение вместо нового, если мессенджер это умеет."""
        if self._started:
            self._out.put((chat_id, text, buttons or [], edit_id))

    def link_for(self, code: str) -> str:
        return ""

    def resolve(self, url: str) -> str:
        """Адрес, по которому файл можно скачать."""
        return url

    def start(self) -> None:
        if self._started or not self.configured:
            return
        self._started = True
        threading.Thread(target=self._sender, daemon=True, name=f"{self.key}-send").start()
        threading.Thread(target=self._poll, daemon=True, name=f"{self.key}-poll").start()

    def _sender(self) -> None:
        while True:
            chat_id, text, buttons, edit_id = self._out.get()
            try:
                self._send(chat_id, text[:4000], buttons, edit_id)
            except Exception as e:
                log.warning("%s: не удалось отправить сообщение: %s", self.title, e)
            time.sleep(self.pause)

    def _poll(self) -> None:
        try:
            self.state["username"] = self._me()
            self.state.update(enabled=True, error="", link=self.link_for(""))
            log.info("%s-бот @%s запущен", self.title, self.state["username"])
        except Exception as e:
            self.state["error"] = str(e)[:200]
            log.error("%s-бот не запущен: %s", self.title, e)
            return
        try:
            self._setup()
        except Exception as e:
            log.warning("%s: меню бота не настроено: %s", self.title, e)
        while True:
            try:
                for inc in self._updates():
                    try:
                        handle(self, inc)
                    except Exception:
                        log.exception("%s: ошибка обработки сообщения", self.title)
                self.state["error"] = ""
            except Exception as e:
                self.state["error"] = str(e)[:200]
                log.warning("%s: %s", self.title, e)
                time.sleep(5)

    def _me(self) -> str: raise NotImplementedError
    def _send(self, chat_id: str, text: str, buttons: Buttons, edit_id: str) -> None: raise NotImplementedError
    def _updates(self) -> list[Incoming]: raise NotImplementedError
    def _setup(self) -> None: pass


class Telegram(Channel):
    key, title, user_field, pause = "telegram", "Telegram", "tg_chat_id", 0.05

    def __init__(self) -> None:
        super().__init__()
        self.configured = bool(settings.telegram_token)
        self._offset = 0

    def _api(self, method: str, **params) -> dict:
        r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_token}/{method}", json=params, timeout=40).json()
        if not r.get("ok"):
            raise RuntimeError(r.get("description", "ошибка Telegram API"))
        return r["result"]

    def link_for(self, code: str) -> str:
        u = self.state["username"]
        return f"https://t.me/{u}" + (f"?start={code}" if code else "") if u else ""

    def resolve(self, url: str) -> str:
        if not url.startswith("tg:"):
            return url
        path = self._api("getFile", file_id=url[3:])["file_path"]
        return f"https://api.telegram.org/file/bot{settings.telegram_token}/{path}"

    def _me(self) -> str:
        return self._api("getMe")["username"]

    def _setup(self) -> None:
        self._api("setMyCommands", commands=[
            {"command": "new", "description": "Новое обращение"},
            {"command": "my", "description": "Мои обращения"},
            {"command": "inbox", "description": "Входящие (для ответственных)"},
            {"command": "settings", "description": "Уведомления и отключение"},
            {"command": "help", "description": "Что умеет бот"},
        ])
        if settings.webapp_url:  # кнопка слева от поля ввода открывает приложение прямо в Telegram
            self._api("setChatMenuButton", menu_button={"type": "web_app", "text": "Открыть",
                                                        "web_app": {"url": settings.webapp_url}})

    def _markup(self, buttons: Buttons) -> dict:
        rows = []
        for row in buttons:
            out = []
            for label, data in row:
                if not data.startswith("web:"):
                    out.append({"text": label, "callback_data": data})
                elif settings.webapp_url:
                    out.append({"text": label, "web_app": {"url": settings.webapp_url + data[4:]}})
            if out:
                rows.append(out)
        return {"inline_keyboard": rows}

    def _send(self, chat_id: str, text: str, buttons: Buttons, edit_id: str) -> None:
        markup = self._markup(buttons)
        if edit_id:
            try:
                self._api("editMessageText", chat_id=chat_id, message_id=int(edit_id), text=text, reply_markup=markup)
                return
            except RuntimeError as e:
                if "not modified" in str(e):
                    return
        params = {"chat_id": chat_id, "text": text}
        if markup["inline_keyboard"]:
            params["reply_markup"] = markup
        self._api("sendMessage", **params)

    def _updates(self) -> list[Incoming]:
        out = []
        for upd in self._api("getUpdates", offset=self._offset, timeout=25, allowed_updates=["message", "callback_query"]):
            self._offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if cb:
                try:
                    self._api("answerCallbackQuery", callback_query_id=cb["id"])  # убрать «часики» на кнопке
                except Exception:
                    pass
                msg = cb.get("message") or {}
                if (msg.get("chat") or {}).get("type") == "private":
                    out.append(Incoming(chat_id=str(msg["chat"]["id"]), callback=cb.get("data") or "",
                                        message_id=str(msg.get("message_id") or "")))
                continue
            msg = upd.get("message") or {}
            if (msg.get("chat") or {}).get("type") != "private":
                continue
            text = (msg.get("text") or msg.get("caption") or "").strip()
            inc = Incoming(chat_id=str(msg["chat"]["id"]), text=text)
            if text.startswith("/start"):
                inc.start_code, inc.text = text.removeprefix("/start").strip(), "/start"
            voice = msg.get("voice") or msg.get("audio")
            if voice:
                inc.audio_url = f"tg:{voice['file_id']}"
            doc = msg.get("document") or msg.get("video")
            if msg.get("photo"):
                photo = msg["photo"][-1]  # последний размер - самый большой
                inc.file_url, inc.file_size = f"tg:{photo['file_id']}", photo.get("file_size") or 0
                inc.file_name = f"photo_{msg.get('message_id')}.jpg"
            elif doc:
                inc.file_url, inc.file_size = f"tg:{doc['file_id']}", doc.get("file_size") or 0
                inc.file_name = doc.get("file_name") or f"file_{msg.get('message_id')}"
            out.append(inc)
        return out


class Max(Channel):
    """MAX (platform-api). Токен передается в заголовке Authorization."""
    key, title, user_field = "max", "MAX", "max_user_id"

    def __init__(self) -> None:
        super().__init__()
        self.configured = bool(settings.max_token)
        self._marker = None
        self._base = settings.max_api_url
        self._http = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            # сертификат проверяется всегда; для домена с сертификатом Минцифры путь к корневому задается в MAX_CA_FILE
            self._http = httpx.Client(headers={"Authorization": settings.max_token}, timeout=40,
                                      verify=settings.max_ca_file or True)
        return self._http

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self._client().request(method, self._base + path, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"MAX API {r.status_code}: {msg[:160]}")
        return r.json()

    def link_for(self, code: str) -> str:
        u = self.state["username"]
        return f"https://max.ru/{u}" + (f"?start={code}" if code else "") if u else ""

    def _me(self) -> str:
        try:
            me = self._call("GET", "/me")
        except httpx.ConnectError as e:
            # новый домен требует корневой сертификат Минцифры; без него работаем через прежний домен
            if "CERTIFICATE_VERIFY_FAILED" not in str(e) or settings.max_ca_file or self._base == settings.max_api_fallback:
                raise
            log.warning("MAX: %s требует сертификат Минцифры (MAX_CA_FILE), используем %s", self._base, settings.max_api_fallback)
            self._base = settings.max_api_fallback
            me = self._call("GET", "/me")
        return me.get("username") or str(me.get("user_id"))

    def _send(self, chat_id: str, text: str, buttons: Buttons, edit_id: str) -> None:
        rows = []
        for row in buttons:
            out = []
            for label, data in row:
                if not data.startswith("web:"):
                    out.append({"type": "callback", "text": label, "payload": data})
                elif settings.webapp_url:
                    out.append({"type": "link", "text": label, "url": settings.webapp_url + data[4:]})
            if out:
                rows.append(out)
        if rows:
            try:
                self._call("POST", "/messages", params={"user_id": chat_id},
                           json={"text": text, "attachments": [{"type": "inline_keyboard", "payload": {"buttons": rows}}]})
                return
            except RuntimeError as e:
                log.warning("MAX: кнопки не приняты, отправляем текст без них: %s", e)
        self._call("POST", "/messages", params={"user_id": chat_id}, json={"text": text})

    def _updates(self) -> list[Incoming]:
        params = {"timeout": 25, "limit": 100, "types": "message_created,bot_started,message_callback"}
        if self._marker is not None:
            params["marker"] = self._marker
        data = self._call("GET", "/updates", params=params)
        if data.get("marker") is not None:
            self._marker = data["marker"]
        out = []
        for upd in data.get("updates") or []:
            kind = upd.get("update_type")
            if kind == "bot_started":
                uid = (upd.get("user") or {}).get("user_id")
                if uid:
                    out.append(Incoming(chat_id=str(uid), text="/start", start_code=(upd.get("payload") or "").strip()))
                continue
            if kind == "message_callback":
                cb = upd.get("callback") or {}
                uid = (cb.get("user") or {}).get("user_id")
                if uid:
                    try:
                        self._call("POST", "/answers", params={"callback_id": cb.get("callback_id")}, json={"notification": "Принято"})
                    except Exception:
                        pass
                    out.append(Incoming(chat_id=str(uid), callback=cb.get("payload") or ""))
                continue
            if kind != "message_created":
                continue
            msg = upd.get("message") or {}
            if (msg.get("recipient") or {}).get("chat_type") != "dialog":
                continue
            sender = msg.get("sender") or {}
            if sender.get("is_bot") or not sender.get("user_id"):
                continue
            body = msg.get("body") or {}
            inc = Incoming(chat_id=str(sender["user_id"]), text=(body.get("text") or "").strip())
            for att in body.get("attachments") or []:
                payload = att.get("payload") or {}
                if att.get("type") == "audio" and not inc.audio_url:
                    inc.audio_url = payload.get("url") or ""
                    inc.transcript = (att.get("transcription") or payload.get("transcription") or "").strip()
                elif att.get("type") in ("image", "file", "video") and not inc.file_url and payload.get("url"):
                    inc.file_url, inc.file_size = payload["url"], att.get("size") or 0
                    inc.file_name = att.get("filename") or {"image": "photo.jpg", "video": "video.mp4"}.get(att["type"], "file")
            out.append(inc)
        return out


CHANNELS: dict[str, Channel] = {}


def channels() -> dict[str, Channel]:
    if not CHANNELS:
        for ch in (Telegram(), Max()):
            CHANNELS[ch.key] = ch
    return CHANNELS


def start() -> None:
    for ch in channels().values():
        ch.start()


def linked(user: User) -> bool:
    return any(getattr(user, ch.user_field, None) for ch in channels().values())


# ---------- уведомления ----------

# при настройке «только требующие действия» остальное остается в колокольчике на сайте
ACTIONABLE = {"assigned", "question", "done", "due", "overdue"}


def notify_buttons(user: User, appeal: Appeal | None, kind: str) -> Buttons:
    """Что можно сделать прямо из уведомления."""
    from . import workflow
    if not appeal or not workflow.can_view(user, appeal):
        return []
    aid, rows = appeal.id, []
    if kind == "question" and appeal.author_id == user.id:
        rows.append([("Ответить", f"ans:{aid}")])
    elif kind == "done" and appeal.author_id == user.id:
        rows.append([("Решено, спасибо", f"ok:{aid}"), ("Не решено", f"bad:{aid}")])
    elif kind in ("assigned", "due", "overdue", "answer") and workflow.can_handle(user, appeal) and appeal.status not in FINAL:
        row = [("Взять в работу", f"st:{aid}:in_progress")] if "in_progress" in workflow.TRANSITIONS[appeal.status] else []
        if appeal.author_id:
            row.append(("Уточнить", f"cl:{aid}"))
        if row:
            rows.append(row)
    rows.append([("Открыть", f"a:{aid}"), ("В приложении", f"web:/appeals/{aid}")])
    return rows


def push_user(user: User | None, text: str, appeal: Appeal | None = None, kind: str = "") -> None:
    """Уведомить сотрудника во всех мессенджерах, которые он привязал."""
    if not user or not linked(user):
        return
    if (user.bot_prefs or {}).get("quiet") and kind and kind not in ACTIONABLE:
        return
    buttons = notify_buttons(user, appeal, kind)
    for ch in channels().values():
        chat_id = getattr(user, ch.user_field, None)
        if chat_id:
            ch.push(chat_id, text, buttons)


def local(dt: datetime) -> datetime:
    return dt + timedelta(hours=settings.tz_hours)


def _digest_text(db: Session, u: User, monday: bool) -> str:
    from . import workflow
    t = now()
    rows = db.scalars(select(Appeal).where(Appeal.assignee_id == u.id, Appeal.status.notin_(FINAL)).order_by(Appeal.due_at)).all()
    lines = []
    if rows:
        over = [a for a in rows if workflow.overdue(a, t)]
        soon = [a for a in rows if a.due_at and t <= a.due_at < t + timedelta(hours=24)]
        fresh = [a for a in rows if a.status == "new"]
        lines.append(f"Сводка на сегодня. Открытых обращений на вас: {len(rows)}")
        for label, items in (("просрочено", over), ("срок истекает в ближайшие сутки", soon), ("новых, еще не принятых", fresh),
                             ("ждут ответа автора", [a for a in rows if a.status == "clarify"])):
            if items:
                lines.append(f"- {label}: {len(items)}")
        first = list(dict.fromkeys(over + soon + fresh))[:5]
        if first:
            lines += ["", "С чего начать:"] + [f"{a.number}: {cut(a.title, 70)}" for a in first]
    if monday and u.role == "director":
        from .api import digest_rules, visible
        lines += ["", "Картина по компании на начало недели:", digest_rules(visible(db, u)).replace("**", "")]
    return "\n".join(lines).strip()


def daily(db: Session) -> int:
    """Утренняя сводка ответственным в рабочие дни; директору по понедельникам - еще и картина по компании."""
    loc = local(now())
    if loc.weekday() >= 5 or not settings.digest_hour <= loc.hour < settings.digest_hour + 3:
        return 0
    today, sent = loc.date().isoformat(), 0
    for u in db.scalars(select(User).where(User.active, User.role != "employee")):
        prefs = dict(u.bot_prefs or {})
        if not linked(u) or prefs.get("digest_on") == today:
            continue
        prefs["digest_on"] = today
        u.bot_prefs = prefs
        text = "" if prefs.get("digest") is False else _digest_text(db, u, loc.weekday() == 0)
        if not text:
            continue
        sent += 1
        for ch in channels().values():
            chat_id = getattr(u, ch.user_field, None)
            if chat_id:
                ch.push(chat_id, text, [[("Входящие", "inbox")], [("Открыть приложение", "web:/")]])
    return sent


# ---------- файлы и голос ----------

def _voice_to_text(ch: Channel, inc: Incoming) -> str:
    if inc.transcript:
        return inc.transcript
    from . import meetings
    data = httpx.get(ch.resolve(inc.audio_url), headers=inc.audio_headers, timeout=60, follow_redirects=True).content
    tmp = UPLOAD_DIR / f"voice_{uuid.uuid4().hex}.oga"
    tmp.write_bytes(data)
    try:
        lines = meetings.transcribe_media(str(tmp)).splitlines()
        return " ".join(ln.split("] ", 1)[-1] for ln in lines).strip()
    finally:
        tmp.unlink(missing_ok=True)


def _download(ch: Channel, inc: Incoming) -> dict:
    """Сохранить присланный файл в хранилище вложений. Возвращает описание для Attachment."""
    name = Path(inc.file_name or "file").name[:255]
    stored = f"{uuid.uuid4().hex}{Path(name).suffix.lower()[:10]}"
    path, size = UPLOAD_DIR / stored, 0
    try:
        with httpx.stream("GET", ch.resolve(inc.file_url), headers=inc.audio_headers, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with path.open("wb") as out:
                for chunk in r.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError("Файл больше 20 МБ")
                    out.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"filename": name, "stored": stored, "size": size}


def _drop_files(files: list[dict]) -> None:
    for f in files:
        (UPLOAD_DIR / Path(f.get("stored") or "-").name).unlink(missing_ok=True)


def cut(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def status_label(frm: str, to: str) -> str:
    """Подпись кнопки смены статуса - те же слова, что в веб-интерфейсе."""
    if to == "review":
        return "Принять" if frm == "new" else "Вернуть на рассмотрение" if frm == "clarify" else "Пересмотреть"
    if to == "in_progress":
        return "Вернуть в работу" if frm == "done" else "Взять в работу"
    return {"done": "Готово", "rejected": "Отклонить", "closed": "Закрыть"}.get(to, to)


# ---------- диалог ----------

def handle(ch: Channel, inc: Incoming) -> None:
    """Диалог с сотрудником. Одинаков для всех мессенджеров."""
    field_ = getattr(User, ch.user_field)
    with session_scope() as db:
        user = db.scalar(select(User).where(field_ == inc.chat_id, User.active))
        if user:
            Dialog(ch, inc, db, user).run()
            return
        code = (inc.start_code or inc.text).strip().upper()
        cand = db.scalar(select(User).where(User.tg_link_code == code, User.active)) if code and code != "/START" else None
        if cand:
            setattr(cand, ch.user_field, inc.chat_id)
            cand.tg_link_code = None
            ch.push(inc.chat_id, f"Здравствуйте, {cand.name}! {ch.title} привязан.\n\n{HELP}", menu_buttons(cand))
        else:
            ch.push(inc.chat_id, f"Этот бот - для сотрудников. Откройте в системе «Профиль», нажмите «Подключить» у {ch.title} "
                                 "и пришлите сюда код.")


def menu_buttons(user: User) -> Buttons:
    rows = [[("Новое обращение", "new"), ("Мои обращения", "my")]]
    if user.role != "employee":
        rows.append([("Входящие", "inbox")])
    rows.append([("Настройки", "settings"), ("Открыть приложение", "web:/")])
    return rows


class Dialog:
    """Один шаг разговора: входящее сообщение или нажатая кнопка от привязанного сотрудника."""

    def __init__(self, ch: Channel, inc: Incoming, db: Session, user: User) -> None:
        self.ch, self.inc, self.db, self.user = ch, inc, db, user

    # --- служебное

    def say(self, text: str, buttons: Buttons | None = None, edit: bool = False) -> None:
        self.ch.push(self.inc.chat_id, text, buttons, self.inc.message_id if edit and self.inc.callback else "")

    @property
    def state(self) -> dict:
        return dict(self.user.bot_state or {})

    @state.setter
    def state(self, value: dict | None) -> None:
        self.user.bot_state = value or None

    def appeal(self, raw) -> Appeal | None:
        from . import workflow
        a = self.db.get(Appeal, int(raw)) if str(raw).isdigit() else None
        return a if a and workflow.can_view(self.user, a) else None

    # --- разбор входящего

    def run(self) -> None:
        inc, text = self.inc, self.inc.text
        if inc.callback:
            self.on_button(inc.callback)
            return
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            {"/new": self.compose, "/my": self.my, "/inbox": self.inbox, "/settings": self.settings,
             "/cancel": self.cancel, "/stop": self.stop}.get(cmd, self.menu)()
            return
        if inc.file_url:
            self.on_file()
            return
        if inc.audio_url and not text:
            self.say("Слушаю запись...")
            try:
                text = _voice_to_text(self.ch, inc)
            except Exception as e:
                log.exception("%s: голосовое не распознано", self.ch.title)
                self.say(f"Не получилось распознать запись ({e}). Напишите текстом, пожалуйста.")
                return
        if not text:
            self.say("Пока я понимаю текст, голосовые, фото и файлы.")
            return
        self.on_text(text)

    def on_text(self, text: str) -> None:
        from . import workflow
        st = self.state
        mode = st.get("mode")
        if mode == "draft" and st.get("title") and not st.get("await"):
            word = text.strip().lower().rstrip(".!")
            if word in ("отправить", "отправляй", "да"):
                self.draft_send()
                return
            if word in ("отмена", "отменить", "нет"):
                self.cancel()
                return
        if mode in ("answer", "comment", "clarify", "status", "reopen"):
            self.on_reply(st, text)
            return
        if mode == "draft":
            # пока черновик не отправлен, следующее сообщение дополняет его; после «Изменить текст» - заменяет
            old = "" if st.get("await") else st.get("text") or ""
            self.draft_make(f"{old}\n{text}".strip(), st.get("files") or [], keep=st)
            return
        if mode != "compose" and self.user.tg_pending_appeal_id:
            # ответственный задал вопрос - следующий текст считаем ответом на него
            a = self.db.get(Appeal, self.user.tg_pending_appeal_id)
            if a and a.author_id == self.user.id and a.status == "clarify":
                workflow.answer(self.db, a, self.user, text)
                self.say(f"Ответ по {a.number} передан ответственному.", [[("Открыть", f"a:{a.id}")]])
                self.next_question(a)
                return
            self.user.tg_pending_appeal_id = None
        self.draft_make(text, [])

    # --- меню и списки

    def menu(self) -> None:
        self.say(HELP, menu_buttons(self.user))

    def compose(self) -> None:
        st = self.state
        if st.get("mode") == "draft" and st.get("title"):
            self.draft_show()
            return
        if st.get("mode") != "draft":
            self.state = {"mode": "compose"}
        self.say(ASK_TEXT, [[("Отмена", "x")]])

    def cancel(self) -> None:
        st = self.state
        if st.get("mode") == "draft":
            _drop_files(st.get("files") or [])
        self.state = None
        self.say("Черновик удален." if st.get("mode") == "draft" else "Хорошо, отменил.", [[("Новое обращение", "new"), ("Мои обращения", "my")]],
                 edit=st.get("mode") == "draft")

    def my(self) -> None:
        rows = self.db.scalars(select(Appeal).where(Appeal.author_id == self.user.id).order_by(Appeal.id.desc()).limit(8)).all()
        if not rows:
            self.say("У вас пока нет обращений.", [[("Новое обращение", "new")]])
            return
        text = "Ваши обращения:\n" + "\n".join(f"{a.number} - {STATUSES[a.status]}: {cut(a.title, 60)}" for a in rows)
        self.say(text, [[(f"{a.number} · {cut(a.title, 30)}", f"a:{a.id}")] for a in rows] + [[("Все в приложении", "web:/home")]])

    def inbox(self) -> None:
        from . import workflow
        if self.user.role == "employee":
            self.say("Входящие есть у тех, кто рассматривает обращения.", menu_buttons(self.user))
            return
        rows = self.db.scalars(select(Appeal).where(Appeal.assignee_id == self.user.id, Appeal.status.notin_(FINAL))).all()
        if not rows:
            self.say("Все разобрано - открытых обращений на вас нет.")
            return
        rows.sort(key=lambda a: (not workflow.overdue(a), a.status != "new", a.due_at or now()))
        mark = lambda a: "просрочено" if workflow.overdue(a) else STATUSES[a.status].lower()  # noqa: E731
        shown = rows[:8]
        text = f"На вас открытых обращений: {len(rows)}\n" + "\n".join(f"{a.number} - {mark(a)}: {cut(a.title, 60)}" for a in shown)
        self.say(text, [[(f"{a.number} · {cut(a.title, 30)}", f"a:{a.id}")] for a in shown] + [[("Все в приложении", "web:/inbox")]])

    def settings(self, edit: bool = False) -> None:
        prefs = self.user.bot_prefs or {}
        rows = [[("Уведомления: только требующие действия" if prefs.get("quiet") else "Уведомления: все", "set:quiet")]]
        if self.user.role != "employee":
            rows.append([("Утренняя сводка: выкл" if prefs.get("digest") is False else "Утренняя сводка: вкл", "set:digest")])
        rows += [[("Отвязать этот чат", "stop")], [("В меню", "menu")]]
        self.say("Настройки. Нажмите, чтобы переключить.", rows, edit=edit)

    def stop(self) -> None:
        self.say("Отвязать этот чат от учетной записи? Уведомления перестанут приходить, обращения останутся в системе.",
                 [[("Да, отвязать", "stop:yes"), ("Нет", "menu")]])

    # --- кнопки

    def on_button(self, data: str) -> None:
        from . import workflow
        op, _, arg = data.partition(":")
        if not arg and op in ("menu", "new", "my", "inbox", "settings", "x", "stop"):
            {"menu": self.menu, "new": self.compose, "my": self.my, "inbox": self.inbox, "settings": self.settings,
             "x": self.cancel, "stop": self.stop}[op]()
            return
        if op == "d":
            self.on_draft(arg)
            return
        if op == "set" and arg in ("quiet", "digest"):
            prefs = dict(self.user.bot_prefs or {})
            if arg == "quiet":
                prefs["quiet"] = not prefs.get("quiet")
            else:
                prefs["digest"] = prefs.get("digest") is False
            self.user.bot_prefs = prefs
            self.settings(edit=True)
            return
        if data == "stop:yes":
            setattr(self.user, self.ch.user_field, None)
            self.state = None
            self.say("Чат отвязан. Подключить снова можно в системе: «Профиль» - «Мессенджеры».", edit=True)
            return
        aid, _, extra = arg.partition(":")
        if op == "sup":  # присоединиться можно и к обращению, полный текст которого сотруднику не виден
            self.support(self.db.get(Appeal, int(aid)) if aid.isdigit() else None)
            return
        a = self.appeal(aid)
        if not a:
            self.say("Обращение не найдено или у вас больше нет к нему доступа.")
            return
        mine, handles = a.author_id == self.user.id, workflow.can_handle(self.user, a)
        cancel: Buttons = [[("Отмена", "x")]]
        try:
            if op == "a":
                self.show(a)
            elif op == "fin":
                self.state = None
                self.show(a)
            elif op == "ans" and mine:
                q = workflow.open_question(a)
                if not q:
                    self.say(f"По {a.number} сейчас нет вопросов без ответа.")
                    return
                self.ask(a, "answer", f"{a.number}: {q.text}\n\nНапишите ответ одним сообщением - текстом или голосом.")
            elif op == "cm" and (mine or handles):
                self.ask(a, "comment", f"Напишите комментарий к {a.number} - " + ("автор его увидит." if handles and not mine else "ответственный его увидит."))
            elif op == "att" and (mine or handles):
                self.ask(a, "attach", f"Пришлите фото или файл - прикреплю к {a.number}.")
            elif op == "ok" and mine:
                workflow.rate(self.db, a, self.user, True)
                self.say(f"Спасибо! Отметил, что вопрос по {a.number} решен.", [[("Открыть", f"a:{a.id}")]])
            elif op == "bad" and mine:
                if a.status != "done" or a.rating:
                    raise ValueError("Результат по этому обращению уже оценен")
                self.ask(a, "reopen", f"Что осталось нерешенным по {a.number}? Напишите - верну обращение в работу.")
            elif op == "cl" and handles:
                if a.status in FINAL or not a.author_id:
                    raise ValueError("Уточнить можно только у автора открытого обращения")
                self.ask(a, "clarify", f"Что уточнить у автора по {a.number}?")
            elif op == "st" and handles and extra in STATUSES:
                if extra in workflow.NEEDS_TEXT:
                    if extra not in workflow.TRANSITIONS[a.status]:
                        raise ValueError(f"Из статуса «{STATUSES[a.status]}» нельзя перейти в «{STATUSES[extra]}»")
                    self.state = {"mode": "status", "appeal_id": a.id, "status": extra}
                    self.say(f"{a.number}: {workflow.NEEDS_TEXT[extra].lower()}.", cancel)
                else:
                    workflow.set_status(self.db, a, extra, self.user)
                    self.show(a, edit=True)
            else:
                self.say("Это действие вам недоступно.")
        except ValueError as e:
            self.say(str(e))

    def ask(self, a: Appeal, mode: str, prompt: str) -> None:
        """Ждать от собеседника текст (или файл) по конкретному обращению."""
        st = self.state
        if st.get("mode") == "draft":  # незавершенный черновик уступает место: файлы не оставляем висеть
            _drop_files(st.get("files") or [])
        self.state = {"mode": mode, "appeal_id": a.id}
        self.say(prompt, [[("Готово", f"fin:{a.id}")] if mode == "attach" else [("Отмена", "x")]])

    def on_reply(self, st: dict, text: str) -> None:
        """Текст, которого бот ждал: ответ на вопрос, комментарий, вопрос автору, итог или причина возврата."""
        from . import workflow
        a, mode = self.appeal(st.get("appeal_id")), st["mode"]
        self.state = None
        if not a:
            self.say("Обращение не найдено или у вас больше нет к нему доступа.")
            return
        mine, handles = a.author_id == self.user.id, workflow.can_handle(self.user, a)
        try:
            if mode == "answer":
                if not mine or not workflow.open_question(a):
                    raise ValueError(f"По {a.number} сейчас нет вопросов без ответа.")
                workflow.answer(self.db, a, self.user, text)
                done = f"Ответ по {a.number} передан ответственному."
            elif mode == "comment":
                if not (mine or handles):
                    raise ValueError("Комментировать могут автор и те, кто рассматривает обращение.")
                workflow.comment(self.db, a, self.user, text)
                done = f"Комментарий добавлен к {a.number}."
            elif mode == "clarify":
                if not handles or a.status in FINAL or not a.author_id:
                    raise ValueError("Уточнить можно только у автора открытого обращения.")
                workflow.ask(self.db, a, self.user, text)
                done = f"Вопрос отправлен автору. {a.number} ждет уточнения."
            elif mode == "status":
                if not handles:
                    raise ValueError("Обращение рассматривает другой сотрудник.")
                workflow.set_status(self.db, a, st.get("status", ""), self.user, text)
                done = f"{a.number}: статус «{STATUSES[a.status]}». Автор получил уведомление."
            else:
                if not mine:
                    raise ValueError("Оценить результат может только автор.")
                workflow.rate(self.db, a, self.user, False, text)
                done = f"Вернул {a.number} в работу и передал ответственному ваш комментарий."
        except ValueError as e:
            self.say(str(e))
            return
        self.say(done, [[("Открыть", f"a:{a.id}")]])
        if mode == "answer":
            self.next_question(a)

    def next_question(self, answered: Appeal) -> None:
        """Если автора ждут и по другим обращениям - напомнить, чтобы второй вопрос не потерялся."""
        from . import workflow
        rows = self.db.scalars(select(Appeal).where(Appeal.author_id == self.user.id, Appeal.status == "clarify",
                                                    Appeal.id != answered.id).order_by(Appeal.id)).all()
        for a in rows:
            q = workflow.open_question(a)
            if q:
                self.say(f"Еще ждет вашего ответа {a.number}: {cut(q.text, 300)}", [[("Ответить", f"ans:{a.id}")]])
                return

    def support(self, a: Appeal | None) -> None:
        from . import workflow
        if not a:
            self.say("Обращение не найдено.")
            return
        try:
            workflow.support(self.db, a, self.user)
        except ValueError as e:
            self.say(str(e))
            return
        st = self.state
        if st.get("mode") == "draft":
            _drop_files(st.get("files") or [])
            self.state = None
        self.say(f"Присоединил вас к {a.number} «{cut(a.title, 80)}». Ответственный видит, что это не единичный случай "
                 f"(всего сообщили: {len(a.supports) + 1}, считая автора). О решении напишу сюда.", edit=st.get("mode") == "draft")

    # --- карточка обращения

    def show(self, a: Appeal, edit: bool = False) -> None:
        from . import workflow
        u = self.user
        mine, handles = a.author_id == u.id, workflow.can_handle(u, a)
        hidden = a.anonymous and not mine

        def who(e: Event) -> str:
            if not e.actor:
                return "ИИ-помощник" if e.data.get("by_ai") else "Система"
            return "Автор" if hidden and e.actor_id == a.author_id else e.actor.name

        lines = [f"{a.number} · {TYPES[a.type]} · {STATUSES[a.status]}", a.title, "",
                 f"Категория: {a.category.name if a.category else 'нет'}",
                 f"Ответственный: {a.assignee.name if a.assignee else 'не назначен'}"]
        if not mine:
            lines.append("Автор: " + ("анонимно" if hidden else a.author.name if a.author else a.author_label or "не указан"))
        if a.status not in FINAL and a.due_at:
            lines.append(f"Срок: {local(a.due_at):%d.%m.%Y}" + (" - просрочено" if workflow.overdue(a) else ""))
        if a.supports:
            lines.append(f"Присоединились коллеги: {len(a.supports)}")
        if a.attachments:
            lines.append(f"Вложений: {len(a.attachments)}")
        lines += ["", cut(a.text, 700)]
        if a.resolution:
            lines += ["", ("Причина отказа: " if a.status == "rejected" else "Итог: ") + cut(a.resolution, 500)]
        q = workflow.open_question(a)
        if q:
            lines += ["", f"Вопрос автору: {cut(q.text, 300)}"]
        recent = [e for e in a.events if e.kind in ("comment", "answer", "rated") and e.text and (handles or not e.internal)][-3:]
        if recent:
            lines += ["", "Последнее в обсуждении:"] + [f"- {who(e)}: {cut(e.text, 200)}" for e in recent]

        rows: Buttons = []
        if mine and q:
            rows.append([("Ответить на вопрос", f"ans:{a.id}")])
        if mine and a.status == "done" and not a.rating:
            rows.append([("Решено, спасибо", f"ok:{a.id}"), ("Не решено", f"bad:{a.id}")])
        if handles:
            allowed = workflow.TRANSITIONS[a.status]
            order = ("review", "in_progress", "done") if a.status != "new" else ("in_progress",)
            row = [(status_label(a.status, s), f"st:{a.id}:{s}") for s in order if s in allowed]
            if a.status not in FINAL and a.author_id:
                row.append(("Уточнить", f"cl:{a.id}"))
            rows += [row[:2], row[2:]]
            if "rejected" in allowed:
                rows.append([("Отклонить", f"st:{a.id}:rejected")])
        if mine or handles:
            rows.append([("Комментарий", f"cm:{a.id}"), ("Прикрепить файл", f"att:{a.id}")])
        elif a.status not in FINAL and not any(s.user_id == u.id for s in a.supports):
            rows.append([("У меня то же", f"sup:{a.id}")])
        rows.append([("Открыть в приложении", f"web:/appeals/{a.id}")])
        self.say("\n".join(lines), [r for r in rows if r], edit=edit)

    # --- черновик нового обращения

    def draft_make(self, text: str, files: list[dict], keep: dict | None = None) -> None:
        from . import ai, workflow
        if len(text.strip()) < MIN_TEXT:
            self.say(SHORT)
            return
        t, _ = ai.triage(text, workflow.categories_for_ai(self.db))
        cat = self.db.scalar(select(Category).where(Category.name == t.category))
        d = {"mode": "draft", "text": text.strip(), "title": t.title, "type": t.type, "category_id": cat.id if cat else None,
             "priority": t.priority, "summary": t.summary, "question": t.question, "anonymous": False, "files": files, "manual": []}
        if keep:  # то, что человек выбрал сам, повторный разбор не перезаписывает
            d["anonymous"], d["manual"] = bool(keep.get("anonymous")), list(keep.get("manual") or [])
            for k in d["manual"]:
                d[k] = keep.get(k)
        self.state = d
        self.draft_show()

    def similar(self, d: dict) -> list[Appeal]:
        from . import workflow
        if d["type"] == "praise":
            return []
        rows = [a for a, _ in workflow.similar(self.db, d["text"], threshold=0.25)]
        return [a for a in rows if a.status not in FINAL and a.author_id != self.user.id
                and not any(s.user_id == self.user.id for s in a.supports)][:2]

    def draft_show(self, edit: bool = False) -> None:
        from . import workflow
        d = self.state
        cat = self.db.get(Category, d["category_id"]) if d.get("category_id") else None
        to, _ = workflow.route(self.db, cat)
        lines = ["Черновик обращения", "", f"«{d['title']}»", f"{TYPES[d['type']]} · {cat.name if cat else 'без категории'}",
                 f"Рассмотрит: {to.name if to else 'администратор'}"]
        if d.get("anonymous"):
            lines.append("Анонимно: ваше имя ответственный не увидит")
        if d.get("files"):
            lines.append(f"Вложений: {len(d['files'])}")
        lines += ["", cut(d["text"], 900)]
        sim = self.similar(d)
        if sim:
            lines += ["", "Похожее уже есть - можно присоединиться вместо нового:"]
            lines += [f"{a.number} «{cut(a.title, 70)}» - {STATUSES[a.status].lower()}" for a in sim]
        lines += ["", "Все верно? Можно дописать сообщением или прислать фото - добавлю в черновик."]
        rows: Buttons = [[("Отправить", "d:send")], [("Тип", "d:type"), ("Категория", "d:cat")],
                         [("Анонимно: да" if d.get("anonymous") else "Анонимно: нет", "d:anon"), ("Изменить текст", "d:text")]]
        rows += [[(f"У меня то же: {a.number}", f"sup:{a.id}")] for a in sim]
        rows.append([("Отмена", "d:cancel")])
        self.say("\n".join(lines), rows, edit=edit)

    def on_draft(self, arg: str) -> None:
        d = self.state
        if d.get("mode") != "draft":
            self.say("Этот черновик уже отправлен или отменен.", [[("Новое обращение", "new")]])
            return
        op, _, value = arg.partition(":")
        if op == "cancel":
            self.cancel()
            return
        if not d.get("title"):  # пришел только файл, описания еще нет
            self.say(ASK_TEXT, [[("Отмена", "d:cancel")]])
            return
        back: Buttons = [[("Назад к черновику", "d:back")]]
        if op == "send":
            self.draft_send()
        elif op == "type":
            self.say("Что это?", [[(label, f"d:t:{k}")] for k, label in TYPES.items()] + back, edit=True)
        elif op == "cat":
            cats = self.db.scalars(select(Category).where(Category.active).order_by(Category.name)).all()
            rows = [[(c.name, f"d:c:{c.id}") for c in cats[i:i + 2]] for i in range(0, len(cats), 2)]
            self.say("К какой области относится?", rows + back, edit=True)
        elif op == "text":
            d["await"] = "text"
            self.state = d
            self.say("Пришлите новый текст - заменю им текущий.", [[("Оставить как есть", "d:back")]], edit=True)
        else:
            if op == "t" and value in TYPES:
                d["type"], d["manual"] = value, list({*d.get("manual", []), "type"})
            elif op == "c" and value.isdigit() and self.db.get(Category, int(value)):
                d["category_id"], d["manual"] = int(value), list({*d.get("manual", []), "category_id"})
            elif op == "anon":
                d["anonymous"] = not d.get("anonymous")
            d.pop("await", None)
            self.state = d
            self.draft_show(edit=True)

    def draft_send(self) -> None:
        from . import ai, workflow
        d = self.state
        cat = self.db.get(Category, d["category_id"]) if d.get("category_id") else None
        # вопрос ИИ задаем сами после подтверждения регистрации, чтобы сообщения шли в понятном порядке
        triage = ai.Triage(type=d["type"], category=cat.name if cat else "", priority=d["priority"], title=d["title"],
                           summary=d.get("summary") or "", question="")
        a = workflow.register(self.db, text=d["text"], author=self.user, source=self.ch.key, type=d["type"],
                              category_id=cat.id if cat else None, priority=d["priority"], title=d["title"],
                              anonymous=bool(d.get("anonymous")), triage=triage)
        for f in d.get("files") or []:
            self.db.add(Attachment(appeal_id=a.id, filename=f["filename"], stored=f["stored"], size=f.get("size") or 0))
        self.state = None
        who = a.assignee.name if a.assignee else "администратор"
        self.say(f"Зарегистрировано: {a.number}\n«{a.title}»\n{TYPES[a.type]} · {a.category.name if a.category else 'без категории'}\n"
                 f"Ответственный: {who}\nСрок рассмотрения: до {local(a.due_at):%d.%m.%Y}\n\nО каждом изменении статуса напишу сюда.",
                 [[("Открыть", f"a:{a.id}"), ("В приложении", f"web:/appeals/{a.id}")], [("Мои обращения", "my")]], edit=True)
        if d.get("question"):
            workflow.ask_by_ai(self.db, a, d["question"], push=False)
            self.say(f"Чтобы {a.number} быстрее взяли в работу, уточните: {d['question']}", [[("Ответить", f"ans:{a.id}")]])

    # --- фото и файлы

    def on_file(self) -> None:
        from . import workflow
        inc, st = self.inc, self.state
        mode = st.get("mode")
        if inc.file_size > MAX_FILE:
            self.say("Файл больше 20 МБ - такой принять не могу. Пришлите ссылку на него в тексте.")
            return
        try:
            f = _download(self.ch, inc)
        except ValueError as e:
            self.say(f"{e} - такой принять не могу. Пришлите ссылку на него в тексте.")
            return
        except Exception:
            log.exception("%s: файл не скачан", self.ch.title)
            self.say("Не получилось получить файл. Попробуйте прислать его еще раз.")
            return

        if mode in ("attach", "answer", "comment", "clarify", "status", "reopen"):
            a = self.appeal(st.get("appeal_id"))
            if not a or not (a.author_id == self.user.id or workflow.can_handle(self.user, a)):
                _drop_files([f])
                self.state = None
                self.say("Обращение не найдено или у вас больше нет к нему доступа.")
                return
            self.db.add(Attachment(appeal_id=a.id, filename=f["filename"], stored=f["stored"], size=f["size"]))
            workflow.comment(self.db, a, self.user, f"Прикреплен файл: {f['filename']}")
            if mode == "attach":
                if inc.text:
                    workflow.comment(self.db, a, self.user, inc.text)
                self.say(f"Файл добавлен к {a.number}. Можно прислать еще.", [[("Готово", f"fin:{a.id}")]])
            elif inc.text:
                self.on_reply(st, inc.text)  # подпись к файлу - это и есть ожидаемый текст
            else:
                self.say(f"Файл добавлен к {a.number}. Жду текст.", [[("Отмена", "x")]])
            return

        files = (st.get("files") or []) + [f] if mode == "draft" else [f]
        text = f"{st.get('text') or ''}\n{inc.text}".strip() if mode == "draft" else inc.text
        if inc.text and len(text) >= 20:
            self.draft_make(text, files, keep=st if mode == "draft" else None)
        elif mode == "draft" and st.get("title"):
            st["files"] = files
            self.state = st
            self.say(f"Файл добавлен в черновик, вложений: {len(files)}.", [[("Отправить", "d:send"), ("Показать черновик", "d:back")]])
        else:
            self.state = {"mode": "draft", "text": text, "files": files}
            self.say("Файл получил. Теперь опишите, что случилось или что предлагаете - текстом или голосом.", [[("Отмена", "d:cancel")]])
