"""Unit tests for the pre-flight data checks. No warehouse needed."""

import datetime as dt

import numpy as np
import pandas as pd

from rca.preflight import check_completeness, detect_leakage, detect_rename, screen_dimensions

END = dt.date(2026, 10, 8)  # a Thursday


def _daily(weeks: int = 8, end: dt.date = END, weekend: float = 0.6):
    days = pd.date_range(end=pd.Timestamp(end), periods=7 * weeks, freq="D")
    rng = np.random.default_rng(0)
    volume = 10_000 * np.where(days.dayofweek >= 5, weekend, 1.0) * rng.normal(1, 0.02, len(days))
    return pd.Series(days.date), pd.Series(volume)


def _table(dim: str, rows, cols=("n0", "n1", "d0", "d1")) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[dim, *cols])


# --------------------------------------------------------------------------- completeness
def test_complete_data_is_untouched():
    c = check_completeness(*_daily())
    assert (c.complete_end, c.excluded_days, c.detail) == (END, [], [])


def test_incomplete_trailing_days_are_excluded():
    days, volume = _daily()
    volume.iloc[-1] *= 0.2  # today's partition has barely landed
    volume.iloc[-2] *= 0.5  # yesterday's is half loaded
    c = check_completeness(days, volume)
    assert c.excluded_days == [END, END - dt.timedelta(days=1)]
    assert c.complete_end == END - dt.timedelta(days=2)
    assert "normal Thursday" in c.detail[0] and "excluded" in c.detail[0]


def test_weekend_dip_is_not_missing_data():
    # Ends on a Sunday at 40% of a weekday: compared with previous Sundays, it is normal.
    assert check_completeness(*_daily(end=dt.date(2026, 10, 4), weekend=0.4)).excluded_days == []


def test_quiet_holiday_is_kept():
    days, volume = _daily()
    volume.iloc[-1] *= 0.3
    c = check_completeness(days, volume, holidays={END})
    assert (c.complete_end, c.excluded_days) == (END, [])
    assert "holiday" in c.detail[0] and "kept" in c.detail[0]


def test_trimming_stops_after_max_days():
    days, volume = _daily()
    volume.iloc[-5:] *= 0.1
    assert len(check_completeness(days, volume, max_days=3).excluded_days) == 3


def test_too_little_history_to_judge():
    days, volume = _daily(weeks=1)
    volume.iloc[-1] *= 0.1
    assert check_completeness(days, volume).excluded_days == []


def test_an_earlier_dip_is_not_trailing():
    days, volume = _daily()
    volume.iloc[-3] *= 0.1  # a real outage two days ago, complete data since
    assert check_completeness(days, volume).excluded_days == []


# --------------------------------------------------------------------------- renames
PLATFORMS = [("android", 350, 360, 35_000, 36_000), ("web", 200, 200, 20_000, 20_000)]


def test_rename_at_the_window_boundary():
    t = _table("platform", [("iOS", 450, 0, 45_000, 0), ("ios_app", 0, 440, 0, 44_000), *PLATFORMS])
    reason = detect_rename(t, "platform")
    assert "'iOS'" in reason and "'ios_app'" in reason and "rename" in reason


def test_rename_partway_through_the_current_window():
    # Renamed on day 3 of 7: both spellings carry current traffic, so neither vanished.
    t = _table("platform", [("iOS", 450, 130, 45_000, 13_000), ("ios", 0, 310, 0, 31_000), *PLATFORMS])
    reason = detect_rename(t, "platform")
    assert "'iOS' and 'ios'" in reason


def test_level_metric_renames_use_the_metric():
    t = _table("platform", [("iOS", 450, 0), ("ios_app", 0, 440), ("android", 350, 360)], cols=("n0", "n1"))
    assert detect_rename(t, "platform") is not None


def test_a_discontinued_value_is_not_a_rename():
    # A channel switched off, nothing new in its place: a real change the RCA should explain.
    t = _table("channel", [("tiktok", 140, 0, 14_000, 0), ("organic", 280, 300, 28_000, 30_000),
                           ("email", 180, 190, 18_000, 19_000)])
    assert detect_rename(t, "channel") is None


def test_a_rollout_is_not_a_rename():
    # A new app version ramping up while the old one shrinks: a candidate cause, not a data change.
    t = _table("app_version", [("ios_7.1", 450, 300, 45_000, 30_000), ("ios_7.2", 0, 140, 0, 14_000), *PLATFORMS])
    assert detect_rename(t, "app_version") is None


# --------------------------------------------------------------------------- leakage
CHECKOUT = [("purchased", 1_000, 900, 1_000, 900), ("abandoned_cart", 0, 0, 4_000, 4_100),
            ("browsed", 0, 0, 25_000, 26_000)]


def test_outcome_derived_dimension_for_a_rate():
    reason = detect_leakage(_table("checkout_status", CHECKOUT), "checkout_status", "ratio")
    assert "derived from the outcome" in reason and "'purchased' 100%" in reason


def test_ordinary_dimension_is_not_leakage():
    t = _table("platform", [("ios", 1_600, 1_300, 45_000, 44_000), *PLATFORMS])
    assert detect_leakage(t, "platform", "ratio") is None


def test_a_segment_that_never_converts_is_not_leakage():
    # Bots never buy, but humans are not guaranteed to: a real segment, not the outcome.
    t = _table("traffic_type", [("human", 1_000, 900, 30_000, 29_000), ("bot", 0, 0, 2_000, 2_500)])
    assert detect_leakage(t, "traffic_type", "ratio") is None


def test_outcome_derived_dimension_for_a_level_metric():
    t = _table("checkout_status", [(v, n0, n1) for v, n0, n1, *_ in CHECKOUT], cols=("n0", "n1"))
    assert "'abandoned_cart', 'browsed' never carry" in detect_leakage(t, "checkout_status", "simple")


def test_outcome_derived_dimension_for_a_non_proportion_ratio():
    # Revenue per session: the numerator exceeds the denominator, so this is the level-metric check.
    t = _table("checkout_status", [("purchased", 80_000, 72_000, 1_000, 900), *CHECKOUT[1:]])
    assert detect_leakage(t, "checkout_status", "ratio") is not None


def test_high_cardinality_zero_value_is_not_leakage():
    rows = [(f"creative_{i}", 100, 110) for i in range(8)] + [("creative_8", 0, 0)]
    assert detect_leakage(_table("ad_creative", rows, cols=("n0", "n1")), "ad_creative", "simple") is None


# --------------------------------------------------------------------------- screen_dimensions
def test_screen_dimensions_honours_meta_tags():
    status, kept, declared, platform = ("session__checkout_status", "session__order_state",
                                        "session__converted_flag", "session__platform")
    tables = {
        frozenset({status}): _table(status, CHECKOUT),
        frozenset({kept}): _table(kept, CHECKOUT),
        frozenset({platform}): _table(platform, [("ios", 1_600, 1_300, 45_000, 44_000), *PLATFORMS]),
    }
    meta = {kept: {"rca": "include"}, declared: {"rca": "outcome"}}
    screen = screen_dimensions(tables, [status, kept, declared, platform], "ratio", meta)
    assert set(screen.excluded) == {status, declared}
    assert screen.excluded[status].startswith("preflight: looks derived from the outcome")
    assert screen.excluded[declared] == "declared meta rca: outcome"
    assert len(screen.notes) == 1 and "`order_state`" in screen.notes[0] and "rca: include" in screen.notes[0]
