import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.currency import FXRateType


class CurrencyCreate(BaseModel):
    code: str = Field(min_length=3, max_length=3)
    name: str
    symbol: str | None = None
    decimal_places: int = 2
    is_base_currency: bool = False


class CurrencyOut(BaseModel):
    code: str
    name: str
    symbol: str | None
    decimal_places: int
    is_base_currency: bool
    is_active: bool

    model_config = {"from_attributes": True}


class FXRateCreate(BaseModel):
    from_currency_code: str = Field(min_length=3, max_length=3)
    to_currency_code: str = Field(min_length=3, max_length=3)
    rate_type: FXRateType = FXRateType.SPOT
    rate_date: datetime.date
    rate: Decimal
    rate_source: str = "MANUAL"


class FXRateOut(BaseModel):
    id: uuid.UUID
    from_currency_code: str
    to_currency_code: str
    rate_type: FXRateType
    rate_date: datetime.date
    rate: Decimal
    rate_source: str
    version: int
    is_current: bool

    model_config = {"from_attributes": True}
