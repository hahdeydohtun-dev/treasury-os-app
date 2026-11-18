"""
Stage 5G adaptive-matching settings (v1: Stage 5C one-to-one suggestions only; account-level learning only).

Settings live in the reconciliation configuration the run already uses: `matching_rule_config["adaptive"]`. That reuses the
existing configuration versioning and audit, so a settings change is always a NEW configuration version and a historical run
still points at the exact version it executed under. Absent settings mean DISABLED.

Safety bounds are enforced HERE, in code, not only in the admin endpoint, because `matching_rule_config` is free-form JSON that
the generic configuration endpoint also accepts. A value outside its bound is clamped (never exceeded), and anything that
cannot be parsed disables adaptive matching for that run, which then falls back to purely deterministic scoring.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

ADAPTIVE_RULE_VERSION = "5G-1.0"
SETTINGS_KEY = "adaptive"
SCOPE_ACCOUNT = "account"
SUPPORT_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH")           # ordered
REQUIRED_SUPPORT_CHOICES = ("LOW", "MEDIUM", "HIGH")

# Hard bounds (inclusive). The ceilings on contribution are also enforced by a database CHECK (+/-15).
BOUNDS: dict[str, tuple[Any, Any]] = {
    "min_evidence": (3, 100),
    "min_distinct_runs": (2, 20),
    "min_signal_families": (2, 4),
    "max_boost": (Decimal(0), Decimal(15)),
    "max_penalty": (Decimal(0), Decimal(15)),
    "half_life_days": (30, 730),
    "max_evidence_age_days": (30, 1095),
}
_INT_KEYS = ("min_evidence", "min_distinct_runs", "min_signal_families", "half_life_days", "max_evidence_age_days")
_DECIMAL_KEYS = ("max_boost", "max_penalty")


@dataclass(frozen=True)
class AdaptiveSettings:
    enabled: bool = False                    # disabled by default
    scope: str = SCOPE_ACCOUNT               # the only supported scope in 5G v1
    min_evidence: int = 5                    # decisions a learned pattern needs before it may influence a score
    min_distinct_runs: int = 2               # ...drawn from at least this many different runs
    min_signal_families: int = 2             # independent signal families that must be supported to apply any contribution
    max_boost: Decimal = Decimal(10)         # maximum positive adaptive contribution (points)
    max_penalty: Decimal = Decimal(10)       # maximum negative adaptive contribution (points)
    half_life_days: int = 180                # evidence weight halves for every full half-life of age
    max_evidence_age_days: int = 540         # older decisions are ignored entirely
    min_support_level: str = "LOW"           # minimum computed support level required to apply a contribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "scope": self.scope, "min_evidence": self.min_evidence,
            "min_distinct_runs": self.min_distinct_runs, "min_signal_families": self.min_signal_families,
            "max_boost": str(self.max_boost), "max_penalty": str(self.max_penalty), "half_life_days": self.half_life_days,
            "max_evidence_age_days": self.max_evidence_age_days, "min_support_level": self.min_support_level,
        }


DEFAULT_SETTINGS = AdaptiveSettings()


class AdaptiveSettingsError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ParsedSettings:
    settings: AdaptiveSettings
    warnings: list[str] = field(default_factory=list)
    invalid: bool = False        # settings were present but unusable: adaptive matching is disabled for the run


def _parse(raw: Any, strict: bool) -> tuple[AdaptiveSettings, list[str], bool]:
    """Returns (settings, problems, unusable). In strict mode every problem is an error; otherwise out-of-range numbers are
    clamped (with a warning) and anything unparsable marks the settings unusable."""
    if raw is None:
        return DEFAULT_SETTINGS, [], False
    if not isinstance(raw, dict):
        return DEFAULT_SETTINGS, ["adaptive settings must be an object"], True
    problems: list[str] = []
    unusable = False
    values: dict[str, Any] = {}
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS.to_dict()))
    if unknown:
        problems.append(f"unknown setting(s) ignored: {', '.join(unknown)}")

    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            values["enabled"] = raw["enabled"]
        else:
            problems.append("enabled must be true or false"); unusable = True
    if "scope" in raw:
        if raw["scope"] == SCOPE_ACCOUNT:
            values["scope"] = SCOPE_ACCOUNT
        else:
            problems.append("scope must be 'account': only account-level learning is supported in Stage 5G"); unusable = True
    if "min_support_level" in raw:
        if raw["min_support_level"] in REQUIRED_SUPPORT_CHOICES:
            values["min_support_level"] = raw["min_support_level"]
        else:
            problems.append(f"min_support_level must be one of {', '.join(REQUIRED_SUPPORT_CHOICES)}"); unusable = True
    for key in _INT_KEYS + _DECIMAL_KEYS:
        if key not in raw:
            continue
        low, high = BOUNDS[key]
        try:
            if isinstance(raw[key], bool):
                raise TypeError
            number = Decimal(str(raw[key]))
            if key in _INT_KEYS and number != number.to_integral_value():
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            problems.append(f"{key} must be a number"); unusable = True
            continue
        value = int(number) if key in _INT_KEYS else number
        if value < low or value > high:
            problems.append(f"{key} must be between {low} and {high}" + ("" if strict else f"; clamped from {value}"))
            value = min(max(value, low), high)
        values[key] = value
    settings = AdaptiveSettings(**{**DEFAULT_SETTINGS.__dict__, **values})
    if settings.max_evidence_age_days < settings.half_life_days:
        problems.append("max_evidence_age_days must not be shorter than half_life_days" + ("" if strict else "; raised to match"))
        settings = AdaptiveSettings(**{**settings.__dict__, "max_evidence_age_days": settings.half_life_days})
    if unusable:
        settings = AdaptiveSettings(**DEFAULT_SETTINGS.__dict__)       # fail safe: deterministic only
    return settings, problems, unusable


def parse_settings(matching_rule_config: Any) -> ParsedSettings:
    """Runtime parsing (lenient and fail-safe). `matching_rule_config` is the configuration's JSON, or None."""
    raw = matching_rule_config.get(SETTINGS_KEY) if isinstance(matching_rule_config, dict) else None
    settings, problems, unusable = _parse(raw, strict=False)
    return ParsedSettings(settings=settings, warnings=problems, invalid=unusable)


def validate_settings_payload(raw: Any) -> AdaptiveSettings:
    """Strict validation for the administration endpoint: any problem, including an out-of-range value, is rejected."""
    settings, problems, _ = _parse(raw, strict=True)
    if problems:
        raise AdaptiveSettingsError(problems)
    return settings


def settings_with_defaults(raw: Any) -> dict[str, Any]:
    return parse_settings({SETTINGS_KEY: raw}).settings.to_dict()
