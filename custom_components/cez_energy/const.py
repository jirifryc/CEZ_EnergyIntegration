import datetime as dt
import re

DOMAIN = "cez_energy"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ELECTROMETER_ID = "electrometer_id"

DEFAULT_SCAN_INTERVAL_REALTIME_MIN = 15
DEFAULT_SCAN_INTERVAL_DAILY_MIN = 1440
DEFAULT_SCAN_INTERVAL_SIGNALS_MIN = 1440
DEFAULT_SCAN_INTERVAL_OUTAGES_MIN = 1440

PND_BASE_URL = "https://pnd.cezdistribuce.cz/cezpnd2/external"
PND_DATA_URL = f"{PND_BASE_URL}/data"
PND_DASHBOARD_URL = f"{PND_BASE_URL}/dashboard/view"

PND_ID_ASSEMBLY_INTERVAL = -1001
PND_ID_ASSEMBLY_DAILY = -1027

HISTORY_DAYS = 14
HISTORY_DAILY_CHUNK_DAYS = 1
CONF_HISTORY_IMPORTED = "history_imported"

DATE_FORMAT_CZ = "%d.%m.%Y %H:%M"
DATE_FORMAT_CZ_DATE = "%d.%m.%Y"

_CZ_24_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+24:00$")


def parse_cz_datetime(timestamp_str: str) -> dt.datetime:
    """Parse ČEZ-style timestamps, handling the '24:00' midnight convention.

    The PND API uses '24:00' to mean midnight at the end of the day,
    e.g. '15.04.2026 24:00' == '16.04.2026 00:00'.
    """
    m = _CZ_24_RE.match(timestamp_str)
    if m:
        day = dt.datetime.strptime(m.group(1), DATE_FORMAT_CZ_DATE)
        return day + dt.timedelta(days=1)
    return dt.datetime.strptime(timestamp_str, DATE_FORMAT_CZ)
