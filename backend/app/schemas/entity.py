import uuid

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    name: str
    code: str = Field(min_length=1, max_length=50)
    reporting_currency_code: str = Field(min_length=3, max_length=3)
    description: str | None = None


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    reporting_currency_code: str
    description: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class LegalEntityCreate(BaseModel):
    group_id: uuid.UUID
    name: str
    code: str = Field(min_length=1, max_length=50)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    functional_currency_code: str = Field(min_length=3, max_length=3)
    registration_number: str | None = None
    tax_id: str | None = None


class LegalEntityOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    name: str
    code: str
    country: str | None
    functional_currency_code: str
    is_active: bool

    model_config = {"from_attributes": True}
