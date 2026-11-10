from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

_ITER = 120_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITER).hex()
    return f"{salt}${h}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITER).hex()
    return hmac.compare_digest(calc, h)


def make_token(user: User) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=12)
    return jwt.encode({"sub": str(user.id), "exp": exp}, settings.jwt_secret, algorithm="HS256")


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Требуется вход")
    try:
        uid = int(jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Сессия истекла, войдите заново")
    user = db.get(User, uid)
    if not user or not user.active:
        raise HTTPException(401, "Пользователь отключен")
    return user


def require(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Недостаточно прав")
        return user
    return dep


handler = require("manager", "director", "admin")  # те, кто рассматривает обращения
overseer = require("director", "admin")            # видят все
admin_only = require("admin")
