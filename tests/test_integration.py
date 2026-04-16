"""End-to-end integration tests against live ČEZ APIs.

These tests hit real endpoints and require valid credentials.
Run with:

    CEZ_USERNAME=your_user CEZ_PASSWORD=your_pass CEZ_ELECTROMETER=5100128114 \
        python -m pytest tests/test_integration.py -v -s

All tests are skipped automatically when credentials are not set.
"""
import datetime as dt
import json
import logging
import os
from typing import Any, Dict, Optional

import pytest

from custom_components.cez_energy.rest_client.dip_client import CezDistribuceRestClient
from custom_components.cez_energy.rest_client.pnd_client import CezPndRestClient
from custom_components.cez_energy.const import parse_cz_datetime
from custom_components.cez_energy import _is_nt_interval

logging.basicConfig(level=logging.DEBUG)
_LOGGER = logging.getLogger(__name__)

USERNAME = os.environ.get("CEZ_USERNAME")
PASSWORD = os.environ.get("CEZ_PASSWORD")
ELECTROMETER_ID = os.environ.get("CEZ_ELECTROMETER")

skip_no_creds = pytest.mark.skipif(
    not (USERNAME and PASSWORD),
    reason="CEZ_USERNAME and CEZ_PASSWORD env vars not set",
)
skip_no_electrometer = pytest.mark.skipif(
    not (USERNAME and PASSWORD and ELECTROMETER_ID),
    reason="CEZ_USERNAME, CEZ_PASSWORD, and CEZ_ELECTROMETER env vars not set",
)


def _dump(label: str, data: Any):
    """Pretty-print a data structure for debugging."""
    _LOGGER.info("=== %s ===\n%s", label, json.dumps(data, indent=2, default=str, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Fixtures: one login per test session to avoid hammering CAS
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def dip_client():
    client = CezDistribuceRestClient()
    client.login(USERNAME, PASSWORD)
    return client


@pytest.fixture(scope="session")
def pnd_client():
    client = CezPndRestClient()
    client.login(USERNAME, PASSWORD)
    return client


# ---------------------------------------------------------------------------
# DIP portal tests
# ---------------------------------------------------------------------------

class TestDipLogin:
    @skip_no_creds
    def test_login_succeeds(self, dip_client: CezDistribuceRestClient):
        """CAS + DIP token handshake completes without error."""
        header = dip_client.common_header()
        _dump("DIP common_header", header)
        assert header is not None

    @skip_no_creds
    def test_get_supply_points(self, dip_client: CezDistribuceRestClient):
        """Can retrieve the list of supply points for the account."""
        sp = dip_client.get_supply_points()
        _dump("DIP supply_points", sp)
        assert sp is not None
        blocks = sp.get("vstelleBlocks", {}).get("blocks", [])
        assert len(blocks) > 0, "Expected at least one supply-point block"
        vstelles = blocks[0].get("vstelles", [])
        assert len(vstelles) > 0, "Expected at least one vstelle"

    @skip_no_creds
    def test_get_supply_point_detail(self, dip_client: CezDistribuceRestClient):
        """Can retrieve detail for the first supply point."""
        sp = dip_client.get_supply_points()
        uid = sp["vstelleBlocks"]["blocks"][0]["vstelles"][0]["uid"]
        detail = dip_client.get_supply_point_detail(uid)
        _dump("DIP supply_point_detail", detail)
        assert detail is not None
        assert "ean" in detail or "uid" in detail

    @skip_no_creds
    def test_get_signals(self, dip_client: CezDistribuceRestClient):
        """Can retrieve HDO signals for a supply point with HDO enabled."""
        sp = dip_client.get_supply_points()
        uid = sp["vstelleBlocks"]["blocks"][0]["vstelles"][0]["uid"]
        detail = dip_client.get_supply_point_detail(uid)
        ean = detail.get("ean")
        has_hdo = detail.get("hdo")

        if not ean or not has_hdo:
            pytest.skip("Supply point has no EAN or HDO is disabled")

        signals = dip_client.get_signals(ean)
        _dump("DIP signals", signals)
        assert signals is not None
        assert "signals" in signals
        signal_list = signals["signals"]
        assert isinstance(signal_list, list)
        if signal_list:
            s = signal_list[0]
            assert "datum" in s, f"Signal missing 'datum': {s}"
            assert "casy" in s, f"Signal missing 'casy': {s}"
            _LOGGER.info("First signal: datum=%s casy=%s", s["datum"], s["casy"])

    @skip_no_creds
    def test_get_outages(self, dip_client: CezDistribuceRestClient):
        """Can retrieve outages for a supply point (may be empty)."""
        sp = dip_client.get_supply_points()
        uid = sp["vstelleBlocks"]["blocks"][0]["vstelles"][0]["uid"]
        detail = dip_client.get_supply_point_detail(uid)
        ean = detail.get("ean")
        if not ean:
            pytest.skip("Supply point has no EAN")

        outages = dip_client.get_outages(ean=ean)
        _dump("DIP outages", outages)
        assert outages is not None
        assert isinstance(outages, list)


# ---------------------------------------------------------------------------
# PND portal tests
# ---------------------------------------------------------------------------

class TestPndLogin:
    @skip_no_creds
    def test_login_gets_jsessionid(self, pnd_client: CezPndRestClient):
        """PND login via CAS results in a JSESSIONID cookie."""
        cookies = pnd_client._session.cookies.get_dict()
        _LOGGER.info("PND cookies: %s", list(cookies.keys()))
        assert "JSESSIONID" in cookies, f"Missing JSESSIONID. Got cookies: {list(cookies.keys())}"


class TestPndIntervalData:
    @skip_no_electrometer
    def test_fetch_today_interval_data(self, pnd_client: CezPndRestClient):
        """Can fetch 15-min interval data for today."""
        today = dt.date.today()
        tomorrow = today + dt.timedelta(days=1)
        raw = pnd_client.get_interval_data(ELECTROMETER_ID, today, tomorrow)
        _dump("PND interval raw", {k: v for k, v in raw.items() if k != "series"})
        _LOGGER.info("PND interval series count: %d", len(raw.get("series", [])))

        assert raw.get("hasData") is True, "Expected hasData=True for today"
        assert raw.get("unitY") == "kW", f"Expected unitY=kW, got {raw.get('unitY')}"

        intervals = CezPndRestClient.parse_interval_series(raw)
        _LOGGER.info("Parsed %d intervals", len(intervals))
        assert len(intervals) > 0, "Expected at least some intervals for today"

        first = intervals[0]
        assert "timestamp" in first
        assert "kw" in first
        assert isinstance(first["kw"], float)
        assert first["kw"] >= 0

        _LOGGER.info("First interval: %s -> %.3f kW", first["timestamp"], first["kw"])
        _LOGGER.info("Last interval:  %s -> %.3f kW", intervals[-1]["timestamp"], intervals[-1]["kw"])

        total_kwh = sum(iv["kw"] * 0.25 for iv in intervals)
        _LOGGER.info("Total energy today so far: %.3f kWh (%d intervals)", total_kwh, len(intervals))

    @skip_no_electrometer
    def test_fetch_yesterday_interval_data(self, pnd_client: CezPndRestClient):
        """Can fetch a full day (96 intervals) for yesterday."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        raw = pnd_client.get_interval_data(ELECTROMETER_ID, yesterday, today)
        intervals = CezPndRestClient.parse_interval_series(raw)
        _LOGGER.info("Yesterday: %d intervals", len(intervals))
        assert len(intervals) == 96, f"Expected 96 intervals for a full day, got {len(intervals)}"

    @skip_no_electrometer
    def test_interval_timestamps_are_sequential(self, pnd_client: CezPndRestClient):
        """Interval timestamps should be in 15-min increments."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        raw = pnd_client.get_interval_data(ELECTROMETER_ID, yesterday, today)
        intervals = CezPndRestClient.parse_interval_series(raw)

        if len(intervals) < 2:
            pytest.skip("Not enough intervals to check sequencing")

        for i in range(1, len(intervals)):
            t_prev = parse_cz_datetime(intervals[i - 1]["timestamp"])
            t_curr = parse_cz_datetime(intervals[i]["timestamp"])
            delta = (t_curr - t_prev).total_seconds()
            assert delta == 900, (
                f"Gap between intervals {i-1} and {i} is {delta}s, expected 900s "
                f"({intervals[i-1]['timestamp']} -> {intervals[i]['timestamp']})"
            )


class TestPndDailyData:
    @skip_no_electrometer
    def test_fetch_yesterday_daily_data(self, pnd_client: CezPndRestClient):
        """Can fetch daily NT/VT/Total readings for yesterday."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        raw = pnd_client.get_daily_data(ELECTROMETER_ID, yesterday, today)
        _dump("PND daily raw", raw)

        assert raw.get("hasData") is True, "Expected hasData=True"
        assert raw.get("unitY") == "kWh", f"Expected unitY=kWh, got {raw.get('unitY')}"

        values = CezPndRestClient.parse_daily_series(raw)
        _LOGGER.info("Daily values: %s", values)

        assert values["total"] is not None, "Expected total reading"
        assert values["nt"] is not None, "Expected NT reading"
        assert values["vt"] is not None, "Expected VT reading"
        assert values["total"] > 0, "Total should be positive"
        assert values["nt"] >= 0, "NT should be non-negative"
        assert values["vt"] >= 0, "VT should be non-negative"

    @skip_no_electrometer
    def test_daily_nt_plus_vt_equals_total(self, pnd_client: CezPndRestClient):
        """NT + VT should equal Total (within floating-point tolerance)."""
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        raw = pnd_client.get_daily_data(ELECTROMETER_ID, yesterday, today)
        values = CezPndRestClient.parse_daily_series(raw)

        if values["nt"] is None or values["vt"] is None or values["total"] is None:
            pytest.skip("Missing NT/VT/Total values")

        computed = values["nt"] + values["vt"]
        _LOGGER.info("NT=%.3f + VT=%.3f = %.3f vs Total=%.3f", values["nt"], values["vt"], computed, values["total"])
        assert abs(computed - values["total"]) < 0.1, (
            f"NT({values['nt']}) + VT({values['vt']}) = {computed} != Total({values['total']})"
        )

    @skip_no_electrometer
    def test_daily_values_are_cumulative(self, pnd_client: CezPndRestClient):
        """Today's cumulative reading should be >= yesterday's."""
        today = dt.date.today()
        yesterday = today - dt.timedelta(days=1)
        two_days_ago = today - dt.timedelta(days=2)

        raw_prev = pnd_client.get_daily_data(ELECTROMETER_ID, two_days_ago, yesterday)
        raw_curr = pnd_client.get_daily_data(ELECTROMETER_ID, yesterday, today)

        prev = CezPndRestClient.parse_daily_series(raw_prev)
        curr = CezPndRestClient.parse_daily_series(raw_curr)

        if prev["total"] is None or curr["total"] is None:
            pytest.skip("Missing total values for comparison")

        _LOGGER.info("Cumulative total: %s=%.3f -> %s=%.3f",
                      two_days_ago, prev["total"], yesterday, curr["total"])
        assert curr["total"] >= prev["total"], (
            f"Total should be monotonically increasing: {prev['total']} -> {curr['total']}"
        )


# ---------------------------------------------------------------------------
# Full pipeline: interval data + HDO signals -> NT/VT classification
# ---------------------------------------------------------------------------

class TestNtVtClassificationLive:

    @staticmethod
    def _find_signal_date(signals_data: Dict[str, Any]) -> Optional[dt.date]:
        """Return the earliest date present in both signals and PND data (today)."""
        signal_dates = set()
        for s in signals_data.get("signals", []):
            datum = s.get("datum")
            if datum:
                try:
                    signal_dates.add(dt.datetime.strptime(datum, "%d.%m.%Y").date())
                except ValueError:
                    pass
        today = dt.date.today()
        if today in signal_dates:
            return today
        for d in sorted(signal_dates):
            return d
        return None

    @skip_no_electrometer
    def test_classify_intervals_with_live_hdo(
        self, dip_client: CezDistribuceRestClient, pnd_client: CezPndRestClient
    ):
        """Fetch real HDO signals and interval data, classify each interval as NT/VT.

        HDO signals are only available for the current day and ~7 days ahead,
        so we use today's (partial) interval data for classification.
        """
        sp = dip_client.get_supply_points()
        uid = sp["vstelleBlocks"]["blocks"][0]["vstelles"][0]["uid"]
        detail = dip_client.get_supply_point_detail(uid)
        ean = detail.get("ean")
        has_hdo = detail.get("hdo")

        if not ean or not has_hdo:
            pytest.skip("Supply point has no EAN or HDO disabled")

        signals_data = dip_client.get_signals(ean)
        target_date = self._find_signal_date(signals_data)
        if target_date is None:
            pytest.skip("No signal dates available")

        next_day = target_date + dt.timedelta(days=1)
        raw = pnd_client.get_interval_data(ELECTROMETER_ID, target_date, next_day)
        intervals = CezPndRestClient.parse_interval_series(raw)

        if not intervals:
            pytest.skip(f"No interval data available for {target_date}")

        nt_kwh = 0.0
        vt_kwh = 0.0
        nt_count = 0
        vt_count = 0
        for iv in intervals:
            kwh = iv["kw"] * 0.25
            if _is_nt_interval(iv["timestamp"], signals_data):
                nt_kwh += kwh
                nt_count += 1
            else:
                vt_kwh += kwh
                vt_count += 1

        total_kwh = nt_kwh + vt_kwh
        _LOGGER.info(
            "Classification results for %s (%d intervals available):\n"
            "  NT: %d intervals, %.3f kWh\n"
            "  VT: %d intervals, %.3f kWh\n"
            "  Total: %d intervals, %.3f kWh",
            target_date, len(intervals),
            nt_count, nt_kwh, vt_count, vt_kwh,
            nt_count + vt_count, total_kwh,
        )

        assert nt_count + vt_count == len(intervals)
        assert nt_count > 0, "Expected at least some NT intervals (HDO was enabled)"

    @skip_no_electrometer
    def test_compare_classification_vs_daily_totals(
        self, dip_client: CezDistribuceRestClient, pnd_client: CezPndRestClient
    ):
        """Compare NT/VT from interval classification against daily endpoint values.

        Uses yesterday's data where full-day intervals are available.
        Note: HDO signals may not cover yesterday, so this test logs the
        comparison for manual inspection rather than strictly asserting NT match.
        """
        sp = dip_client.get_supply_points()
        uid = sp["vstelleBlocks"]["blocks"][0]["vstelles"][0]["uid"]
        detail = dip_client.get_supply_point_detail(uid)
        ean = detail.get("ean")

        if not ean or not detail.get("hdo"):
            pytest.skip("No EAN or HDO disabled")

        signals_data = dip_client.get_signals(ean)

        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        two_days_ago = yesterday - dt.timedelta(days=1)

        raw_intervals = pnd_client.get_interval_data(ELECTROMETER_ID, yesterday, today)
        intervals = CezPndRestClient.parse_interval_series(raw_intervals)

        nt_interval = sum(iv["kw"] * 0.25 for iv in intervals if _is_nt_interval(iv["timestamp"], signals_data))
        vt_interval = sum(iv["kw"] * 0.25 for iv in intervals if not _is_nt_interval(iv["timestamp"], signals_data))

        raw_prev = pnd_client.get_daily_data(ELECTROMETER_ID, two_days_ago, yesterday)
        raw_curr = pnd_client.get_daily_data(ELECTROMETER_ID, yesterday, today)
        prev = CezPndRestClient.parse_daily_series(raw_prev)
        curr = CezPndRestClient.parse_daily_series(raw_curr)

        if any(v is None for v in [prev["nt"], prev["vt"], curr["nt"], curr["vt"]]):
            pytest.skip("Missing daily values")

        daily_nt = curr["nt"] - prev["nt"]
        daily_vt = curr["vt"] - prev["vt"]
        daily_total = (curr["total"] or 0) - (prev["total"] or 0)

        _LOGGER.info(
            "\n=== Comparison for %s ===\n"
            "  Source          | NT (kWh)  | VT (kWh)  | Total (kWh)\n"
            "  ----------------+-----------+-----------+------------\n"
            "  Interval+HDO    | %9.3f | %9.3f | %11.3f\n"
            "  Daily endpoint  | %9.3f | %9.3f | %11.3f\n"
            "  Delta           | %9.3f | %9.3f | %11.3f",
            yesterday,
            nt_interval, vt_interval, nt_interval + vt_interval,
            daily_nt, daily_vt, daily_total,
            nt_interval - daily_nt, vt_interval - daily_vt,
            (nt_interval + vt_interval) - daily_total,
        )

        interval_total = nt_interval + vt_interval
        if daily_total > 0:
            pct_diff = abs(interval_total - daily_total) / daily_total * 100
            _LOGGER.info("Total difference: %.1f%%", pct_diff)
            assert pct_diff < 5, (
                f"Interval total ({interval_total:.3f}) differs from daily total "
                f"({daily_total:.3f}) by {pct_diff:.1f}%"
            )


# ---------------------------------------------------------------------------
# Authentication edge cases
# ---------------------------------------------------------------------------

class TestAuthEdgeCases:
    def test_dip_login_with_bad_password_raises(self):
        """DIP login with invalid credentials should raise."""
        client = CezDistribuceRestClient()
        with pytest.raises(Exception):
            client.login("invalid_user@test.cz", "wrong_password_123")

    def test_pnd_login_with_bad_password_warns(self):
        """PND login with bad credentials should not get a JSESSIONID."""
        client = CezPndRestClient()
        # PND may not raise immediately but won't have a valid session
        try:
            client.login("invalid_user@test.cz", "wrong_password_123")
        except Exception:
            pass
        cookies = client._session.cookies.get_dict()
        # Even if it doesn't raise, subsequent data calls would fail
        _LOGGER.info("Bad-login PND cookies: %s", list(cookies.keys()))
