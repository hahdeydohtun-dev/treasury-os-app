"""
Excel Data Hub foundation (SECTION 10-19).

ExcelTemplate is the versioned registry of supported upload templates.
ImportBatch is the central lifecycle object for one upload-to-import run;
`raw_rows` holds the parsed (but not yet committed) spreadsheet data as
JSON so the workflow can go upload -> validate -> preview -> confirm
without re-uploading the file, while nothing lands in the real domain
tables until the user confirms. ImportIssue is the per-row/column
validation error or warning shown in the preview.
"""
import datetime
import uuid
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin, UUIDPKMixin


class ImportBatchStatus(str, Enum):
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    READY_FOR_IMPORT = "READY_FOR_IMPORT"
    IMPORTING = "IMPORTING"
    IMPORTED = "IMPORTED"
    PARTIALLY_IMPORTED = "PARTIALLY_IMPORTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IssueSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ExcelTemplate(Base, TimestampMixin):
    """
    Versioned template registry (SECTION 13). `code` is the stable
    identifier used by the API/frontend; `version` is bumped whenever the
    required/optional column set changes. Uploads referencing an
    unsupported (code, version) pair are rejected outright.
    """
    __tablename__ = "excel_templates"
    __table_args__ = ()

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    required_columns: Mapped[list] = mapped_column(JSONB, nullable=False)
    optional_columns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class ImportBatch(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "import_batches"

    template_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    template_version: Mapped[int] = mapped_column(nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    uploaded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), default="EXCEL_UPLOAD", nullable=False)
    status: Mapped[ImportBatchStatus] = mapped_column(
        default=ImportBatchStatus.UPLOADED, nullable=False, index=True
    )

    total_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    valid_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    invalid_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    warning_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    imported_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(default=0, nullable=False)

    # Parsed spreadsheet rows staged here between validate and confirm-import;
    # nothing lands in a domain table until the import is confirmed.
    raw_rows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    issues: Mapped[list["ImportIssue"]] = relationship(
        back_populates="import_batch", cascade="all, delete-orphan"
    )


class ImportIssue(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "import_issues"

    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(nullable=False)
    column_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    severity: Mapped[IssueSeverity] = mapped_column(nullable=False)
    error_code: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)

    import_batch: Mapped["ImportBatch"] = relationship(back_populates="issues")
