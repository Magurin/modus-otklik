"""Точка входа API на Vercel.

Бессерверная функция не хранит файлы между запусками, поэтому база живет в /tmp: при холодном старте
туда кладется снимок демо-данных (api/demo.db). Изменения сохраняются, пока функция «теплая»,
и сбрасываются к демо-состоянию при новом деплое. Для постоянного хранения задайте DB_URL (PostgreSQL).
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

data = Path(os.environ.get("DATA_DIR") or "/tmp/modus")
data.mkdir(parents=True, exist_ok=True)
snapshot = Path(__file__).resolve().parent / "demo.db"
if not (data / "feedback.db").exists() and snapshot.exists() and not os.environ.get("DB_URL"):
    shutil.copy(snapshot, data / "feedback.db")
os.environ.setdefault("DATA_DIR", str(data))

from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402

seed_if_empty()  # на случай, если платформа не вызывает событие запуска ASGI

__all__ = ["app"]
