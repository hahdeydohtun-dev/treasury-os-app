import datetime

from app.models.forecast import WeekStatus
from app.services.forecast_dates import generate_week_bounds, week_status


def test_generates_13_weeks_of_7_days_each():
    weeks = generate_week_bounds(datetime.date(2026, 1, 1))
    assert len(weeks) == 13
    for week_number, start, end in weeks:
        assert (end - start).days == 6


def test_weeks_are_contiguous_no_gaps_or_overlap():
    weeks = generate_week_bounds(datetime.date(2026, 1, 1))
    for i in range(1, len(weeks)):
        prev_end = weeks[i - 1][2]
        this_start = weeks[i][1]
        assert this_start == prev_end + datetime.timedelta(days=1)


def test_week_numbering_is_1_indexed():
    weeks = generate_week_bounds(datetime.date(2026, 1, 1))
    assert weeks[0][0] == 1
    assert weeks[-1][0] == 13


def test_handles_month_boundary_correctly():
    """SECTION 3: do not assume every month has exactly 4 weeks."""
    weeks = generate_week_bounds(datetime.date(2026, 1, 15))
    assert weeks[-1][2] == datetime.date(2026, 1, 15) + datetime.timedelta(days=13 * 7 - 1)


def test_week_status_future_current_completed():
    start, end = datetime.date(2026, 6, 1), datetime.date(2026, 6, 7)
    assert week_status(start, end, datetime.date(2026, 5, 1)) == WeekStatus.FUTURE
    assert week_status(start, end, datetime.date(2026, 6, 4)) == WeekStatus.CURRENT
    assert week_status(start, end, datetime.date(2026, 7, 1)) == WeekStatus.COMPLETED
