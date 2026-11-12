from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import bots, mail, mcp, mocks, orgsync, workflow
from .api import router
from .config import BACKEND_DIR, request_base, settings
from sqlalchemy import select

from .db import session_scope
from .models import Meeting
from .seed import seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx пишет в журнал адрес каждого запроса, а в адресе Telegram API - токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")


def _deadline_loop() -> None:
    while True:
        try:
            with session_scope() as db:
                n = workflow.check_deadlines(db)
                if n:
                    log.info("Эскалировано просроченных обращений: %s", n)
                bots.daily(db)
            with session_scope() as db:
                orgsync.maybe_auto(db)
        except Exception:
            log.exception("Контроль сроков")
        time.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_if_empty()
    with session_scope() as db:  # совещания, обработку которых оборвал перезапуск: показываем как ошибку с кнопкой «Повторить»
        for m in db.scalars(select(Meeting).where(Meeting.status.in_(("queued", "transcribing", "extracting")))):
            m.status, m.error = "error", "Обработка прервана перезапуском сервера"
    if settings.background:
        bots.start()
        mail.start()
        threading.Thread(target=_deadline_loop, daemon=True, name="deadlines").start()
    yield


app = FastAPI(title="Модус.Отклик - обратная связь сотрудников", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5180", "http://localhost:5180"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def remember_base(request: Request, call_next):
    """Запомнить адрес, по которому пришел запрос: из него строятся ссылки, если APP_URL не задан.
    За прокси (vite, Railway) учитываем X-Forwarded-*, иначе ссылки вели бы на внутренний порт."""
    h = request.headers
    host = h.get("x-forwarded-host") or h.get("host") or request.url.netloc
    proto = h.get("x-forwarded-proto") or request.url.scheme
    token = request_base.set(f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}")
    try:
        return await call_next(request)
    finally:
        request_base.reset(token)


app.include_router(router)
app.include_router(mcp.router)  # общий список обращений как инструмент ИИ-агентов
if settings.mocks:  # эмуляторы 1С и Битрикс24: можно открыть в браузере или указать их адрес в ONEC_ODATA_URL / BITRIX_WEBHOOK_URL
    app.include_router(mocks.router)

# собранный фронтенд раздаем тем же сервером: для запуска достаточно одного процесса
DIST = BACKEND_DIR.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):  # неизвестный адрес API - ошибка JSON, а не страница фронтенда
            raise HTTPException(404, "Нет такого адреса API - возможно, сервер запущен со старой версией")
        f = DIST / path
        return FileResponse(f if path and f.is_file() and DIST in f.resolve().parents else DIST / "index.html")
