"""stage5g adaptive score explanations

Revision ID: a7c1e9b4d2f6
Revises: d5e508d02a28
Create Date: 2026-11-18 09:00:00.000000

One new table holding the immutable explanation of an adaptive recommendation per Stage 5C suggestion. No existing table,
column, constraint or index is changed. Two triggers make the table append-only and scope-consistent at the database level.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7c1e9b4d2f6'
down_revision: Union[str, None] = 'd5e508d02a28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reconciliation_adaptive_scores',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reconciliation_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('legal_entity_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('bank_account_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('currency_code', sa.String(length=3), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('match_type', sa.String(length=30), nullable=False),
        sa.Column('deterministic_score', sa.Numeric(6, 2), nullable=False),
        sa.Column('adaptive_contribution', sa.Numeric(6, 2), nullable=False),
        sa.Column('recommendation_score', sa.Numeric(6, 2), nullable=False),
        sa.Column('score_band', sa.String(length=10), nullable=False),
        sa.Column('fallback', sa.Boolean(), nullable=False),
        sa.Column('fallback_reason', sa.Text(), nullable=True),
        sa.Column('support_level', sa.String(length=10), nullable=False),
        sa.Column('accepted_examples', sa.Integer(), nullable=False),
        sa.Column('rejected_examples', sa.Integer(), nullable=False),
        sa.Column('learning_rule_version', sa.String(length=20), nullable=False),
        sa.Column('base_rule_version', sa.String(length=50), nullable=False),
        sa.Column('configuration_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('configuration_version', sa.Integer(), nullable=True),
        sa.Column('evidence_cutoff', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settings_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('deterministic_score >= 0 AND deterministic_score <= 100', name='ck_adaptive_score_deterministic_range'),
        sa.CheckConstraint('recommendation_score >= 0 AND recommendation_score <= 100', name='ck_adaptive_score_recommendation_range'),
        sa.CheckConstraint('adaptive_contribution >= -15 AND adaptive_contribution <= 15', name='ck_adaptive_score_contribution_ceiling'),
        sa.CheckConstraint("score_band IN ('AUTO', 'REVIEW')", name='ck_adaptive_score_band'),
        sa.CheckConstraint("support_level IN ('NONE', 'LOW', 'MEDIUM', 'HIGH')", name='ck_adaptive_score_support_level'),
        sa.CheckConstraint('accepted_examples >= 0 AND rejected_examples >= 0', name='ck_adaptive_score_example_counts'),
        sa.CheckConstraint(
            'NOT fallback OR (adaptive_contribution = 0 AND recommendation_score = deterministic_score AND fallback_reason IS NOT NULL)',
            name='ck_adaptive_score_fallback_is_deterministic'),
        sa.ForeignKeyConstraint(['suggestion_id'], ['reconciliation_match_suggestions.id']),
        sa.ForeignKeyConstraint(['reconciliation_run_id'], ['reconciliation_runs.id']),
        sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id']),
        sa.ForeignKeyConstraint(['bank_account_id'], ['bank_accounts.id']),
        sa.ForeignKeyConstraint(['configuration_id'], ['reconciliation_configurations.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('suggestion_id', name='uq_reconciliation_adaptive_score_suggestion'),
    )
    op.create_index('ix_reconciliation_adaptive_scores_reconciliation_run_id', 'reconciliation_adaptive_scores', ['reconciliation_run_id'])
    op.create_index('ix_adaptive_scores_scope', 'reconciliation_adaptive_scores', ['legal_entity_id', 'bank_account_id'])

    # Append-only: a stored explanation can never be edited or removed (history must stay reproducible).
    op.execute("""
        CREATE FUNCTION reconciliation_adaptive_scores_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reconciliation_adaptive_scores rows are immutable (% refused)', TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_reconciliation_adaptive_scores_immutable
        BEFORE UPDATE OR DELETE ON reconciliation_adaptive_scores
        FOR EACH ROW EXECUTE FUNCTION reconciliation_adaptive_scores_immutable()
    """)
    # Scope isolation: the run, entity and bank account on a row must be the suggestion's own.
    op.execute("""
        CREATE FUNCTION reconciliation_adaptive_scores_scope_check() RETURNS trigger AS $$
        DECLARE
            owner RECORD;
        BEGIN
            SELECT s.reconciliation_run_id AS run_id, r.legal_entity_id AS entity_id, r.bank_account_id AS account_id
              INTO owner
              FROM reconciliation_match_suggestions s
              JOIN reconciliation_runs r ON r.id = s.reconciliation_run_id
             WHERE s.id = NEW.suggestion_id;
            IF NOT FOUND OR owner.run_id <> NEW.reconciliation_run_id
               OR owner.entity_id <> NEW.legal_entity_id OR owner.account_id <> NEW.bank_account_id THEN
                RAISE EXCEPTION 'adaptive score scope does not match the scope of its suggestion run'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_reconciliation_adaptive_scores_scope_check
        BEFORE INSERT ON reconciliation_adaptive_scores
        FOR EACH ROW EXECUTE FUNCTION reconciliation_adaptive_scores_scope_check()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_reconciliation_adaptive_scores_scope_check ON reconciliation_adaptive_scores")
    op.execute("DROP TRIGGER IF EXISTS trg_reconciliation_adaptive_scores_immutable ON reconciliation_adaptive_scores")
    op.execute("DROP FUNCTION IF EXISTS reconciliation_adaptive_scores_scope_check()")
    op.execute("DROP FUNCTION IF EXISTS reconciliation_adaptive_scores_immutable()")
    op.drop_index('ix_adaptive_scores_scope', table_name='reconciliation_adaptive_scores')
    op.drop_index('ix_reconciliation_adaptive_scores_reconciliation_run_id', table_name='reconciliation_adaptive_scores')
    op.drop_table('reconciliation_adaptive_scores')
