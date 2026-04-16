"""Sensor platform for ČEZ Energy integration.

Exposes cumulative NT/VT/Total energy sensors (TOTAL_INCREASING for the
HA Energy Dashboard) and a current-power sensor from 15-min interval data.

The cumulative sensors report authoritative meter readings from the daily
endpoint when available, with intraday estimates from 15-min data added on top.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CezEnergyHub, DailyData, RealtimeData
from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    hub: CezEnergyHub = hass.data[DOMAIN][entry.entry_id]
    eid = hub.electrometer_id

    entities = [
        CezCumulativeEnergySensor(hub, eid, "nt", "ČEZ Spotřeba NT", f"{eid}_cumulative_nt"),
        CezCumulativeEnergySensor(hub, eid, "vt", "ČEZ Spotřeba VT", f"{eid}_cumulative_vt"),
        CezCumulativeEnergySensor(hub, eid, "total", "ČEZ Spotřeba celkem", f"{eid}_cumulative_total"),
        CezTodayEnergySensor(hub, eid, "nt", "ČEZ Dnešní NT", f"{eid}_today_nt"),
        CezTodayEnergySensor(hub, eid, "vt", "ČEZ Dnešní VT", f"{eid}_today_vt"),
        CezTodayEnergySensor(hub, eid, "total", "ČEZ Dnešní celkem", f"{eid}_today_total"),
        CezCurrentPowerSensor(hub, eid),
    ]
    async_add_entities(entities)


class CezCumulativeEnergySensor(CoordinatorEntity, SensorEntity):
    """Cumulative meter reading for NT, VT, or Total (TOTAL_INCREASING).

    Shows the authoritative daily base reading plus intraday estimates from
    15-min interval data, so the value ticks up throughout the day.  When
    the daily coordinator refreshes (typically once a day) the base is
    realigned to the authoritative meter reading.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        hub: CezEnergyHub,
        electrometer_id: str,
        kind: str,
        name: str,
        unique_id: str,
    ) -> None:
        super().__init__(hub.realtime_coordinator)
        self._hub = hub
        self._kind = kind
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_device_info = hub.device_info
        self._unsub_daily: Optional[Callable] = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Also refresh when the daily coordinator gets new authoritative data.
        @callback
        def _on_daily_update() -> None:
            self.async_write_ha_state()

        self._unsub_daily = self._hub.daily_coordinator.async_add_listener(
            _on_daily_update
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_daily:
            self._unsub_daily()
            self._unsub_daily = None

    @property
    def native_value(self) -> Optional[float]:
        daily: Optional[DailyData] = self._hub.daily_coordinator.data
        realtime: Optional[RealtimeData] = self.coordinator.data

        base = None
        if daily:
            if self._kind == "nt":
                base = daily.cumulative_nt
            elif self._kind == "vt":
                base = daily.cumulative_vt
            elif self._kind == "total":
                base = daily.cumulative_total

        if base is None:
            return None

        intraday = 0.0
        if realtime:
            if self._kind == "nt":
                intraday = realtime.nt_kwh
            elif self._kind == "vt":
                intraday = realtime.vt_kwh
            elif self._kind == "total":
                intraday = realtime.total_kwh

        return _round(base + intraday)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        daily: Optional[DailyData] = self._hub.daily_coordinator.data
        realtime: Optional[RealtimeData] = self.coordinator.data
        return {
            "electrometer_id": self._hub.electrometer_id,
            "daily_base_updated": str(daily.last_updated) if daily and daily.last_updated else None,
            "realtime_updated": str(realtime.last_updated) if realtime and realtime.last_updated else None,
        }


class CezTodayEnergySensor(CoordinatorEntity, SensorEntity):
    """Today's estimated NT/VT/Total consumption from 15-min interval data."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        hub: CezEnergyHub,
        electrometer_id: str,
        kind: str,
        name: str,
        unique_id: str,
    ) -> None:
        super().__init__(hub.realtime_coordinator)
        self._hub = hub
        self._kind = kind
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_device_info = hub.device_info

    @property
    def native_value(self) -> Optional[float]:
        data: Optional[RealtimeData] = self.coordinator.data
        if not data:
            return None
        if self._kind == "nt":
            return data.nt_kwh
        elif self._kind == "vt":
            return data.vt_kwh
        elif self._kind == "total":
            return data.total_kwh
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data: Optional[RealtimeData] = self.coordinator.data
        return {
            "electrometer_id": self._hub.electrometer_id,
            "interval_count": len(data.intervals) if data else 0,
            "last_updated": str(data.last_updated) if data and data.last_updated else None,
        }


class CezCurrentPowerSensor(CoordinatorEntity, SensorEntity):
    """Latest 15-minute average power in kW."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: CezEnergyHub, electrometer_id: str) -> None:
        super().__init__(hub.realtime_coordinator)
        self._hub = hub
        self._attr_name = "ČEZ Aktuální výkon"
        self._attr_unique_id = f"{electrometer_id}_current_power"
        self._attr_device_info = hub.device_info

    @property
    def native_value(self) -> Optional[float]:
        data: Optional[RealtimeData] = self.coordinator.data
        if not data or data.current_power_kw is None:
            return None
        return round(data.current_power_kw, 3)


def _round(val: Optional[float]) -> Optional[float]:
    return round(val, 3) if val is not None else None
