"""add funding action integrity constraints

Revision ID: 84d4f574d592
Revises: c56185410e6b
Create Date: 2026-09-19 01:18:55.304629

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '84d4f574d592'
down_revision: Union[str, None] = 'c56185410e6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SECTION 1: at most one of linked_drawdown_id / linked_repayment_id
    # may be set on a given FundingAction.
    op.create_check_constraint(
        "ck_funding_action_single_link",
        "funding_actions",
        "NOT (linked_drawdown_id IS NOT NULL AND linked_repayment_id IS NOT NULL)",
    )
    # SECTION 6: each drawdown/repayment may be linked from at most one
    # FundingAction - partial unique indexes (only enforced where the
    # link is actually set, since most rows will have NULL here).
    op.create_index(
        "ux_funding_actions_linked_drawdown_id", "funding_actions", ["linked_drawdown_id"],
        unique=True, postgresql_where=sa.text("linked_drawdown_id IS NOT NULL"),
    )
    op.create_index(
        "ux_funding_actions_linked_repayment_id", "funding_actions", ["linked_repayment_id"],
        unique=True, postgresql_where=sa.text("linked_repayment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_funding_actions_linked_repayment_id", table_name="funding_actions")
    op.drop_index("ux_funding_actions_linked_drawdown_id", table_name="funding_actions")
    op.drop_constraint("ck_funding_action_single_link", "funding_actions", type_="check")
