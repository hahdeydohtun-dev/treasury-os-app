import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class BankBalanceCreate(BaseModel):
    bank_account_id: uuid.UUID
    balance_date: datetime.date
    currency_code: str = Field(min_length=3, max_length=3)
    opening_balance: Decimal | None = None
    closing_balance: Decimal
    available_balance: Decimal | None = None
    ledger_balance: Decimal | None = None
    source: str = "MANUAL"


class BankBalanceOut(BaseModel):
    id: uuid.UUID
    bank_account_id: uuid.UUID
    balance_date: datetime.date
    currency_code: str
    opening_balance: Decimal | None
    closing_balance: Decimal
    available_balance: Decimal | None
    ledger_balance: Decimal | None
    source: str
    import_batch_id: uuid.UUID | None

    model_config = {"from_attributes": True}
