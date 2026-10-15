"""stage5e match decisions

Revision ID: f4c533db287d
Revises: 23ffdb804f43
Create Date: 2026-10-15 10:41:04.210132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f4c533db287d'
down_revision: Union[str, None] = '23ffdb804f43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Stage 5E: append-only record of explicit accept/reject decisions on engine proposals. Adds a
    # table only - no existing column, status or financial value is changed. One decision per
    # proposal is enforced by unique constraints, so a conflicting concurrent decision is refused by
    # the database itself.
    op.create_table(
        'reconciliation_match_decisions',
        sa.Column('reconciliation_run_id', sa.UUID(), nullable=False),
        sa.Column('legal_entity_id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.Enum('SUGGESTION', 'GROUP', name='matchproposalkind'), nullable=False),
        sa.Column('suggestion_id', sa.UUID(), nullable=True),
        sa.Column('group_id', sa.UUID(), nullable=True),
        sa.Column('decision', sa.Enum('ACCEPTED', 'REJECTED', name='matchdecisiontype'), nullable=False),
        sa.Column('previous_status', sa.String(length=20), nullable=False),
        sa.Column('new_status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('decided_by_user_id', sa.UUID(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('(suggestion_id IS NOT NULL)::int + (group_id IS NOT NULL)::int = 1',
                           name='ck_reconciliation_match_decision_exactly_one_subject'),
        sa.ForeignKeyConstraint(['decided_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['group_id'], ['reconciliation_match_groups.id']),
        sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id']),
        sa.ForeignKeyConstraint(['reconciliation_run_id'], ['reconciliation_runs.id']),
        sa.ForeignKeyConstraint(['suggestion_id'], ['reconciliation_match_suggestions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('suggestion_id', name='uq_reconciliation_match_decision_suggestion'),
        sa.UniqueConstraint('group_id', name='uq_reconciliation_match_decision_group'),
    )
    op.create_index(op.f('ix_reconciliation_match_decisions_reconciliation_run_id'),
                    'reconciliation_match_decisions', ['reconciliation_run_id'])
    op.create_index(op.f('ix_reconciliation_match_decisions_legal_entity_id'),
                    'reconciliation_match_decisions', ['legal_entity_id'])
    op.create_index(op.f('ix_reconciliation_match_decisions_decision'),
                    'reconciliation_match_decisions', ['decision'])


def downgrade() -> None:
    # Drops the decision table only. This is reconciliation workflow history, not financial data;
    # the proposals' ACCEPTED/REJECTED statuses (set by decisions) stay on the proposal rows, so a
    # downgrade after decisions exist leaves statuses without their decision records - restore from
    # backup if that history matters.
    op.drop_table('reconciliation_match_decisions')
    op.execute("DROP TYPE IF EXISTS matchdecisiontype")
    op.execute("DROP TYPE IF EXISTS matchproposalkind")
