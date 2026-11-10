"""Совещания: запись или текст -> расшифровка -> пункты (проблемы, предложения, решения, поручения).

Речь распознается локально (faster-whisper), аудио наружу не уходит. Где Whisper нет (Vercel) и задан ключ
OpenAI, браузер режет запись на куски и каждый распознается через API (transcribe_api).
Пункты - черновики: обращением пункт становится только после того, как человек его подтвердил.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx
from sqlalchemy import select

from . import ai
from .config import UPLOAD_DIR, settings
from .db import session_scope
from .models import Category, Meeting, MeetingItem
from .workflow import categories_for_ai

log = logging.getLogger("meetings")

TEXT_EXT = {".txt", ".md", ".vtt", ".srt"}
MEDIA_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".webm", ".avi"}
ALLOWED_EXT = TEXT_EXT | MEDIA_EXT | {".docx"}

_model = None
_model_lock = threading.Lock()   # модель одна на процесс
_run_lock = threading.Lock()     # распознаем по одной записи: иначе не хватит памяти


def _cuda_dlls() -> None:
    """Windows: библиотеки CUDA из pip-пакетов nvidia-* лежат в site-packages и сами не находятся."""
    import os
    import site
    for root in site.getsitepackages():
        for d in Path(root, "nvidia").glob("*/bin"):
            os.add_dll_directory(str(d))
            os.environ["PATH"] = f"{d}{os.pathsep}{os.environ.get('PATH', '')}"


def _load(device: str):
    from faster_whisper import WhisperModel
    if device == "cuda" and hasattr(__import__("os"), "add_dll_directory"):
        _cuda_dlls()
    compute = "float16" if device == "cuda" else "int8"
    return WhisperModel(settings.whisper_model, device=device, compute_type=compute)


def _decode(path: str):
    """Звук из любого аудио/видео -> моно 16 кГц. Через ffmpeg: он понимает все форматы и не зависит от версии PyAV."""
    import subprocess

    import imageio_ffmpeg
    import numpy as np
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("Не удалось прочитать звук из файла: " + p.stderr.decode("utf-8", "replace")[-200:].strip())
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _run(model, path: str) -> str:
    segments, _ = model.transcribe(_decode(path), language="ru", vad_filter=True, beam_size=5)
    return "\n".join(f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] {s.text.strip()}" for s in segments)


def transcribe_media(path: str) -> str:
    """Распознать речь. Сначала пробуем видеокарту, при любой ошибке - процессор."""
    global _model
    with _run_lock:
        with _model_lock:
            if _model is None:
                want = settings.whisper_device
                try:
                    _model = _load("cuda" if want in ("auto", "cuda") else "cpu")
                except Exception as e:
                    log.warning("Whisper на видеокарте не запустился (%s), используем процессор", e)
                    _model = _load("cpu")
        try:
            return _run(_model, path)
        except RuntimeError as e:
            # нет библиотек CUDA: ошибка всплывает только на первом распознавании
            log.warning("Whisper: %s - переключаемся на процессор", e)
            with _model_lock:
                _model = _load("cpu")
            return _run(_model, path)


MAX_API_PART = 4_400_000  # кусок WAV 16 кГц моно до 2 мин (~3,8 МБ); тело запроса на Vercel ограничено 4,5 МБ
SPEECH_PROMPT = ("Рабочее совещание в ГК «Модус Трейд»: поставка и внедрение систем безопасности - видеонаблюдение, "
                 "СКУД, ОПС, пожарная безопасность, периметр; продажи, закупки, проектирование, пусконаладка, сервис.")
SPEECH_KEYWORDS = ["Модус Трейд", "СКУД", "ОПС", "пусконаладка", "Битрикс24", "1С"]


class SpeechError(Exception):
    """Понятная пользователю причина, по которой кусок записи не распознан."""


def transcribe_api(audio: bytes, filename: str, prev: str = "") -> str:
    """Распознать один кусок записи через OpenAI-совместимый /audio/transcriptions.

    prev - конец расшифровки предыдущего куска: с ним модель не теряет нить на стыке.
    """
    model = settings.transcribe_model
    prompt = SPEECH_PROMPT + (f"\n{prev.strip()[-300:]}" if prev.strip() else "")
    data: dict = {"model": model, "response_format": "json", "prompt": prompt}
    if model.startswith("gpt-transcribe"):
        data |= {"languages[]": "ru", "keywords[]": SPEECH_KEYWORDS}
    else:  # gpt-4o-transcribe, whisper-1 и локальные серверы с OpenAI-совместимым API
        data["language"] = "ru"
    try:
        r = httpx.post(f"{settings.openai_base_url}/audio/transcriptions", data=data,
                       files={"file": (filename, audio, "audio/wav")},
                       headers={"Authorization": f"Bearer {settings.openai_key}"}, timeout=110.0)
    except httpx.HTTPError as e:
        log.warning("Распознавание через API: нет связи (%s)", e)
        raise SpeechError("Нет связи с сервисом распознавания речи, попробуйте еще раз") from e
    if r.status_code in (401, 403):
        log.error("Распознавание через API: ключ не принят (%s)", r.status_code)
        raise SpeechError("Сервис распознавания речи не принял ключ OpenAI")
    if r.status_code == 429:
        raise SpeechError("Превышен лимит запросов OpenAI или закончились средства на счете")
    if r.status_code >= 400:
        log.warning("Распознавание через API: ошибка %s: %s", r.status_code, r.text[:300])
        raise SpeechError(f"Сервис распознавания речи вернул ошибку {r.status_code}")
    try:
        return str(r.json().get("text") or "").strip()
    except ValueError as e:
        raise SpeechError("Сервис распознавания речи ответил не данными") from e


def read_text_file(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def process(meeting_id: int) -> None:
    """Фоновая обработка одного совещания. Ошибка не теряется - видна в карточке совещания."""
    try:
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            transcript, stored = m.transcript, m.stored
            if not transcript:
                m.status = "transcribing"
        if not transcript:
            path = UPLOAD_DIR / stored
            transcript = read_text_file(path) if path.suffix.lower() not in MEDIA_EXT else transcribe_media(str(path))
            if not transcript.strip():
                raise RuntimeError("В записи не найдено речи")
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            m.transcript = transcript
            m.status = "extracting"
            cats = categories_for_ai(db)
        result, engine = ai.extract_meeting(transcript, cats)
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            by_name = {c.name: c.id for c in db.scalars(select(Category))}
            for old in list(m.items):
                if old.state == "draft":
                    db.delete(old)
            for p in result.points:
                db.add(MeetingItem(meeting_id=m.id, kind=p.kind, title=p.title[:200], text=p.text, quote=p.quote,
                                   speaker=p.speaker[:120], category_id=by_name.get(p.category), priority=p.priority))
            m.summary = result.summary
            m.engine = engine
            m.status = "ready"
            m.error = ""
    except Exception as e:
        log.exception("Совещание %s: ошибка обработки", meeting_id)
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            if m:
                m.status = "error"
                m.error = str(e)[:500]


def start(meeting_id: int) -> None:
    threading.Thread(target=process, args=(meeting_id,), daemon=True, name=f"meeting-{meeting_id}").start()
