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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from homeassistant.components.recorder.models import StatisticMetaData, StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ELECTROMETER_ID,
    CONF_HISTORY_IMPORTED,
    DEFAULT_SCAN_INTERVAL_REALTIME_MIN,
    DEFAULT_SCAN_INTERVAL_DAILY_MIN,
    DEFAULT_SCAN_INTERVAL_SIGNALS_MIN,
    parse_cz_datetime,
    DEFAULT_SCAN_INTERVAL_OUTAGES_MIN,
    HISTORY_DAYS,
    HISTORY_DAILY_CHUNK_DAYS,
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


async def async_import_history(
    hass: HomeAssistant,
    pnd_client: CezPndRestClient,
    electrometer_id: str,
) -> None:
    """Fetch 90 days of historical data from PND and inject into HA long-term statistics.

    Creates six external statistic series:
    - 3 from daily endpoint: cumulative NT, VT, Total (kWh)
    - 3 from interval endpoint: total energy (kWh), mean power (kW), max power (kW)
    """

    def _fetch_history() -> Dict[str, Any]:
        today = dt.date.today()
        start = today - dt.timedelta(days=HISTORY_DAYS)

        # --- Daily data (chunked into 30-day requests) ---
        all_daily_raw: List[Dict[str, Any]] = []
        chunk_start = start
        while chunk_start < today:
            chunk_end = min(chunk_start + dt.timedelta(days=HISTORY_DAILY_CHUNK_DAYS), today)
            _LOGGER.info(
                "History import: fetching daily data %s -> %s",
                chunk_start, chunk_end,
            )
            try:
                raw = pnd_client.get_daily_data(electrometer_id, chunk_start, chunk_end)
                all_daily_raw.append(raw)
            except Exception as e:
                _LOGGER.warning("History import: daily chunk %s->%s failed: %s", chunk_start, chunk_end, e)
            chunk_start = chunk_end

        # --- Interval data (day by day) ---
        all_interval_raw: List[Dict[str, Any]] = []
        day = start
        while day < today:
            next_day = day + dt.timedelta(days=1)
            try:
                raw = pnd_client.get_interval_data(electrometer_id, day, next_day)
                all_interval_raw.append(raw)
            except Exception as e:
                _LOGGER.warning("History import: interval day %s failed: %s", day, e)
            day = next_day
            if (day - start).days % 10 == 0:
                _LOGGER.info("History import: fetched intervals for %d/%d days", (day - start).days, HISTORY_DAYS)

        return {"daily": all_daily_raw, "intervals": all_interval_raw}

    raw_data = await hass.async_add_executor_job(_fetch_history)

    # --- Build daily statistics (NT, VT, Total) ---
    daily_points = _build_daily_statistics(raw_data["daily"])

    # --- Build interval statistics (total kWh, mean kW, max kW) ---
    interval_points = _build_interval_statistics(raw_data["intervals"])

    eid = electrometer_id

    # Import daily series
    for key, name in [("nt", "ČEZ Historical NT"), ("vt", "ČEZ Historical VT"), ("total", "ČEZ Historical Total")]:
        stats = daily_points.get(key, [])
        if not stats:
            continue
        _LOGGER.info("History import: injecting %d hourly points for daily_%s", len(stats), key)
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{eid}_daily_{key}",
                unit_of_measurement="kWh",
                mean_type=StatisticMeanType.NONE,
            ),
            stats,
        )

    # Import interval energy series
    if interval_points.get("energy"):
        _LOGGER.info("History import: injecting %d hourly points for interval_total", len(interval_points["energy"]))
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name="ČEZ Historical Interval Total",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{eid}_interval_total",
                unit_of_measurement="kWh",
                mean_type=StatisticMeanType.NONE,
            ),
            interval_points["energy"],
        )

    # Import interval mean power series
    if interval_points.get("power_mean"):
        _LOGGER.info("History import: injecting %d hourly points for interval_power_mean", len(interval_points["power_mean"]))
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_mean=True,
                has_sum=False,
                name="ČEZ Historical Interval Power Mean",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{eid}_interval_power_mean",
                unit_of_measurement="kW",
                mean_type=StatisticMeanType.ARITHMETIC,
            ),
            interval_points["power_mean"],
        )

    # Import interval max power series
    if interval_points.get("power_max"):
        _LOGGER.info("History import: injecting %d hourly points for interval_power_max", len(interval_points["power_max"]))
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=False,
                name="ČEZ Historical Interval Power Max",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{eid}_interval_power_max",
                unit_of_measurement="kW",
                mean_type=StatisticMeanType.NONE,
            ),
            interval_points["power_max"],
        )

    _LOGGER.info("History import: complete for electrometer %s", eid)


def _build_daily_statistics(
    daily_raw_list: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Convert daily API responses into hourly StatisticData dicts for HA.

    Daily endpoint returns cumulative meter readings per day.
    We assign each day's reading to midnight (UTC-aware) and compute sum
    as the delta from the first reading.
    """
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Prague")

    raw_by_series: Dict[str, List[tuple]] = {"nt": [], "vt": [], "total": []}

    for raw in daily_raw_list:
        if not raw.get("hasData"):
            continue
        for series in raw.get("series", []):
            name = series.get("name", "")
            data = series.get("data", [])
            if "+E_NT/" in name:
                key = "nt"
            elif "+E_VT/" in name:
                key = "vt"
            elif "-E/" in name:
                continue
            elif "+E/" in name:
                key = "total"
            else:
                continue
            for entry in data:
                if len(entry) >= 2:
                    try:
                        ts = parse_cz_datetime(entry[0])
                        val = float(entry[1])
                        raw_by_series[key].append((ts, val))
                    except (ValueError, TypeError):
                        continue

    result: Dict[str, List[Dict[str, Any]]] = {}
    for key, points in raw_by_series.items():
        if not points:
            continue
        points.sort(key=lambda x: x[0])
        first_value = points[0][1]
        stats = []
        for ts, cumulative in points:
            hour_start = dt.datetime(ts.year, ts.month, ts.day, tzinfo=tz)
            stats.append({
                "start": hour_start,
                "state": round(cumulative, 3),
                "sum": round(cumulative - first_value, 3),
            })
        result[key] = stats

    return result


def _build_interval_statistics(
    interval_raw_list: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Convert interval API responses into hourly StatisticData dicts for HA.

    Groups 15-min kW intervals into hourly buckets and computes:
    - energy: cumulative kWh (sum) and hourly kWh (state)
    - power_mean: mean kW per hour
    - power_max: max kW per hour
    """
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Prague")

    all_intervals: List[Dict[str, Any]] = []
    for raw in interval_raw_list:
        all_intervals.extend(CezPndRestClient.parse_interval_series(raw))

    if not all_intervals:
        return {}

    # Group by hour (timestamp is end of 15-min interval, so subtract to get start)
    hourly: Dict[dt.datetime, List[float]] = {}
    for iv in all_intervals:
        try:
            ts_end = parse_cz_datetime(iv["timestamp"])
        except (ValueError, TypeError):
            continue
        ts_start = ts_end - dt.timedelta(minutes=1)
        hour_start = dt.datetime(ts_start.year, ts_start.month, ts_start.day, ts_start.hour, tzinfo=tz)
        hourly.setdefault(hour_start, []).append(iv["kw"])

    sorted_hours = sorted(hourly.keys())
    if not sorted_hours:
        return {}

    energy_stats = []
    power_mean_stats = []
    power_max_stats = []
    cumulative_kwh = 0.0

    for hour in sorted_hours:
        kw_values = hourly[hour]
        hourly_kwh = sum(kw * 0.25 for kw in kw_values)
        cumulative_kwh += hourly_kwh
        mean_kw = sum(kw_values) / len(kw_values) if kw_values else 0.0
        max_kw = max(kw_values) if kw_values else 0.0

        energy_stats.append({
            "start": hour,
            "state": round(hourly_kwh, 3),
            "sum": round(cumulative_kwh, 3),
        })
        power_mean_stats.append({
            "start": hour,
            "mean": round(mean_kw, 3),
        })
        power_max_stats.append({
            "start": hour,
            "max": round(max_kw, 3),
        })

    return {
        "energy": energy_stats,
        "power_mean": power_mean_stats,
        "power_max": power_max_stats,
    }


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

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.electrometer_id)},
            name=f"Elektroměr {self.electrometer_id}",
            manufacturer="ČEZ Distribuce",
            model="Elektroměr",
            entry_type="service",
        )

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
