import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.expected_cash_flow import ForecastItemStatus


class ExpectedCollectionCreate(BaseModel):
    legal_entity_id: uuid.UUID
    expected_date: datetime.date
    currency_code: str = Field(min_length=3, max_length=3)
    amount: Decimal
    counterparty: str | None = None
    reference: str | None = None
    category: str | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    status: ForecastItemStatus = ForecastItemStatus.OPEN
    notes: str | None = None
    source_type: str = "MANUAL"


class ExpectedCollectionOut(ExpectedCollectionCreate):
    id: uuid.UUID
    import_batch_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class ExpectedPaymentCreate(BaseModel):
    legal_entity_id: uuid.UUID
    expected_date: datetime.date
    currency_code: str = Field(min_length=3, max_length=3)
    amount: Decimal
    counterparty: str | None = None
    reference: str | None = None
    category: str | None = None
    priority: str | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    status: ForecastItemStatus = ForecastItemStatus.OPEN
    notes: str | None = None
    source_type: str = "MANUAL"


class ExpectedPaymentOut(ExpectedPaymentCreate):
    id: uuid.UUID
    import_batch_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class BankChargeCreate(BaseModel):
    legal_entity_id: uuid.UUID
    bank_id: uuid.UUID
    bank_account_id: uuid.UUID
    charge_date: datetime.date
    currency_code: str = Field(min_length=3, max_length=3)
    amount: Decimal
    charge_type: str | None = None
    reference: str | None = None
    description: str | None = None
    source_type: str = "MANUAL"


class BankChargeOut(BankChargeCreate):
    id: uuid.UUID
    import_batch_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}
