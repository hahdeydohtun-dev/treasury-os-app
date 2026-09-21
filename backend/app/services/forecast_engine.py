"""
13-Week Cash Flow Forecast calculation engine.

See FORECAST_METHODOLOGY.md for the full methodology write-up. This
module performs every financial calculation in the forecast - the
frontend never computes an authoritative number (SECTION 45).

`calculate_forecast` is idempotent and safe to call repeatedly on a DRAFT
forecast (it replaces that forecast's weeks/lines/alerts each time); it
refuses to run on a PUBLISHED forecast (SECTION 5: "a published forecast
should be immutable") - recalculating a published forecast means creating
a new version via `roll_forecast` or a fresh `Forecast` row instead.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import LegalEntity
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment, ForecastItemStatus
from app.models.forecast import (
    AdjustmentStatus,
    AlertSeverity,
    Forecast,
    ForecastAdjustment,
    ForecastAlert,
    ForecastLine,
    ForecastSourceType,
    ForecastStatus,
    ForecastValueBasis,
    ForecastWeek,
    LiquidityScopeType,
    LiquidityThreshold,
    RecurringCashFlow,
    RecurringFrequency,
)
from app.models.lookup import CashDirection
from app.models.treasury_transaction import TransactionStatus, TreasuryTransaction
from app.services.cash_position_service import calculate_cash_position
from app.services.forecast_category_mapping import (
    resolve_category_for_event_type,
    resolve_category_for_free_text,
)
from app.services.forecast_dates import FORECAST_HORIZON_WEEKS, generate_week_bounds, week_status
from app.services.fx_conversion_service import get_conversion_rate

TWO_DP = Decimal("0.01")

# Transfer-direction event types where the sign of cash movement is known
# (money leaving vs arriving). Anything else classified TRANSFER but not
# listed here contributes 0 to net_transfers - a documented limitation
# (see FORECAST_METHODOLOGY.md) rather than a guess.
TRANSFER_SIGN_BY_EVENT_TYPE = {
    "INTERCOMPANY_PAYMENT": -1,
    "INTERCOMPANY_RECEIPT": 1,
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_DP, rounding=ROUND_HALF_UP)


def _week_for_date(weeks, d: datetime.date) -> int | None:
    for week_number, start, end in weeks:
        if start <= d <= end:
            return week_number
    return None


class _RawLine:
    """Intermediate representation before scenario adjustment and FX conversion."""

    def __init__(
        self, legal_entity_id, currency_code, category_code, direction, week_number,
        original_amount, probability, source_type, source_id, description, counterparty,
        event_date, sign=1,
    ):
        self.legal_entity_id = legal_entity_id
        self.currency_code = currency_code
        self.category_code = category_code
        self.direction = direction
        self.week_number = week_number
        self.original_amount = original_amount
        self.probability = probability
        self.source_type = source_type
        self.source_id = source_id
        self.description = description
        self.counterparty = counterparty
        self.event_date = event_date
        self.sign = sign


async def _resolve_entities(db: AsyncSession, forecast: Forecast) -> list:
    if forecast.legal_entity_id:
        entity = await db.get(LegalEntity, forecast.legal_entity_id)
        return [entity] if entity else []
    stmt = select(LegalEntity).where(LegalEntity.is_active.is_(True))
    if forecast.group_id:
        stmt = stmt.where(LegalEntity.group_id == forecast.group_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_assumption_map(db: AsyncSession, forecast: Forecast) -> dict:
    """
    Queries directly rather than accessing `forecast.assumptions` lazily -
    the relationship may not be eagerly loaded depending on how the caller
    fetched the Forecast object, and a lazy load here would fail under
    async SQLAlchemy (MissingGreenlet).
    """
    from app.models.forecast import ForecastScenarioAssumption

    result = await db.execute(
        select(ForecastScenarioAssumption).where(
            ForecastScenarioAssumption.forecast_id == forecast.id
        )
    )
    by_type: dict = {}
    for a in result.scalars().all():
        by_type.setdefault(a.assumption_type, []).append(a)
    return by_type


async def _gather_expected_collections(db, entities, weeks, horizon_start, horizon_end) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []
    stmt = select(ExpectedCollection).where(
        ExpectedCollection.legal_entity_id.in_(entity_ids),
        ExpectedCollection.expected_date >= horizon_start,
        ExpectedCollection.expected_date <= horizon_end,
        ExpectedCollection.status != ForecastItemStatus.CANCELLED,
    )
    rows = (await db.execute(stmt)).scalars().all()
    lines = []
    for row in rows:
        wn = _week_for_date(weeks, row.expected_date)
        if wn is None:
            continue
        category = resolve_category_for_free_text(row.category, "INFLOW")
        lines.append(_RawLine(
            row.legal_entity_id, row.currency_code, category, CashDirection.INFLOW, wn,
            row.amount, row.probability, ForecastSourceType.EXPECTED_COLLECTION, str(row.id),
            f"Expected collection: {row.counterparty or 'unspecified'}", row.counterparty,
            row.expected_date,
        ))
    return lines


async def _gather_expected_payments(db, entities, weeks, horizon_start, horizon_end) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []
    stmt = select(ExpectedPayment).where(
        ExpectedPayment.legal_entity_id.in_(entity_ids),
        ExpectedPayment.expected_date >= horizon_start,
        ExpectedPayment.expected_date <= horizon_end,
        ExpectedPayment.status != ForecastItemStatus.CANCELLED,
    )
    rows = (await db.execute(stmt)).scalars().all()
    lines = []
    for row in rows:
        wn = _week_for_date(weeks, row.expected_date)
        if wn is None:
            continue
        category = resolve_category_for_free_text(row.category, "OUTFLOW")
        lines.append(_RawLine(
            row.legal_entity_id, row.currency_code, category, CashDirection.OUTFLOW, wn,
            row.amount, row.probability, ForecastSourceType.EXPECTED_PAYMENT, str(row.id),
            f"Expected payment: {row.counterparty or 'unspecified'}", row.counterparty,
            row.expected_date,
        ))
    return lines


def _expand_recurring_occurrences(
    start_date, end_date, frequency, custom_interval_days, horizon_start, horizon_end
) -> list:
    occurrences = []
    cursor = max(start_date, horizon_start)
    hard_end = min(end_date, horizon_end) if end_date else horizon_end

    step_days = {
        RecurringFrequency.DAILY: 1,
        RecurringFrequency.WEEKLY: 7,
        RecurringFrequency.BIWEEKLY: 14,
        RecurringFrequency.CUSTOM: custom_interval_days or 30,
    }.get(frequency)

    if step_days:
        while cursor <= hard_end:
            occurrences.append(cursor)
            cursor += datetime.timedelta(days=step_days)
        return occurrences

    months_step = 3 if frequency == RecurringFrequency.QUARTERLY else 1
    day = start_date.day
    while cursor <= hard_end:
        occurrences.append(cursor)
        month = cursor.month - 1 + months_step
        year = cursor.year + month // 12
        month = month % 12 + 1
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        cursor = datetime.date(year, month, min(day, last_day))
    return occurrences


async def _gather_recurring_flows(db, entities, weeks, horizon_start, horizon_end) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []
    stmt = select(RecurringCashFlow).where(
        RecurringCashFlow.legal_entity_id.in_(entity_ids),
        RecurringCashFlow.is_active.is_(True),
        RecurringCashFlow.start_date <= horizon_end,
    )
    rows = (await db.execute(stmt)).scalars().all()
    lines: list = []
    for row in rows:
        occurrences = _expand_recurring_occurrences(
            row.start_date, row.end_date, row.frequency, row.custom_interval_days,
            horizon_start, horizon_end,
        )
        by_week: dict = {}
        for occ in occurrences:
            wn = _week_for_date(weeks, occ)
            if wn is not None:
                by_week[wn] = by_week.get(wn, 0) + 1
        for wn, count in by_week.items():
            lines.append(_RawLine(
                row.legal_entity_id, row.currency_code, row.category_code, row.direction, wn,
                row.amount * count, None, ForecastSourceType.RECURRING, str(row.id),
                f"Recurring: {row.name}" + (f" x{count}" if count > 1 else ""), None,
                weeks[wn - 1][1],
            ))
    return lines


async def _gather_actuals(db, entities, weeks, horizon_start, horizon_end) -> list:
    entity_ids = [e.id for e in entities]
    if not entity_ids:
        return []
    stmt = select(TreasuryTransaction).where(
        TreasuryTransaction.legal_entity_id.in_(entity_ids),
        TreasuryTransaction.status == TransactionStatus.POSTED,
        TreasuryTransaction.event_date >= horizon_start,
        TreasuryTransaction.event_date <= horizon_end,
    )
    rows = (await db.execute(stmt)).scalars().all()
    lines = []
    for row in rows:
        wn = _week_for_date(weeks, row.event_date)
        if wn is None:
            continue
        if row.direction == CashDirection.NON_CASH:
            continue
        sign = 1
        if row.direction == CashDirection.TRANSFER:
            sign = TRANSFER_SIGN_BY_EVENT_TYPE.get(row.event_type_code, 0)
            if sign == 0:
                continue
            category = "INTERCOMPANY_TRANSFER"
        else:
            category = resolve_category_for_event_type(row.event_type_code, row.direction.value)
        lines.append(_RawLine(
            row.legal_entity_id, row.transaction_currency_code, category, row.direction, wn,
            row.transaction_amount, None, ForecastSourceType.ACTUAL, str(row.id),
            row.narration or row.reference or "Posted transaction", row.counterparty,
            row.event_date, sign=sign,
        ))
    return lines


async def _gather_manual_adjustments(db, forecast_id) -> list:
    stmt = select(ForecastAdjustment).where(
        ForecastAdjustment.forecast_id == forecast_id,
        ForecastAdjustment.status.in_([AdjustmentStatus.APPLIED, AdjustmentStatus.APPROVED]),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        _RawLine(
            row.legal_entity_id, row.currency_code, row.category_code, row.direction,
            row.week_number, row.amount, None, ForecastSourceType.MANUAL_ADJUSTMENT, str(row.id),
            f"Manual adjustment: {row.reason}", None, None,
        )
        for row in rows
    ]


async def _gather_facility_events(db, entities, weeks, horizon_start, horizon_end) -> list:
    """
    SECTION 23: converts facility_forecast_adapter's plain-dict events
    into _RawLine rows, the same shape every other source produces -
    the forecast engine's downstream logic (scenario assumptions, value
    basis, FX conversion, weekly aggregation) needs no special-casing
    for facility-driven lines.
    """
    from app.services.facility_forecast_adapter import gather_facility_forecast_events

    events = await gather_facility_forecast_events(db, entities, horizon_start, horizon_end)
    lines: list = []
    for event in events:
        wn = _week_for_date(weeks, event["event_date"])
        if wn is None:
            continue
        lines.append(_RawLine(
            event["legal_entity_id"], event["currency_code"], event["category_code"],
            event["direction"], wn, event["amount"], None,
            ForecastSourceType(event["source_type"]),
            event["source_id"], event["description"], event["counterparty"], event["event_date"],
        ))
    return lines


async def _gather_investment_events(db, entities, weeks, horizon_start, horizon_end) -> list:
    """
    SECTION 20: same pattern as _gather_facility_events - converts
    investment_forecast_adapter's plain-dict events into _RawLine rows,
    so investment-driven lines flow through the exact same downstream
    logic (scenario assumptions, FX conversion, weekly aggregation) as
    every other source with no special-casing.
    """
    from app.services.investment_forecast_adapter import gather_investment_forecast_events

    events = await gather_investment_forecast_events(db, entities, horizon_start, horizon_end)
    lines: list = []
    for event in events:
        wn = _week_for_date(weeks, event["event_date"])
        if wn is None:
            continue
        lines.append(_RawLine(
            event["legal_entity_id"], event["currency_code"], event["category_code"],
            event["direction"], wn, event["amount"], None,
            ForecastSourceType(event["source_type"]),
            event["source_id"], event["description"], event["counterparty"], event["event_date"],
        ))
    return lines


def _apply_scenario_assumptions(lines, assumptions, num_weeks) -> list:
    notes: list = []

    for a in assumptions.get("COLLECTION_DELAY_WEEKS", []):
        delay = int(a.numeric_value)
        for line in lines:
            if line.source_type == ForecastSourceType.EXPECTED_COLLECTION:
                new_week = line.week_number + delay
                if new_week > num_weeks:
                    notes.append(
                        f"Collection {line.source_id} pushed beyond the 13-week horizon by "
                        f"the {delay}-week delay assumption and was excluded."
                    )
                    line.week_number = -1
                else:
                    line.week_number = new_week

    for a in assumptions.get("COLLECTION_PROBABILITY_HAIRCUT_PCT", []):
        haircut = a.numeric_value
        for line in lines:
            if (
                line.source_type == ForecastSourceType.EXPECTED_COLLECTION
                and line.probability is not None
            ):
                line.probability = max(0, int(Decimal(line.probability) - haircut))

    for a in assumptions.get("PAYMENT_ACCELERATION_WEEKS", []):
        accel = int(a.numeric_value)
        for line in lines:
            if line.source_type == ForecastSourceType.EXPECTED_PAYMENT:
                line.week_number = max(1, line.week_number - accel)

    for a in assumptions.get("UNEXPECTED_OUTFLOW_AMOUNT", []):
        if not lines:
            continue
        target_entity = lines[0].legal_entity_id
        currency = a.currency_code or lines[0].currency_code
        lines.append(_RawLine(
            target_entity, currency, a.category_code or "OTHER_OUTFLOW", CashDirection.OUTFLOW,
            1, a.numeric_value, None, ForecastSourceType.SCENARIO_ADJUSTMENT, str(a.id),
            a.description or "Stress scenario: unexpected outflow", None, None,
        ))

    return notes


def _min_cash_buffer_multiplier(assumptions) -> Decimal:
    rows = assumptions.get("MIN_CASH_BUFFER_MULTIPLIER", [])
    if rows:
        return rows[0].numeric_value
    return Decimal(1)


async def _lookup_minimum_liquidity(db, forecast, reporting_currency, as_of) -> Decimal:
    stmt = select(LiquidityThreshold).where(LiquidityThreshold.is_active.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    total = Decimal(0)

    for row in rows:
        applies = False
        if row.scope_type == LiquidityScopeType.GROUP and forecast.legal_entity_id is None or (
            row.scope_type == LiquidityScopeType.ENTITY
            and forecast.legal_entity_id is not None
            and row.legal_entity_id == forecast.legal_entity_id
        ) or row.scope_type == LiquidityScopeType.CURRENCY and row.currency_code:
            applies = True

        if not applies:
            continue

        amount = row.minimum_amount
        currency = row.currency_code or reporting_currency
        if currency != reporting_currency:
            conversion = await get_conversion_rate(db, currency, reporting_currency, as_of)
            if conversion is None:
                continue
            amount = amount * conversion.rate
        total += amount

    return total


async def calculate_forecast(db: AsyncSession, forecast: Forecast) -> Forecast:
    if forecast.status == ForecastStatus.PUBLISHED:
        raise ValueError("Cannot recalculate a published forecast; create a new version.")

    await db.execute(delete(ForecastLine).where(ForecastLine.forecast_id == forecast.id))
    await db.execute(delete(ForecastWeek).where(ForecastWeek.forecast_id == forecast.id))
    await db.execute(delete(ForecastAlert).where(ForecastAlert.forecast_id == forecast.id))
    await db.flush()

    warnings: list = []
    entities = await _resolve_entities(db, forecast)
    if not entities:
        warnings.append("No legal entities in scope - forecast will be empty.")

    weeks = generate_week_bounds(forecast.forecast_start_date, FORECAST_HORIZON_WEEKS)
    horizon_start, horizon_end = weeks[0][1], weeks[-1][2]
    today = datetime.date.today()

    opening_cash_snapshot: dict = {}
    opening_by_currency: dict = {}
    for entity in entities:
        position = await calculate_cash_position(
            db, legal_entity_id=entity.id,
            as_of=forecast.forecast_start_date - datetime.timedelta(days=1),
        )
        if position.account_count == 0:
            warnings.append(
                f"No bank balance found for entity {entity.code} as of the forecast start date; "
                "opening cash assumed 0 for this entity."
            )
        for currency, amount in position.by_currency.items():
            key = f"{entity.id}|{currency}"
            opening_cash_snapshot[key] = str(amount)
            opening_by_currency[currency] = opening_by_currency.get(currency, Decimal(0)) + amount

    raw_lines: list = []
    raw_lines += await _gather_expected_collections(db, entities, weeks, horizon_start, horizon_end)
    raw_lines += await _gather_expected_payments(db, entities, weeks, horizon_start, horizon_end)
    raw_lines += await _gather_recurring_flows(db, entities, weeks, horizon_start, horizon_end)
    raw_lines += await _gather_actuals(db, entities, weeks, horizon_start, horizon_end)
    raw_lines += await _gather_manual_adjustments(db, forecast.id)
    raw_lines += await _gather_facility_events(db, entities, weeks, horizon_start, horizon_end)
    raw_lines += await _gather_investment_events(db, entities, weeks, horizon_start, horizon_end)

    assumptions = await _get_assumption_map(db, forecast)
    warnings += _apply_scenario_assumptions(raw_lines, assumptions, FORECAST_HORIZON_WEEKS)
    raw_lines = [line for line in raw_lines if line.week_number != -1]

    min_cash_multiplier = _min_cash_buffer_multiplier(assumptions)

    week_objs: dict = {}
    for week_number, start, end in weeks:
        fw = ForecastWeek(
            forecast_id=forecast.id, week_number=week_number, start_date=start, end_date=end,
            status=week_status(start, end, today),
        )
        db.add(fw)
        week_objs[week_number] = fw
    await db.flush()

    for line in raw_lines:
        if (
            line.source_type in (
                ForecastSourceType.EXPECTED_COLLECTION, ForecastSourceType.EXPECTED_PAYMENT,
            )
            and forecast.value_basis == ForecastValueBasis.PROBABILITY_ADJUSTED
            and line.probability is not None
        ):
            adjusted = line.original_amount * Decimal(line.probability) / Decimal(100)
        else:
            adjusted = line.original_amount
        adjusted = adjusted * line.sign

        week_end = weeks[line.week_number - 1][2]
        conversion = await get_conversion_rate(
            db, line.currency_code, forecast.reporting_currency_code, week_end
        )
        if conversion is None:
            warnings.append(
                f"No FX rate found for {line.currency_code}->{forecast.reporting_currency_code} "
                f"as of week {line.week_number}; line excluded from the consolidated total "
                "(still visible in the currency view)."
            )
            reporting_amount = Decimal(0)
            fx_rate = fx_type = fx_date = None
        else:
            reporting_amount = _round(adjusted * conversion.rate)
            fx_rate, fx_type, fx_date = conversion.rate, conversion.rate_type, conversion.rate_date

        db.add(ForecastLine(
            forecast_id=forecast.id, week_id=week_objs[line.week_number].id,
            legal_entity_id=line.legal_entity_id, category_code=line.category_code,
            direction=line.direction, transaction_currency_code=line.currency_code,
            original_amount=_round(line.original_amount), probability=line.probability,
            adjusted_amount=_round(adjusted),
            reporting_currency_code=forecast.reporting_currency_code,
            reporting_amount=reporting_amount,
            fx_rate=fx_rate, fx_rate_type=fx_type, fx_rate_date=fx_date,
            source_type=line.source_type, source_id=line.source_id,
            description=line.description, counterparty=line.counterparty,
        ))
    await db.flush()

    opening_cash_reporting = Decimal(0)
    for currency, amount in opening_by_currency.items():
        conversion = await get_conversion_rate(
            db, currency, forecast.reporting_currency_code, horizon_start
        )
        if conversion is None:
            warnings.append(
                f"No FX rate for opening cash currency {currency}; excluded from consolidated "
                "opening cash."
            )
            continue
        opening_cash_reporting += amount * conversion.rate

    lines_result = await db.execute(
        select(ForecastLine).where(ForecastLine.forecast_id == forecast.id)
    )
    all_lines = list(lines_result.scalars().all())

    running_opening = _round(opening_cash_reporting)
    for week_number, start, end in weeks:
        week_lines = [ln for ln in all_lines if week_objs[week_number].id == ln.week_id]
        inflows = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.INFLOW),
            Decimal(0),
        )
        outflows = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.OUTFLOW),
            Decimal(0),
        )
        transfers = sum(
            (ln.reporting_amount for ln in week_lines if ln.direction == CashDirection.TRANSFER),
            Decimal(0),
        )

        net_cash_flow = inflows - outflows + transfers
        closing = _round(running_opening + net_cash_flow)

        min_liquidity = await _lookup_minimum_liquidity(
            db, forecast, forecast.reporting_currency_code, end
        )
        min_liquidity = _round(min_liquidity * min_cash_multiplier)
        surplus_or_gap = _round(closing - min_liquidity)
        coverage = (closing / min_liquidity) if min_liquidity > 0 else None

        fw = week_objs[week_number]
        fw.opening_cash = running_opening
        fw.total_inflows = _round(inflows)
        fw.total_outflows = _round(outflows)
        fw.net_transfers = _round(transfers)
        fw.net_cash_flow = _round(net_cash_flow)
        fw.closing_cash = closing
        fw.minimum_required_liquidity = min_liquidity
        fw.surplus_or_gap = surplus_or_gap
        fw.liquidity_available = closing
        fw.liquidity_required = min_liquidity
        fw.liquidity_coverage_ratio = _round(coverage) if coverage is not None else None

        running_opening = closing

    forecast.data_quality_warnings = warnings
    forecast.opening_cash_snapshot = opening_cash_snapshot
    await db.flush()

    await _generate_alerts(db, forecast, week_objs, all_lines)
    await db.flush()
    await db.refresh(forecast, attribute_names=["weeks"])
    return forecast


async def _generate_alerts(db, forecast: Forecast, week_objs, all_lines) -> None:
    for week_number, fw in week_objs.items():
        if fw.surplus_or_gap < 0:
            magnitude = abs(fw.surplus_or_gap)
            severity = (
                AlertSeverity.CRITICAL
                if fw.minimum_required_liquidity > 0 and magnitude > fw.minimum_required_liquidity
                else AlertSeverity.HIGH
            )
            db.add(ForecastAlert(
                forecast_id=forecast.id, severity=severity, week_number=week_number,
                metric="LIQUIDITY_GAP", threshold_value=fw.minimum_required_liquidity,
                actual_value=fw.closing_cash,
                message=(
                    f"Week {week_number} projected liquidity gap of {magnitude} "
                    f"{forecast.reporting_currency_code} (closing cash {fw.closing_cash} vs "
                    f"minimum {fw.minimum_required_liquidity})."
                ),
            ))
        elif fw.liquidity_coverage_ratio is not None and fw.liquidity_coverage_ratio < Decimal("1.2"):
            db.add(ForecastAlert(
                forecast_id=forecast.id, severity=AlertSeverity.MEDIUM, week_number=week_number,
                metric="LOW_LIQUIDITY_COVERAGE", threshold_value=Decimal("1.2"),
                actual_value=fw.liquidity_coverage_ratio,
                message=(
                    f"Week {week_number} liquidity coverage is {fw.liquidity_coverage_ratio}x, "
                    "close to the minimum requirement."
                ),
            ))

    currencies = {ln.transaction_currency_code for ln in all_lines}
    for currency in currencies:
        running = Decimal(0)
        for key, amount in forecast.opening_cash_snapshot.items():
            if key.endswith(f"|{currency}"):
                running += Decimal(amount)
        for week_number in sorted(week_objs):
            week_lines = [
                ln for ln in all_lines
                if ln.week_id == week_objs[week_number].id
                and ln.transaction_currency_code == currency
            ]
            net = sum(
                (
                    ln.adjusted_amount if ln.direction != CashDirection.OUTFLOW
                    else -ln.adjusted_amount
                )
                for ln in week_lines
            )
            running += net
            if running < 0:
                db.add(ForecastAlert(
                    forecast_id=forecast.id, severity=AlertSeverity.HIGH, week_number=week_number,
                    currency_code=currency, metric="CURRENCY_LIQUIDITY_SHORTFALL",
                    actual_value=_round(running),
                    message=(
                        f"Week {week_number}: {currency} cash position projected negative "
                        f"({_round(running)} {currency}) even if group consolidated cash is "
                        "positive."
                    ),
                ))
