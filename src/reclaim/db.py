"""SQLAlchemy 2.0 persistence layer.

Schema is intentionally Postgres-compatible: JSON used for nested payloads,
`String` for enum values, indexed unique constraints for dedupe + idempotency.
SQLite is the dev engine; swap ``DATABASE_URL`` to a Postgres DSN to migrate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import Settings, get_settings

logger = logging.getLogger("reclaim.db")


class Base(DeclarativeBase):
    """Declarative base for all ORM tables."""


class RecoveryCaseRow(Base):
    __tablename__ = "recovery_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True)  # dedupe key
    customer_id: Mapped[str] = mapped_column(String(255))
    subscription_id: Mapped[str] = mapped_column(String(255), index=True)
    failure_reason: Mapped[str] = mapped_column(String(512))
    failure_reason_raw: Mapped[str] = mapped_column(String(512))
    amount: Mapped[float] = mapped_column(Float)
    attempt_number: Mapped[int] = mapped_column(Integer)
    customer_tier: Mapped[str] = mapped_column(String(32))
    payment_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditLogRow(Base):
    """Append-only decision trail. Insert-only by contract; the writer never
    exposes update/delete."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(255), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    agent_reasoning: Mapped[str] = mapped_column(String(4096), default="")
    input_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String(255), default="")
    action_taken: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(255), default="")
    fallback_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutedActionRow(Base):
    """Idempotency ledger: one row per executed (case, attempt, action).

    The UNIQUE constraint is the hard guarantee that a duplicated Act call can
    never double-retry or double-charge."""
    __tablename__ = "executed_actions"
    __table_args__ = (
        UniqueConstraint("case_id", "attempt_number", "action", name="uq_executed_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(255), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def build_engine(database_url: str) -> Engine:
    # check_same_thread=False so a session can be shared between FastAPI
    # threads / Celery workers in dev; not needed under Postgres.
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )


def init_schema(engine: Engine) -> None:
    """Create tables if they do not yet exist (idempotent)."""
    Base.metadata.create_all(engine)
    logger.info("schema ensured on %s", engine.url)


class Database:
    """Thin wrapper owning one engine + session factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = build_engine(self.settings.database_url)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_session(self) -> Session:
        return self._session_factory()

    def close(self) -> None:
        self.engine.dispose()


_default_db: Database | None = None


def get_db() -> Database:
    """Process-wide default Database (lazily initialized)."""
    global _default_db
    if _default_db is None:
        _default_db = Database()
    return _default_db


def reset_db_for_tests(db: Database | None = None) -> None:
    """Drop the cached default instance so tests can point at a fresh file."""
    global _default_db
    if _default_db is not None:
        _default_db.close()
    _default_db = db


def utcnow() -> datetime:
    return datetime.now(UTC)


def row_to_dict(row: Any) -> dict[str, Any]:
    """Serialize an ORM row to a plain dict (single level)."""
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}