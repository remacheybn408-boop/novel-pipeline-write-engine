from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")
    # Incrementing this invalidates every previously issued stateless JWT.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Registration time; reported to the license center on handshakes.
    # Nullable: rows predating migration 0045 have no value.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=lambda: datetime.now(UTC)
    )
