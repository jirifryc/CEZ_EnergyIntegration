"""Tests for the calendar entities (HDO signals and outages)."""
import datetime as dt

import pytest
from types import SimpleNamespace

from custom_components.cez_energy.calendar import (
    CezOutageCalendar,
    CezCurrentSignalCalendar,
)


class DummyCoordinator:
    def __init__(self, data):
        self.data = data

    def async_add_listener(self, _cb):
        return lambda: None


class DummyHub(SimpleNamespace):
    pass


@pytest.mark.asyncio
async def test_current_signal_calendar_event_returns_active_interval():
    today = dt.datetime.now().strftime("%d.%m.%Y")
    now_local = dt.datetime.now().astimezone()
    start_local = (now_local - dt.timedelta(minutes=10)).time().replace(second=0, microsecond=0)
    end_local = (now_local + dt.timedelta(minutes=10)).time().replace(second=0, microsecond=0)

    signals = {
        "signals": [
            {
                "datum": today,
                "casy": f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')}",
                "nazevSignalu": "A1",
            },
        ]
    }

    hass = SimpleNamespace()
    hass.config = SimpleNamespace(
        time_zone=str(now_local.tzinfo) if now_local.tzinfo else "Europe/Prague"
    )

    ean = "123456789"
    hub = DummyHub(
        ean=ean,
        signals_coordinator=DummyCoordinator({ean: signals}),
        hass=hass,
        points=[],
    )

    cal = CezCurrentSignalCalendar.__new__(CezCurrentSignalCalendar)
    cal._hub = hub
    cal._ean = ean
    cal.coordinator = hub.signals_coordinator
    cal._attr_name = "HDO"
    cal._attr_unique_id = f"{ean}_current_signal_calendar"

    ev = cal.event
    assert ev is not None
    assert ev.summary == "Signál A1"
    assert ev.start < ev.end


@pytest.mark.asyncio
async def test_outage_calendar_parses_midnight_crossing():
    ean = "123456789"
    outages = {
        ean: [
            {
                "dateFormatted": "29.10.2025",
                "timeFormatted": "22:00 - 01:00",
                "number": "12345",
                "sortedParts": [{"description": "Plánovaná odstávka"}],
            }
        ]
    }

    hass = SimpleNamespace()
    hass.config = SimpleNamespace(time_zone="Europe/Prague")

    hub = DummyHub(
        ean=ean,
        outages_coordinator=DummyCoordinator(outages),
    )

    cal = CezOutageCalendar.__new__(CezOutageCalendar)
    cal._hub = hub
    cal._ean = ean
    cal.coordinator = hub.outages_coordinator
    cal._attr_name = "Odstávky"
    cal._attr_unique_id = f"{ean}_outages"

    start = dt.datetime(2025, 10, 29, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2025, 10, 30, 23, 59, tzinfo=dt.timezone.utc)

    events = await cal.async_get_events(hass, start, end)
    assert len(events) == 1
    ev = events[0]
    assert ev.summary.startswith("Odstávka")
    assert "Plánovaná odstávka" in ev.description
    assert ev.end > ev.start


@pytest.mark.asyncio
async def test_signal_calendar_merges_overlapping_intervals():
    ean = "123456789"
    day = "10.10.2025"
    signals = {
        ean: {
            "signals": [
                {"datum": day, "casy": "00:00-05:00;04:00-06:00"},
                {"datum": day, "casy": "10:00-12:00"},
            ]
        }
    }

    hass = SimpleNamespace()
    hass.config = SimpleNamespace(time_zone="Europe/Prague")

    hub = DummyHub(
        ean=ean,
        signals_coordinator=DummyCoordinator(signals),
        hass=hass,
        points=[],
    )

    cal = CezCurrentSignalCalendar.__new__(CezCurrentSignalCalendar)
    cal._hub = hub
    cal._ean = ean
    cal.coordinator = hub.signals_coordinator
    cal._attr_name = "HDO"
    cal._attr_unique_id = f"{ean}_signal"

    start = dt.datetime(2025, 10, 9, 22, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2025, 10, 11, 0, 0, tzinfo=dt.timezone.utc)

    events = await cal.async_get_events(hass, start, end)
    # 00:00-05:00 and 04:00-06:00 should merge into 00:00-06:00
    # Plus 10:00-12:00 = 2 events total
    assert len(events) == 2
