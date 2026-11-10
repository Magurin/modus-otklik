from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import bots, mail, workflow
from .api import router
from .config import BACKEND_DIR, settings
from sqlalchemy import select

from .db import session_scope
from .models import Meeting
from .seed import seed_if_empty

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")


def _deadline_loop() -> None:
    while True:
        try:
            with session_scope() as db:
                n = workflow.check_deadlines(db)
                if n:
                    log.info("Эскалировано просроченных обращений: %s", n)
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
app.include_router(router)

# собранный фронтенд раздаем тем же сервером: для запуска достаточно одного процесса
DIST = BACKEND_DIR.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() and DIST in f.resolve().parents else DIST / "index.html")
