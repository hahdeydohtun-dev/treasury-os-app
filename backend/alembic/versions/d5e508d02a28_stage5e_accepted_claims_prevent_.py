"""stage5e accepted claims prevent duplicate acceptance

Revision ID: d5e508d02a28
Revises: f4c533db287d
Create Date: 2026-10-15 15:14:54.920426

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e508d02a28'
down_revision: Union[str, None] = 'f4c533db287d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CLAIMS_SQL = """
    SELECT d.id AS decision_id, d.legal_entity_id, 'MATCH' AS kind,
           s.bank_statement_transaction_id AS bank_id, NULL::uuid AS ledger_id
      FROM reconciliation_match_decisions d JOIN reconciliation_match_suggestions s ON s.id = d.suggestion_id
     WHERE d.decision = 'ACCEPTED' AND s.bank_statement_transaction_id IS NOT NULL
    UNION ALL
    SELECT d.id, d.legal_entity_id, 'MATCH', NULL::uuid, s.treasury_transaction_id
      FROM reconciliation_match_decisions d JOIN reconciliation_match_suggestions s ON s.id = d.suggestion_id
     WHERE d.decision = 'ACCEPTED' AND s.treasury_transaction_id IS NOT NULL
    UNION ALL
    SELECT d.id, d.legal_entity_id,
           CASE WHEN g.relationship_type = 'INTERNAL_TRANSFER' THEN 'TRANSFER' ELSE 'MATCH' END,
           m.bank_statement_transaction_id, m.treasury_transaction_id
      FROM reconciliation_match_decisions d
      JOIN reconciliation_match_groups g ON g.id = d.group_id
      JOIN reconciliation_match_group_members m ON m.match_group_id = g.id
     WHERE d.decision = 'ACCEPTED'
"""


def upgrade() -> None:
    # Stage 5E final integrity: a transaction must not be accepted through two proposals. This table holds one
    # row per transaction per ACCEPTED proposal; the partial unique indexes make a conflicting acceptance
    # impossible at the database level (even across runs, even under concurrent requests).
    #
    # Pre-flight: decisions made BEFORE this migration were never checked for overlap. If the existing
    # ACCEPTED decisions already contain a conflict, stop with an explicit error and change nothing - an
    # operator must decide which acceptance stands; this migration will not silently pick one.
    conflicts = op.get_bind().execute(sa.text(f"""
        WITH claims AS ({_CLAIMS_SQL})
        SELECT kind, bank_id, ledger_id, array_agg(decision_id::text) AS decisions
          FROM claims GROUP BY kind, bank_id, ledger_id HAVING count(*) > 1
    """)).fetchall()
    if conflicts:
        raise RuntimeError(
            "Cannot add accepted-claim uniqueness: existing ACCEPTED decisions already claim the same "
            f"transaction more than once: {[tuple(r) for r in conflicts]}. Resolve these first."
        )

    op.create_table(
        'reconciliation_accepted_claims',
        sa.Column('decision_id', sa.UUID(), nullable=False),
        sa.Column('legal_entity_id', sa.UUID(), nullable=False),
        sa.Column('claim_kind', sa.Enum('MATCH', 'TRANSFER', name='acceptedclaimkind'), nullable=False),
        sa.Column('bank_statement_transaction_id', sa.UUID(), nullable=True),
        sa.Column('treasury_transaction_id', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            '(bank_statement_transaction_id IS NOT NULL)::int + (treasury_transaction_id IS NOT NULL)::int = 1',
            name='ck_reconciliation_accepted_claim_exactly_one_transaction'),
        sa.ForeignKeyConstraint(['decision_id'], ['reconciliation_match_decisions.id']),
        sa.ForeignKeyConstraint(['legal_entity_id'], ['legal_entities.id']),
        sa.ForeignKeyConstraint(['bank_statement_transaction_id'], ['bank_statement_transactions.id']),
        sa.ForeignKeyConstraint(['treasury_transaction_id'], ['treasury_transactions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reconciliation_accepted_claims_decision_id'), 'reconciliation_accepted_claims', ['decision_id'])
    op.create_index(op.f('ix_reconciliation_accepted_claims_legal_entity_id'), 'reconciliation_accepted_claims', ['legal_entity_id'])
    # The guarantee: at most one accepted claim per transaction per namespace. Partial indexes, because exactly
    # one of the two transaction columns is NULL on every row.
    op.execute("CREATE UNIQUE INDEX ux_accepted_claim_bank ON reconciliation_accepted_claims "
               "(bank_statement_transaction_id, claim_kind) WHERE bank_statement_transaction_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX ux_accepted_claim_ledger ON reconciliation_accepted_claims "
               "(treasury_transaction_id, claim_kind) WHERE treasury_transaction_id IS NOT NULL")

    # Backfill claims for decisions already ACCEPTED (they were validated above to be conflict-free).
    op.execute(f"""
        INSERT INTO reconciliation_accepted_claims
            (id, decision_id, legal_entity_id, claim_kind, bank_statement_transaction_id, treasury_transaction_id)
        SELECT gen_random_uuid(), decision_id, legal_entity_id, kind::acceptedclaimkind, bank_id, ledger_id
          FROM ({_CLAIMS_SQL}) AS claims
    """)


def downgrade() -> None:
    # Drops the claims table (workflow bookkeeping, no financial data). Proposals keep their ACCEPTED status and
    # decision records; only the cross-proposal uniqueness guarantee is removed by a downgrade.
    op.drop_table('reconciliation_accepted_claims')
    op.execute("DROP TYPE IF EXISTS acceptedclaimkind")
