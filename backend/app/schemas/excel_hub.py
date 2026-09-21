import datetime
import uuid

from pydantic import BaseModel

from app.models.excel_hub import ImportBatchStatus, IssueSeverity


class ExcelTemplateOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    version: int
    is_active: bool
    required_columns: list[str]
    optional_columns: list[str]

    model_config = {"from_attributes": True}


class ImportIssueOut(BaseModel):
    row_number: int
    column_name: str | None
    value: str | None
    severity: IssueSeverity
    error_code: str
    message: str

    model_config = {"from_attributes": True}


class ImportBatchOut(BaseModel):
    id: uuid.UUID
    template_code: str
    template_version: int
    legal_entity_id: uuid.UUID | None
    file_name: str
    uploaded_by_user_id: uuid.UUID
    uploaded_at: datetime.datetime
    status: ImportBatchStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    warning_rows: int
    imported_rows: int
    duplicate_rows: int

    model_config = {"from_attributes": True}


class ImportBatchDetailOut(ImportBatchOut):
    issues: list[ImportIssueOut] = []


class ImportPreviewRow(BaseModel):
    row_number: int
    data: dict
    is_valid: bool
    is_duplicate: bool


class ImportConfirmResult(BaseModel):
    batch: ImportBatchOut
    imported_rows: int
    skipped_rows: int
