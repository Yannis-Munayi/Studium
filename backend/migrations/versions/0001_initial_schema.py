"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-14

The full Studium data layer, spec section 6. Table and index operations below
are generated from the SQLAlchemy metadata and reviewed by hand; the DDL that
autogenerate cannot see -- extensions, functions, enum types, triggers, and the
subject-metadata procedure -- is written explicitly.

Deviations from the spec text are recorded in backend/DIVERGENCES.md. The
load-bearing one for this file: the spec's uuid_generate_v7() fallback emits 33
hex characters and so fails on every insert. The corrected assembly is below.
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# --- functions ------------------------------------------------------------

# uuid-ossp ships v1/v3/v4/v5 only -- it has no v7 generator, despite spec
# section 4 listing it as the source. Prefer the pg_uuidv7 extension where the
# deploy target has it; this is the portable fallback.
#
# Layout: 48-bit millisecond timestamp | version 7 | 12 bits rand_a |
# variant 0b10 | 62 bits rand_b = 32 hex characters exactly.
UUID_GENERATE_V7 = """
CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
DECLARE
  v_time_ms bigint := (extract(epoch from clock_timestamp()) * 1000)::bigint;
  v_rand    bytea  := gen_random_bytes(10);
  v_hex     text   := encode(v_rand, 'hex');
BEGIN
  RETURN (
    lpad(to_hex(v_time_ms), 12, '0') ||
    '7' || substr(v_hex, 1, 3) ||
    to_hex((get_byte(v_rand, 2) & 63) | 128) ||
    substr(v_hex, 7, 14)
  )::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""

SET_UPDATED_AT = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# learning_sessions.total_cost_usd is denormalised for an O(1) "what did this
# session cost". Spec section 6.6 puts this trigger on session_turns insert,
# but session_turns has no cost column -- cost lives on agent_traces, one row
# per LLM call, which is what actually needs summing.
BUMP_SESSION_COST = """
CREATE OR REPLACE FUNCTION bump_session_cost() RETURNS TRIGGER AS $$
BEGIN
  UPDATE learning_sessions ls
     SET total_cost_usd = ls.total_cost_usd + NEW.cost_usd
    FROM session_turns st
   WHERE st.id = NEW.session_turn_id
     AND ls.id = st.session_id;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Invoked from application code after graph mutations, not from row triggers:
# an MVP-scale bulk import would fire a row trigger thousands of times, and one
# call after the import is cheaper.
REFRESH_SUBJECT_METADATA = """
CREATE OR REPLACE FUNCTION refresh_subject_metadata(p_subject_id uuid)
RETURNS void AS $$
BEGIN
  INSERT INTO subject_metadata AS sm (
    subject_id, concept_count, edge_count, load_bearing_count,
    estimated_total_minutes, last_computed_at
  )
  SELECT s.id,
         (SELECT count(*) FROM concepts c WHERE c.subject_id = s.id),
         (SELECT count(*) FROM concept_edges e WHERE e.subject_id = s.id),
         (SELECT count(*) FROM concepts c
           WHERE c.subject_id = s.id AND c.is_load_bearing),
         (SELECT COALESCE(sum(c.estimated_minutes), 0) FROM concepts c
           WHERE c.subject_id = s.id),
         NOW()
    FROM subjects s
   WHERE s.id = p_subject_id
  ON CONFLICT (subject_id) DO UPDATE
     SET concept_count           = EXCLUDED.concept_count,
         edge_count              = EXCLUDED.edge_count,
         load_bearing_count      = EXCLUDED.load_bearing_count,
         estimated_total_minutes = EXCLUDED.estimated_total_minutes,
         last_computed_at        = EXCLUDED.last_computed_at;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # --- extensions (section 6.0) -----------------------------------------
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')

    # --- functions ---------------------------------------------------------
    op.execute(UUID_GENERATE_V7)
    op.execute(SET_UPDATED_AT)

    # --- enum types --------------------------------------------------------
    op.execute("CREATE TYPE agent_identity AS ENUM ('learner', 'orchestrator', 'curator', 'lecturer', 'tutor', 'evaluator', 'confusion_tracker', 'reviewer', 'system')")
    op.execute("CREATE TYPE session_mode AS ENUM ('orientation', 'lecture', 'tutorial', 'lab', 'review', 'office_hours', 'summative_assessment')")
    op.execute("CREATE TYPE artifact_status AS ENUM ('draft', 'reviewed', 'active', 'retired')")
    op.execute("CREATE TYPE license_kind AS ENUM ('public_domain', 'cc_by', 'cc_by_sa', 'cc_by_nc', 'user_uploaded', 'permission_granted', 'fair_use')")
    op.execute("CREATE TYPE concept_edge_kind AS ENUM ('prerequisite', 'dependency', 'generalization', 'application', 'related')")
    op.execute("CREATE TYPE concept_source_role AS ENUM ('canonical_definition', 'primary_exposition', 'worked_example', 'exercise', 'historical', 'alternative_stance')")
    op.execute("CREATE TYPE artifact_kind AS ENUM ('lecture_segment', 'tutorial_seed', 'worked_example', 'practice_problem', 'check_question', 'model_answer', 'rubric_prompt', 'orientation_segment')")
    op.execute("CREATE TYPE artifact_stance AS ENUM ('formal', 'intuitive', 'applied', 'historical', 'default')")
    op.execute("CREATE TYPE mastery_event_kind AS ENUM ('lecture_check_correct', 'lecture_check_incorrect', 'tutorial_turn_success', 'tutorial_turn_stuck', 'tutorial_turn_recovery', 'practice_correct', 'practice_incorrect', 'practice_partial', 'assessment_scored', 'review_correct', 'review_incorrect', 'manual_adjustment', 'decay_refresh')")
    op.execute("CREATE TYPE journal_status AS ENUM ('open', 'partial', 'resolved', 'archived')")
    op.execute("CREATE TYPE journal_event_kind AS ENUM ('created', 'revisited', 'partially_addressed', 'resolved', 'reopened', 'archived', 'hypothesis_updated', 'learner_note_added')")
    op.execute("CREATE TYPE assessment_mode AS ENUM ('formative', 'summative')")
    op.execute("CREATE TYPE assessment_trigger AS ENUM ('session_close', 'learner_initiated', 'scheduled', 'unit_gate')")
    op.execute("CREATE TYPE portfolio_item_kind AS ENUM ('proof', 'code', 'prose', 'derivation', 'diagram', 'notebook')")
    op.execute("CREATE TYPE ingestion_job_kind AS ENUM ('extract_text', 'chunk', 'embed', 'suggest_concept_mapping')")
    op.execute("CREATE TYPE ingestion_job_status AS ENUM ('pending', 'running', 'done', 'failed', 'cancelled')")
    op.execute("CREATE TYPE review_flag_source AS ENUM ('system_confidence', 'learner_report', 'tracker_pattern', 'evaluator_disagreement', 'random_sample')")
    op.execute("CREATE TYPE review_status AS ENUM ('pending', 'in_review', 'resolved', 'dismissed')")

    # --- tables and indexes ------------------------------------------------
    op.create_table('users',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('auth_provider', sa.Text(), server_default=sa.text("'local'"), nullable=False),
    sa.Column('role', sa.Text(), server_default=sa.text("'learner'"), nullable=False),
    sa.Column('locale', sa.Text(), server_default=sa.text("'en-CA'"), nullable=False),
    sa.Column('timezone', sa.Text(), server_default=sa.text("'America/Toronto'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("auth_provider IN ('local', 'google', 'github')", name=op.f('ck_users_auth_provider')),
    sa.CheckConstraint("role IN ('learner', 'reviewer', 'admin')", name=op.f('ck_users_role')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_table('audit_log',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('target_type', sa.Text(), nullable=False),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reason', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_log_actor_user_id'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_table('auth_sessions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(token_hash) = 64', name=op.f('ck_auth_sessions_token_hash_len')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_auth_sessions_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_auth_sessions_token_hash'))
    )
    op.create_table('cost_ledger',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('tokens_in', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('tokens_out', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cache_read_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cache_write_tokens', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=4), server_default=sa.text('0'), nullable=False),
    sa.Column('session_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_cost_ledger_user_id'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cost_ledger')),
    sa.UniqueConstraint('user_id', 'day', 'model', name='uq_cost_ledger_day', postgresql_nulls_not_distinct=True)
    )
    op.create_table('subjects',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('short_description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('long_description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('authored_by', sa.UUID(), nullable=True),
    sa.Column('status', postgresql.ENUM('draft', 'reviewed', 'active', 'retired', name='artifact_status', create_type=False), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('assessment_threshold', sa.REAL(), server_default=sa.text('0.75'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,63}$'", name=op.f('ck_subjects_slug_format')),
    sa.CheckConstraint('assessment_threshold >= 0 AND assessment_threshold <= 1', name=op.f('ck_subjects_assessment_threshold_range')),
    sa.ForeignKeyConstraint(['authored_by'], ['users.id'], name=op.f('fk_subjects_authored_by'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subjects')),
    sa.UniqueConstraint('slug', name=op.f('uq_subjects_slug'))
    )
    op.create_table('user_budget_caps',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('daily_soft_usd', sa.Numeric(precision=10, scale=2), server_default=sa.text('5.00'), nullable=False),
    sa.Column('daily_hard_usd', sa.Numeric(precision=10, scale=2), server_default=sa.text('8.00'), nullable=False),
    sa.Column('monthly_soft_usd', sa.Numeric(precision=10, scale=2), server_default=sa.text('75.00'), nullable=False),
    sa.Column('monthly_hard_usd', sa.Numeric(precision=10, scale=2), server_default=sa.text('100.00'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_budget_caps_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_budget_caps'))
    )
    op.create_table('user_profiles',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('background_notes', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('stated_goals', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('accessibility', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_profiles_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_profiles'))
    )
    op.create_table('concepts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('short_description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('long_description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('depth', sa.SmallInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('is_load_bearing', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('estimated_minutes', sa.SmallInteger(), server_default=sa.text('30'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,80}$'", name=op.f('ck_concepts_slug_format')),
    sa.CheckConstraint('depth BETWEEN 1 AND 5', name=op.f('ck_concepts_depth_range')),
    sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], name=op.f('fk_concepts_subject_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concepts')),
    sa.UniqueConstraint('subject_id', 'slug', name='uq_concepts_subject_slug')
    )
    op.create_table('sources',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('authors', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('publication_year', sa.Integer(), nullable=True),
    sa.Column('license', postgresql.ENUM('public_domain', 'cc_by', 'cc_by_sa', 'cc_by_nc', 'user_uploaded', 'permission_granted', 'fair_use', name='license_kind', create_type=False), nullable=False),
    sa.Column('license_notes', sa.Text(), nullable=True),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('storage_path', sa.Text(), nullable=False),
    sa.Column('content_sha256', sa.Text(), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('token_count', sa.Integer(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', postgresql.ENUM('draft', 'reviewed', 'active', 'retired', name='artifact_status', create_type=False), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(content_sha256) = 64', name=op.f('ck_sources_content_sha256_len')),
    sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], name=op.f('fk_sources_subject_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_sources_uploaded_by'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sources')),
    sa.UniqueConstraint('subject_id', 'content_sha256', name='uq_sources_subject_hash')
    )
    op.create_table('subject_metadata',
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('concept_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('edge_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('load_bearing_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('estimated_total_minutes', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_computed_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], name=op.f('fk_subject_metadata_subject_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('subject_id', name=op.f('pk_subject_metadata'))
    )
    op.create_table('concept_edges',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('from_concept_id', sa.UUID(), nullable=False),
    sa.Column('to_concept_id', sa.UUID(), nullable=False),
    sa.Column('kind', postgresql.ENUM('prerequisite', 'dependency', 'generalization', 'application', 'related', name='concept_edge_kind', create_type=False), nullable=False),
    sa.Column('weight', sa.REAL(), server_default=sa.text('1.0'), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('from_concept_id <> to_concept_id', name=op.f('ck_concept_edges_no_self_edge')),
    sa.CheckConstraint('weight > 0 AND weight <= 1.0', name=op.f('ck_concept_edges_weight_range')),
    sa.ForeignKeyConstraint(['from_concept_id'], ['concepts.id'], name=op.f('fk_concept_edges_from_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], name=op.f('fk_concept_edges_subject_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_concept_id'], ['concepts.id'], name=op.f('fk_concept_edges_to_concept_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_edges')),
    sa.UniqueConstraint('from_concept_id', 'to_concept_id', 'kind', name='uq_concept_edges_triple')
    )
    op.create_table('concept_sources',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('chunk_ids', postgresql.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('role', postgresql.ENUM('canonical_definition', 'primary_exposition', 'worked_example', 'exercise', 'historical', 'alternative_stance', name='concept_source_role', create_type=False), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_concept_sources_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_concept_sources_source_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_sources')),
    sa.UniqueConstraint('concept_id', 'source_id', 'role', name='uq_concept_sources_triple')
    )
    op.create_table('content_artifacts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=False),
    sa.Column('kind', postgresql.ENUM('lecture_segment', 'tutorial_seed', 'worked_example', 'practice_problem', 'check_question', 'model_answer', 'rubric_prompt', 'orientation_segment', name='artifact_kind', create_type=False), nullable=False),
    sa.Column('stance', postgresql.ENUM('formal', 'intuitive', 'applied', 'historical', 'default', name='artifact_stance', create_type=False), server_default=sa.text("'default'"), nullable=False),
    sa.Column('title', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('generated_by', postgresql.ENUM('learner', 'orchestrator', 'curator', 'lecturer', 'tutor', 'evaluator', 'confusion_tracker', 'reviewer', 'system', name='agent_identity', create_type=False), nullable=False),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('prompt_hash', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('status', postgresql.ENUM('draft', 'reviewed', 'active', 'retired', name='artifact_status', create_type=False), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('reviewed_by', sa.UUID(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('superseded_by', sa.UUID(), nullable=True),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(prompt_hash) = 64', name=op.f('ck_content_artifacts_prompt_hash_len')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_content_artifacts_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_content_artifacts_reviewed_by'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['superseded_by'], ['content_artifacts.id'], name=op.f('fk_content_artifacts_superseded_by'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_content_artifacts'))
    )
    op.create_table('ingestion_jobs',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('kind', postgresql.ENUM('extract_text', 'chunk', 'embed', 'suggest_concept_mapping', name='ingestion_job_kind', create_type=False), nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'running', 'done', 'failed', 'cancelled', name='ingestion_job_status', create_type=False), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('attempt', sa.SmallInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=4), server_default=sa.text('0'), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_ingestion_jobs_source_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ingestion_jobs'))
    )
    op.create_table('learner_subjects',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('subject_version', sa.Integer(), nullable=False),
    sa.Column('enrolled_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('current_focus_concept_id', sa.UUID(), nullable=True),
    sa.Column('syllabus_plan', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('intake_summary', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['current_focus_concept_id'], ['concepts.id'], name=op.f('fk_learner_subjects_current_focus_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], name=op.f('fk_learner_subjects_subject_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_learner_subjects_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_learner_subjects')),
    sa.UniqueConstraint('id', 'user_id', name='uq_learner_subjects_id_user'),
    sa.UniqueConstraint('user_id', 'subject_id', name='uq_learner_subjects_pair')
    )
    op.create_table('rubric_criteria',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('key_points', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('weight', sa.SmallInteger(), nullable=False),
    sa.Column('min_words', sa.SmallInteger(), server_default=sa.text('20'), nullable=False),
    sa.Column('status', postgresql.ENUM('draft', 'reviewed', 'active', 'retired', name='artifact_status', create_type=False), server_default=sa.text("'draft'"), nullable=False),
    sa.Column('reviewed_by', sa.UUID(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("jsonb_typeof(key_points) = 'array'", name=op.f('ck_rubric_criteria_key_points_array')),
    sa.CheckConstraint('weight IN (1, 2, 3)', name=op.f('ck_rubric_criteria_weight_range')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_rubric_criteria_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_rubric_criteria_reviewed_by'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rubric_criteria')),
    sa.UniqueConstraint('concept_id', 'slug', name='uq_rubric_criteria_concept_slug')
    )
    op.create_table('source_chunks',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('page_start', sa.Integer(), nullable=True),
    sa.Column('page_end', sa.Integer(), nullable=True),
    sa.Column('section_path', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_source_chunks_source_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source_chunks')),
    sa.UniqueConstraint('source_id', 'chunk_index', name='uq_source_chunks_index')
    )
    op.create_table('concept_mastery',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('learner_subject_id', sa.UUID(), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=False),
    sa.Column('p_known', sa.REAL(), server_default=sa.text('0.1'), nullable=False),
    sa.Column('p_known_decayed', sa.REAL(), server_default=sa.text('0.1'), nullable=False),
    sa.Column('evidence_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_evidence_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_reached_mastery_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bkt_params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('p_known >= 0.0 AND p_known <= 1.0', name=op.f('ck_concept_mastery_p_known_range')),
    sa.CheckConstraint('p_known_decayed >= 0.0 AND p_known_decayed <= 1.0', name=op.f('ck_concept_mastery_p_known_decayed_range')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_concept_mastery_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['learner_subject_id'], ['learner_subjects.id'], name=op.f('fk_concept_mastery_learner_subject_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_concept_mastery')),
    sa.UniqueConstraint('learner_subject_id', 'concept_id', name='uq_concept_mastery_pair')
    )
    op.create_table('content_citations',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('artifact_id', sa.UUID(), nullable=False),
    sa.Column('source_chunk_id', sa.UUID(), nullable=False),
    sa.Column('quoted_span', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['content_artifacts.id'], name=op.f('fk_content_citations_artifact_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_chunk_id'], ['source_chunks.id'], name=op.f('fk_content_citations_source_chunk_id'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_content_citations')),
    sa.UniqueConstraint('artifact_id', 'source_chunk_id', name='uq_content_citations_pair')
    )
    op.create_table('learning_sessions',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('learner_subject_id', sa.UUID(), nullable=False),
    sa.Column('mode', postgresql.ENUM('orientation', 'lecture', 'tutorial', 'lab', 'review', 'office_hours', 'summative_assessment', name='session_mode', create_type=False), nullable=False),
    sa.Column('focus_concept_id', sa.UUID(), nullable=True),
    sa.Column('target_duration_minutes', sa.SmallInteger(), server_default=sa.text('90'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_reason', sa.Text(), nullable=True),
    sa.Column('total_cost_usd', sa.Numeric(precision=10, scale=4), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("end_reason IN ('completed', 'time_up', 'learner_stop', 'idle_timeout', 'error')", name=op.f('ck_learning_sessions_end_reason')),
    sa.ForeignKeyConstraint(['focus_concept_id'], ['concepts.id'], name=op.f('fk_learning_sessions_focus_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['learner_subject_id', 'user_id'], ['learner_subjects.id', 'learner_subjects.user_id'], name='fk_learning_sessions_enrollment', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_learning_sessions_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_learning_sessions'))
    )
    op.create_table('review_cards',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('learner_subject_id', sa.UUID(), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=False),
    sa.Column('stability', sa.REAL(), server_default=sa.text('1.0'), nullable=False),
    sa.Column('difficulty', sa.REAL(), server_default=sa.text('5.0'), nullable=False),
    sa.Column('retrievability', sa.REAL(), server_default=sa.text('1.0'), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    sa.Column('last_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reps', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('lapses', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'new'"), nullable=False),
    sa.Column('suspended', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('new', 'learning', 'review', 'relearning')", name=op.f('ck_review_cards_state')),
    sa.CheckConstraint('difficulty >= 1 AND difficulty <= 10', name=op.f('ck_review_cards_difficulty_range')),
    sa.CheckConstraint('retrievability >= 0 AND retrievability <= 1', name=op.f('ck_review_cards_retrievability_range')),
    sa.CheckConstraint('stability > 0', name=op.f('ck_review_cards_stability_positive')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_review_cards_concept_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['learner_subject_id'], ['learner_subjects.id'], name=op.f('fk_review_cards_learner_subject_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_cards')),
    sa.UniqueConstraint('learner_subject_id', 'concept_id', name='uq_review_cards_pair')
    )
    op.create_table('source_chunk_embeddings',
    sa.Column('chunk_id', sa.UUID(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=False),
    sa.Column('model_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chunk_id'], ['source_chunks.id'], name=op.f('fk_source_chunk_embeddings_chunk_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('chunk_id', name=op.f('pk_source_chunk_embeddings'))
    )
    op.create_table('assessment_attempts',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('learner_subject_id', sa.UUID(), nullable=True),
    sa.Column('scope_concept_id', sa.UUID(), nullable=True),
    sa.Column('mode', postgresql.ENUM('formative', 'summative', name='assessment_mode', create_type=False), server_default=sa.text("'formative'"), nullable=False),
    sa.Column('triggered_by', postgresql.ENUM('session_close', 'learner_initiated', 'scheduled', 'unit_gate', name='assessment_trigger', create_type=False), nullable=False),
    sa.Column('score', sa.REAL(), nullable=True),
    sa.Column('passed', sa.Boolean(), nullable=True),
    sa.Column('threshold', sa.REAL(), server_default=sa.text('0.75'), nullable=False),
    sa.Column('proctored', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('graded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('grading_cost_usd', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('overall_feedback', sa.Text(), nullable=True),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(passed IS NULL) = (score IS NULL)', name=op.f('ck_assessment_attempts_passed_requires_score')),
    sa.CheckConstraint('score >= 0 AND score <= 1', name=op.f('ck_assessment_attempts_score_range')),
    sa.CheckConstraint('threshold >= 0 AND threshold <= 1', name=op.f('ck_assessment_attempts_threshold_range')),
    sa.ForeignKeyConstraint(['learner_subject_id', 'user_id'], ['learner_subjects.id', 'learner_subjects.user_id'], name='fk_assessment_attempts_enrollment', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['scope_concept_id'], ['concepts.id'], name=op.f('fk_assessment_attempts_scope_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_assessment_attempts_session_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_assessment_attempts_user_id'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_attempts'))
    )
    op.create_table('journal_entries',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('learner_subject_id', sa.UUID(), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=True),
    sa.Column('first_session_id', sa.UUID(), nullable=True),
    sa.Column('status', postgresql.ENUM('open', 'partial', 'resolved', 'archived', name='journal_status', create_type=False), server_default=sa.text("'open'"), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('hypothesis', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('learner_note', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_touched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("origin IN ('learner_flagged', 'tracker_inferred', 'check_failed', 'assessment_gap')", name=op.f('ck_journal_entries_origin')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_journal_entries_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['first_session_id'], ['learning_sessions.id'], name=op.f('fk_journal_entries_first_session_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['learner_subject_id', 'user_id'], ['learner_subjects.id', 'learner_subjects.user_id'], name='fk_journal_entries_enrollment', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_journal_entries_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_journal_entries'))
    )
    op.create_table('mastery_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('concept_mastery_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('kind', postgresql.ENUM('lecture_check_correct', 'lecture_check_incorrect', 'tutorial_turn_success', 'tutorial_turn_stuck', 'tutorial_turn_recovery', 'practice_correct', 'practice_incorrect', 'practice_partial', 'assessment_scored', 'review_correct', 'review_incorrect', 'manual_adjustment', 'decay_refresh', name='mastery_event_kind', create_type=False), nullable=False),
    sa.Column('p_known_before', sa.REAL(), nullable=False),
    sa.Column('p_known_after', sa.REAL(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['concept_mastery_id'], ['concept_mastery.id'], name=op.f('fk_mastery_events_concept_mastery_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_mastery_events_session_id'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mastery_events'))
    )
    op.create_table('portfolio_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('learner_subject_id', sa.UUID(), nullable=False),
    sa.Column('chain_index', sa.Integer(), nullable=False),
    sa.Column('concept_id', sa.UUID(), nullable=True),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('kind', postgresql.ENUM('proof', 'code', 'prose', 'derivation', 'diagram', 'notebook', name='portfolio_item_kind', create_type=False), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('storage_path', sa.Text(), nullable=True),
    sa.Column('content_sha256', sa.Text(), nullable=False),
    sa.Column('parent_item_id', sa.UUID(), nullable=True),
    sa.Column('signature', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(content_sha256) = 64', name=op.f('ck_portfolio_items_content_sha256_len')),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_portfolio_items_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['learner_subject_id', 'user_id'], ['learner_subjects.id', 'learner_subjects.user_id'], name='fk_portfolio_items_enrollment', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_item_id'], ['portfolio_items.id'], name=op.f('fk_portfolio_items_parent_item_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_portfolio_items_session_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_portfolio_items_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_portfolio_items')),
    sa.UniqueConstraint('learner_subject_id', 'chain_index', name='uq_portfolio_chain')
    )
    op.create_table('retrieval_checks',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('based_on_session_id', sa.UUID(), nullable=True),
    sa.Column('prompts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('responses', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('scores', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('overall_score', sa.REAL(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('overall_score >= 0 AND overall_score <= 1', name=op.f('ck_retrieval_checks_overall_score_range')),
    sa.ForeignKeyConstraint(['based_on_session_id'], ['learning_sessions.id'], name=op.f('fk_retrieval_checks_based_on_session_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_retrieval_checks_session_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_retrieval_checks'))
    )
    op.create_table('review_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('card_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('artifact_id', sa.UUID(), nullable=True),
    sa.Column('rating', sa.SmallInteger(), nullable=False),
    sa.Column('response_text', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('elapsed_seconds', sa.Integer(), nullable=True),
    sa.Column('stability_before', sa.REAL(), nullable=False),
    sa.Column('stability_after', sa.REAL(), nullable=False),
    sa.Column('difficulty_before', sa.REAL(), nullable=False),
    sa.Column('difficulty_after', sa.REAL(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('rating IN (1, 2, 3, 4)', name=op.f('ck_review_events_rating_range')),
    sa.ForeignKeyConstraint(['artifact_id'], ['content_artifacts.id'], name=op.f('fk_review_events_artifact_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['card_id'], ['review_cards.id'], name=op.f('fk_review_events_card_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_review_events_session_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_events'))
    )
    op.create_table('session_summaries',
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('key_points', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('open_threads', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('concepts_touched', postgresql.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('generated_by', postgresql.ENUM('learner', 'orchestrator', 'curator', 'lecturer', 'tutor', 'evaluator', 'confusion_tracker', 'reviewer', 'system', name='agent_identity', create_type=False), server_default=sa.text("'orchestrator'"), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=4), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_session_summaries_session_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('session_id', name=op.f('pk_session_summaries'))
    )
    op.create_table('session_turns',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('turn_index', sa.Integer(), nullable=False),
    sa.Column('actor', postgresql.ENUM('learner', 'orchestrator', 'curator', 'lecturer', 'tutor', 'evaluator', 'confusion_tracker', 'reviewer', 'system', name='agent_identity', create_type=False), nullable=False),
    sa.Column('input', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('primitive', sa.Text(), nullable=True),
    sa.Column('concept_id', sa.UUID(), nullable=True),
    sa.Column('artifact_id', sa.UUID(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['content_artifacts.id'], name=op.f('fk_session_turns_artifact_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['concept_id'], ['concepts.id'], name=op.f('fk_session_turns_concept_id'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_session_turns_session_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_session_turns')),
    sa.UniqueConstraint('session_id', 'turn_index', name='uq_session_turns_index')
    )
    op.create_table('agent_traces',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('session_turn_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('agent', postgresql.ENUM('learner', 'orchestrator', 'curator', 'lecturer', 'tutor', 'evaluator', 'confusion_tracker', 'reviewer', 'system', name='agent_identity', create_type=False), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_messages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('system_prompt_hash', sa.Text(), nullable=False),
    sa.Column('completion', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('tools_used', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('stop_reason', sa.Text(), nullable=True),
    sa.Column('tokens_in', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('tokens_out', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cache_write_5m_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cache_write_1h_tokens', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('server_tool_use', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=6), nullable=False),
    sa.Column('langfuse_trace_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("agent <> 'learner'", name=op.f('ck_agent_traces_agent_not_learner')),
    sa.CheckConstraint('length(system_prompt_hash) = 64', name=op.f('ck_agent_traces_system_prompt_hash_len')),
    sa.ForeignKeyConstraint(['session_turn_id'], ['session_turns.id'], name=op.f('fk_agent_traces_session_turn_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_agent_traces_user_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_traces')),
    sa.UniqueConstraint('session_turn_id', name=op.f('uq_agent_traces_session_turn_id'))
    )
    op.create_table('assessment_responses',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('rubric_criterion_id', sa.UUID(), nullable=False),
    sa.Column('criterion_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('prompt_shown', sa.Text(), nullable=False),
    sa.Column('learner_response', sa.Text(), nullable=False),
    sa.Column('grade', sa.SmallInteger(), nullable=True),
    sa.Column('feedback', sa.Text(), nullable=True),
    sa.Column('missing_points', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('graded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('grade IN (0, 1, 2)', name=op.f('ck_assessment_responses_grade_range')),
    sa.ForeignKeyConstraint(['attempt_id'], ['assessment_attempts.id'], name=op.f('fk_assessment_responses_attempt_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rubric_criterion_id'], ['rubric_criteria.id'], name=op.f('fk_assessment_responses_rubric_criterion_id'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_responses')),
    sa.UniqueConstraint('attempt_id', 'rubric_criterion_id', name='uq_assessment_responses_pair')
    )
    op.create_table('content_review_queue',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('artifact_id', sa.UUID(), nullable=True),
    sa.Column('session_turn_id', sa.UUID(), nullable=True),
    sa.Column('source', postgresql.ENUM('system_confidence', 'learner_report', 'tracker_pattern', 'evaluator_disagreement', 'random_sample', name='review_flag_source', create_type=False), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('severity', sa.SmallInteger(), server_default=sa.text('2'), nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'in_review', 'resolved', 'dismissed', name='review_status', create_type=False), server_default=sa.text("'pending'"), nullable=False),
    sa.Column('assigned_to', sa.UUID(), nullable=True),
    sa.Column('resolution_note', sa.Text(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('artifact_id IS NOT NULL OR session_turn_id IS NOT NULL', name=op.f('ck_content_review_queue_has_target')),
    sa.CheckConstraint('severity BETWEEN 1 AND 3', name=op.f('ck_content_review_queue_severity_range')),
    sa.ForeignKeyConstraint(['artifact_id'], ['content_artifacts.id'], name=op.f('fk_content_review_queue_artifact_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['assigned_to'], ['users.id'], name=op.f('fk_content_review_queue_assigned_to'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_turn_id'], ['session_turns.id'], name=op.f('fk_content_review_queue_session_turn_id'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_content_review_queue'))
    )
    op.create_table('journal_events',
    sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v7()'), nullable=False),
    sa.Column('entry_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=True),
    sa.Column('kind', postgresql.ENUM('created', 'revisited', 'partially_addressed', 'resolved', 'reopened', 'archived', 'hypothesis_updated', 'learner_note_added', name='journal_event_kind', create_type=False), nullable=False),
    sa.Column('note', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['entry_id'], ['journal_entries.id'], name=op.f('fk_journal_events_entry_id'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['learning_sessions.id'], name=op.f('fk_journal_events_session_id'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_journal_events'))
    )
    op.create_index('idx_users_email_active', 'users', ['email'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_audit_log_actor_time', 'audit_log', ['actor_user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_audit_log_target', 'audit_log', ['target_type', 'target_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_auth_sessions_expires', 'auth_sessions', ['expires_at'], unique=False, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_index('idx_auth_sessions_user_active', 'auth_sessions', ['user_id'], unique=False, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_index('idx_cost_ledger_day', 'cost_ledger', [sa.literal_column('day DESC')], unique=False)
    op.create_index('idx_cost_ledger_user_day', 'cost_ledger', ['user_id', sa.literal_column('day DESC')], unique=False)
    op.create_index('idx_subjects_active', 'subjects', ['slug'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_subjects_authored_by', 'subjects', ['authored_by'], unique=False)
    op.create_index('idx_concepts_load_bearing', 'concepts', ['subject_id'], unique=False, postgresql_where=sa.text('is_load_bearing'))
    op.create_index('idx_concepts_subject_position', 'concepts', ['subject_id', 'position'], unique=False)
    op.create_index('idx_sources_subject', 'sources', ['subject_id'], unique=False)
    op.create_index('idx_sources_subject_status', 'sources', ['subject_id', 'status'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_sources_uploaded_by', 'sources', ['uploaded_by'], unique=False, postgresql_where=sa.text('uploaded_by IS NOT NULL'))
    op.create_index('idx_concept_edges_from', 'concept_edges', ['from_concept_id', 'kind'], unique=False)
    op.create_index('idx_concept_edges_subject', 'concept_edges', ['subject_id'], unique=False)
    op.create_index('idx_concept_edges_to', 'concept_edges', ['to_concept_id', 'kind'], unique=False)
    op.create_index('idx_concept_sources_concept', 'concept_sources', ['concept_id', 'role'], unique=False)
    op.create_index('idx_concept_sources_source', 'concept_sources', ['source_id'], unique=False)
    op.create_index('idx_artifacts_active_slot', 'content_artifacts', ['concept_id', 'kind', 'stance', sa.literal_column("((metadata ->> 'segment_index'))")], unique=True, postgresql_where=sa.text("status = 'active' AND retired_at IS NULL AND deleted_at IS NULL"))
    op.create_index('idx_artifacts_concept', 'content_artifacts', ['concept_id'], unique=False)
    op.create_index('idx_artifacts_concept_kind_active', 'content_artifacts', ['concept_id', 'kind', 'stance'], unique=False, postgresql_where=sa.text("status = 'active' AND retired_at IS NULL AND deleted_at IS NULL"))
    op.create_index('idx_artifacts_review_queue', 'content_artifacts', ['generated_at'], unique=False, postgresql_where=sa.text("status = 'draft' AND deleted_at IS NULL"))
    op.create_index('idx_artifacts_reviewed_by', 'content_artifacts', ['reviewed_by'], unique=False)
    op.create_index('idx_artifacts_status', 'content_artifacts', ['status'], unique=False, postgresql_where=sa.text("status IN ('draft', 'reviewed') AND deleted_at IS NULL"))
    op.create_index('idx_artifacts_superseded_by', 'content_artifacts', ['superseded_by'], unique=False)
    op.create_index('idx_ingestion_jobs_pending', 'ingestion_jobs', ['created_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('idx_ingestion_jobs_source', 'ingestion_jobs', ['source_id', 'kind', 'status'], unique=False)
    op.create_index('idx_learner_subjects_focus', 'learner_subjects', ['current_focus_concept_id'], unique=False)
    op.create_index('idx_learner_subjects_subject', 'learner_subjects', ['subject_id'], unique=False)
    op.create_index('idx_learner_subjects_user_active', 'learner_subjects', ['user_id'], unique=False, postgresql_where=sa.text('archived_at IS NULL'))
    op.create_index('idx_rubric_criteria_concept_active', 'rubric_criteria', ['concept_id'], unique=False, postgresql_where=sa.text("status = 'active' AND retired_at IS NULL"))
    op.create_index('idx_rubric_criteria_reviewed_by', 'rubric_criteria', ['reviewed_by'], unique=False)
    op.create_index('idx_source_chunks_source', 'source_chunks', ['source_id'], unique=False)
    op.create_index('idx_concept_mastery_concept', 'concept_mastery', ['concept_id'], unique=False)
    op.create_index('idx_concept_mastery_learner', 'concept_mastery', ['learner_subject_id', sa.literal_column('p_known_decayed DESC')], unique=False)
    op.create_index('idx_concept_mastery_stale', 'concept_mastery', ['last_evidence_at'], unique=False, postgresql_where=sa.text('last_evidence_at IS NOT NULL'))
    op.create_index('idx_content_citations_chunk', 'content_citations', ['source_chunk_id'], unique=False)
    op.create_index('idx_learning_sessions_active', 'learning_sessions', ['user_id'], unique=False, postgresql_where=sa.text('ended_at IS NULL'))
    op.create_index('idx_learning_sessions_focus', 'learning_sessions', ['focus_concept_id'], unique=False)
    op.create_index('idx_learning_sessions_learner_subject', 'learning_sessions', ['learner_subject_id', sa.literal_column('started_at DESC')], unique=False)
    op.create_index('idx_learning_sessions_user_time', 'learning_sessions', ['user_id', sa.literal_column('started_at DESC')], unique=False)
    op.create_index('idx_review_cards_concept', 'review_cards', ['concept_id'], unique=False)
    op.create_index('idx_review_cards_due', 'review_cards', ['learner_subject_id', 'due_at'], unique=False, postgresql_where=sa.text('NOT suspended'))
    op.create_index('idx_review_cards_state', 'review_cards', ['learner_subject_id', 'state'], unique=False, postgresql_where=sa.text('NOT suspended'))
    op.create_index('idx_source_chunk_embeddings_hnsw', 'source_chunk_embeddings', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_with={'m': 16, 'ef_construction': 64}, postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('idx_assessment_attempts_concept', 'assessment_attempts', ['scope_concept_id'], unique=False)
    op.create_index('idx_assessment_attempts_scope', 'assessment_attempts', ['learner_subject_id', 'scope_concept_id', sa.literal_column('submitted_at DESC')], unique=False)
    op.create_index('idx_assessment_attempts_session', 'assessment_attempts', ['session_id'], unique=False)
    op.create_index('idx_assessment_attempts_user_time', 'assessment_attempts', ['user_id', sa.literal_column('started_at DESC')], unique=False)
    op.create_index('idx_journal_entries_concept', 'journal_entries', ['concept_id'], unique=False, postgresql_where=sa.text('concept_id IS NOT NULL'))
    op.create_index('idx_journal_entries_first_session', 'journal_entries', ['first_session_id'], unique=False)
    op.create_index('idx_journal_entries_learner_subject', 'journal_entries', ['learner_subject_id', 'status', sa.literal_column('last_touched_at DESC')], unique=False)
    op.create_index('idx_journal_entries_user_open', 'journal_entries', ['user_id', sa.literal_column('last_touched_at DESC')], unique=False, postgresql_where=sa.text("status IN ('open', 'partial')"))
    op.create_index('idx_mastery_events_mastery_time', 'mastery_events', ['concept_mastery_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_mastery_events_session', 'mastery_events', ['session_id'], unique=False, postgresql_where=sa.text('session_id IS NOT NULL'))
    op.create_index('idx_portfolio_concept', 'portfolio_items', ['concept_id', sa.literal_column('created_at DESC')], unique=False, postgresql_where=sa.text('concept_id IS NOT NULL'))
    op.create_index('idx_portfolio_parent', 'portfolio_items', ['parent_item_id'], unique=False)
    op.create_index('idx_portfolio_session', 'portfolio_items', ['session_id'], unique=False, postgresql_where=sa.text('session_id IS NOT NULL'))
    op.create_index('idx_portfolio_user_created', 'portfolio_items', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_retrieval_checks_based_on', 'retrieval_checks', ['based_on_session_id'], unique=False)
    op.create_index('idx_retrieval_checks_session', 'retrieval_checks', ['session_id'], unique=False)
    op.create_index('idx_review_events_artifact', 'review_events', ['artifact_id'], unique=False)
    op.create_index('idx_review_events_card_time', 'review_events', ['card_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_review_events_session', 'review_events', ['session_id'], unique=False)
    op.create_index('idx_session_turns_actor', 'session_turns', ['session_id', 'actor'], unique=False)
    op.create_index('idx_session_turns_artifact', 'session_turns', ['artifact_id'], unique=False)
    op.create_index('idx_session_turns_concept', 'session_turns', ['concept_id', sa.literal_column('created_at DESC')], unique=False, postgresql_where=sa.text('concept_id IS NOT NULL'))
    op.create_index('idx_agent_traces_created', 'agent_traces', [sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_agent_traces_model_created', 'agent_traces', ['model', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_agent_traces_user_created', 'agent_traces', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_assessment_responses_criterion', 'assessment_responses', ['rubric_criterion_id'], unique=False)
    op.create_index('idx_review_queue_artifact', 'content_review_queue', ['artifact_id'], unique=False)
    op.create_index('idx_review_queue_assigned', 'content_review_queue', ['assigned_to', 'status'], unique=False, postgresql_where=sa.text('assigned_to IS NOT NULL'))
    op.create_index('idx_review_queue_pending', 'content_review_queue', [sa.literal_column('severity DESC'), 'created_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index('idx_review_queue_turn', 'content_review_queue', ['session_turn_id'], unique=False)
    op.create_index('idx_journal_events_entry_time', 'journal_events', ['entry_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('idx_journal_events_session', 'journal_events', ['session_id'], unique=False)

    # --- functions that depend on tables existing --------------------------
    op.execute(BUMP_SESSION_COST)
    op.execute(REFRESH_SUBJECT_METADATA)

    # --- updated_at triggers -----------------------------------------------
    op.execute(
        "CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_user_profiles_updated_at BEFORE UPDATE ON user_profiles "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_subjects_updated_at BEFORE UPDATE ON subjects "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_concepts_updated_at BEFORE UPDATE ON concepts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_sources_updated_at BEFORE UPDATE ON sources "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_content_artifacts_updated_at BEFORE UPDATE ON content_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_rubric_criteria_updated_at BEFORE UPDATE ON rubric_criteria "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_learner_subjects_updated_at BEFORE UPDATE ON learner_subjects "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_concept_mastery_updated_at BEFORE UPDATE ON concept_mastery "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_learning_sessions_updated_at BEFORE UPDATE ON learning_sessions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_journal_entries_updated_at BEFORE UPDATE ON journal_entries "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_review_cards_updated_at BEFORE UPDATE ON review_cards "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_cost_ledger_updated_at BEFORE UPDATE ON cost_ledger "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_user_budget_caps_updated_at BEFORE UPDATE ON user_budget_caps "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_ingestion_jobs_updated_at BEFORE UPDATE ON ingestion_jobs "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_content_review_queue_updated_at BEFORE UPDATE ON content_review_queue "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_assessment_attempts_updated_at BEFORE UPDATE ON assessment_attempts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER trg_subject_metadata_updated_at BEFORE UPDATE ON subject_metadata "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        "CREATE TRIGGER trg_agent_traces_session_cost AFTER INSERT ON agent_traces "
        "FOR EACH ROW EXECUTE FUNCTION bump_session_cost()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_agent_traces_session_cost ON agent_traces")
    op.execute("DROP TRIGGER IF EXISTS trg_subject_metadata_updated_at ON subject_metadata")
    op.execute("DROP TRIGGER IF EXISTS trg_assessment_attempts_updated_at ON assessment_attempts")
    op.execute("DROP TRIGGER IF EXISTS trg_content_review_queue_updated_at ON content_review_queue")
    op.execute("DROP TRIGGER IF EXISTS trg_ingestion_jobs_updated_at ON ingestion_jobs")
    op.execute("DROP TRIGGER IF EXISTS trg_user_budget_caps_updated_at ON user_budget_caps")
    op.execute("DROP TRIGGER IF EXISTS trg_cost_ledger_updated_at ON cost_ledger")
    op.execute("DROP TRIGGER IF EXISTS trg_review_cards_updated_at ON review_cards")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entries_updated_at ON journal_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_learning_sessions_updated_at ON learning_sessions")
    op.execute("DROP TRIGGER IF EXISTS trg_concept_mastery_updated_at ON concept_mastery")
    op.execute("DROP TRIGGER IF EXISTS trg_learner_subjects_updated_at ON learner_subjects")
    op.execute("DROP TRIGGER IF EXISTS trg_rubric_criteria_updated_at ON rubric_criteria")
    op.execute("DROP TRIGGER IF EXISTS trg_content_artifacts_updated_at ON content_artifacts")
    op.execute("DROP TRIGGER IF EXISTS trg_sources_updated_at ON sources")
    op.execute("DROP TRIGGER IF EXISTS trg_concepts_updated_at ON concepts")
    op.execute("DROP TRIGGER IF EXISTS trg_subjects_updated_at ON subjects")
    op.execute("DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON user_profiles")
    op.execute("DROP TRIGGER IF EXISTS trg_users_updated_at ON users")

    op.execute("DROP FUNCTION IF EXISTS refresh_subject_metadata(uuid)")
    op.execute("DROP FUNCTION IF EXISTS bump_session_cost()")

    op.drop_table('journal_events')
    op.drop_table('content_review_queue')
    op.drop_table('assessment_responses')
    op.drop_table('agent_traces')
    op.drop_table('session_turns')
    op.drop_table('session_summaries')
    op.drop_table('review_events')
    op.drop_table('retrieval_checks')
    op.drop_table('portfolio_items')
    op.drop_table('mastery_events')
    op.drop_table('journal_entries')
    op.drop_table('assessment_attempts')
    op.drop_table('source_chunk_embeddings')
    op.drop_table('review_cards')
    op.drop_table('learning_sessions')
    op.drop_table('content_citations')
    op.drop_table('concept_mastery')
    op.drop_table('source_chunks')
    op.drop_table('rubric_criteria')
    op.drop_table('learner_subjects')
    op.drop_table('ingestion_jobs')
    op.drop_table('content_artifacts')
    op.drop_table('concept_sources')
    op.drop_table('concept_edges')
    op.drop_table('subject_metadata')
    op.drop_table('sources')
    op.drop_table('concepts')
    op.drop_table('user_profiles')
    op.drop_table('user_budget_caps')
    op.drop_table('subjects')
    op.drop_table('cost_ledger')
    op.drop_table('auth_sessions')
    op.drop_table('audit_log')
    op.drop_table('users')

    op.execute("DROP TYPE IF EXISTS review_status")
    op.execute("DROP TYPE IF EXISTS review_flag_source")
    op.execute("DROP TYPE IF EXISTS ingestion_job_status")
    op.execute("DROP TYPE IF EXISTS ingestion_job_kind")
    op.execute("DROP TYPE IF EXISTS portfolio_item_kind")
    op.execute("DROP TYPE IF EXISTS assessment_trigger")
    op.execute("DROP TYPE IF EXISTS assessment_mode")
    op.execute("DROP TYPE IF EXISTS journal_event_kind")
    op.execute("DROP TYPE IF EXISTS journal_status")
    op.execute("DROP TYPE IF EXISTS mastery_event_kind")
    op.execute("DROP TYPE IF EXISTS artifact_stance")
    op.execute("DROP TYPE IF EXISTS artifact_kind")
    op.execute("DROP TYPE IF EXISTS concept_source_role")
    op.execute("DROP TYPE IF EXISTS concept_edge_kind")
    op.execute("DROP TYPE IF EXISTS license_kind")
    op.execute("DROP TYPE IF EXISTS artifact_status")
    op.execute("DROP TYPE IF EXISTS session_mode")
    op.execute("DROP TYPE IF EXISTS agent_identity")

    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")
    # Extensions are left installed: they may be shared with other schemas in
    # the same database, and dropping "vector" would take the column types with
    # it if this migration is ever run against a non-dedicated database.
