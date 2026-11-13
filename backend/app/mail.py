"""Почта: письма на общий ящик обратной связи становятся обращениями.

Ящик опрашивается по IMAP (IMAP_HOST, IMAP_USER, IMAP_PASSWORD). Автор определяется по адресу отправителя;
письмо с незнакомого адреса тоже регистрируется, но помечается адресом вместо автора.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import threading
import time
from email.header import decode_header, make_header
from email.utils import parseaddr

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import session_scope
from .models import Appeal, User

log = logging.getLogger("mail")
state = {"enabled": False, "error": "", "last_check": None}


def clean_body(body: str) -> str:
    """Убрать цитаты переписки и подпись - в обращении должен остаться только текст автора."""
    out = []
    for line in body.replace("\r", "").split("\n"):
        s = line.strip()
        if s.startswith(">") or re.match(r"^(-- ?$|С уважением|Отправлено с|Sent from|От: |From: |\d{1,2}[./]\d{1,2}[./]\d{2,4}.*(пишет|wrote))", s):
            break
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def intake(db: Session, sender: str, subject: str, body: str) -> Appeal:
    from . import workflow
    addr = parseaddr(sender)[1].lower() or sender.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == addr, User.active))
    subject = re.sub(r"^((re|fwd?|отв|пересл)\s*:\s*)+", "", subject.strip(), flags=re.I)
    text = clean_body(body)
    full = f"{subject}\n\n{text}".strip() if subject and subject.lower() not in text.lower() else (text or subject)
    return workflow.register(db, text=full, author=user, author_label="" if user else addr, source="email")


def _text_part(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    html = ""
    for p in parts:
        if p.get_content_maintype() != "text" or p.get_filename():
            continue
        raw = p.get_payload(decode=True) or b""
        body = raw.decode(p.get_content_charset() or "utf-8", errors="replace")
        if p.get_content_subtype() == "plain":
            return body
        html = html or body
    return re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>|</p>", "\n", html, flags=re.I))


def check_once() -> int:
    n = 0
    box = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        box.login(settings.imap_user, settings.imap_password)
        box.select(settings.imap_folder)
        to = settings.imap_to
        _, data = box.search(None, "UNSEEN", "TO", f'"{to}"') if to else box.search(None, "UNSEEN")
        for num in data[0].split():
            _, parts = box.fetch(num, "(RFC822)")  # письмо помечается прочитанным
            msg = email.message_from_bytes(parts[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            sender = str(make_header(decode_header(msg.get("From", ""))))
            body = _text_part(msg)
            if not (subject or body).strip():
                continue
            with session_scope() as db:
                a = intake(db, sender, subject, body)
                log.info("Письмо от %s -> %s", sender, a.number)
            n += 1
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return n


def _loop() -> None:
    while True:
        try:
            check_once()
            state.update(error="", last_check=time.strftime("%H:%M:%S"))
        except Exception as e:
            state["error"] = str(e)
            log.warning("Почта: %s", e)
        time.sleep(60)


def start() -> None:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        return
    state["enabled"] = True
    threading.Thread(target=_loop, daemon=True, name="mail-poll").start()
