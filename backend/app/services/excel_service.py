"""
Excel ingestion service — implements the SECTION 11 workflow:

  SELECT TEMPLATE -> UPLOAD -> READ FILE -> VALIDATE STRUCTURE ->
  VALIDATE DATA -> CHECK DUPLICATES -> SHOW PREVIEW -> USER CONFIRMS ->
  IMPORT TO POSTGRESQL -> IMPORT SUMMARY

Nothing lands in a domain table until `confirm_import` runs. Up to that
point everything lives in `ImportBatch.raw_rows` (parsed) and
`ImportIssue` rows (validation results), so the preview reflects exactly
what will be imported.
"""
import datetime
import io
import uuid

import openpyxl
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.excel_hub import ImportBatch, ImportBatchStatus, ImportIssue, IssueSeverity
from app.services.excel_templates import TEMPLATE_REGISTRY, TemplateSpec


class TemplateNotFoundError(Exception):
    pass


class TemplateVersionError(Exception):
    pass


def get_template_spec(template_code: str, template_version: int) -> TemplateSpec:
    spec = TEMPLATE_REGISTRY.get(template_code)
    if spec is None:
        raise TemplateNotFoundError(f"Unknown template code: {template_code}")
    if spec.version != template_version:
        raise TemplateVersionError(
            f"Unsupported template version {template_version} for {template_code}; "
            f"current version is {spec.version}."
        )
    return spec


def parse_workbook(file_bytes: bytes) -> tuple[list[str], list[dict]]:
    """
    Reads the first worksheet. Row 1 is the header. Returns (headers, rows)
    where each row is a dict keyed by header, 1-indexed row_number stored
    under '__row__' for traceability back to the source file.
    """
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    sheet = workbook.worksheets[0]
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in header_row]

    rows: list[dict] = []
    for idx, raw_row in enumerate(rows_iter, start=2):  # excel row 2 = first data row
        if raw_row is None or all(v is None for v in raw_row):
            continue
        row = {headers[i]: raw_row[i] for i in range(len(headers)) if i < len(raw_row)}
        row["__row__"] = idx
        rows.append(row)
    return headers, rows


def _json_safe(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def check_structure(headers: list[str], spec: TemplateSpec) -> list[str]:
    """Returns a list of missing required column names, if any."""
    present = {h for h in headers if h}
    return [col for col in spec.required_columns if col not in present]


async def validate_rows(
    rows: list[dict], spec: TemplateSpec, db: AsyncSession
) -> tuple[list[dict], int, int, int]:
    """
    Runs the template's row validator against every row, plus intra-batch
    duplicate detection using the template's business key (SECTION 15).
    Returns (issues_as_dicts, valid_count, invalid_count, duplicate_count).
    Duplicate rows are flagged as warnings (not hard errors) so the user
    can still choose to review/skip them, but they are excluded from
    import by default.
    """
    issues: list[dict] = []
    seen_keys: dict[tuple, int] = {}
    valid_count = 0
    invalid_count = 0
    duplicate_count = 0

    for row in rows:
        row_number = row["__row__"]
        row_issues = await spec.validate_row(row, db)
        is_duplicate = False

        if spec.duplicate_key:
            key = spec.duplicate_key(row)
            if key in seen_keys:
                is_duplicate = True
                issues.append({
                    "row_number": row_number,
                    "column_name": None,
                    "value": None,
                    "severity": IssueSeverity.WARNING,
                    "error_code": "DUPLICATE_ROW",
                    "message": f"Duplicate of row {seen_keys[key]} in this file "
                               "(same business key).",
                })
                duplicate_count += 1
            else:
                seen_keys[key] = row_number

        has_error = any(i.severity == "ERROR" for i in row_issues)
        for issue in row_issues:
            issues.append({
                "row_number": row_number,
                "column_name": issue.column,
                "value": str(issue.value) if issue.value is not None else None,
                "severity": IssueSeverity(issue.severity),
                "error_code": issue.error_code,
                "message": issue.message,
            })

        if has_error:
            invalid_count += 1
        elif not is_duplicate:
            valid_count += 1

    return issues, valid_count, invalid_count, duplicate_count


async def create_batch_from_upload(
    db: AsyncSession,
    *,
    template_code: str,
    template_version: int,
    file_name: str,
    file_bytes: bytes,
    uploaded_by_user_id: uuid.UUID,
    legal_entity_id: uuid.UUID | None,
) -> ImportBatch:
    """Runs READ -> VALIDATE STRUCTURE -> VALIDATE DATA -> CHECK DUPLICATES."""
    spec = get_template_spec(template_code, template_version)
    headers, rows = parse_workbook(file_bytes)

    batch = ImportBatch(
        template_code=template_code,
        template_version=template_version,
        legal_entity_id=legal_entity_id,
        file_name=file_name,
        uploaded_by_user_id=uploaded_by_user_id,
        uploaded_at=datetime.datetime.now(datetime.UTC),
        status=ImportBatchStatus.VALIDATING,
        total_rows=len(rows),
        raw_rows=[{k: _json_safe(v) for k, v in row.items()} for row in rows],
    )
    db.add(batch)
    await db.flush()

    missing_columns = check_structure(headers, spec)
    if missing_columns:
        for col in missing_columns:
            db.add(ImportIssue(
                import_batch_id=batch.id,
                row_number=1,
                column_name=col,
                value=None,
                severity=IssueSeverity.ERROR,
                error_code="MISSING_REQUIRED_COLUMN",
                message=f"Required column '{col}' is missing from the uploaded file.",
            ))
        batch.status = ImportBatchStatus.FAILED
        batch.invalid_rows = batch.total_rows
        await db.flush()
        return batch

    issues, valid_count, invalid_count, duplicate_count = await validate_rows(rows, spec, db)
    for issue in issues:
        db.add(ImportIssue(import_batch_id=batch.id, **issue))

    batch.valid_rows = valid_count
    batch.invalid_rows = invalid_count
    batch.duplicate_rows = duplicate_count
    batch.warning_rows = sum(1 for i in issues if i["severity"] == IssueSeverity.WARNING)
    batch.status = (
        ImportBatchStatus.READY_FOR_IMPORT if valid_count > 0 else ImportBatchStatus.VALIDATED
    )
    await db.flush()
    return batch


async def confirm_import(
    db: AsyncSession, batch: ImportBatch, authorized_entity_ids=None,
) -> tuple[int, int]:
    """
    IMPORT TO POSTGRESQL -> IMPORT SUMMARY. Re-validates (idempotent) and
    imports every row with no ERROR-level issue and no duplicate flag.
    Returns (imported_count, skipped_count).

    `authorized_entity_ids` (Stage 2 Hardening, SECTION 11/TEST 10): either
    "ALL" (unrestricted) or a concrete set[UUID] of entities the confirming
    user is authorized to import data for. A row whose resolved entity
    (via the template's `resolve_entity_id`) is not in this set is
    rejected and counted as skipped, with a recorded issue - never
    silently imported. Templates with no entity concept (e.g. FX Rates,
    where `resolve_entity_id` is None) are unaffected.
    """
    if batch.status not in (
        ImportBatchStatus.READY_FOR_IMPORT, ImportBatchStatus.PARTIALLY_IMPORTED
    ):
        raise ValueError(f"Batch is not ready for import (status={batch.status}).")

    spec = get_template_spec(batch.template_code, batch.template_version)
    batch.status = ImportBatchStatus.IMPORTING
    await db.flush()

    rows = batch.raw_rows
    _, valid_count, invalid_count, duplicate_count = await validate_rows(rows, spec, db)

    seen_keys: set[tuple] = set()
    imported = 0
    skipped = 0

    for row in rows:
        row_issues = await spec.validate_row(row, db)
        has_error = any(i.severity == "ERROR" for i in row_issues)
        is_duplicate = False
        if spec.duplicate_key:
            key = spec.duplicate_key(row)
            if key in seen_keys:
                is_duplicate = True
            else:
                seen_keys.add(key)

        if has_error or is_duplicate:
            skipped += 1
            continue

        if (
            spec.resolve_entity_id is not None
            and authorized_entity_ids is not None
            and authorized_entity_ids != "ALL"
        ):
            row_entity_id = await spec.resolve_entity_id(row, db)
            if row_entity_id is None or row_entity_id not in authorized_entity_ids:
                db.add(ImportIssue(
                    import_batch_id=batch.id,
                    row_number=row.get("__row__", 0),
                    column_name="Entity",
                    value=row.get("Entity"),
                    severity=IssueSeverity.ERROR,
                    error_code="UNAUTHORIZED_ENTITY",
                    message="Row references an entity you are not authorized to import "
                            "data for; it was rejected and not imported.",
                ))
                skipped += 1
                continue

        await spec.import_row(row, db, batch.id, batch.legal_entity_id)
        imported += 1

    batch.imported_rows = imported
    batch.valid_rows = valid_count
    batch.invalid_rows = invalid_count
    batch.duplicate_rows = duplicate_count
    if imported == 0:
        batch.status = ImportBatchStatus.FAILED
    elif skipped > 0:
        batch.status = ImportBatchStatus.PARTIALLY_IMPORTED
    else:
        batch.status = ImportBatchStatus.IMPORTED
    await db.flush()
    return imported, skipped
