"""stage5e open item workflow

Revision ID: 23ffdb804f43
Revises: 4792dd5b9d52
Create Date: 2026-10-15 09:25:26.544353

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '23ffdb804f43'
down_revision: Union[str, None] = '4792dd5b9d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Stage 5E. NOTE: autogenerate also proposed dropping/recreating several
    # hand-written partial unique indexes from earlier stages (it cannot reflect
    # postgresql_where indexes); those lines were removed deliberately - none of
    # them are touched here.
    op.execute("ALTER TYPE openitemcategory ADD VALUE IF NOT EXISTS 'AMBIGUOUS_MATCH'")

    priority = sa.Enum('LOW', 'MEDIUM', 'HIGH', name='openitempriority')
    priority.create(op.get_bind(), checkfirst=True)

    # New NOT NULL columns are added nullable, backfilled for any pre-existing
    # row (application code never created open items before Stage 5E, so this is
    # a safeguard), then tightened - historical rows are preserved, never dropped.
    op.add_column('reconciliation_open_items', sa.Column('source_key', sa.String(length=120), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('item_date', sa.Date(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column(
        'priority', priority, nullable=False, server_default='MEDIUM'))
    op.execute("UPDATE reconciliation_open_items SET source_key = 'LEGACY:' || id::text WHERE source_key IS NULL")
    op.execute("UPDATE reconciliation_open_items SET item_date = created_at::date WHERE item_date IS NULL")
    op.alter_column('reconciliation_open_items', 'source_key', nullable=False)
    op.alter_column('reconciliation_open_items', 'item_date', nullable=False)
    op.alter_column('reconciliation_open_items', 'priority', server_default=None)

    op.add_column('reconciliation_open_items', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('assigned_by_user_id', sa.UUID(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('resolved_by_user_id', sa.UUID(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('resolution_reason', sa.Text(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('closed_by_user_id', sa.UUID(), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('reconciliation_open_items', sa.Column('rejection_reason', sa.Text(), nullable=True))
    op.create_index(op.f('ix_reconciliation_open_items_item_date'), 'reconciliation_open_items', ['item_date'])
    op.create_index(op.f('ix_reconciliation_open_items_priority'), 'reconciliation_open_items', ['priority'])
    op.create_unique_constraint(
        'uq_reconciliation_open_item_source', 'reconciliation_open_items', ['reconciliation_run_id', 'source_key'])
    for column in ('assigned_by_user_id', 'resolved_by_user_id', 'closed_by_user_id'):
        op.create_foreign_key(f'fk_reconciliation_open_items_{column}', 'reconciliation_open_items',
                              'users', [column], ['id'])

    op.create_table(
        'reconciliation_open_item_events',
        sa.Column('open_item_id', sa.UUID(), nullable=False),
        sa.Column('legal_entity_id', sa.UUID(), nullable=False),
        sa.Column('event_type', sa.Enum(
            'CREATED', 'ASSIGNED', 'REASSIGNED', 'INVESTIGATION_STARTED', 'COMMENT', 'EVIDENCE',
            'RESOLVED', 'CLOSED', 'REJECTED', 'REOPENED', name='openitemeventtype'), nullable=False),
        sa.Column('actor_user_id', sa.UUID(), nullable=True),
        sa.Column('from_status', postgresql.ENUM(name='openitemstatus', create_type=False), nullable=True),
        sa.Column('to_status', postgresql.ENUM(name='openitemstatus', create_type=False), nullable=True),
        sa.Column('assignee_user_id', sa.UUID(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('reference', sa.String(length=255), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['assignee_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id']),
        sa.ForeignKeyConstraint(['open_item_id'], ['reconciliation_open_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reconciliation_open_item_events_legal_entity_id'),
                    'reconciliation_open_item_events', ['legal_entity_id'])
    op.create_index(op.f('ix_reconciliation_open_item_events_open_item_id'),
                    'reconciliation_open_item_events', ['open_item_id'])


def downgrade() -> None:
    # Reverts the Stage 5E SCHEMA only. This removes the workflow history table and the
    # workflow columns (assignment/resolution state): that is operational workflow data,
    # not financial data - no TreasuryTransaction/BankStatementTransaction/BankBalance
    # value is touched. PostgreSQL cannot remove an enum value, so
    # openitemcategory keeps 'AMBIGUOUS_MATCH' (harmless once nothing references it).
    op.drop_table('reconciliation_open_item_events')
    for column in ('assigned_by_user_id', 'resolved_by_user_id', 'closed_by_user_id'):
        op.drop_constraint(f'fk_reconciliation_open_items_{column}', 'reconciliation_open_items', type_='foreignkey')
    op.drop_constraint('uq_reconciliation_open_item_source', 'reconciliation_open_items', type_='unique')
    op.drop_index(op.f('ix_reconciliation_open_items_priority'), table_name='reconciliation_open_items')
    op.drop_index(op.f('ix_reconciliation_open_items_item_date'), table_name='reconciliation_open_items')
    for column in ('rejection_reason', 'rejected_at', 'closed_by_user_id', 'closed_at', 'resolution_reason',
                   'resolved_by_user_id', 'assigned_at', 'assigned_by_user_id', 'due_date', 'priority',
                   'item_date', 'source_key'):
        op.drop_column('reconciliation_open_items', column)
    op.execute("DROP TYPE IF EXISTS openitemeventtype")
    op.execute("DROP TYPE IF EXISTS openitempriority")
