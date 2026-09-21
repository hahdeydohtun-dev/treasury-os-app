import datetime
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    apply_resolved_entity_scope,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import _grants_permission, get_current_user, require_permission
from app.db.session import get_db
from app.models.entity import LegalEntity
from app.models.excel_hub import ExcelTemplate, ImportBatch, ImportBatchStatus
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.excel_hub import (
    ExcelTemplateOut,
    ImportBatchDetailOut,
    ImportBatchOut,
    ImportConfirmResult,
)
from app.services.audit_service import record_audit_event
from app.services.excel_service import (
    TemplateNotFoundError,
    TemplateVersionError,
    confirm_import,
    create_batch_from_upload,
)
from app.services.excel_templates import TEMPLATE_REGISTRY

router = APIRouter(prefix="/excel", tags=["excel-data-hub"])


@router.get("/templates", response_model=list[ExcelTemplateOut])
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[ExcelTemplate]:
    """
    Returns the seeded template registry rows (name/version/columns) for
    the frontend's "select a template" step. The registry is the source of
    truth for validation behavior (app/services/excel_templates.py); this
    table mirrors it for discoverability and version display.
    """
    result = await db.execute(select(ExcelTemplate).where(ExcelTemplate.is_active.is_(True)))
    return list(result.scalars().all())


@router.post("/uploads", response_model=ImportBatchDetailOut, status_code=status.HTTP_201_CREATED)
async def upload_excel_file(
    template_code: str = Form(...),
    template_version: int = Form(...),
    legal_entity_id: uuid.UUID | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportBatch:
    """
    SECTION 11 workflow steps 1-6: select template, upload, read, validate
    structure/data, check duplicates. Returns the batch with its full issue
    list so the frontend can render the preview immediately.

    A user without upload permission for the target entity is rejected
    before the file is even parsed (SECTION 24: "a user with Entity A
    access must not be able to upload data into Entity B").
    """
    if not _grants_permission(user, TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.UPLOAD,
                               legal_entity_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No upload permission for this entity.",
        )

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Only .xlsx/.xlsm files are supported.")

    file_bytes = await file.read()

    try:
        batch = await create_batch_from_upload(
            db,
            template_code=template_code,
            template_version=template_version,
            file_name=file.filename or "upload.xlsx",
            file_bytes=file_bytes,
            uploaded_by_user_id=user.id,
            legal_entity_id=legal_entity_id,
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TemplateVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await record_audit_event(
        db, module=TreasuryModule.EXCEL_DATA_HUB.value, action="UPLOAD",
        record_type="ImportBatch", record_id=str(batch.id), user_id=user.id,
        legal_entity_id=legal_entity_id,
        new_value={"file_name": batch.file_name, "template_code": template_code,
                   "total_rows": batch.total_rows},
    )
    await db.commit()
    await db.refresh(batch, attribute_names=["issues"])
    return batch


@router.get("/validation/{batch_id}", response_model=ImportBatchDetailOut)
async def get_validation_result(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportBatch:
    """The preview (SECTION 17): total/valid/invalid/warning/duplicate counts + issue list."""
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    # Detail-level authorization (Stage 2 Hardening): checked against the
    # BATCH's own entity, not a request query parameter - a batch_id
    # cannot be enumerated across entities merely because the caller
    # holds EXCEL_DATA_HUB:VIEW for *some* entity. Also correctly checks
    # group membership for GROUP_WIDE grants (unlike a bare
    # `_grants_permission` call).
    scope = await get_authorized_scope(db, user, TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.VIEW)
    if batch.legal_entity_id is not None:
        entity = await db.get(LegalEntity, batch.legal_entity_id)
        entity_group_id = entity.group_id if entity else None
    else:
        entity_group_id = None
    if not scope.allows_entity(batch.legal_entity_id, entity_group_id):
        raise HTTPException(status_code=403, detail="No permission for this import batch.")
    await db.refresh(batch, attribute_names=["issues"])
    return batch


@router.post("/imports/{batch_id}/confirm", response_model=ImportConfirmResult)
async def confirm_import_batch(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImportConfirmResult:
    """
    USER CONFIRMS IMPORT -> IMPORT TO POSTGRESQL -> IMPORT SUMMARY. A user
    without Import permission for the batch's entity cannot confirm it.
    """
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")

    scope = await get_authorized_scope(db, user, TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.IMPORT)
    if batch.legal_entity_id is not None:
        entity = await db.get(LegalEntity, batch.legal_entity_id)
        entity_group_id = entity.group_id if entity else None
    else:
        entity_group_id = None
    if not scope.allows_entity(batch.legal_entity_id, entity_group_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No import permission for this entity.",
        )

    # SECTION 11/TEST 10: even if the batch-level check passes, individual
    # ROWS referencing an entity outside the confirming user's authorized
    # scope must be rejected, not silently imported - a file can name any
    # entity in its own "Entity" column regardless of who uploaded it.
    resolved_ids = await resolve_scope_entity_ids(db, scope)

    try:
        imported, skipped = await confirm_import(db, batch, authorized_entity_ids=resolved_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await record_audit_event(
        db, module=TreasuryModule.EXCEL_DATA_HUB.value, action="IMPORT",
        record_type="ImportBatch", record_id=str(batch.id), user_id=user.id,
        legal_entity_id=batch.legal_entity_id,
        new_value={"imported_rows": imported, "skipped_rows": skipped,
                   "status": batch.status.value},
    )
    await db.commit()
    await db.refresh(batch)
    return ImportConfirmResult(batch=batch, imported_rows=imported, skipped_rows=skipped)


@router.get("/imports", response_model=list[ImportBatchOut])
async def list_import_history(
    legal_entity_id: uuid.UUID | None = None,
    template_code: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ImportBatch]:
    scope = await get_authorized_scope(db, user, TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No EXCEL_DATA_HUB:VIEW permission.")
    if legal_entity_id is not None and not scope.allows_entity(legal_entity_id):
        raise HTTPException(status_code=403, detail="No permission for this entity.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(ImportBatch).order_by(ImportBatch.uploaded_at.desc())
    stmt = apply_resolved_entity_scope(stmt, ImportBatch.legal_entity_id, resolved_ids)
    if legal_entity_id:
        stmt = stmt.where(ImportBatch.legal_entity_id == legal_entity_id)
    if template_code:
        stmt = stmt.where(ImportBatch.template_code == template_code)
    stmt = stmt.limit(min(limit, 200))
    result = await db.execute(stmt)
    return list(result.scalars().all())


class FreshnessEntry(BaseModel):
    template_code: str
    last_upload_at: datetime.datetime | None
    last_status: ImportBatchStatus | None
    last_imported_rows: int | None
    age_hours: float | None
    freshness_status: str  # CURRENT | STALE | NO_DATA


@router.get("/freshness", response_model=list[FreshnessEntry])
async def get_data_freshness(
    stale_after_hours: float = 24.0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(TreasuryModule.EXCEL_DATA_HUB, TreasuryAction.VIEW)),
) -> list[FreshnessEntry]:
    """
    SECTION 18: per data-type (template) freshness. `stale_after_hours` is
    the configurable threshold (default 24h) — pass a different value per
    the data type's real-world cadence if needed.
    """
    entries: list[FreshnessEntry] = []
    now = datetime.datetime.now(datetime.UTC)

    for template_code in TEMPLATE_REGISTRY:
        stmt = (
            select(ImportBatch)
            .where(
                ImportBatch.template_code == template_code,
                ImportBatch.status.in_(
                    [ImportBatchStatus.IMPORTED, ImportBatchStatus.PARTIALLY_IMPORTED]
                ),
            )
            .order_by(ImportBatch.uploaded_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        last_batch = result.scalars().first()

        if last_batch is None:
            entries.append(FreshnessEntry(
                template_code=template_code, last_upload_at=None, last_status=None,
                last_imported_rows=None, age_hours=None, freshness_status="NO_DATA",
            ))
            continue

        uploaded_at = last_batch.uploaded_at
        if uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.replace(tzinfo=datetime.UTC)
        age_hours = (now - uploaded_at).total_seconds() / 3600
        entries.append(FreshnessEntry(
            template_code=template_code,
            last_upload_at=last_batch.uploaded_at,
            last_status=last_batch.status,
            last_imported_rows=last_batch.imported_rows,
            age_hours=round(age_hours, 1),
            freshness_status="STALE" if age_hours > stale_after_hours else "CURRENT",
        ))

    return entries
