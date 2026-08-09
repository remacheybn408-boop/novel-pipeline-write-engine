"""Foreign key backfill for project-owned tables.

Adds named FOREIGN KEY constraints (ON DELETE CASCADE) from every
project-owned table to projects.id, plus the chain columns
(branches/messages/chapter versions/workflow children/agent run children/
outline versions) to their parents. Orphan rows left over from the
pre-FK era are deleted first, parents before children, so the new
constraints can be created on clean data. User-level audit tables
(model_usage_records, tool_call_log) deliberately keep no FKs: they are
accounting history and their refs are nullified by repository deletes
instead. Two columns are also deliberately FK-free because production
writes values no parent row can match:
conversation_events.conversation_id (polymorphic stream key — message:
topics store message ids, see infrastructure/events/database.py) and
agent_memories.run_id (PROJECT_WIDE_RUN "" sentinel, see
application/agents/memory_service.py). Inspector-guarded, idempotent,
reversible (0034 style).
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_project_fk_backfill"
down_revision = "0034_tool_call_log"
branch_labels = None
depends_on = None

# (table, constraint, column, referenced table) — parents listed before
# children so downgrade drops children first when iterated in reverse.
_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("conversations", "fk_conversations_project_id", "project_id", "projects"),
    ("conversation_branches", "fk_conversation_branches_conversation_id", "conversation_id", "conversations"),
    ("messages", "fk_messages_branch_id", "branch_id", "conversation_branches"),
    ("message_edits", "fk_message_edits_message_id", "message_id", "messages"),
    ("message_chunks", "fk_message_chunks_message_id", "message_id", "messages"),
    ("chapters", "fk_chapters_project_id", "project_id", "projects"),
    ("chapter_versions", "fk_chapter_versions_chapter_id", "chapter_id", "chapters"),
    ("revision_proposals", "fk_revision_proposals_chapter_id", "chapter_id", "chapters"),
    ("workflow_runs", "fk_workflow_runs_project_id", "project_id", "projects"),
    ("workflow_definitions", "fk_workflow_definitions_project_id", "project_id", "projects"),
    ("workflow_steps", "fk_workflow_steps_workflow_run_id", "workflow_run_id", "workflow_runs"),
    ("workflow_events", "fk_workflow_events_workflow_run_id", "workflow_run_id", "workflow_runs"),
    ("model_calls", "fk_model_calls_workflow_run_id", "workflow_run_id", "workflow_runs"),
    ("workflow_node_states", "fk_workflow_node_states_run_id", "run_id", "workflow_runs"),
    ("agent_runs", "fk_agent_runs_project_id", "project_id", "projects"),
    ("agent_tasks", "fk_agent_tasks_run_id", "run_id", "agent_runs"),
    ("agent_events", "fk_agent_events_run_id", "run_id", "agent_runs"),
    ("agent_artifacts", "fk_agent_artifacts_run_id", "run_id", "agent_runs"),
    ("agent_reviews", "fk_agent_reviews_run_id", "run_id", "agent_runs"),
    ("agent_policy_snapshots", "fk_agent_policy_snapshots_run_id", "run_id", "agent_runs"),
    ("agent_evaluations", "fk_agent_evaluations_run_id", "run_id", "agent_runs"),
    ("agent_memories", "fk_agent_memories_project_id", "project_id", "projects"),
    ("agent_graph_revisions", "fk_agent_graph_revisions_project_id", "project_id", "projects"),
    ("outlines", "fk_outlines_project_id", "project_id", "projects"),
    ("outline_versions", "fk_outline_versions_outline_id", "outline_id", "outlines"),
    ("story_bible_entries", "fk_story_bible_entries_project_id", "project_id", "projects"),
    ("review_reports", "fk_review_reports_project_id", "project_id", "projects"),
    ("export_manifests", "fk_export_manifests_project_id", "project_id", "projects"),
    ("quality_reports", "fk_quality_reports_project_id", "project_id", "projects"),
    ("context_items", "fk_context_items_project_id", "project_id", "projects"),
    ("context_snapshots", "fk_context_snapshots_project_id", "project_id", "projects"),
    ("artifacts", "fk_artifacts_project_id", "project_id", "projects"),
    ("embeddings", "fk_embeddings_project_id", "project_id", "projects"),
    ("attachments", "fk_attachments_project_id", "project_id", "projects"),
)

# Orphan cleanup, parents before children so each NOT IN sweep exposes the
# next level of dangling rows. Every statement is idempotent by nature.
_ORPHAN_DELETES: tuple[str, ...] = (
    "DELETE FROM conversations WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM conversation_branches WHERE conversation_id NOT IN (SELECT id FROM conversations)",
    "DELETE FROM messages WHERE branch_id NOT IN (SELECT id FROM conversation_branches)",
    "DELETE FROM message_edits WHERE message_id NOT IN (SELECT id FROM messages)",
    "DELETE FROM message_chunks WHERE message_id NOT IN (SELECT id FROM messages)",
    "DELETE FROM chapters WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM chapter_versions WHERE chapter_id NOT IN (SELECT id FROM chapters)",
    "DELETE FROM revision_proposals WHERE chapter_id NOT IN (SELECT id FROM chapters)",
    "DELETE FROM workflow_runs WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM workflow_definitions WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM workflow_steps WHERE workflow_run_id NOT IN (SELECT id FROM workflow_runs)",
    "DELETE FROM workflow_events WHERE workflow_run_id NOT IN (SELECT id FROM workflow_runs)",
    "DELETE FROM model_calls WHERE workflow_run_id NOT IN (SELECT id FROM workflow_runs)",
    "DELETE FROM workflow_node_states WHERE run_id NOT IN (SELECT id FROM workflow_runs)",
    "DELETE FROM agent_runs WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM agent_tasks WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_events WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_artifacts WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_reviews WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_policy_snapshots WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_evaluations WHERE run_id NOT IN (SELECT id FROM agent_runs)",
    "DELETE FROM agent_memories WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM agent_graph_revisions WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM outlines WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM outline_versions WHERE outline_id NOT IN (SELECT id FROM outlines)",
    "DELETE FROM story_bible_entries WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM review_reports WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM export_manifests WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM quality_reports WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM context_items WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM context_snapshots WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM artifacts WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM embeddings WHERE project_id NOT IN (SELECT id FROM projects)",
    "DELETE FROM attachments WHERE project_id NOT IN (SELECT id FROM projects)",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for statement in _ORPHAN_DELETES:
        table = statement.split()[2]
        if table in tables:
            op.execute(sa.text(statement))
    for table, constraint, column, ref_table in _FOREIGN_KEYS:
        if table not in tables or ref_table not in tables:
            continue
        # Match by target, not by name: metadata-driven migrations (0002/
        # 0005/0006 create tables from current Base.metadata) may already
        # have created this FK under an auto-generated name on fresh
        # installs; adding a second one would duplicate the constraint.
        existing = sa.inspect(bind).get_foreign_keys(table)
        if any(fk["name"] == constraint or (fk["referred_table"] == ref_table and fk["constrained_columns"] == [column]) for fk in existing):
            continue
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(constraint, ref_table, [column], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, constraint, _column, _ref_table in reversed(_FOREIGN_KEYS):
        if table not in tables:
            continue
        existing = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(table)}
        if constraint not in existing:
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint, type_="foreignkey")
