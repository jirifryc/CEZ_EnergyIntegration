"""Calendar platform for ČEZ Energy integration.

Exposes two calendar entities per supply point:
- HDO signal calendar showing low-tariff time windows
- Outage calendar showing planned power outages
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CezEnergyHub
from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    hub: CezEnergyHub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for p in getattr(hub, "points", []) or []:
        ean = (p.supply_point_detail or {}).get("ean")
        if not ean:
            continue
        entities.append(CezOutageCalendar(hub, ean))
        entities.append(CezCurrentSignalCalendar(hub, ean))
    async_add_entities(entities)


class CezOutageCalendar(CoordinatorEntity, CalendarEntity):
    """Calendar showing planned power outages for a supply point."""

    def __init__(self, hub: CezEnergyHub, ean: str) -> None:
        super().__init__(hub.outages_coordinator)
        self._hub = hub
        self._ean = ean
        self._attr_name = "ČEZ Odstávky"
        self._attr_unique_id = f"{ean}_outages_calendar"

    @property
    def event(self) -> Optional[CalendarEvent]:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> List[CalendarEvent]:
        data = self.coordinator.data or {}
        outages: List[Dict[str, Any]] = data.get(self._ean, [])
        events: List[CalendarEvent] = []

        for o in outages:
            try:
                date_val = o.get("dateFormatted") or o.get("date")
                if not date_val:
                    continue
                if "." in date_val:
                    day = dt.datetime.strptime(date_val, "%d.%m.%Y")
                else:
                    day = dt.datetime.fromisoformat(date_val)
                time_fmt = o.get("timeFormatted") or "00:00 - 23:59"
                start_s, end_s = [x.strip() for x in time_fmt.replace("\u2013", "-").split("-")]
                start_dt = dt.datetime.combine(day.date(), dt.time.fromisoformat(start_s))
                end_dt = dt.datetime.combine(day.date(), dt.time.fromisoformat(end_s))
            except Exception:
                continue

            tz_ref = start_date.tzinfo or dt.timezone.utc
            ha_tz = dt_util.get_time_zone(getattr(hass.config, "time_zone", None)) or tz_ref
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=ha_tz)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=ha_tz)
            start_dt = start_dt.astimezone(tz_ref)
            end_dt = end_dt.astimezone(tz_ref)

            if end_dt <= start_dt:
                end_dt += dt.timedelta(days=1)
            if end_dt < start_date or start_dt > end_date:
                continue

            number = o.get("number", "")
            summary = f"Odstávka {number}" if number else "Odstávka"

            for p in o.get("sortedParts") or o.get("parts") or []:
                desc = []
                main_location = p.get("description", "")
                streets = p.get("sortedStreets") or p.get("streets") or []
                if not streets:
                    desc.append(main_location)
                for s in streets:
                    if not isinstance(s, dict):
                        continue
                    street_name = s.get("streetName", "")
                    street_nums = s.get("streetNumbers", [])
                    if not isinstance(street_nums, list):
                        street_nums = []
                    for n in street_nums:
                        if not isinstance(n, dict):
                            continue
                        street_number = ""
                        if n.get("buildingId") or n.get("streetId"):
                            street_number = n.get("buildingId", "") + "/" + n.get("streetId", "")
                        if n.get("cadastralTerritoryCode") or n.get("parcelaId"):
                            street_number += " ("
                            if n.get("cadastralTerritoryCode"):
                                street_number += "k.ú " + n.get("cadastralTerritoryCode")
                            if n.get("cadastralTerritoryCode") and n.get("parcelaId"):
                                street_number += ", "
                            if n.get("parcelaId"):
                                street_number += "parc. č. " + n.get("parcelaId")
                            street_number += ")"
                        desc.append(f"{main_location}, {street_name} {street_number}")
                    if not street_nums:
                        desc.append(f"{main_location}, {street_name}")
                events.append(
                    CalendarEvent(
                        summary=f"{summary} - {main_location}",
                        start=start_dt,
                        end=end_dt,
                        description=";\n".join(desc),
                        location=main_location,
                    )
                )
        return events


class CezCurrentSignalCalendar(CoordinatorEntity, CalendarEntity):
    """Calendar showing HDO signal windows for a supply point."""

    def __init__(self, hub: CezEnergyHub, ean: str) -> None:
        super().__init__(hub.signals_coordinator)
        self._hub = hub
        self._ean = ean
        self._attr_name = "HDO \u2013 aktuální signál"
        self._attr_unique_id = f"{ean}_current_signal_calendar"

    @property
    def event(self) -> Optional[CalendarEvent]:
        data: Dict[str, Any] = self.coordinator.data or {}
        signals: List[Dict[str, Any]] = (data.get(self._ean) or {}).get("signals", [])
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
            except Exception:
                continue

            casy = s.get("casy", "")
            name = _signal_name(s)

            for part in [p.strip() for p in casy.split(";") if p.strip()]:
                try:
                    start_s, end_s = [x.strip() for x in part.replace("\u2013", "-").split("-")]
                    sdt = dt.datetime.combine(day.date(), dt.time.fromisoformat(start_s))
                    edt = dt.datetime.combine(day.date(), dt.time.fromisoformat(end_s))
                except Exception:
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
                    location = _build_location(self._hub, self._ean)
                    return CalendarEvent(
                        summary=f"Signál {name}", start=sdt, end=edt, location=location
                    )
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> List[CalendarEvent]:
        data: Dict[str, Any] = self.coordinator.data or {}
        signals: List[Dict[str, Any]] = (data.get(self._ean) or {}).get("signals", [])
        if not signals:
            return []

        tz_ref = start_date.tzinfo or dt.timezone.utc
        ha_tz = dt_util.get_time_zone(getattr(hass.config, "time_zone", None)) or tz_ref

        by_name: Dict[str, List[Tuple[dt.datetime, dt.datetime]]] = {}
        for s in signals:
            date_str = s.get("datum")
            if not date_str:
                continue
            try:
                day = dt.datetime.strptime(date_str, "%d.%m.%Y")
            except Exception:
                continue

            casy = s.get("casy", "")
            name = _signal_name(s)

            for part in [p.strip() for p in casy.split(";") if p.strip()]:
                try:
                    start_s, end_s = [x.strip() for x in part.replace("\u2013", "-").split("-")]
                    sdt = dt.datetime.combine(day.date(), dt.time.fromisoformat(start_s))
                    edt = dt.datetime.combine(day.date(), dt.time.fromisoformat(end_s))
                except Exception:
                    continue

                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=ha_tz)
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=ha_tz)
                sdt = sdt.astimezone(tz_ref)
                edt = edt.astimezone(tz_ref)

                if edt <= sdt:
                    edt += dt.timedelta(days=1)
                by_name.setdefault(name, []).append((sdt, edt))

        events: List[CalendarEvent] = []
        for name, intervals in by_name.items():
            if not intervals:
                continue
            intervals.sort(key=lambda x: x[0])
            merged: List[Tuple[dt.datetime, dt.datetime]] = []
            for sdt, edt in intervals:
                if not merged:
                    merged.append((sdt, edt))
                    continue
                last_s, last_e = merged[-1]
                if sdt <= last_e:
                    if edt > last_e:
                        merged[-1] = (last_s, edt)
                else:
                    merged.append((sdt, edt))

            for sdt, edt in merged:
                if edt < start_date or sdt > end_date:
                    continue
                ev_start = max(sdt, start_date)
                ev_end = min(edt, end_date)
                events.append(CalendarEvent(summary=f"Signál {name}", start=ev_start, end=ev_end))
        return events


def _signal_name(signal: Dict[str, Any]) -> str:
    return (
        signal.get("nazevSignalu")
        or signal.get("nazev")
        or signal.get("signal")
        or signal.get("name")
        or signal.get("oznaceni")
        or "HDO signál"
    )


def _address_from_detail(spd: Dict[str, Any]) -> Optional[str]:
    try:
        addr_node = spd.get("adresa") or spd.get("address")
        if isinstance(addr_node, dict):
            for key in ("adresaComplete", "complete", "full"):
                val = addr_node.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        street = spd.get("ulice") or spd.get("uliceNazev") or spd.get("street")
        number = (
            spd.get("cisloPopisne") or spd.get("cp") or spd.get("cisloDomovni") or spd.get("houseNumber")
        )
        city = spd.get("mesto") or spd.get("obec") or spd.get("city")
        psc = spd.get("psc") or spd.get("zip") or spd.get("pscKod")
        parts: List[str] = []
        if street and number:
            parts.append(f"{street} {number}")
        elif street:
            parts.append(str(street))
        elif number:
            parts.append(str(number))
        locality: List[str] = []
        if psc:
            locality.append(str(psc))
        if city:
            locality.append(str(city))
        if locality:
            parts.append(", ".join(locality))
        addr = ", ".join([p for p in parts if p and str(p).strip()])
        return addr if addr else None
    except Exception:
        return None


def _build_location(hub: CezEnergyHub, ean: Optional[str]) -> Optional[str]:
    try:
        location_parts: List[str] = []
        if ean:
            location_parts.append(f"EAN {ean}")
        if ean:
            for p in getattr(hub, "points", []) or []:
                spd = getattr(p, "supply_point_detail", {}) or {}
                if spd.get("ean") == ean:
                    addr = _address_from_detail(spd)
                    if addr:
                        location_parts.append(addr)
                    break
        return " \u2013 ".join(location_parts) if location_parts else None
    except Exception:
        return None
