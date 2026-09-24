import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.models.bank_statement import BankStatementEntryType, BankStatementTransactionStatus


class BankStatementTransactionOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_id: uuid.UUID
    bank_account_id: uuid.UUID
    statement_period_start: datetime.date
    statement_period_end: datetime.date
    transaction_date: datetime.date
    value_date: datetime.date | None
    posting_date: datetime.date | None
    entry_type: BankStatementEntryType
    amount: Decimal
    currency_code: str
    balance_after_transaction: Decimal | None
    bank_reference: str | None
    external_transaction_id: str | None
    narration: str | None
    has_strong_identity: bool
    status: BankStatementTransactionStatus
    import_batch_id: uuid.UUID
    source_row_number: int

    model_config = {"from_attributes": True}
