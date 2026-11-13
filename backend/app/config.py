"""Настройки: переменные окружения + backend/.env (значения из окружения важнее)."""
from __future__ import annotations

import os
import secrets
from contextvars import ContextVar
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    p = BACKEND_DIR / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Бессерверный стенд (Vercel): писать можно только в /tmp, фоновых потоков нет, распознавания речи нет
SERVERLESS = bool(_env("VERCEL"))

DATA_DIR = Path(_env("DATA_DIR") or ("/tmp/modus" if SERVERLESS else BACKEND_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def _secret(name: str) -> str:
    f = DATA_DIR / name
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


class Settings:
    db_url = _env("DB_URL") or f"sqlite:///{(DATA_DIR / 'feedback.db').as_posix()}"
    jwt_secret = _env("JWT_SECRET") or _secret("jwt.secret")
    demo_login = _env("DEMO_LOGIN", "1") == "1"  # быстрый вход под демо-ролями на экране логина
    app_url = _env("APP_URL", "http://127.0.0.1:5180")
    app_url_set = bool(_env("APP_URL"))  # не задан - ссылки строим по адресу, с которого пришел запрос

    # ИИ: auto - модель, если задан ключ (Claude, иначе OpenAI), без ключа - правила; rules - всегда правила (офлайн)
    ai_mode = _env("AI_MODE", "auto")
    anthropic_key = _env("ANTHROPIC_API_KEY")
    llm_model = _env("LLM_MODEL", "claude-opus-5-5")
    openai_key = _env("OPENAI_API_KEY")
    openai_model = _env("OPENAI_MODEL", "gpt-5.6-luna")
    openai_base_url = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    # Совещания: распознавание речи. local - Whisper на этом сервере; api - через OpenAI (/audio/transcriptions),
    # запись режется на куски в браузере; off - только текст. По умолчанию: local, на Vercel - api, если есть ключ
    speech_mode_env = _env("SPEECH_MODE").lower()
    whisper_model = _env("WHISPER_MODEL", "medium")
    whisper_device = _env("WHISPER_DEVICE", "auto")
    transcribe_model = _env("TRANSCRIBE_MODEL", "gpt-transcribe")

    # Каналы приема
    telegram_token = _env("TELEGRAM_BOT_TOKEN")
    # Telegram Mini App: публичный https-адрес этого приложения. Без него бот работает только диалогом
    webapp_url = (_env("WEBAPP_URL") or (app_url if app_url.startswith("https://") else "")).rstrip("/")
    tz_hours = int(_env("TZ_OFFSET_HOURS", "5"))     # часовой пояс компании относительно UTC: даты в боте и время сводки
    digest_hour = int(_env("DIGEST_HOUR", "9"))      # во сколько по местному времени бот присылает сводку ответственным
    imap_host = _env("IMAP_HOST")
    imap_user = _env("IMAP_USER")
    imap_password = _env("IMAP_PASSWORD")
    imap_folder = _env("IMAP_FOLDER", "INBOX")
    imap_to = _env("IMAP_TO")  # брать только письма на этот адрес: остальная почта ящика обращениями не становится

    # Оргструктура из внешних систем. Адрес не задан - работает встроенный эмулятор (тот же HTTP-клиент, подмена транспорта)
    onec_odata_url = _env("ONEC_ODATA_URL").rstrip("/")      # .../<база>/odata/standard.odata
    onec_user = _env("ONEC_USER")
    onec_password = _env("ONEC_PASSWORD")
    bitrix_webhook = _env("BITRIX_WEBHOOK_URL").rstrip("/")  # https://<портал>.bitrix24.ru/rest/<id>/<ключ>
    org_sync_source = _env("ORG_SYNC_SOURCE")                # 1c или b24 - синхронизировать по расписанию
    org_sync_hours = int(_env("ORG_SYNC_HOURS", "6"))
    mocks = _env("MOCKS", "1") == "1"                        # отдавать эмуляторы 1С и Битрикс24 по /mock/...

    # Google Календарь: OAuth-клиент типа «Веб-приложение» (console.cloud.google.com), адрес возврата -
    # <APP_URL>/api/calendar/google/callback. Без ключей остается ссылка «добавить в календарь» в карточке обращения
    google_client_id = _env("GOOGLE_CLIENT_ID")
    google_client_secret = _env("GOOGLE_CLIENT_SECRET")

    serverless = SERVERLESS
    background = _env("BACKGROUND", "0" if SERVERLESS else "1") == "1"  # фоновые задачи: бот, почта, контроль сроков
    first_response_hours = int(_env("FIRST_RESPONSE_HOURS", "48"))

    @property
    def llm_provider(self) -> str:
        """claude, openai или пустая строка (правила)."""
        if self.ai_mode == "rules":
            return ""
        return "claude" if self.anthropic_key else "openai" if self.openai_key else ""

    @property
    def speech_mode(self) -> str:
        """local, api или пустая строка (записи не распознаются). Звук в API уходит только при ключе OpenAI."""
        api_ok = bool(self.openai_key) and self.ai_mode != "rules"
        mode = self.speech_mode_env or ("api" if SERVERLESS else "local")
        if mode == "api":
            return "api" if api_ok else ""
        return "local" if mode == "local" and not SERVERLESS else ""

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_provider)

    @property
    def llm_label(self) -> str:
        return {"claude": self.llm_model, "openai": self.openai_model}.get(self.llm_provider, "rules")


settings = Settings()

# Адрес, по которому пришел текущий запрос (заполняет main.py). Нужен, когда APP_URL не задан:
# локально фронтенд может работать на любом порту, и ссылки в ответах должны вести туда же
request_base: ContextVar[str] = ContextVar("request_base", default="")


def public_url() -> str:
    """Публичный адрес приложения для ссылок: APP_URL, иначе адрес текущего запроса, иначе значение по умолчанию."""
    if settings.app_url_set:
        return settings.app_url.rstrip("/")
    return (request_base.get() or settings.app_url).rstrip("/")
