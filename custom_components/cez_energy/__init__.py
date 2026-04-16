"""Home Assistant integration for ČEZ Energy.

Fetches 15-minute interval data and daily NT/VT meter readings from the PND
portal, HDO signals and outage data from the DIP portal, and exposes them
as sensors, binary sensors, and calendar entities.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ELECTROMETER_ID,
    DEFAULT_SCAN_INTERVAL_REALTIME_MIN,
    DEFAULT_SCAN_INTERVAL_DAILY_MIN,
    DEFAULT_SCAN_INTERVAL_SIGNALS_MIN,
    parse_cz_datetime,
    DEFAULT_SCAN_INTERVAL_OUTAGES_MIN,
)
from .rest_client.dip_client import CezDistribuceRestClient
from .rest_client.pnd_client import CezPndRestClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "calendar"]


@dataclass
class CezSupplyPoint:
    """Data about a single supply point discovered from the DIP portal."""
    header: Dict[str, Any]
    supply_point: Dict[str, Any]
    supply_point_detail: Dict[str, Any]
    signals: Dict[str, Any] = field(default_factory=dict)
    outages_for_ean: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RealtimeData:
    """Processed 15-min interval data combined with HDO classification."""
    intervals: List[Dict[str, Any]] = field(default_factory=list)
    nt_kwh: float = 0.0
    vt_kwh: float = 0.0
    total_kwh: float = 0.0
    current_power_kw: Optional[float] = None
    last_updated: Optional[dt.datetime] = None


@dataclass
class DailyData:
    """Authoritative daily cumulative meter readings from PND."""
    cumulative_total: Optional[float] = None
    cumulative_nt: Optional[float] = None
    cumulative_vt: Optional[float] = None
    cumulative_export: Optional[float] = None
    last_updated: Optional[dt.datetime] = None


def _parse_hdo_time(day: dt.date, time_str: str) -> dt.datetime:
    """Parse an HDO time string, handling the 24:00 convention."""
    if time_str == "24:00":
        return dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time.fromisoformat(time_str))


def _is_nt_interval(timestamp_str: str, signals_data: Dict[str, Any]) -> bool:
    """Determine if a 15-min interval falls within an NT (low tariff) HDO window.

    HDO signals contain time ranges during which the low tariff is active.
    If ANY signal covers the interval, it's NT; otherwise VT.
    """
    try:
        interval_dt = parse_cz_datetime(timestamp_str)
    except (ValueError, TypeError):
        return False

    interval_end = interval_dt
    interval_start = interval_end - dt.timedelta(minutes=15)

    signals = signals_data.get("signals", [])
    for s in signals:
        date_str = s.get("datum")
        if not date_str:
            continue
        try:
            day = dt.datetime.strptime(date_str, "%d.%m.%Y")
        except (ValueError, TypeError):
            continue

        if day.date() != interval_start.date():
            continue

        casy = s.get("casy", "")
        for part in [p.strip() for p in casy.split(";") if p.strip()]:
            try:
                start_s, end_s = [x.strip() for x in part.replace("\u2013", "-").split("-")]
                sdt = _parse_hdo_time(day.date(), start_s)
                edt = _parse_hdo_time(day.date(), end_s)
            except (ValueError, TypeError):
                continue
            if edt <= sdt:
                edt += dt.timedelta(days=1)
            if sdt < interval_end and edt > interval_start:
                return True
    return False


class CezEnergyHub:
    """Central hub managing PND + DIP clients and four data coordinators."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        electrometer_id: str,
    ) -> None:
        self.hass = hass
        self._dip_client = CezDistribuceRestClient()
        self._pnd_client = CezPndRestClient()
        self._username = username
        self._password = password
        self.electrometer_id = electrometer_id

        self.points: List[CezSupplyPoint] = []

        self.realtime_coordinator: Optional[DataUpdateCoordinator] = None
        self.daily_coordinator: Optional[DataUpdateCoordinator] = None
        self.signals_coordinator: Optional[DataUpdateCoordinator] = None
        self.outages_coordinator: Optional[DataUpdateCoordinator] = None

        self._last_daily_data = DailyData()
        self._last_realtime_data = RealtimeData()

    @property
    def ean(self) -> Optional[str]:
        try:
            if self.points:
                return (self.points[0].supply_point_detail or {}).get("ean")
        except Exception:
            pass
        return None

    async def _login_and_load(self) -> None:
        def _blocking():
            # Login to DIP portal
            self._dip_client.login(self._username, self._password)
            header = self._dip_client.common_header()

            supply_points = self._dip_client.get_supply_points()
            self.points = []
            for sp_block in supply_points.get("vstelleBlocks", {}).get("blocks", []) or []:
                for sp in sp_block.get("vstelles", []) or []:
                    try:
                        uid = sp["uid"]
                        sp_detail = self._dip_client.get_supply_point_detail(uid)
                        ean = sp_detail.get("ean") or sp.get("ean")
                        signals = (
                            self._dip_client.get_signals(ean)
                            if ean and sp_detail.get("hdo")
                            else {"signals": []}
                        )
                        outages = self._dip_client.get_outages(ean=ean) or [] if ean else []
                        self.points.append(
                            CezSupplyPoint(header, sp, sp_detail, signals, outages)
                        )
                    except Exception as e:
                        _LOGGER.warning("Failed to load supply point %s: %s", sp, e)

            if not self.points:
                sp_block = supply_points["vstelleBlocks"]["blocks"][0]
                sp = sp_block["vstelles"][0]
                uid = sp["uid"]
                sp_detail = self._dip_client.get_supply_point_detail(uid)
                ean = sp_detail.get("ean")
                signals = (
                    self._dip_client.get_signals(ean)
                    if sp_detail.get("hdo") and ean
                    else {"signals": []}
                )
                outages = self._dip_client.get_outages(ean=ean) or [] if ean else []
                self.points.append(CezSupplyPoint(header, sp, sp_detail, signals, outages))

            # Login to PND portal
            self._pnd_client.login(self._username, self._password)

        await self.hass.async_add_executor_job(_blocking)

    async def async_setup(self) -> None:
        await self._login_and_load()

        # --- Realtime coordinator (15-min interval data) ---
        async def _update_realtime() -> RealtimeData:
            def _blocking():
                today = dt.date.today()
                tomorrow = today + dt.timedelta(days=1)
                try:
                    raw = self._pnd_client.get_interval_data(
                        self.electrometer_id, today, tomorrow
                    )
                    intervals = CezPndRestClient.parse_interval_series(raw)
                except Exception as e:
                    _LOGGER.warning("Failed fetching interval data: %s", e)
                    return self._last_realtime_data

                # Get current HDO signals for classification
                signals_data: Dict[str, Any] = {"signals": []}
                if self.signals_coordinator and self.signals_coordinator.data:
                    ean = self.ean
                    if ean:
                        signals_data = self.signals_coordinator.data.get(ean, {"signals": []})

                nt_kwh = 0.0
                vt_kwh = 0.0
                current_power = None
                for iv in intervals:
                    kwh = iv["kw"] * 0.25
                    if _is_nt_interval(iv["timestamp"], signals_data):
                        nt_kwh += kwh
                    else:
                        vt_kwh += kwh
                    current_power = iv["kw"]

                data = RealtimeData(
                    intervals=intervals,
                    nt_kwh=round(nt_kwh, 3),
                    vt_kwh=round(vt_kwh, 3),
                    total_kwh=round(nt_kwh + vt_kwh, 3),
                    current_power_kw=current_power,
                    last_updated=dt.datetime.now(),
                )
                self._last_realtime_data = data
                return data

            return await self.hass.async_add_executor_job(_blocking)

        # --- Daily coordinator (cumulative meter readings) ---
        async def _update_daily() -> DailyData:
            def _blocking():
                today = dt.date.today()
                yesterday = today - dt.timedelta(days=1)
                try:
                    raw = self._pnd_client.get_daily_data(
                        self.electrometer_id, yesterday, today
                    )
                    values = CezPndRestClient.parse_daily_series(raw)
                except Exception as e:
                    _LOGGER.warning("Failed fetching daily data: %s", e)
                    return self._last_daily_data

                data = DailyData(
                    cumulative_total=values.get("total"),
                    cumulative_nt=values.get("nt"),
                    cumulative_vt=values.get("vt"),
                    cumulative_export=values.get("export"),
                    last_updated=dt.datetime.now(),
                )
                self._last_daily_data = data
                return data

            return await self.hass.async_add_executor_job(_blocking)

        # --- Signals coordinator (HDO schedule) ---
        async def _update_signals() -> Dict[str, Any]:
            def _blocking():
                result: Dict[str, Any] = {}
                for p in self.points:
                    try:
                        ean = p.supply_point_detail.get("ean")
                        if not ean or not p.supply_point_detail.get("hdo"):
                            continue
                        result[ean] = self._dip_client.get_signals(ean)
                    except Exception as e:
                        _LOGGER.warning("Failed refreshing signals: %s", e)
                        ean = p.supply_point_detail.get("ean")
                        if ean:
                            result[ean] = p.signals
                return result

            return await self.hass.async_add_executor_job(_blocking)

        # --- Outages coordinator ---
        async def _update_outages() -> Dict[str, List[Dict[str, Any]]]:
            def _blocking():
                result: Dict[str, List[Dict[str, Any]]] = {}
                for p in self.points:
                    try:
                        ean = p.supply_point_detail.get("ean")
                        if not ean:
                            continue
                        result[ean] = self._dip_client.get_outages(ean=ean) or []
                    except Exception as e:
                        _LOGGER.warning("Failed refreshing outages: %s", e)
                        ean = p.supply_point_detail.get("ean")
                        if ean:
                            result[ean] = p.outages_for_ean
                return result

            return await self.hass.async_add_executor_job(_blocking)

        self.signals_coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name="ČEZ Energy signals",
            update_method=_update_signals,
            update_interval=dt.timedelta(minutes=DEFAULT_SCAN_INTERVAL_SIGNALS_MIN),
        )
        self.outages_coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name="ČEZ Energy outages",
            update_method=_update_outages,
            update_interval=dt.timedelta(minutes=DEFAULT_SCAN_INTERVAL_OUTAGES_MIN),
        )
        self.realtime_coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name="ČEZ Energy realtime",
            update_method=_update_realtime,
            update_interval=dt.timedelta(minutes=DEFAULT_SCAN_INTERVAL_REALTIME_MIN),
        )
        self.daily_coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name="ČEZ Energy daily",
            update_method=_update_daily,
            update_interval=dt.timedelta(minutes=DEFAULT_SCAN_INTERVAL_DAILY_MIN),
        )

        # Signals must be primed first (realtime depends on it for NT/VT classification)
        await self.signals_coordinator.async_config_entry_first_refresh()
        await self.outages_coordinator.async_config_entry_first_refresh()
        await self.realtime_coordinator.async_config_entry_first_refresh()
        await self.daily_coordinator.async_config_entry_first_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    hub = CezEnergyHub(
        hass,
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        electrometer_id=data[CONF_ELECTROMETER_ID],
    )
    await hub.async_setup()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)
        if DOMAIN in hass.data and not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok
