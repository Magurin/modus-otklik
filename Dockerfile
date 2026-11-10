# Сервер целиком одним контейнером: API, Telegram-бот, фоновые задачи и собранный фронтенд.
# Для Railway и любого Docker-хостинга. Данные (база, вложения, ключ сессий, модель Whisper) - в томе /data.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf \
    WHISPER_MODEL=small \
    WHISPER_DEVICE=cpu
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app backend/app
COPY --from=web /web/dist frontend/dist

# Railway передает порт в PORT
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
