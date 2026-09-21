"""
Rolling 13-week date generation (SECTION 3/4).

Weeks are Monday-Sunday, 7 calendar days each, starting from the forecast
start date - so month boundaries never distort week length (SECTION 3:
"Do not assume every month has exactly four weeks" - satisfied by not
using calendar months at all).
"""
import datetime

FORECAST_HORIZON_WEEKS = 13


def generate_week_bounds(
    forecast_start_date: datetime.date, num_weeks: int = FORECAST_HORIZON_WEEKS
) -> list[tuple[int, datetime.date, datetime.date]]:
    """Returns [(week_number, start_date, end_date), ...], 1-indexed, 7 days each."""
    weeks = []
    for i in range(num_weeks):
        start = forecast_start_date + datetime.timedelta(days=7 * i)
        end = start + datetime.timedelta(days=6)
        weeks.append((i + 1, start, end))
    return weeks


def week_status(week_start: datetime.date, week_end: datetime.date, as_of: datetime.date) -> str:
    from app.models.forecast import WeekStatus

    if as_of > week_end:
        return WeekStatus.COMPLETED
    if week_start <= as_of <= week_end:
        return WeekStatus.CURRENT
    return WeekStatus.FUTURE
