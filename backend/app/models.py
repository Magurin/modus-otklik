from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    """UTC без tzinfo - так хранит SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Справочники статусов и типов: код -> подпись. Порядок = порядок стадий.
STATUSES = {
    "new": "Новое",
    "review": "На рассмотрении",
    "clarify": "Ожидает уточнения",
    "in_progress": "В работе",
    "done": "Реализовано",
    "rejected": "Отклонено",
    "closed": "Закрыто",
}
FINAL = {"done", "rejected", "closed"}
MIN_TEXT = 5  # короче обращение не принимаем: из одного слова не понять, о чем речь
TYPES = {"problem": "Проблема", "proposal": "Предложение", "praise": "Положительный отзыв"}
PRIORITIES = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
SOURCES = {"web": "Форма", "telegram": "Telegram", "max": "MAX", "email": "Почта", "meeting": "Совещание", "agent": "ИИ-агент", "import": "Импорт"}
ROLES = {"employee": "Сотрудник", "manager": "Руководитель", "director": "Директор", "admin": "Администратор"}


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    head_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", use_alter=True), nullable=True)
    ext_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # id во внешних системах: {"1c": guid, "b24": "5"}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="employee")
    position: Mapped[str] = mapped_column(String(160), default="")
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    is_absent: Mapped[bool] = mapped_column(Boolean, default=False)  # отпуск, командировка - обращения уходят заместителю
    deputy_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    tg_chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    max_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    tg_link_code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)   # код привязки любого мессенджера
    tg_pending_appeal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ждем ответ на уточнение в боте
    bot_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # незавершенный шаг диалога с ботом: черновик, ожидание текста
    bot_prefs: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # настройки уведомлений в боте и отметка об отправленной сводке
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    ext_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # id во внешних системах: 1С, Битрикс24, файл выгрузки
    gcal: Mapped[dict | None] = mapped_column(JSON, nullable=True)     # подключенный Google Календарь: {"refresh_token", "email"}
    agent_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # sha256 личного ключа для ИИ-агентов (MCP)

    department: Mapped[Department | None] = relationship(foreign_keys=[department_id])
    deputy: Mapped["User | None"] = relationship(remote_side=[id], foreign_keys=[deputy_id])


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    responsible_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sla_days: Mapped[int] = mapped_column(Integer, default=10)  # срок рассмотрения
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    responsible: Mapped[User | None] = relationship(foreign_keys=[responsible_id])


class Appeal(Base):
    __tablename__ = "appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20), default="problem")
    title: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    solution: Mapped[str] = mapped_column(Text, default="")  # предлагаемое автором решение
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    source: Mapped[str] = mapped_column(String(20), default="web")
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    author_label: Mapped[str] = mapped_column(String(200), default="")  # если автора нет в системе (почта, совещание)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)  # подразделение автора
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    meeting_id: Mapped[int | None] = mapped_column(ForeignKey("meetings.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")  # итог: решение или мотивированный отказ
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    ai_filled: Mapped[bool] = mapped_column(Boolean, default=False)  # тип/категорию определил ИИ, человек не проверял
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # оценка автора после «Реализовано»: 1 - решено, -1 - нет
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда напомнили о подходящем сроке
    gcal: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # события в Google Календарях: {id сотрудника: {"id", "h"}}

    author: Mapped[User | None] = relationship(foreign_keys=[author_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    category: Mapped[Category | None] = relationship()
    department: Mapped[Department | None] = relationship()
    events: Mapped[list["Event"]] = relationship(back_populates="appeal", order_by="Event.id", cascade="all, delete-orphan")
    attachments: Mapped[list["Attachment"]] = relationship(cascade="all, delete-orphan")
    supports: Mapped[list["Support"]] = relationship(cascade="all, delete-orphan")


class Event(Base):
    """История обращения: все, что с ним происходило. Ничего не удаляется."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # created, routed, status, comment, question, answer, assigned, edited, escalated, supported, rated
    text: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)  # видно только тем, кто рассматривает
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    appeal: Mapped[Appeal] = relationship(back_populates="events")
    actor: Mapped[User | None] = relationship()


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0)


class Support(Base):
    """«У меня то же самое»: сотрудник присоединился к чужому обращению вместо дубля."""
    __tablename__ = "supports"
    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    appeal_id: Mapped[int | None] = mapped_column(ForeignKey("appeals.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    held_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    stored: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued, transcribing, extracting, ready, error
    error: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    engine: Mapped[str] = mapped_column(String(40), default="")  # чем разбирали: claude или rules
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    uploaded_by: Mapped[User | None] = relationship()
    items: Mapped[list["MeetingItem"]] = relationship(order_by="MeetingItem.id", cascade="all, delete-orphan")


class MeetingItem(Base):
    """То, что ИИ вытащил из совещания. Обращением становится только после подтверждения человеком."""
    __tablename__ = "meeting_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # problem, proposal, praise, decision, task
    title: Mapped[str] = mapped_column(String(200), default="")
    text: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text, default="")  # дословный фрагмент - чтобы проверить ИИ
    speaker: Mapped[str] = mapped_column(String(120), default="")
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    state: Mapped[str] = mapped_column(String(20), default="draft")  # draft, accepted, rejected
    appeal_id: Mapped[int | None] = mapped_column(ForeignKey("appeals.id"), nullable=True)

    category: Mapped[Category | None] = relationship()


class OrgSync(Base):
    """Журнал синхронизаций оргструктуры с внешними системами (только примененные, не предпросмотр)."""
    __tablename__ = "org_syncs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))  # 1c, b24, file
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # пусто - по расписанию
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    actor: Mapped[User | None] = relationship()


class Digest(Base):
    """Сводка для руководителя (кэш, чтобы не дергать модель на каждое открытие экрана)."""
    __tablename__ = "digests"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    themes: Mapped[list] = mapped_column(JSON, default=list)  # повторяющиеся темы, которые выделила модель
    engine: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
