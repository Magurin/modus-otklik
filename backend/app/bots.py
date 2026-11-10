"""Боты в мессенджерах: Telegram и MAX. Сотрудник пишет или надиктовывает обращение, получает номер и уведомления.

Логика диалога одна на оба мессенджера (handle), различается только транспорт (Channel).
Привязка к учетной записи - по одноразовому коду из профиля: посторонний написать не может.
Оба бота работают через long polling, внешний адрес и вебхук не нужны.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from .config import UPLOAD_DIR, settings
from .db import session_scope
from .models import STATUSES, TYPES, Appeal, User

log = logging.getLogger("bots")

HELP = ("Напишите или надиктуйте голосовым, что мешает работе или что можно улучшить - я зарегистрирую обращение "
        "и передам ответственному.\n\n/my - мои обращения и их статусы")


@dataclass
class Incoming:
    """Входящее событие, одинаковое для всех мессенджеров."""
    chat_id: str
    text: str = ""
    start_code: str = ""            # код из ссылки запуска бота
    audio_url: str = ""             # голосовое: откуда скачать
    audio_headers: dict = field(default_factory=dict)
    transcript: str = ""            # если мессенджер сам расшифровал голосовое


class Channel:
    """Транспорт одного мессенджера. Наследник реализует _me, _send, _updates."""
    key = ""            # telegram | max
    title = ""
    user_field = ""     # поле User, где хранится id собеседника

    def __init__(self) -> None:
        self.state = {"enabled": False, "username": "", "error": "", "link": ""}
        self._out: queue.Queue[tuple[str, str]] = queue.Queue()
        self._started = False

    configured = False

    def push(self, chat_id: str, text: str) -> None:
        """Поставить сообщение в очередь (не блокирует запрос и не падает без сети)."""
        if self._started:
            self._out.put((chat_id, text))

    def link_for(self, code: str) -> str:
        return ""

    def start(self) -> None:
        if self._started or not self.configured:
            return
        self._started = True
        threading.Thread(target=self._sender, daemon=True, name=f"{self.key}-send").start()
        threading.Thread(target=self._poll, daemon=True, name=f"{self.key}-poll").start()

    def _sender(self) -> None:
        while True:
            chat_id, text = self._out.get()
            try:
                self._send(chat_id, text[:4000])
            except Exception as e:
                log.warning("%s: не удалось отправить сообщение: %s", self.title, e)
            time.sleep(0.5)  # MAX: не больше двух сообщений в секунду в один диалог

    def _poll(self) -> None:
        try:
            self.state["username"] = self._me()
            self.state.update(enabled=True, error="", link=self.link_for(""))
            log.info("%s-бот @%s запущен", self.title, self.state["username"])
        except Exception as e:
            self.state["error"] = str(e)[:200]
            log.error("%s-бот не запущен: %s", self.title, e)
            return
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
    def _send(self, chat_id: str, text: str) -> None: raise NotImplementedError
    def _updates(self) -> list[Incoming]: raise NotImplementedError


class Telegram(Channel):
    key, title, user_field = "telegram", "Telegram", "tg_chat_id"

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

    def _me(self) -> str:
        return self._api("getMe")["username"]

    def _send(self, chat_id: str, text: str) -> None:
        self._api("sendMessage", chat_id=chat_id, text=text)

    def _updates(self) -> list[Incoming]:
        out = []
        for upd in self._api("getUpdates", offset=self._offset, timeout=25, allowed_updates=["message"]):
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if (msg.get("chat") or {}).get("type") != "private":
                continue
            text = (msg.get("text") or msg.get("caption") or "").strip()
            inc = Incoming(chat_id=str(msg["chat"]["id"]), text=text)
            if text.startswith("/start"):
                inc.start_code, inc.text = text.removeprefix("/start").strip(), "/start"
            voice = msg.get("voice") or msg.get("audio")
            if voice:
                path = self._api("getFile", file_id=voice["file_id"])["file_path"]
                inc.audio_url = f"https://api.telegram.org/file/bot{settings.telegram_token}/{path}"
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

    def _send(self, chat_id: str, text: str) -> None:
        self._call("POST", "/messages", params={"user_id": chat_id}, json={"text": text})

    def _updates(self) -> list[Incoming]:
        params = {"timeout": 25, "limit": 100, "types": "message_created,bot_started"}
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
                if att.get("type") == "audio":
                    payload = att.get("payload") or {}
                    inc.audio_url = payload.get("url") or ""
                    inc.transcript = (att.get("transcription") or payload.get("transcription") or "").strip()
                    break
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


def push_user(user: User | None, text: str) -> None:
    """Уведомить сотрудника во всех мессенджерах, которые он привязал."""
    if not user:
        return
    for ch in channels().values():
        chat_id = getattr(user, ch.user_field, None)
        if chat_id:
            ch.push(chat_id, text)


def linked(user: User) -> bool:
    return any(getattr(user, ch.user_field, None) for ch in channels().values())


def _voice_to_text(inc: Incoming) -> str:
    if inc.transcript:
        return inc.transcript
    from . import meetings
    data = httpx.get(inc.audio_url, headers=inc.audio_headers, timeout=60, follow_redirects=True).content
    tmp = UPLOAD_DIR / f"voice_{abs(hash(inc.audio_url)) % 10**12}.oga"
    tmp.write_bytes(data)
    try:
        lines = meetings.transcribe_media(str(tmp)).splitlines()
        return " ".join(ln.split("] ", 1)[-1] for ln in lines).strip()
    finally:
        tmp.unlink(missing_ok=True)


def handle(ch: Channel, inc: Incoming) -> None:
    """Диалог с сотрудником. Одинаков для всех мессенджеров."""
    from . import workflow
    say = lambda t: ch.push(inc.chat_id, t)  # noqa: E731
    field_ = getattr(User, ch.user_field)
    text = inc.text

    with session_scope() as db:
        user = db.scalar(select(User).where(field_ == inc.chat_id, User.active))
        if not user:
            code = (inc.start_code or text).strip().upper()
            cand = db.scalar(select(User).where(User.tg_link_code == code, User.active)) if code and code != "/START" else None
            if cand:
                setattr(cand, ch.user_field, inc.chat_id)
                cand.tg_link_code = None
                say(f"Здравствуйте, {cand.name}! {ch.title} привязан.\n\n{HELP}")
            else:
                say(f"Этот бот - для сотрудников. Откройте в системе «Профиль», нажмите «Подключить» у {ch.title} "
                    "и пришлите сюда код.")
            return

        if text in ("/start", "/help"):
            say(HELP)
            return
        if text == "/my":
            rows = db.scalars(select(Appeal).where(Appeal.author_id == user.id).order_by(Appeal.id.desc()).limit(8)).all()
            say("\n".join(f"{a.number} - {STATUSES[a.status]}: {a.title}" for a in rows) or "У вас пока нет обращений.")
            return

        if inc.audio_url and not text:
            say("Слушаю запись...")
            try:
                text = _voice_to_text(inc)
            except Exception as e:
                log.exception("%s: голосовое не распознано", ch.title)
                say(f"Не получилось распознать запись ({e}). Напишите текстом, пожалуйста.")
                return
        if not text:
            say("Пока я понимаю только текст и голосовые сообщения.")
            return

        if user.tg_pending_appeal_id:
            a = db.get(Appeal, user.tg_pending_appeal_id)
            if a and a.status == "clarify":
                workflow.answer(db, a, user, text)
                say(f"Ответ по {a.number} передан ответственному.")
                return
            user.tg_pending_appeal_id = None

        if len(text) < 20:
            say("Опишите чуть подробнее: что происходит и чему это мешает.")
            return
        a = workflow.register(db, text=text, author=user, source=ch.key)
        who = a.assignee.name if a.assignee else "администратор"
        cat = a.category.name if a.category else "без категории"
        say(f"Зарегистрировано: {a.number}\n{TYPES[a.type]} · {cat}\nОтветственный: {who}\n\nО каждом изменении статуса напишу сюда.")
