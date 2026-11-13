"""Окружение тестов: чистая временная база, без сети и внешних систем.

Задается до импорта приложения: настройки читаются один раз, а backend/.env подставляет только то, чего нет в окружении,
поэтому пустые значения здесь перекрывают боевые ключи и адреса из .env.
"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="modus-test-")
os.environ.update(AI_MODE="rules", BACKGROUND="0", TELEGRAM_BOT_TOKEN="", IMAP_HOST="",
                  ONEC_ODATA_URL="", BITRIX_WEBHOOK_URL="", ORG_SYNC_SOURCE="", WEBAPP_URL="")
