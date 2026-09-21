"""
Funding capacity and funding gap analysis (SECTION 24-26).

Cash, committed funding capacity, and uncommitted/potential capacity are
kept as three distinct numbers throughout - never combined into one
misleading liquidity figure (SECTION 3/24).
"""
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.facility import CommitmentType, Facility, FacilityStatus
from app.services.facility_engine import calculate_utilization


@dataclass
class FundingCapacityResult:
    committed_available: Decimal = Decimal(0)
    uncommitted_potential: Decimal = Decimal(0)
    total_potential_funding: Decimal = Decimal(0)
    by_currency: dict = field(default_factory=dict)
    facility_count: int = 0


async def calculate_funding_capacity(
    db: AsyncSession, legal_entity_ids, currency_code: str | None = None,
) -> FundingCapacityResult:
    stmt = select(Facility).where(
        Facility.is_active.is_(True), Facility.status == FacilityStatus.ACTIVE,
    )
    if legal_entity_ids != "ALL":
        stmt = stmt.where(Facility.legal_entity_id.in_(legal_entity_ids))
    if currency_code:
        stmt = stmt.where(Facility.currency_code == currency_code.upper())

    result = await db.execute(stmt)
    facilities = list(result.scalars().all())

    capacity = FundingCapacityResult(facility_count=len(facilities))
    for facility in facilities:
        utilization = calculate_utilization(facility)
        currency = facility.currency_code
        by_ccy = capacity.by_currency.setdefault(
            currency, {"committed_available": Decimal(0), "uncommitted_potential": Decimal(0)}
        )
        if facility.commitment_type == CommitmentType.COMMITTED:
            capacity.committed_available += utilization.available_amount
            by_ccy["committed_available"] += utilization.available_amount
        else:
            capacity.uncommitted_potential += utilization.undrawn_amount
            by_ccy["uncommitted_potential"] += utilization.undrawn_amount

    capacity.total_potential_funding = capacity.committed_available + capacity.uncommitted_potential
    return capacity


@dataclass
class FundingGapWeek:
    week_number: int
    projected_closing_cash: Decimal
    minimum_required_liquidity: Decimal
    cash_shortfall: Decimal
    committed_capacity_applied: Decimal
    remaining_unfunded_gap: Decimal


async def calculate_funding_gaps(
    db: AsyncSession, forecast_weeks, legal_entity_ids, currency_code: str | None = None,
) -> list:
    capacity = await calculate_funding_capacity(db, legal_entity_ids, currency_code)
    gaps: list = []
    for week in forecast_weeks:
        shortfall = week.minimum_required_liquidity - week.closing_cash
        if shortfall <= 0:
            continue
        applied = min(shortfall, capacity.committed_available)
        remaining = shortfall - applied
        gaps.append(FundingGapWeek(
            week_number=week.week_number, projected_closing_cash=week.closing_cash,
            minimum_required_liquidity=week.minimum_required_liquidity,
            cash_shortfall=shortfall, committed_capacity_applied=applied,
            remaining_unfunded_gap=remaining,
        ))
    return gaps


@dataclass
class MaturityBucketResult:
    bucket: str
    facility_count: int
    total_committed_limit: Decimal


MATURITY_BUCKETS = [
    ("0-30 days", 0, 30), ("31-60 days", 31, 60), ("61-90 days", 61, 90),
    ("91-180 days", 91, 180), ("181-365 days", 181, 365), (">365 days", 366, None),
]


async def facilities_by_maturity_bucket(
    db: AsyncSession, legal_entity_ids, as_of: datetime.date | None = None,
) -> list:
    as_of = as_of or datetime.date.today()
    stmt = select(Facility).where(Facility.is_active.is_(True))
    if legal_entity_ids != "ALL":
        stmt = stmt.where(Facility.legal_entity_id.in_(legal_entity_ids))
    result = await db.execute(stmt)
    facilities = list(result.scalars().all())

    buckets = []
    for label, min_days, max_days in MATURITY_BUCKETS:
        count = 0
        total = Decimal(0)
        for f in facilities:
            days = (f.maturity_date - as_of).days
            if days < 0:
                continue
            in_bucket = (days > min_days) if max_days is None else (min_days <= days <= max_days)
            if in_bucket:
                count += 1
                total += f.committed_limit
        buckets.append(MaturityBucketResult(bucket=label, facility_count=count, total_committed_limit=total))
    return buckets
