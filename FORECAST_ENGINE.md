# 13-Week Cash Flow Forecast Engine - Architecture

This document covers the Stage 2 forecast engine's architecture: how it's
structured, how it fits the existing Treasury OS data model, and the
design decisions behind each piece. See FORECAST_METHODOLOGY.md for the
calculation methodology itself, and FORECAST_DATA_SOURCES.md for exactly
which Stage 0/1 data feeds it.

## Why this is a real engine, not a dashboard

Nothing in this module is precomputed or hard-coded. Every number a user
sees - opening cash, weekly inflows/outflows, closing cash, minimum
liquidity, surplus/gap, liquidity coverage, alerts, variance, accuracy -
is calculated by app/services/forecast_engine.py (and its companion
services) from real rows in bank_balances, expected_collections,
expected_payments, treasury_transactions, recurring_cash_flows,
forecast_adjustments, and fx_rates at calculation time. Recalculating
a forecast re-derives everything from scratch; nothing is cached forward
incorrectly, and a forecast that hasn't been calculated yet has no weeks
at all (the API returns 400, not stale/empty data dressed up as a result).

## Module layout

```
app/models/forecast.py                  - all Stage 2 tables
app/services/
  forecast_dates.py                     - rolling 13-week date generation
  forecast_category_mapping.py          - event-type/free-text -> category
  fx_conversion_service.py              - currency conversion (reuses FXRate)
  forecast_engine.py                    - the calculation engine itself
  forecast_views_service.py             - entity/currency drill-down views
  forecast_variance_service.py          - forecast-vs-actual, accuracy
  forecast_lifecycle_service.py         - roll-forward, publish, archive
app/api/v1/forecast.py                  - the full REST surface
app/db/seed_forecast_categories.py      - default category hierarchy
app/db/seed_forecast_demo_data.py       - DEMO DATA extension
```

## Data model summary

| Table | Role |
|---|---|
| forecast_categories | Configurable cash-flow category hierarchy (SECTION 9) |
| forecasts | One versioned forecast run/snapshot (SECTION 5) |
| forecast_weeks | Consolidated (reporting-currency) weekly waterfall |
| forecast_lines | Every individual cash-flow driver, fully traceable |
| forecast_scenario_assumptions | Configurable BASE/CONSERVATIVE/STRESS parameters |
| forecast_adjustments | Manual, additive, auditable forecast overrides |
| recurring_cash_flows | Recurring forecast assumptions (never generate actuals) |
| liquidity_thresholds | Configurable minimum cash requirements |
| forecast_alerts | Liquidity/coverage alerts generated at calculation time |

Full column-level detail is in DOMAIN_MODEL.md.

## Why weeks don't store per-entity/per-currency rows

A group-wide forecast can span many entities and currencies. Storing one
ForecastWeek row per (week, entity, currency) combination would be a
combinatorial explosion and, worse, a second source of truth that could
drift from the underlying lines. Instead:

- ForecastWeek stores exactly one row per week - the consolidated
  waterfall for the forecast's whole scope, in the forecast's reporting
  currency.
- ForecastLine carries both the entity and the original transaction
  currency on every row, plus the FX-converted reporting amount.
- Entity and currency "views" (forecast_views_service.py) are computed
  live by filtering and summing ForecastLine rows - there is exactly one
  source of truth (the lines), and every view is a projection of it.

This is also what makes SECTION 7's requirement concrete: the currency
view sums each line's original, un-converted adjusted_amount and
tracks its own native-currency running balance from the entity's actual
opening cash in that currency - so a currency-specific shortfall can never
be hidden by a healthy FX-converted consolidated number. This is verified
directly in tests/test_forecast_engine.py::test_currency_view_never_hides_a_shortfall.

## Rolling forecast and versioning

Forecast.version + Forecast.parent_forecast_id implement SECTION 4/5
together: rolling forward (POST /forecast/{id}/roll) creates a new
Forecast row one week later than its parent, copies the parent's
scenario assumptions, and leaves the parent completely untouched.
Recalculating a PUBLISHED forecast is rejected outright
(calculate_forecast raises ValueError) - a published forecast is
immutable; the only way to get new numbers is a new version.

## What-if scenarios (SECTION 32)

POST /forecast/{id}/what-if never touches the base forecast. It creates
a brand-new DRAFT Forecast (same scope/scenario/basis as the base),
copies over the base's existing scenario assumptions and manual
adjustments, adds the hypothetical adjustments from the request, runs the
full calculation engine on it, and returns a week-by-week comparison
against the base. The scratch forecast is a first-class Forecast row
(so its own drill-down, lines, etc. all work normally) - it's simply never
published unless a user explicitly promotes it.

## Authorization

Every Stage 2 endpoint reuses the existing TreasuryModule.FORECAST_13WK
enum value and the standard TreasuryAction set (VIEW/CREATE/EDIT/APPROVE/
ADJUST/EXPORT/CONFIGURE) - no RBAC schema changes were needed. The mapping
from the spec's named permissions:

| Spec permission | Implementation |
|---|---|
| FORECAST_VIEW | FORECAST_13WK + VIEW |
| FORECAST_CREATE | FORECAST_13WK + CREATE |
| FORECAST_EDIT | FORECAST_13WK + EDIT (used by /calculate) |
| FORECAST_PUBLISH | FORECAST_13WK + APPROVE |
| FORECAST_ADJUST | FORECAST_13WK + ADJUST |
| FORECAST_SCENARIO | FORECAST_13WK + CONFIGURE (assumption changes) |
| FORECAST_EXPORT | FORECAST_13WK + EXPORT |
| FORECAST_ADMIN | FORECAST_13WK + CONFIGURE (categories/recurring/thresholds) |

Because several endpoints take their target entity from the request body
or from a loaded Forecast.legal_entity_id rather than a URL path
parameter, they authenticate via get_current_user and call
_grants_permission(...) explicitly, following the same pattern
established in Stage 1 for the same reason (see ARCHITECTURE.md section 12.2).

## Two bugs found and fixed during development

1. Duplicate Postgres enum type in autogenerated migrations. Every
   Stage 2 table reusing the CashDirection enum (already created in
   Stage 1) needed create_type=False added manually to the generated
   migration - Alembic's autogenerate doesn't know the type already
   exists in the target database. Same root cause, same fix, as a Stage 1
   migration issue.
2. Lazy relationship access under async SQLAlchemy. Early versions of
   calculate_forecast and roll_forecast accessed forecast.assumptions
   or forecast.weeks as ORM relationships, which only works if the object
   was loaded with selectinload(...) for that relationship. Called from
   a freshly-created Forecast object (e.g. in tests, or the what-if
   endpoint's scratch forecast), this crashes with MissingGreenlet. Fixed
   by querying the relevant table directly with select(...) everywhere
   the engine needs assumptions or manual adjustments, rather than relying
   on the caller to have eagerly loaded the right relationship.
