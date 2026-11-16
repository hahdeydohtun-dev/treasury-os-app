"""
Stage 5F: reconciliation report endpoints (read-only).

Authorization is enforced here on the server for every request: the entities a caller may see are derived from their
BANK_RECONCILIATION permissions (VIEW for screens; the intersection of VIEW and EXPORT for downloads) and applied in
SQL by the report service. An entity or bank account id in the query string only NARROWS the result and is refused if
it falls outside that derived scope - it can never widen it. Nothing here writes to a reconciliation or financial
table; the only write is the audit record of the request itself.
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.rbac import User
from app.models.reconciliation import OpenItemCategory, OpenItemStatus, ReconciliationRunStatus
from app.services import reconciliation_report_export as exporter
from app.services import reconciliation_report_service as svc
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/reconciliation/reports", tags=["reconciliation-reports"])

CATALOG = [
    {"key": "overview", "title": "Overview", "description": "Headline indicators across the authorized scope.", "exportable": False},
    {"key": "run-summary", "title": "Run summary", "description": "Each reconciliation run: transactions, accepted, unmatched, proposals and exceptions.", "exportable": True},
    {"key": "matching-performance", "title": "Matching performance", "description": "Accepted, pending and rejected matches by type, as decision counts and unique transactions.", "exportable": True},
    {"key": "exceptions", "title": "Open exceptions", "description": "Unresolved reconciliation exceptions with age, assignee and history.", "exportable": True},
    {"key": "ageing", "title": "Open-item ageing", "description": "Outstanding exceptions by age band, per currency, account, entity and category.", "exportable": True},
    {"key": "assignment", "title": "Assignment and investigation", "description": "Workload, resolution time, inactivity and reopening.", "exportable": True},
    {"key": "bank-accounts", "title": "Bank accounts", "description": "Per-account reconciliation state, exceptions and completion rate.", "exportable": True},
    {"key": "entity-dashboard", "title": "Entity dashboard", "description": "Entity-level reconciliation indicators.", "exportable": True},
    {"key": "group-dashboard", "title": "Group dashboard", "description": "Consolidated and entity-by-entity view of the authorized entities.", "exportable": True},
]


def report_filters(
    date_from: datetime.date | None = None, date_to: datetime.date | None = None, as_of: datetime.date | None = None,
    legal_entity_id: uuid.UUID | None = None, bank_account_id: uuid.UUID | None = None,
    currency_code: str | None = Query(None, min_length=3, max_length=3),
    run_status: list[ReconciliationRunStatus] | None = Query(None), exception_status: list[OpenItemStatus] | None = Query(None),
    category: list[OpenItemCategory] | None = Query(None), assigned_to_user_id: uuid.UUID | None = None, unassigned: bool = False,
    txn_date_from: datetime.date | None = None, txn_date_to: datetime.date | None = None,
    min_age_days: int | None = Query(None, ge=0), max_age_days: int | None = Query(None, ge=0),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
) -> svc.ReportFilters:
    return svc.build_filters(
        date_from=date_from, date_to=date_to, as_of=as_of, legal_entity_id=legal_entity_id, bank_account_id=bank_account_id,
        currency_code=currency_code, run_status=run_status, exception_status=exception_status, category=category,
        assigned_to_user_id=assigned_to_user_id, unassigned=unassigned, txn_date_from=txn_date_from, txn_date_to=txn_date_to,
        min_age_days=min_age_days, max_age_days=max_age_days, page=page, page_size=page_size,
    )


def _check(report: str) -> None:
    if report not in svc.REPORTS:
        raise HTTPException(status_code=404, detail="Unknown report.")


async def _audit(db: AsyncSession, user_id: uuid.UUID, scope: svc.ReportScope | None, report: str, action: str, filters: svc.ReportFilters,
                 *, fmt: str | None = None, outcome: str = "SUCCESS", rows: int | None = None, reason: str | None = None) -> None:
    """One audit record per request: who, what report, which scope and filters, format, outcome. No transaction data."""
    entity_scope = None if scope is None else ("ALL" if scope.entity_ids == "ALL" else sorted(str(e) for e in scope.entity_ids))
    await record_audit_event(
        db, module=svc.MODULE.value, action=action, record_type="ReconciliationReport", record_id=report, user_id=user_id,
        legal_entity_id=filters.legal_entity_id,
        new_value={"report": report, "format": fmt, "filters": filters.describe(), "entity_scope": entity_scope,
                   "outcome": outcome, "rows": rows}, reason=reason,
    )
    await db.commit()


@router.get("")
async def list_reports(user: User = Depends(get_current_user)) -> list[dict]:
    """The available reports. No data is returned, so no entity scope applies."""
    return CATALOG


@router.get("/{report}")
async def get_report(
    report: str, response: Response, filters: svc.ReportFilters = Depends(report_filters),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    _check(report)
    response.headers["Cache-Control"] = "no-store"      # reports are computed per request and per user; never cached
    scope = await svc.build_scope(db, user, filters)
    result = await svc.REPORTS[report](db, scope, filters)
    first = result["tables"][0]["total_rows"] if result["tables"] else None
    await _audit(db, user.id, scope, report, "REPORT_GENERATED", filters, rows=first)
    return result


@router.get("/{report}/export")
async def export_report(
    report: str, format: str = Query("xlsx", pattern="^(xlsx|pdf)$"), filters: svc.ReportFilters = Depends(report_filters),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> StreamingResponse:
    _check(report)
    if report not in svc.EXPORTABLE:
        raise HTTPException(status_code=404, detail="This report cannot be exported.")
    filters.paginate = False        # an export contains every authorized record matching the filters, not one page
    user_id = user.id               # read before any rollback: a rolled-back session expires the user object
    scope = None
    try:
        scope = await svc.build_scope(db, user, filters, export=True)
        result = await svc.REPORTS[report](db, scope, filters)
        content = exporter.build_xlsx(result) if format == "xlsx" else exporter.build_pdf(result)
    except HTTPException as exc:
        await db.rollback()
        await _audit(db, user_id, scope, report, "REPORT_EXPORT_FAILED", filters, fmt=format, outcome="FAILURE", reason=f"HTTP {exc.status_code}")
        raise
    except Exception as exc:
        await db.rollback()
        await _audit(db, user_id, scope, report, "REPORT_EXPORT_FAILED", filters, fmt=format, outcome="FAILURE", reason=type(exc).__name__)
        raise
    first = result["tables"][0]["total_rows"] if result["tables"] else None
    await _audit(db, user_id, scope, report, "REPORT_EXPORTED", filters, fmt=format, rows=first)
    return StreamingResponse(
        iter([content]), media_type=exporter.XLSX_MIME if format == "xlsx" else exporter.PDF_MIME,
        headers={"Content-Disposition": f'attachment; filename="{exporter.export_filename(result, format)}"', "Cache-Control": "no-store"},
    )
