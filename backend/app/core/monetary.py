"""
Persisted monetary precision policy.

Treasury OS persists monetary amounts at TWO decimal places system-wide:
every monetary column is Numeric(20,2), the forecast/investment/facility
engines quantize to two places with ROUND_HALF_UP, and API responses are
two-decimal strings. `Currency.decimal_places` therefore cannot be allowed
to declare more precision than the ledger can hold (0, 1 or 2 are
supported; a zero-decimal currency such as JPY is stored as e.g. 150123.00).

PostgreSQL silently ROUNDS a value with extra digits when inserting it into
a Numeric(20,2) column, so unsupported precision must be refused at the
write boundary rather than discovered later. `Decimal("100.250")` has a
trailing zero but is the same value as 100.25 and is accepted.
"""
from decimal import Decimal

PERSISTED_MONETARY_DECIMAL_PLACES = 2
_QUANTUM = Decimal(1).scaleb(-PERSISTED_MONETARY_DECIMAL_PLACES)


def has_excess_precision(value: Decimal) -> bool:
    """True if `value` cannot be stored at the persisted precision without changing its numeric value."""
    return value != value.quantize(_QUANTUM)


def assert_persistable_amount(value, field_name: str):
    """Model-level backstop: refuse (never silently round) an amount the ledger cannot represent."""
    if isinstance(value, Decimal) and has_excess_precision(value):
        raise ValueError(
            f"{field_name} {value} has more than {PERSISTED_MONETARY_DECIMAL_PLACES} decimal places; "
            f"persisted monetary precision is {PERSISTED_MONETARY_DECIMAL_PLACES} and the value would be "
            f"silently rounded."
        )
    return value
