"""Button platform for ČEZ Energy integration.

Provides a button to manually trigger a data refresh of all coordinators.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant

from . import CezEnergyHub
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    hub: CezEnergyHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CezRefreshButton(hub)])


class CezRefreshButton(ButtonEntity):
    """Button that triggers a manual refresh of all ČEZ data."""

    _attr_icon = "mdi:refresh"

    def __init__(self, hub: CezEnergyHub) -> None:
        self._hub = hub
        self._attr_name = "ČEZ Obnovit data"
        self._attr_unique_id = f"{hub.electrometer_id}_refresh"
        self._attr_device_info = hub.device_info

    async def async_press(self) -> None:
        _LOGGER.info("Manual refresh triggered for electrometer %s", self._hub.electrometer_id)
        for coordinator in (
            self._hub.daily_coordinator,
            self._hub.realtime_coordinator,
            self._hub.signals_coordinator,
            self._hub.outages_coordinator,
        ):
            if coordinator is not None:
                await coordinator.async_request_refresh()
