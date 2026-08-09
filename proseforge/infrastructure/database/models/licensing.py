from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.infrastructure.database.base import Base


class LicenseStateModel(Base):
    """Single-row license state for this deployment (id is always 1)."""

    __tablename__ = "license_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # AES-GCM encrypted API key (CredentialCipher, base64); never stored raw.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    certificate_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server clock from the last verified certificate (ISO string); combined
    # with last_handshake_monotonic to compute grace without trusting the
    # local system clock.
    last_server_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_handshake_monotonic: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class LicenseFreeUsageModel(Base):
    """Legacy per-use quota cache (pre time-based billing).

    Unused since the switch to duration subscriptions (7/30/365 days);
    table kept to avoid a migration.
    """

    __tablename__ = "license_free_usage"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    free_cluster_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
