"""
Stage 5G: adaptive-matching administration and explanations (v1: Stage 5C one-to-one suggestions; account-level learning).

* Settings are stored in the reconciliation configuration the runs already use (`matching_rule_config["adaptive"]`), so every
  change is a NEW configuration version through the existing `create_configuration_version`, needs the existing
  BANK_RECONCILIATION:CONFIGURE permission, and is audited. Adaptive matching is DISABLED unless explicitly enabled.
* Nothing here accepts, rejects, scores-in-place or otherwise changes a suggestion or any financial record.
* Entity scope is derived from the caller's permissions on the server; an inaccessible suggestion id reveals nothing.
"""
import datetime
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.banking import BankAccount
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.models.reconciliation import (
    ReconciliationConfiguration,
    ReconciliationMatchSuggestion,
    ReconciliationRun,
)
from app.models.reconciliation_adaptive import ReconciliationAdaptiveScore
from app.services.audit_service import record_audit_event
from app.services.reconciliation_adaptive_learning import aggregate, evaluate_pattern, load_evidence
from app.services.reconciliation_adaptive_service import explanation_payload, settings_for
from app.services.reconciliation_adaptive_settings import (
    ADAPTIVE_RULE_VERSION,
    BOUNDS,
    DEFAULT_SETTINGS,
    SETTINGS_KEY,
    AdaptiveSettingsError,
    validate_settings_payload,
)
from app.services.reconciliation_scoring import MATCHING_RULE_VERSION
from app.services.reconciliation_service import (
    create_configuration_version,
    resolve_effective_configuration,
)

router = APIRouter(prefix="/reconciliation/adaptive", tags=["reconciliation-adaptive"])
MODULE = TreasuryModule.BANK_RECONCILIATION
MAX_EXPLANATION_IDS = 200
V1_SCOPE_NOTE = ("Stage 5G v1 learns from and scores Stage 5C one-to-one suggestions only, at account level. One-to-many, "
                 "many-to-one, batch-payment and internal-transfer proposals are not learned from and are not scored.")


class AdaptiveSettingsUpdate(BaseModel):
    legal_entity_id: uuid.UUID
    bank_account_id: uuid.UUID | None = None
    currency_code: str | None = Field(None, min_length=3, max_length=3)
    settings: dict
    reason: str | None = Field(None, max_length=500)


async def _account(db: AsyncSession, entity_id: uuid.UUID, account_id: uuid.UUID | None) -> BankAccount | None:
    if account_id is None:
        return None
    account = await db.get(BankAccount, account_id)
    if account is None or account.legal_entity_id != entity_id:
        raise HTTPException(status_code=404, detail="Bank account not found.")
    return account


async def _exact_config(db: AsyncSession, entity_id, account_id, currency) -> ReconciliationConfiguration | None:
    stmt = select(ReconciliationConfiguration).where(
        ReconciliationConfiguration.is_current.is_(True), ReconciliationConfiguration.legal_entity_id == entity_id,
        ReconciliationConfiguration.bank_account_id.is_(None) if account_id is None else ReconciliationConfiguration.bank_account_id == account_id,
        ReconciliationConfiguration.currency_code.is_(None) if currency is None else ReconciliationConfiguration.currency_code == currency)
    return (await db.execute(stmt)).scalars().first()


async def _effective_config(db: AsyncSession, entity_id, account: BankAccount | None, currency: str | None):
    """The configuration a run for this scope would actually use (most specific wins), exactly as run creation resolves it."""
    if account is not None:
        return await resolve_effective_configuration(db, entity_id, account.id, currency or account.currency_code)
    return await _exact_config(db, entity_id, None, currency)


def _settings_response(config: ReconciliationConfiguration | None, entity_id, account_id, currency) -> dict:
    parsed = settings_for(config)
    return {
        "legal_entity_id": str(entity_id), "bank_account_id": str(account_id) if account_id else None, "currency_code": currency,
        "configuration_id": str(config.id) if config else None, "configuration_version": config.version if config else None,
        "configuration_scope": ({"legal_entity_id": str(config.legal_entity_id) if config.legal_entity_id else None,
                                 "bank_account_id": str(config.bank_account_id) if config.bank_account_id else None,
                                 "currency_code": config.currency_code} if config else None),
        "settings": parsed.settings.to_dict(), "warnings": parsed.warnings, "settings_unusable": parsed.invalid,
        "defaults": DEFAULT_SETTINGS.to_dict(), "bounds": {k: [str(lo), str(hi)] for k, (lo, hi) in BOUNDS.items()},
        "learning_rule_version": ADAPTIVE_RULE_VERSION, "base_rule_version": MATCHING_RULE_VERSION,
        "disabled_by_default": True, "automatic_acceptance": False, "scope_note": V1_SCOPE_NOTE,
        "human_review_required": "Adaptive recommendations are suggestions. A person must accept or reject every match.",
    }


@router.get("/settings")
async def get_settings(
    legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID | None = None, currency_code: str | None = Query(None, min_length=3, max_length=3),
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, legal_entity_id)
    account = await _account(db, legal_entity_id, bank_account_id)
    currency = currency_code.upper() if currency_code else None
    return _settings_response(await _effective_config(db, legal_entity_id, account, currency), legal_entity_id, bank_account_id, currency)


@router.put("/settings")
async def update_settings(
    payload: AdaptiveSettingsUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    await assert_entity_access(db, user, MODULE, TreasuryAction.CONFIGURE, payload.legal_entity_id)
    account = await _account(db, payload.legal_entity_id, payload.bank_account_id)
    currency = payload.currency_code.upper() if payload.currency_code else None
    try:
        new_settings = validate_settings_payload(payload.settings)
    except AdaptiveSettingsError as exc:
        raise HTTPException(status_code=422, detail=exc.problems) from exc

    # The new version is a fork of what runs for this scope use TODAY, so enabling or tuning adaptive matching can never
    # silently change tolerances, the high-value rule or any other existing setting.
    exact = await _exact_config(db, payload.legal_entity_id, payload.bank_account_id, currency)
    base = exact or await _effective_config(db, payload.legal_entity_id, account, currency)
    previous = (base.matching_rule_config or {}).get(SETTINGS_KEY) if base is not None else None
    carried = dict(base.matching_rule_config or {}) if base is not None else {}
    carried[SETTINGS_KEY] = new_settings.to_dict()
    config = await create_configuration_version(db, {
        "legal_entity_id": payload.legal_entity_id, "bank_account_id": payload.bank_account_id, "currency_code": currency,
        "amount_tolerance_pct": base.amount_tolerance_pct if base is not None else Decimal(0),
        "date_tolerance_days": base.date_tolerance_days if base is not None else 0,
        "high_value_threshold": base.high_value_threshold if base is not None else None,
        "duplicate_policy": base.duplicate_policy if base is not None else None, "matching_rule_config": carried,
    }, user.id)
    await record_audit_event(
        db, module=MODULE.value, action="ADAPTIVE_CONFIGURE", record_type="ReconciliationConfiguration", record_id=str(config.id),
        user_id=user.id, legal_entity_id=payload.legal_entity_id,
        previous_value={"adaptive": previous, "based_on_configuration_id": str(base.id) if base is not None else None,
                        "based_on_version": base.version if base is not None else None},
        new_value={"adaptive": new_settings.to_dict(), "configuration_version": config.version,
                   "bank_account_id": str(payload.bank_account_id) if payload.bank_account_id else None, "currency_code": currency},
        reason=payload.reason)
    await db.commit()
    await db.refresh(config)
    return _settings_response(config, payload.legal_entity_id, payload.bank_account_id, currency)


@router.get("/health")
async def learning_health(
    legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """Learning health for ONE entity and bank account: what evidence exists, what was excluded and why, and how recent runs used it."""
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, legal_entity_id)
    account = await _account(db, legal_entity_id, bank_account_id)
    config = await _effective_config(db, legal_entity_id, account, None)
    parsed = settings_for(config)
    cutoff = datetime.datetime.now(datetime.UTC)
    evidence = await load_evidence(db, legal_entity_id, bank_account_id, cutoff, parsed.settings)
    stats = aggregate(evidence.rows, cutoff, parsed.settings)
    per_family: dict[str, dict] = {}
    for (_cur, _dir, _type, family, _key), stat in stats.items():
        entry = per_family.setdefault(family, {"patterns": 0, "supported": 0})
        entry["patterns"] += 1
        entry["supported"] += 1 if evaluate_pattern(stat, parsed.settings)[0] else 0
    A = ReconciliationAdaptiveScore
    total, contributing, last_at = (await db.execute(
        select(func.count(), func.count().filter(A.fallback.is_(False)), func.max(A.created_at))
        .where(A.legal_entity_id == legal_entity_id, A.bank_account_id == bank_account_id))).one()
    return {
        "legal_entity_id": str(legal_entity_id), "bank_account_id": str(bank_account_id), "evaluated_at": cutoff.isoformat(),
        "enabled": parsed.settings.enabled, "settings_unusable": parsed.invalid, "warnings": parsed.warnings,
        "configuration_version": config.version if config else None, "learning_rule_version": ADAPTIVE_RULE_VERSION,
        "evidence": {
            "decisions_scanned": evidence.scanned, "scan_truncated": evidence.truncated, "eligible": len(evidence.rows),
            "eligible_accepted": sum(1 for e in evidence.rows if e.accepted), "eligible_rejected": sum(1 for e in evidence.rows if not e.accepted),
            "excluded_by_reason": {k: v for k, v in evidence.exclusions.items() if v},
            "repeated_decisions_superseded": evidence.superseded, "pairs_with_conflicting_dispositions": evidence.conflicting_pairs,
            "latest_eligible_decision_at": max((e.decided_at for e in evidence.rows), default=None),
        },
        "patterns_by_family": per_family,
        "scored_suggestions": {"total": total, "with_adaptive_contribution": contributing, "fell_back_to_deterministic": total - contributing,
                               "last_annotated_at": last_at},
        "scope_note": V1_SCOPE_NOTE,
    }


@router.get("/explanations")
async def get_explanations(
    suggestion_id: list[uuid.UUID] = Query(..., max_length=MAX_EXPLANATION_IDS), include_decision_ids: bool = False,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """Adaptive explanations for suggestions the caller may view. An id the caller cannot see, or that does not exist, is simply absent."""
    view = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if view.is_empty:
        raise HTTPException(status_code=403, detail="No BANK_RECONCILIATION:VIEW permission.")
    visible = await resolve_scope_entity_ids(db, view)
    configure_ids: set | str = set()
    if include_decision_ids:
        configure = await get_authorized_scope(db, user, MODULE, TreasuryAction.CONFIGURE)
        if configure.is_empty:
            raise HTTPException(status_code=403, detail="Decision references require BANK_RECONCILIATION:CONFIGURE.")
        configure_ids = await resolve_scope_entity_ids(db, configure)
    S, R = ReconciliationMatchSuggestion, ReconciliationRun
    owned = (await db.execute(select(S.id, R.legal_entity_id).join(R, R.id == S.reconciliation_run_id).where(S.id.in_(suggestion_id)))).all()
    accessible = {sid: entity for sid, entity in owned if visible == "ALL" or entity in visible}
    rows = {r.suggestion_id: r for r in (await db.execute(
        select(ReconciliationAdaptiveScore).where(ReconciliationAdaptiveScore.suggestion_id.in_(list(accessible))))).scalars()} if accessible else {}
    explanations = {}
    for sid, row in rows.items():
        with_ids = include_decision_ids and (configure_ids == "ALL" or accessible[sid] in configure_ids)
        explanations[str(sid)] = explanation_payload(row, include_decision_ids=with_ids)
    return {
        "explanations": explanations, "without_explanation": sorted(str(s) for s in accessible if s not in rows),
        "note": "A suggestion without an explanation was created while adaptive matching was disabled or was not eligible for scoring. "
                "Adaptive recommendations are suggestions only; a person must accept or reject every match.",
        "scope_note": V1_SCOPE_NOTE,
    }
