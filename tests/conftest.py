"""Minimal mocks for homeassistant modules so tests can import integration code
without requiring the full Home Assistant installation."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock


def _make_module(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _setup_ha_mocks():
    """Install lightweight stubs for homeassistant.* into sys.modules."""
    if "homeassistant" in sys.modules:
        return

    ha = _make_module("homeassistant")
    sys.modules["homeassistant"] = ha

    # homeassistant.core
    def _callback_decorator(func):
        return func

    ha_core = _make_module("homeassistant.core", HomeAssistant=MagicMock, callback=_callback_decorator)
    sys.modules["homeassistant.core"] = ha_core

    # homeassistant.const
    ha_const = _make_module(
        "homeassistant.const",
        CONF_SCAN_INTERVAL="scan_interval",
        UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh"),
        UnitOfPower=SimpleNamespace(KILO_WATT="kW"),
    )
    sys.modules["homeassistant.const"] = ha_const

    # homeassistant.config_entries
    ha_ce = _make_module("homeassistant.config_entries", ConfigEntry=MagicMock, ConfigFlow=MagicMock)
    sys.modules["homeassistant.config_entries"] = ha_ce
    sys.modules["homeassistant"] = _make_module("homeassistant", config_entries=ha_ce)

    # homeassistant.helpers
    ha_helpers = _make_module("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = ha_helpers

    ha_cv = _make_module("homeassistant.helpers.config_validation", string=str, positive_int=int)
    sys.modules["homeassistant.helpers.config_validation"] = ha_cv

    ha_typing = _make_module("homeassistant.helpers.typing", ConfigType=dict)
    sys.modules["homeassistant.helpers.typing"] = ha_typing

    class FakeDeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            for k, v in kwargs.items():
                setattr(self, k, v)

    ha_device_registry = _make_module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=FakeDeviceInfo,
        DeviceEntryType=SimpleNamespace(SERVICE="service"),
    )
    sys.modules["homeassistant.helpers.device_registry"] = ha_device_registry

    ha_entity = _make_module("homeassistant.helpers.entity")
    sys.modules["homeassistant.helpers.entity"] = ha_entity

    # DataUpdateCoordinator
    class FakeCoordinator:
        def __init__(self, *a, **kw):
            self.data = None

        async def async_config_entry_first_refresh(self):
            pass

    ha_uc = _make_module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=FakeCoordinator,
        CoordinatorEntity=type("CoordinatorEntity", (), {"__init__": lambda self, *a, **kw: None}),
    )
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_uc

    # homeassistant.components.sensor
    class FakeSensorDeviceClass:
        ENERGY = "energy"
        POWER = "power"

    class FakeSensorStateClass:
        TOTAL_INCREASING = "total_increasing"
        TOTAL = "total"
        MEASUREMENT = "measurement"

    ha_sensor = _make_module(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorDeviceClass=FakeSensorDeviceClass,
        SensorStateClass=FakeSensorStateClass,
    )
    sys.modules["homeassistant.components.sensor"] = ha_sensor
    sys.modules["homeassistant.components"] = _make_module("homeassistant.components")

    # homeassistant.components.binary_sensor
    class FakeBSDeviceClass:
        POWER = "power"

    ha_bs = _make_module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=type("BinarySensorEntity", (), {}),
        BinarySensorDeviceClass=FakeBSDeviceClass,
    )
    sys.modules["homeassistant.components.binary_sensor"] = ha_bs

    # homeassistant.components.calendar
    class FakeCalendarEvent:
        def __init__(self, *, summary="", start=None, end=None, description=None, location=None):
            self.summary = summary
            self.start = start
            self.end = end
            self.description = description
            self.location = location

    ha_cal = _make_module(
        "homeassistant.components.calendar",
        CalendarEntity=type("CalendarEntity", (), {}),
        CalendarEvent=FakeCalendarEvent,
    )
    sys.modules["homeassistant.components.calendar"] = ha_cal

    # homeassistant.util.dt
    import datetime as _dt
    import zoneinfo as _zi

    def _get_time_zone(tz_name):
        if tz_name is None:
            return None
        try:
            return _zi.ZoneInfo(tz_name)
        except Exception:
            return None

    def _now():
        return _dt.datetime.now().astimezone()

    ha_util = _make_module("homeassistant.util")
    ha_util_dt = _make_module(
        "homeassistant.util.dt",
        get_time_zone=_get_time_zone,
        now=_now,
    )
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_util_dt

    # homeassistant.data_entry_flow
    ha_def = _make_module("homeassistant.data_entry_flow", FlowResult=dict)
    sys.modules["homeassistant.data_entry_flow"] = ha_def

    # homeassistant.components.recorder (for statistics import)
    class FakeStatisticMeanType:
        NONE = 0
        ARITHMETIC = 1
        CIRCULAR = 2

    class FakeStatisticMetaData:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    ha_recorder = _make_module("homeassistant.components.recorder")
    ha_recorder_models = _make_module(
        "homeassistant.components.recorder.models",
        StatisticMetaData=FakeStatisticMetaData,
        StatisticMeanType=FakeStatisticMeanType,
    )
    ha_recorder_statistics = _make_module(
        "homeassistant.components.recorder.statistics",
        async_add_external_statistics=MagicMock(),
    )
    sys.modules["homeassistant.components.recorder"] = ha_recorder
    sys.modules["homeassistant.components.recorder.models"] = ha_recorder_models
    sys.modules["homeassistant.components.recorder.statistics"] = ha_recorder_statistics


_setup_ha_mocks()
