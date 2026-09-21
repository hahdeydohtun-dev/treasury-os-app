# 13-Week Cash Flow Forecast - Calculation Methodology

This is the authoritative reference for how every number in the forecast
is calculated. If the dashboard and this document ever disagree, this
document (and the code it describes - app/services/forecast_engine.py)
is correct; report the dashboard as a display bug.

## 1. Horizon and weeks

The forecast horizon is always exactly 13 weeks of 7 calendar days each,
starting from forecast_start_date. Weeks do not follow calendar months
(a month is not assumed to have exactly 4 weeks - SECTION 3). Week 1 runs
[forecast_start_date, forecast_start_date + 6 days], week 2 starts the
day after week 1 ends, and so on through week 13.

Each week's status is computed against today's date:
- COMPLETED - the week's end date is in the past
- CURRENT - today falls within the week
- FUTURE - the week hasn't started yet

## 2. Opening cash (week 1)

Opening cash for week 1 is pulled from the existing Cash Position service
(Stage 1), per entity, as of the day before forecast_start_date, broken
down by currency. This is the same "latest bank balance on or before a
date" logic used everywhere else in the system - the forecast does not
invent a second way to determine current cash. If an entity has no bank
balance at all, its opening cash is treated as 0 and a data-quality
warning is recorded (SECTION 46) rather than silently proceeding as if
that were a real zero balance.

The exact snapshot used is stored on Forecast.opening_cash_snapshot
(keyed "<entity_id>|<currency_code>") so it's always traceable to
source (SECTION 8) - re-reading a calculated forecast never requires
re-querying bank balances to explain where week 1 started from.

For week 2 onward: opening cash = the previous week's closing cash. There
is no separate re-query of bank balances for later weeks - forecast weeks
build on each other exactly as SECTION 8 specifies.

## 3. Gathering cash-flow lines

Every calculation gathers raw cash-flow lines from five sources, each
producing rows with a resolved (entity, currency, category, direction,
week, amount, probability, source_type, source_id) tuple:

1. Expected Collections (expected_collections, INFLOW) - included if
   expected_date falls in the horizon and status != CANCELLED.
2. Expected Payments (expected_payments, OUTFLOW) - same date/status
   rule.
3. Recurring Cash Flows (recurring_cash_flows) - expanded into
   concrete occurrence dates for the horizon (see section 6 below), then
   bucketed into weeks. Recurring flows never create TreasuryTransaction
   rows - they remain forecast assumptions until real data arrives
   (SECTION 21).
4. Actuals (treasury_transactions, status = POSTED) - any posted
   transaction whose event_date falls in the horizon. NON_CASH
   transactions are excluded entirely; TRANSFER transactions are
   included only if their sign is known (see section 5).
5. Manual Adjustments (forecast_adjustments, status in
   APPLIED/APPROVED) - explicit user-entered lines tied to a specific
   forecast, entity, currency, week, category, and amount.

Every gathered line becomes one ForecastLine row - nothing is
aggregated away before storage, so every dollar in a weekly total can be
traced back to the record that produced it (SECTION 23).

## 4. Value basis: GROSS vs PROBABILITY_ADJUSTED

Set per forecast (Forecast.value_basis), never silently chosen by the
system (SECTION 10):

- GROSS - every Expected Collection/Payment line uses its full
  original_amount, regardless of probability.
- PROBABILITY_ADJUSTED - Expected Collection/Payment lines with a
  non-null probability are scaled: adjusted_amount = original_amount *
  probability / 100. A line with no probability set is treated as 100%
  certain (used at full amount) rather than excluded or guessed at.

Actuals, recurring flows, and manual adjustments are never
probability-scaled - they represent either something that already
happened or something a user explicitly asserted, not an uncertain
expectation.

## 5. Transfers and the group-consolidation rule

A TreasuryTransaction classified TRANSFER only contributes to
net_transfers if its sign (money leaving vs. arriving) is known. This is
inferred from event_type_code:

| event_type_code | sign |
|---|---|
| INTERCOMPANY_PAYMENT | -1 (outflow-like) |
| INTERCOMPANY_RECEIPT | +1 (inflow-like) |
| anything else classified TRANSFER | excluded (sign ambiguous) |

Two legs of the same transfer (e.g. Entity B pays, Entity A receives)
naturally cancel to zero at the group level once both legs are in scope,
because one contributes -X and the other +X to net_transfers - no
special elimination logic is needed (SECTION 12/33). Entity-level views
still show each leg's own -X or +X correctly, since ForecastLine always
carries the entity that leg belongs to.

## 6. Recurring flow expansion

RecurringCashFlow.frequency determines how occurrence dates are
generated within the horizon:

- DAILY / WEEKLY / BIWEEKLY / CUSTOM - fixed day-interval stepping (1, 7,
  14, or custom_interval_days days respectively) from
  max(start_date, horizon_start) through min(end_date, horizon_end).
- MONTHLY / QUARTERLY - calendar-month stepping (1 or 3 months),
  preserving the original day-of-month where possible and falling back to
  the last valid day of a shorter month (e.g. a 31st-of-month flow lands
  on the 28th/29th in February).

Multiple occurrences of the same recurring flow landing in the same week
(possible for DAILY/CUSTOM with a short interval) are combined into a
single ForecastLine (amount x occurrence count) rather than one row per
occurrence, to keep line counts sane - the line's description notes the
occurrence count when greater than one.

## 7. Scenario assumptions

ForecastScenarioAssumption rows are interpreted by
_apply_scenario_assumptions before FX conversion and week aggregation.
Supported assumption_type values:

| assumption_type | effect |
|---|---|
| COLLECTION_DELAY_WEEKS | Shifts every Expected Collection line's week forward by N weeks. A collection pushed beyond week 13 is excluded, with a data-quality note explaining why. |
| COLLECTION_PROBABILITY_HAIRCUT_PCT | Reduces every Expected Collection line's probability by N percentage points (floored at 0) before the value-basis calculation runs. |
| PAYMENT_ACCELERATION_WEEKS | Shifts every Expected Payment line's week earlier by N weeks (floored at week 1). |
| UNEXPECTED_OUTFLOW_AMOUNT | Adds one flat additional OUTFLOW line in week 1, for the amount/currency/category specified on the assumption. |
| MIN_CASH_BUFFER_MULTIPLIER | Multiplies every week's looked-up minimum required liquidity by this factor (default 1.0 if not set). |

Assumptions are plain data rows attached to a specific Forecast - there
is no scenario-specific code path; BASE, CONSERVATIVE, and STRESS are
simply forecasts with zero, moderate, or aggressive assumption rows
respectively (SECTION 16-19: "do not hard-code assumptions into Python
functions").

## 8. FX conversion

Every line's adjusted_amount is converted to the forecast's
reporting_currency_code using the existing FX architecture
(app/services/fx_conversion_service.py, built entirely on FXRate -
no second FX system). The conversion looks up the latest current SPOT
rate on or before the line's week-end date; if the direct pair isn't
found, it tries the inverse pair and reciprocates. If no usable rate
exists at all, the line's reporting_amount is set to 0 and excluded
from the consolidated total, with a data-quality warning recorded - the
line itself is never dropped, so it still appears correctly in the
currency-native view (SECTION 46: never silently produce a misleading
forecast).

Opening cash per currency is converted the same way for the consolidated
week-1 opening figure.

## 9. Weekly waterfall

For each week, in order:

```
total_inflows   = sum(reporting_amount) where direction = INFLOW
total_outflows  = sum(reporting_amount) where direction = OUTFLOW
net_transfers   = sum(reporting_amount) where direction = TRANSFER
net_cash_flow   = total_inflows - total_outflows + net_transfers
closing_cash    = opening_cash + net_cash_flow
```

Then liquidity:

```
minimum_required_liquidity = lookup_thresholds() * MIN_CASH_BUFFER_MULTIPLIER
surplus_or_gap              = closing_cash - minimum_required_liquidity
liquidity_available          = closing_cash
liquidity_required            = minimum_required_liquidity
liquidity_coverage_ratio       = closing_cash / minimum_required_liquidity
                                  (null if minimum_required_liquidity is 0)
```

liquidity_available is currently just the projected closing cash - no
committed facility or overdraft-limit-aware headroom is modeled yet
(that belongs to Stage 4, Funding & Credit Facilities). This is a
deliberate simplification, not an oversight, and is called out again in
DEVELOPMENT_ROADMAP.md.

## 10. Minimum liquidity threshold lookup

LiquidityThreshold rows are matched to a forecast by scope:

- GROUP scope applies only to group-wide forecasts (legal_entity_id
  is null).
- ENTITY scope applies only when its legal_entity_id matches the
  forecast's.
- CURRENCY scope thresholds always contribute (converted to the
  reporting currency) - they represent an additional currency-specific
  floor layered on top of the group/entity minimum, not a replacement for
  it.

All applicable thresholds are summed, then multiplied by the scenario's
MIN_CASH_BUFFER_MULTIPLIER (default 1.0).

## 11. Rounding

All monetary values are stored and calculated as Decimal, never
float, and every stored amount is rounded to 2 decimal places using
ROUND_HALF_UP at the point it's persisted (_round() in
forecast_engine.py). Intermediate calculations (e.g. probability
scaling, FX multiplication) are not rounded until the final value for a
field is about to be written - so a chain of operations doesn't
accumulate rounding error before the final round. Liquidity coverage
ratio is rounded to 2 decimal places for display purposes.

## 12. Alerts (generated at calculation time)

For every week:
- surplus_or_gap < 0 -> a LIQUIDITY_GAP alert. Severity is CRITICAL if
  the gap's magnitude exceeds the minimum liquidity requirement itself
  (a genuinely severe shortfall), else HIGH.
- Otherwise, if liquidity_coverage_ratio < 1.2 -> a LOW_LIQUIDITY_COVERAGE
  alert (MEDIUM), flagging a week that's technically covered but with
  little margin.

Independently, for every currency appearing in any line: a running
native-currency balance is tracked week by week from that currency's
opening cash snapshot; the first week it goes negative generates a
CURRENCY_LIQUIDITY_SHORTFALL alert (HIGH), regardless of how healthy the
consolidated reporting-currency figure looks that week.

## 13. Forecast vs. actual and accuracy

Computed on read, never persisted (forecast_variance_service.py), from
ForecastLine rows belonging to COMPLETED weeks only. Every line is
signed (+reporting_amount for INFLOW/TRANSFER, -reporting_amount for
OUTFLOW) and bucketed by source_type: ACTUAL lines form the "actual"
side, everything else (EXPECTED_COLLECTION, EXPECTED_PAYMENT, RECURRING,
MANUAL_ADJUSTMENT, SCENARIO_ADJUSTMENT) forms the "forecast" side.

```
variance_amount     = actual_amount - forecast_amount
variance_percentage = variance_amount / abs(forecast_amount) * 100
```

This sign convention is fixed and documented so "positive variance" always
means the same thing (SECTION 25: "must clearly distinguish Forecast -
Actual and Actual - Forecast" - this implementation always computes
Actual - Forecast, and the API/UI must always present it that way, never
switch the sign per view).

Accuracy is 1 - (total |variance| / total |forecast|), clamped to
[0, 1], computed overall and separately for inflow-only and
outflow-only categories. The target variance threshold (SECTION 26: the
Treasury KPI target of <=5%) is a request parameter
(target_variance_pct, defaulting to 5), never hard-coded as a pass/fail
constant in the engine - an unfavorable result is reported plainly, not
hidden.
