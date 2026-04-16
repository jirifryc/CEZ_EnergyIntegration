"""Tests for the historical data import functions.

Unit tests use fabricated API responses to verify the statistics building logic.
Integration tests (requiring credentials) hit the live PND API.
"""
import datetime as dt
import logging
import os
import zoneinfo

import pytest

from custom_components.cez_energy import (
    _build_daily_statistics,
    _build_interval_statistics,
)
from custom_components.cez_energy.const import parse_cz_datetime

_LOGGER = logging.getLogger(__name__)
TZ = zoneinfo.ZoneInfo("Europe/Prague")


# ---------------------------------------------------------------------------
# Fabricated API responses for unit tests
# ---------------------------------------------------------------------------

def _make_daily_response(entries_by_series: dict) -> dict:
    """Build a fake daily API response.

    entries_by_series: {"nt": [(ts_str, val), ...], "vt": [...], "total": [...]}
    """
    series = []
    name_map = {"nt": "+E_NT/foo", "vt": "+E_VT/foo", "total": "+E/foo"}
    for key, entries in entries_by_series.items():
        series.append({
            "name": name_map[key],
            "data": [[ts, val] for ts, val in entries],
        })
    return {"hasData": True, "series": series}


def _make_interval_response(entries: list) -> dict:
    """Build a fake interval API response.

    entries: [(ts_str, kw_value), ...]
    """
    return {
        "hasData": True,
        "series": [{
            "name": "power",
            "data": [[ts, kw, "OK"] for ts, kw in entries],
        }],
    }


# ---------------------------------------------------------------------------
# Unit tests: _build_daily_statistics
# ---------------------------------------------------------------------------

class TestBuildDailyStatistics:
    def test_basic_three_day_cumulative(self):
        """Three days of cumulative readings produce correct sum deltas."""
        raw = _make_daily_response({
            "nt": [
                ("10.01.2026 00:00", 1000.0),
                ("11.01.2026 00:00", 1005.0),
                ("12.01.2026 00:00", 1012.0),
            ],
            "vt": [
                ("10.01.2026 00:00", 500.0),
                ("11.01.2026 00:00", 503.0),
                ("12.01.2026 00:00", 508.0),
            ],
            "total": [
                ("10.01.2026 00:00", 1500.0),
                ("11.01.2026 00:00", 1508.0),
                ("12.01.2026 00:00", 1520.0),
            ],
        })

        result = _build_daily_statistics([raw])

        assert "nt" in result
        assert "vt" in result
        assert "total" in result

        nt = result["nt"]
        assert len(nt) == 3
        assert nt[0]["state"] == 1000.0
        assert nt[0]["sum"] == 0.0
        assert nt[1]["sum"] == 5.0
        assert nt[2]["sum"] == 12.0

        vt = result["vt"]
        assert vt[0]["sum"] == 0.0
        assert vt[2]["sum"] == 8.0

        total = result["total"]
        assert total[0]["sum"] == 0.0
        assert total[2]["sum"] == 20.0

    def test_timestamps_are_tz_aware(self):
        raw = _make_daily_response({
            "total": [("15.03.2026 00:00", 100.0)],
        })
        result = _build_daily_statistics([raw])
        point = result["total"][0]
        assert point["start"].tzinfo is not None
        assert point["start"].tzinfo == TZ

    def test_empty_response(self):
        result = _build_daily_statistics([{"hasData": False}])
        assert result == {}

    def test_multiple_chunks_merged(self):
        """Data from multiple API calls should merge and sort correctly."""
        chunk1 = _make_daily_response({
            "total": [
                ("01.01.2026 00:00", 100.0),
                ("02.01.2026 00:00", 110.0),
            ],
        })
        chunk2 = _make_daily_response({
            "total": [
                ("03.01.2026 00:00", 125.0),
                ("04.01.2026 00:00", 140.0),
            ],
        })
        result = _build_daily_statistics([chunk1, chunk2])
        total = result["total"]
        assert len(total) == 4
        assert total[0]["sum"] == 0.0
        assert total[3]["sum"] == 40.0
        for i in range(1, len(total)):
            assert total[i]["start"] > total[i - 1]["start"]


# ---------------------------------------------------------------------------
# Unit tests: _build_interval_statistics
# ---------------------------------------------------------------------------

class TestBuildIntervalStatistics:
    def _make_full_hour(self, date_str: str, hour: int, kw: float = 1.0):
        """Create 4 intervals (15-min each) for a single hour.

        Timestamps are end-of-interval: HH:15, HH:30, HH:45, (HH+1):00.
        """
        base = dt.datetime.strptime(date_str, "%d.%m.%Y").replace(hour=hour)
        entries = []
        for quarter in range(1, 5):
            end_time = base + dt.timedelta(minutes=quarter * 15)
            ts = end_time.strftime("%d.%m.%Y %H:%M")
            entries.append((ts, kw))
        return entries

    def test_single_hour_four_intervals(self):
        """4 intervals in one hour -> 1 hourly point with correct kWh."""
        entries = self._make_full_hour("10.01.2026", 6, kw=2.0)
        raw = _make_interval_response(entries)
        result = _build_interval_statistics([raw])

        assert "energy" in result
        assert "power_mean" in result
        assert "power_max" in result

        assert len(result["energy"]) == 1
        energy = result["energy"][0]
        assert energy["state"] == 2.0  # 4 * 2.0 kW * 0.25h = 2.0 kWh
        assert energy["sum"] == 2.0

        assert result["power_mean"][0]["mean"] == 2.0
        assert result["power_max"][0]["max"] == 2.0

    def test_two_hours_cumulative_sum(self):
        """Two hours of data -> cumulative sum increases."""
        entries = (
            self._make_full_hour("10.01.2026", 6, kw=1.0) +
            self._make_full_hour("10.01.2026", 7, kw=3.0)
        )
        raw = _make_interval_response(entries)
        result = _build_interval_statistics([raw])

        energy = result["energy"]
        assert len(energy) == 2
        assert energy[0]["state"] == 1.0  # 4 * 1.0 * 0.25
        assert energy[0]["sum"] == 1.0
        assert energy[1]["state"] == 3.0  # 4 * 3.0 * 0.25
        assert energy[1]["sum"] == 4.0  # cumulative

    def test_varying_power_within_hour(self):
        """Different kW in same hour -> mean and max are correct."""
        entries = [
            ("10.01.2026 10:15", 1.0),
            ("10.01.2026 10:30", 2.0),
            ("10.01.2026 10:45", 3.0),
            ("10.01.2026 11:00", 4.0),
        ]
        raw = _make_interval_response(entries)
        result = _build_interval_statistics([raw])

        assert result["power_mean"][0]["mean"] == 2.5
        assert result["power_max"][0]["max"] == 4.0
        assert result["energy"][0]["state"] == 2.5  # (1+2+3+4) * 0.25

    def test_empty_response(self):
        result = _build_interval_statistics([{"hasData": False}])
        assert result == {}

    def test_timestamps_are_tz_aware(self):
        entries = self._make_full_hour("10.01.2026", 8, kw=1.0)
        raw = _make_interval_response(entries)
        result = _build_interval_statistics([raw])
        assert result["energy"][0]["start"].tzinfo == TZ

    def test_multi_day_data(self):
        """Data spanning multiple days groups into correct hourly buckets."""
        entries = (
            self._make_full_hour("10.01.2026", 23, kw=1.0) +
            self._make_full_hour("11.01.2026", 0, kw=2.0)
        )
        raw = _make_interval_response(entries)
        result = _build_interval_statistics([raw])

        energy = result["energy"]
        assert len(energy) == 2
        assert energy[0]["start"].day == 10
        assert energy[0]["start"].hour == 23
        assert energy[1]["start"].day == 11
        assert energy[1]["start"].hour == 0


# ---------------------------------------------------------------------------
# Live integration tests (require credentials)
# ---------------------------------------------------------------------------

USERNAME = os.environ.get("CEZ_USERNAME")
PASSWORD = os.environ.get("CEZ_PASSWORD")
ELECTROMETER_ID = os.environ.get("CEZ_ELECTROMETER")

skip_no_electrometer = pytest.mark.skipif(
    not (USERNAME and PASSWORD and ELECTROMETER_ID),
    reason="CEZ_USERNAME, CEZ_PASSWORD, and CEZ_ELECTROMETER env vars not set",
)


@pytest.fixture(scope="module")
def pnd_client():
    from custom_components.cez_energy.rest_client.pnd_client import CezPndRestClient
    client = CezPndRestClient()
    client.login(USERNAME, PASSWORD)
    return client


class TestHistoryImportLive:
    @skip_no_electrometer
    def test_fetch_and_build_daily_statistics(self, pnd_client):
        """Fetch 7 days of daily data and verify statistics structure."""
        today = dt.date.today()
        week_ago = today - dt.timedelta(days=7)

        raw = pnd_client.get_daily_data(ELECTROMETER_ID, week_ago, today)
        result = _build_daily_statistics([raw])

        assert len(result) > 0, "Expected at least one series"
        for key, stats in result.items():
            _LOGGER.info("Daily %s: %d points", key, len(stats))
            assert len(stats) > 0
            for pt in stats:
                assert "start" in pt
                assert "state" in pt
                assert "sum" in pt
                assert pt["start"].tzinfo is not None
            assert stats[0]["sum"] == 0.0
            for i in range(1, len(stats)):
                assert stats[i]["sum"] >= stats[i - 1]["sum"], (
                    f"sum should be monotonically increasing: "
                    f"{stats[i-1]['sum']} -> {stats[i]['sum']}"
                )

    @skip_no_electrometer
    def test_fetch_and_build_interval_statistics(self, pnd_client):
        """Fetch 3 days of interval data in a single request and verify statistics."""
        today = dt.date.today()
        three_days_ago = today - dt.timedelta(days=3)

        raw = pnd_client.get_interval_data(ELECTROMETER_ID, three_days_ago, today)
        result = _build_interval_statistics([raw])

        assert "energy" in result
        assert "power_mean" in result
        assert "power_max" in result

        energy = result["energy"]
        _LOGGER.info("Interval energy: %d hourly points", len(energy))
        assert len(energy) > 0

        for pt in energy:
            assert "start" in pt
            assert "state" in pt
            assert "sum" in pt
            assert pt["start"].tzinfo is not None
            assert pt["state"] >= 0

        assert energy[0]["sum"] >= 0
        for i in range(1, len(energy)):
            assert energy[i]["sum"] >= energy[i - 1]["sum"]

        for pt in result["power_mean"]:
            assert "mean" in pt
            assert pt["mean"] >= 0

        for pt in result["power_max"]:
            assert "max" in pt
            assert pt["max"] >= 0
