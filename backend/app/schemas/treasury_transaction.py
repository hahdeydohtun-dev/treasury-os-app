import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.lookup import CashDirection
from app.models.treasury_transaction import TransactionStatus


class TreasuryTransactionCreate(BaseModel):
    legal_entity_id: uuid.UUID
    business_unit_id: uuid.UUID | None = None
    event_type_code: str
    direction: CashDirection | None = None  # defaults from event_type if omitted
    event_date: datetime.date
    value_date: datetime.date | None = None
    posting_date: datetime.date | None = None

    transaction_currency_code: str = Field(min_length=3, max_length=3)
    transaction_amount: Decimal
    functional_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    functional_amount: Decimal | None = None
    reporting_currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    reporting_amount: Decimal | None = None

    exchange_rate: Decimal | None = None
    exchange_rate_type: str | None = None
    exchange_rate_date: datetime.date | None = None
    exchange_rate_source: str | None = None

    bank_id: uuid.UUID | None = None
    bank_account_id: uuid.UUID | None = None
    transfer_pair_id: uuid.UUID | None = None

    counterparty: str | None = None
    reference: str | None = None
    external_reference: str | None = None
    narration: str | None = None

    source_type: str = "MANUAL"
    source_file: str | None = None
    source_record_id: str | None = None


class TreasuryTransactionOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    business_unit_id: uuid.UUID | None
    event_type_code: str
    direction: CashDirection
    event_date: datetime.date
    value_date: datetime.date | None
    transaction_currency_code: str
    transaction_amount: Decimal
    functional_currency_code: str | None
    functional_amount: Decimal | None
    reporting_currency_code: str | None
    reporting_amount: Decimal | None
    bank_id: uuid.UUID | None
    bank_account_id: uuid.UUID | None
    transfer_pair_id: uuid.UUID | None
    counterparty: str | None
    reference: str | None
    external_reference: str | None
    narration: str | None
    status: TransactionStatus
    source_type: str
    source_file: str | None
    source_record_id: str | None
    import_batch_id: uuid.UUID | None

    model_config = {"from_attributes": True}
