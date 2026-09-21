import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.banking import BankAccountStatus
from app.models.lookup import CashDirection


class AccountTypeOut(BaseModel):
    code: str
    name: str
    description: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class CashEventTypeOut(BaseModel):
    code: str
    name: str
    default_direction: CashDirection
    description: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class BankCreate(BaseModel):
    name: str
    swift_code: str | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)


class BankOut(BaseModel):
    id: uuid.UUID
    name: str
    swift_code: str | None
    country: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class BankAccountCreate(BaseModel):
    legal_entity_id: uuid.UUID
    bank_id: uuid.UUID
    account_name: str
    account_number: str
    currency_code: str = Field(min_length=3, max_length=3)
    account_type_code: str
    status: BankAccountStatus = BankAccountStatus.ACTIVE
    opening_date: datetime.date | None = None
    closing_date: datetime.date | None = None
    minimum_operating_balance: Decimal | None = None
    overdraft_limit: Decimal | None = None
    gl_reference: str | None = None


class BankAccountOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    bank_id: uuid.UUID
    account_name: str
    account_number_masked: str
    currency_code: str
    account_type_code: str
    status: BankAccountStatus
    opening_date: datetime.date | None
    closing_date: datetime.date | None
    minimum_operating_balance: Decimal | None
    overdraft_limit: Decimal | None
    is_active: bool

    model_config = {"from_attributes": True}

    @staticmethod
    def mask_account_number(account_number: str) -> str:
        if len(account_number) <= 4:
            return "*" * len(account_number)
        return "*" * (len(account_number) - 4) + account_number[-4:]
