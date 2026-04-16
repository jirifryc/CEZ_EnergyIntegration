"""Binary sensor platform for ČEZ Energy integration.

Exposes a binary sensor that indicates whether the low tariff (NT) is
currently active based on HDO signal data from the DIP portal.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CezEnergyHub
from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    hub: CezEnergyHub = hass.data[DOMAIN][entry.entry_id]
    ean = hub.ean
    if not ean:
        return
    async_add_entities([CezLowTariffBinarySensor(hub, ean)])


class CezLowTariffBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when the current time falls within an HDO low-tariff (NT) window."""

    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, hub: CezEnergyHub, ean: str) -> None:
        super().__init__(hub.signals_coordinator)
        self._hub = hub
        self._ean = ean
        self._attr_name = "ČEZ Nízký tarif (NT)"
        self._attr_unique_id = f"{ean}_low_tariff_active"

    @property
    def is_on(self) -> Optional[bool]:
        data: Dict[str, Any] = self.coordinator.data or {}
        signals_data = data.get(self._ean) or {}
        signals: List[Dict[str, Any]] = signals_data.get("signals", [])
        if not signals:
            return None

        now = dt_util.now()
        ha_tz = dt_util.get_time_zone(
            getattr(self._hub.hass.config, "time_zone", None)
        ) or now.tzinfo

        for s in signals:
            date_str = s.get("datum")
            if not date_str:
                continue
            try:
                day = dt.datetime.strptime(date_str, "%d.%m.%Y")
            except (ValueError, TypeError):
                continue

            casy = s.get("casy", "")
            for part in [p.strip() for p in casy.split(";") if p.strip()]:
                try:
                    start_s, end_s = [
                        x.strip() for x in part.replace("\u2013", "-").split("-")
                    ]
                    sdt = dt.datetime.combine(day.date(), dt.time.fromisoformat(start_s))
                    edt = dt.datetime.combine(day.date(), dt.time.fromisoformat(end_s))
                except (ValueError, TypeError):
                    continue

                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=ha_tz)
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=ha_tz)
                sdt = sdt.astimezone(now.tzinfo)
                edt = edt.astimezone(now.tzinfo)

                if edt <= sdt:
                    edt += dt.timedelta(days=1)
                if sdt <= now <= edt:
                    return True
        return False

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return {"ean": self._ean}
