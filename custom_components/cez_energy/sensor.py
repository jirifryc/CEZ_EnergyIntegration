"""Sensor platform for ČEZ Energy integration.

Exposes cumulative NT/VT/Total energy sensors (TOTAL_INCREASING for the
HA Energy Dashboard) and a current-power sensor from 15-min interval data.

The cumulative sensors report authoritative meter readings from the daily
endpoint when available, with intraday estimates from 15-min data added on top.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
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
    """Cumulative meter reading for NT, VT, or Total (TOTAL_INCREASING)."""

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
        super().__init__(hub.daily_coordinator)
        self._hub = hub
        self._kind = kind
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_device_info = hub.device_info

    @property
    def native_value(self) -> Optional[float]:
        data: Optional[DailyData] = self.coordinator.data
        if not data:
            return None
        if self._kind == "nt":
            return _round(data.cumulative_nt)
        elif self._kind == "vt":
            return _round(data.cumulative_vt)
        elif self._kind == "total":
            return _round(data.cumulative_total)
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data: Optional[DailyData] = self.coordinator.data
        return {
            "electrometer_id": self._hub.electrometer_id,
            "last_updated": str(data.last_updated) if data and data.last_updated else None,
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
