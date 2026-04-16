"""Tests for the PND client data parsing methods."""
from custom_components.cez_energy.rest_client.pnd_client import CezPndRestClient


SAMPLE_INTERVAL_RESPONSE = {
    "hasData": True,
    "size": 4,
    "unitY": "kW",
    "series": [{
        "name": "+A/5100128114",
        "data": [
            ["15.04.2026 00:15", 6.788, "naměřená data OK"],
            ["15.04.2026 00:30", 3.116, "naměřená data OK"],
            ["15.04.2026 00:45", 0.272, "naměřená data OK"],
            ["15.04.2026 01:00", 0.296, "naměřená data OK"],
        ],
    }],
}

SAMPLE_DAILY_RESPONSE = {
    "hasData": True,
    "size": 1,
    "unitY": "kWh",
    "series": [
        {"name": "+E/5100128114", "data": [["15.04.2026 24:00", 5641.212, "naměřená data OK"]]},
        {"name": "-E/5100128114", "data": [["15.04.2026 24:00", 0.059, "naměřená data OK"]]},
        {"name": "+E_NT/5100128114", "data": [["15.04.2026 24:00", 5466.108, "naměřená data OK"]]},
        {"name": "+E_VT/5100128114", "data": [["15.04.2026 24:00", 175.103, "naměřená data OK"]]},
    ],
}


def test_parse_interval_series():
    intervals = CezPndRestClient.parse_interval_series(SAMPLE_INTERVAL_RESPONSE)
    assert len(intervals) == 4
    assert intervals[0]["timestamp"] == "15.04.2026 00:15"
    assert intervals[0]["kw"] == 6.788
    assert intervals[0]["status"] == "naměřená data OK"


def test_parse_interval_series_empty():
    result = CezPndRestClient.parse_interval_series({"hasData": False, "series": []})
    assert result == []


def test_parse_daily_series():
    values = CezPndRestClient.parse_daily_series(SAMPLE_DAILY_RESPONSE)
    assert values["total"] == 5641.212
    assert values["nt"] == 5466.108
    assert values["vt"] == 175.103
    assert values["export"] == 0.059


def test_parse_daily_series_empty():
    values = CezPndRestClient.parse_daily_series({"hasData": False, "series": []})
    assert values["total"] is None
    assert values["nt"] is None
    assert values["vt"] is None
    assert values["export"] is None


def test_parse_daily_nt_plus_vt_equals_total():
    values = CezPndRestClient.parse_daily_series(SAMPLE_DAILY_RESPONSE)
    assert abs(values["nt"] + values["vt"] - values["total"]) < 0.01
