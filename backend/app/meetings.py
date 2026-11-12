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
    import numpy as np
    return np.frombuffer(_pcm(path), dtype=np.int16).astype(np.float32) / 32768.0


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


API_PART_SECONDS = 120  # кусок для распознавания через API: 2 мин WAV 16 кГц моно = 3,84 МБ, в пределах MAX_API_PART


def _wav(pcm: bytes, rate: int = 16000) -> bytes:
    """Заголовок WAV для сырого звука s16le моно."""
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _pcm(path: str) -> bytes:
    """Звук из любого аудио/видео -> сырой s16le моно 16 кГц."""
    import subprocess

    import imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("Не удалось прочитать звук из файла: " + p.stderr.decode("utf-8", "replace")[-200:].strip())
    return p.stdout


def transcribe_api_file(path: str) -> str:
    """Распознать запись целиком через API на сервере: режем на куски по 2 минуты, как это делает браузер.
    Нужно, когда запись приходит не из браузера, а от ИИ-агента (ссылкой или файлом)."""
    pcm = _pcm(path)
    step = API_PART_SECONDS * 16000 * 2
    lines: list[str] = []
    for n, start in enumerate(range(0, len(pcm), step)):
        part = pcm[start:start + step]
        if len(part) < 16000:  # хвост короче полсекунды - тишина или обрезок
            continue
        text = transcribe_api(_wav(part), f"part{n + 1}.wav", "\n".join(lines))
        if text:
            sec = n * API_PART_SECONDS
            lines.append(f"[{sec // 60:02d}:{sec % 60:02d}] {text}")
    return "\n".join(lines)


def transcribe_any(path: str) -> str:
    """Распознать запись тем способом, который доступен на этом сервере."""
    mode = settings.speech_mode
    if mode == "local":
        return transcribe_media(path)
    if mode == "api":
        try:
            return transcribe_api_file(path)
        except SpeechError as e:
            raise RuntimeError(str(e)) from e
    raise RuntimeError("Здесь записи не распознаются - пришлите протокол или расшифровку текстом")


# ---------- запись по ссылке (для ИИ-агентов) ----------

MAX_URL_FILE = 500 * 1024 * 1024


def _public_host(host: str) -> None:
    """Скачиваем только из интернета: адреса внутренней сети и самого сервера запрещены."""
    import ipaddress
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"Не удалось найти адрес {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("Ссылка ведет во внутреннюю сеть - нужна публичная ссылка на файл")


def check_url(url: str) -> str:
    """Проверить ссылку до создания совещания и вернуть расширение файла."""
    from urllib.parse import urlsplit
    u = urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Нужна ссылка http(s) на файл записи или протокола")
    _public_host(u.hostname)
    return Path(u.path).suffix.lower()


def download(url: str, dest: Path) -> str:
    """Скачать файл по ссылке (с проверкой каждого перенаправления). Возвращает имя файла из ссылки."""
    from urllib.parse import unquote, urljoin, urlsplit
    for _ in range(6):
        check_url(url)
        with httpx.stream("GET", url, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Файл по ссылке не скачался: ответ {r.status_code}")
            size = 0
            with dest.open("wb") as out:
                for chunk in r.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_URL_FILE:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise RuntimeError(f"Файл больше {MAX_URL_FILE // 1024 // 1024} МБ")
                    out.write(chunk)
            return Path(unquote(urlsplit(url).path)).name[:255]
    raise RuntimeError("Слишком много перенаправлений по ссылке")


def process_url(meeting_id: int, url: str, ext: str) -> None:
    """Скачать запись по ссылке и разобрать совещание."""
    import uuid
    try:
        stored = f"{uuid.uuid4().hex}{ext}"
        name = download(url, UPLOAD_DIR / stored)
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            m.stored = stored
            m.filename = m.filename or name
    except Exception as e:
        log.warning("Совещание %s: запись по ссылке не скачалась: %s", meeting_id, e)
        with session_scope() as db:
            m = db.get(Meeting, meeting_id)
            if m:
                m.status, m.error = "error", str(e)[:500]
        return
    process(meeting_id)


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
            transcript = read_text_file(path) if path.suffix.lower() not in MEDIA_EXT else transcribe_any(str(path))
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


def start(meeting_id: int, url: str = "", ext: str = "") -> None:
    target, args = (process_url, (meeting_id, url, ext)) if url else (process, (meeting_id,))
    threading.Thread(target=target, args=args, daemon=True, name=f"meeting-{meeting_id}").start()


def run(meeting_id: int, url: str = "", ext: str = "") -> None:
    """На своем сервере совещание разбирается в фоне. В бессерверной среде фоновых потоков нет - разбираем сразу."""
    if settings.serverless:
        process_url(meeting_id, url, ext) if url else process(meeting_id)
    else:
        start(meeting_id, url, ext)


APPEAL_KINDS = ("problem", "proposal", "praise")  # из этих пунктов получаются обращения


def accept_item(db, m: Meeting, i: MeetingItem, user, author=None):
    """Подтвердить пункт совещания. Проблема, предложение или благодарность становится обращением
    и уходит по маршруту; решения и поручения остаются в протоколе. Возвращает обращение или None."""
    from . import workflow
    a = None
    if i.kind in APPEAL_KINDS:
        label = "" if author else f"Со слов на совещании{': ' + i.speaker if i.speaker else ''}"
        a = workflow.register(db, text=i.text, author=author, author_label=label, source="meeting", type=i.kind,
                              category_id=i.category_id, priority=i.priority, title=i.title or None,
                              meeting_id=m.id, registrar=user)
        if i.quote:
            workflow.add_event(db, a, "comment", None, f"Из расшифровки совещания «{m.title}» ({m.held_at:%d.%m.%Y}): «{i.quote}»",
                               internal=True)
        i.appeal_id = a.id
    i.state = "accepted"
    return a
