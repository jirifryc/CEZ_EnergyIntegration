"""Tests for the HDO-based NT/VT interval classification logic."""
from custom_components.cez_energy import _is_nt_interval


def _make_signals(date_str: str, casy: str):
    return {"signals": [{"datum": date_str, "casy": casy}]}


def test_interval_within_nt_window():
    signals = _make_signals("15.04.2026", "00:00-06:00;20:00-23:59")
    assert _is_nt_interval("15.04.2026 01:00", signals) is True
    assert _is_nt_interval("15.04.2026 05:45", signals) is True
    assert _is_nt_interval("15.04.2026 20:15", signals) is True


def test_interval_outside_nt_window():
    signals = _make_signals("15.04.2026", "00:00-06:00;20:00-23:59")
    assert _is_nt_interval("15.04.2026 10:00", signals) is False
    assert _is_nt_interval("15.04.2026 12:30", signals) is False
    assert _is_nt_interval("15.04.2026 18:00", signals) is False


def test_interval_at_boundary():
    signals = _make_signals("15.04.2026", "06:00-08:00")
    # 06:15 -> interval is 06:00-06:15, overlaps with HDO 06:00-08:00
    assert _is_nt_interval("15.04.2026 06:15", signals) is True
    # 08:15 -> interval is 08:00-08:15, does not overlap (HDO ends at 08:00)
    assert _is_nt_interval("15.04.2026 08:15", signals) is False


def test_no_signals_returns_vt():
    assert _is_nt_interval("15.04.2026 01:00", {"signals": []}) is False


def test_wrong_date_returns_vt():
    signals = _make_signals("16.04.2026", "00:00-06:00")
    assert _is_nt_interval("15.04.2026 01:00", signals) is False


def test_invalid_timestamp():
    signals = _make_signals("15.04.2026", "00:00-06:00")
    assert _is_nt_interval("invalid", signals) is False
    assert _is_nt_interval("", signals) is False


def test_multiple_signals_any_match():
    signals = {
        "signals": [
            {"datum": "15.04.2026", "casy": "00:00-03:00"},
            {"datum": "15.04.2026", "casy": "20:00-23:59"},
        ]
    }
    assert _is_nt_interval("15.04.2026 02:00", signals) is True
    assert _is_nt_interval("15.04.2026 21:00", signals) is True
    assert _is_nt_interval("15.04.2026 12:00", signals) is False


def test_casy_with_2400_end_time():
    """HDO signals from ČEZ use 24:00 to mean midnight (end of day)."""
    signals = _make_signals("15.04.2026", "20:30-24:00")
    assert _is_nt_interval("15.04.2026 21:00", signals) is True
    assert _is_nt_interval("15.04.2026 23:45", signals) is True
    # 24:00 timestamp = midnight on 16.04, interval 23:45-24:00 of the 15th
    assert _is_nt_interval("15.04.2026 24:00", signals) is True
    assert _is_nt_interval("15.04.2026 19:00", signals) is False


def test_real_world_signal_pattern():
    """Test with actual signal pattern from ČEZ DIP API."""
    signals = _make_signals(
        "15.04.2026",
        "00:00-07:50;   08:50-11:50;   12:50-14:35;   15:35-19:30;   20:30-24:00",
    )
    assert _is_nt_interval("15.04.2026 01:00", signals) is True
    assert _is_nt_interval("15.04.2026 07:45", signals) is True
    # 08:00 = interval 07:45-08:00, still overlaps with 00:00-07:50
    assert _is_nt_interval("15.04.2026 08:00", signals) is True
    # 08:15 = interval 08:00-08:15, falls in gap between 07:50 and 08:50
    assert _is_nt_interval("15.04.2026 08:15", signals) is False
    assert _is_nt_interval("15.04.2026 09:00", signals) is True
    assert _is_nt_interval("15.04.2026 23:00", signals) is True
    assert _is_nt_interval("15.04.2026 24:00", signals) is True
