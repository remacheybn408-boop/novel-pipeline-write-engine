"""Purge non-text models from the catalog.

model_catalog accumulated every product the vendor /models endpoints
returned (whisper, tts, dall-e, seedance, kolors, embeddings, rerankers,
agnes-image/video, ...). The catalog only feeds text chat generation, so
these rows are deleted once here; new rows are filtered at intake by the
repository upsert. The exclusion pattern's single source of truth is
proseforge/domain/model/modality.py (_EXCLUDE_PATTERNS) — keep this
migration's regex in sync manually (migrations are frozen at authoring
time and must not import live domain code).

Downgrade is a no-op: deleted rows cannot be reconstructed (they reappear
naturally on the next provider sync if still listed upstream).
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_model_modality_cleanup"
down_revision = "0035_project_fk_backfill"
branch_labels = None
depends_on = None

# Mirrors domain/model/modality.py _EXCLUDE_PATTERNS at authoring time.
_EXCLUSION_REGEX = (
    "embed|rerank|tts|whisper|dall|gpt-image|image|video|audio|moderation|"
    "seedance|seedream|kolors|cosyvoice|fish-audio|bge-|cogview|cogvideo|wanx|"
    "flux|stable-diffusion|sd3|sdxl|midjourney|clip|blip|vits|speech|voice|asr"
)


def upgrade() -> None:
    bind = op.get_bind()
    if "model_catalog" not in set(sa.inspect(bind).get_table_names()):
        return
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DELETE FROM model_catalog WHERE model_id ~* :pattern").bindparams(pattern=_EXCLUSION_REGEX))
        return
    # No ~* on sqlite: filter row-by-row with the same regex.
    import re

    compiled = re.compile(_EXCLUSION_REGEX, re.IGNORECASE)
    rows = bind.execute(sa.text("SELECT id, model_id FROM model_catalog")).fetchall()
    stale_ids = [row[0] for row in rows if compiled.search(row[1])]
    for stale_id in stale_ids:
        bind.execute(sa.text("DELETE FROM model_catalog WHERE id = :id"), {"id": stale_id})


def downgrade() -> None:
    # Data cannot be restored; next provider sync re-adds upstream models.
    pass
