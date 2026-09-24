"""
Bank statement evidence layer (Stage 5A).

A `BankStatementTransaction` is EXTERNAL BANK EVIDENCE - what the bank
itself reports happened on an account. It is deliberately NOT a
`TreasuryTransaction` (the internal treasury cash-event ledger) and NOT a
`BankBalance` (the reported balance snapshot). Stage 5A introduces no
second cash ledger: this table is read-only evidence, produced solely by
importing a bank statement through the Excel Data Hub, and it has no
effect whatsoever on `TreasuryTransaction`, `BankBalance`, operational
available cash, or any investment/facility cash calculation. Matching
this evidence against `TreasuryTransaction` rows is explicitly Stage 5C's
job, not Stage 5A's - see docs/STAGE_5A_BANK_STATEMENT_INGESTION.md.

The partial unique index enforcing exact-duplicate protection for
strong-identity rows (`ux_bank_statement_transactions_strong_duplicate_key`)
is hand-written directly into its migration, not declared here - the
same established convention as every other partial unique index in this
codebase (SQLAlchemy's autogenerate does not reflect
`postgresql_where`-qualified indexes correctly).
"""
import datetime
import uuid
from decimal import Decimal
from enum import Enum

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class BankStatementEntryType(str, Enum):
    """
    The bank's own DEBIT/CREDIT convention for the account being
    statemented - deliberately kept distinct from
    `app.models.lookup.CashDirection` (INFLOW/OUTFLOW). A bank statement
    is external evidence in the bank's own vocabulary; only a future
    matching stage translates it against Treasury OS's own cash-direction
    concept, never this ingestion layer.
    """
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class BankStatementTransactionStatus(str, Enum):
    """
    ACTIVE is the only status Stage 5A itself ever sets. REVERSED is
    reserved for a future correction workflow (the Stage 5A spec
    mentions "corrected/deleted if such operations are supported") -
    Stage 5A does not implement that workflow, so no code path other than
    a manual/administrative future addition ever sets REVERSED.
    """
    ACTIVE = "ACTIVE"
    REVERSED = "REVERSED"


class BankStatementTransaction(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "bank_statement_transactions"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    bank_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("banks.id"), nullable=False, index=True
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False, index=True
    )

    # --- Statement period this row was declared under (SECTION 7/13) ---
    statement_period_start: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    statement_period_end: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    # --- Dates (SECTION 5/10): three distinct dates, never conflated ---
    transaction_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    value_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True, index=True)
    posting_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    entry_type: Mapped[BankStatementEntryType] = mapped_column(nullable=False)
    # Always positive; direction is carried by entry_type, never by sign,
    # mirroring the same convention TreasuryTransaction already uses.
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False, index=True
    )
    balance_after_transaction: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    bank_reference: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    external_transaction_id: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)
    narration: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Duplicate identity (SECTION 11/12) ---
    duplicate_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    has_strong_identity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[BankStatementTransactionStatus] = mapped_column(
        default=BankStatementTransactionStatus.ACTIVE, nullable=False, index=True
    )

    # --- Source traceability (SECTION 17) - mandatory, never discarded ---
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=False, index=True
    )
    source_row_number: Mapped[int] = mapped_column(nullable=False)
