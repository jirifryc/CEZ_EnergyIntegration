"""REST client for the ČEZ PND (energy data) portal.

Handles authentication via CAS and provides methods to fetch
15-minute interval data and daily cumulative meter readings.
"""
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from ..const import (
    PND_BASE_URL,
    PND_DATA_URL,
    PND_DASHBOARD_URL,
    PND_ID_ASSEMBLY_INTERVAL,
    PND_ID_ASSEMBLY_DAILY,
)
from .base import CAS_BASE_URL, LOGIN_RETRIES, log_history

_LOGGER = logging.getLogger(__name__)

PND_LOGIN_URL = f"{PND_BASE_URL}/login"


class CezPndRestClient:
    """Client for the PND portal at pnd.cezdistribuce.cz.

    PND uses CAS SSO. The login flow is:
    1. GET the PND login URL -> redirects to CAS
    2. POST CAS form with credentials -> redirects back through PND
    3. Follow all redirects -> end up with JSESSIONID cookie
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.max_redirects = 20
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
        })
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    def login(self, username: Optional[str] = None, password: Optional[str] = None):
        if username:
            self._username = username
        if password:
            self._password = password

        # Step 1: navigate to PND login endpoint which should redirect to CAS
        _LOGGER.debug("PND: navigating to %s", PND_LOGIN_URL)
        response = self._session.get(PND_LOGIN_URL, allow_redirects=True)
        _LOGGER.debug("PND: after login GET -> status=%s url=%s", response.status_code, response.url)
        _LOGGER.debug("PND: redirect history: %s", [(r.status_code, r.url) for r in response.history])

        # Try the login page URL first, then fall back to dashboard
        if not self._try_cas_login(response):
            _LOGGER.debug("PND: login URL didn't reach CAS, trying dashboard URL")
            response = self._session.get(PND_DASHBOARD_URL, allow_redirects=True)
            _LOGGER.debug("PND: after dashboard GET -> status=%s url=%s", response.status_code, response.url)
            if not self._try_cas_login(response):
                _LOGGER.debug("PND: dashboard didn't reach CAS either, trying direct CAS with PND service")
                self._cas_login_for_pnd()

        self._log_session_state("after login")

    def _try_cas_login(self, response: requests.Response) -> bool:
        """If the response is a CAS login page, submit credentials. Returns True if handled."""
        if "cas.cez.cz" not in response.url:
            # Check if the response HTML contains a CAS login form anyway
            if "execution" not in response.text:
                return False

        soup = BeautifulSoup(response.text, "html.parser")
        execution_input = soup.find("input", {"name": "execution"})
        if not execution_input:
            _LOGGER.debug("PND: page at %s has no execution field", response.url)
            return False

        # Find the form action URL (may differ from current URL)
        form = soup.find("form")
        if form and form.get("action"):
            action_url = form["action"]
            if not action_url.startswith("http"):
                from urllib.parse import urljoin
                action_url = urljoin(response.url, action_url)
        else:
            action_url = response.url

        _LOGGER.debug("PND: submitting CAS form to %s", action_url)
        response = self._session.post(
            action_url,
            data={
                "username": self._username,
                "password": self._password,
                "execution": execution_input["value"],
                "_eventId": "submit",
                "geolocation": "",
            },
            allow_redirects=True,
        )
        _LOGGER.debug("PND: after CAS POST -> status=%s url=%s", response.status_code, response.url)
        _LOGGER.debug("PND: post-login redirects: %s", [(r.status_code, r.url) for r in response.history])
        return True

    def _cas_login_for_pnd(self):
        """Direct CAS login using PND's service URL as the callback."""
        import urllib.parse

        service_url = PND_LOGIN_URL
        cas_login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(service_url)}"

        _LOGGER.debug("PND: direct CAS login via %s", cas_login_url)
        response = self._session.get(cas_login_url, allow_redirects=True)
        _LOGGER.debug("PND: CAS page -> status=%s url=%s", response.status_code, response.url)

        soup = BeautifulSoup(response.text, "html.parser")
        execution_input = soup.find("input", {"name": "execution"})
        if not execution_input:
            _LOGGER.warning("PND: CAS direct login page missing execution field at %s", response.url)
            _LOGGER.debug("PND: response snippet: %.500s", response.text[:500])
            return

        form = soup.find("form")
        action_url = response.url
        if form and form.get("action"):
            action = form["action"]
            if not action.startswith("http"):
                from urllib.parse import urljoin
                action_url = urljoin(response.url, action)
            else:
                action_url = action

        response = self._session.post(
            action_url,
            data={
                "username": self._username,
                "password": self._password,
                "execution": execution_input["value"],
                "_eventId": "submit",
                "geolocation": "",
            },
            allow_redirects=True,
        )
        _LOGGER.debug("PND: after direct CAS POST -> status=%s url=%s", response.status_code, response.url)
        _LOGGER.debug("PND: redirects: %s", [(r.status_code, r.url[:100]) for r in response.history])

        # After CAS login, navigate to the PND dashboard to establish the session
        response = self._session.get(PND_DASHBOARD_URL, allow_redirects=True)
        _LOGGER.debug("PND: dashboard after CAS -> status=%s url=%s", response.status_code, response.url)

    def _log_session_state(self, context: str):
        cookies = self._session.cookies.get_dict()
        cookie_names = list(cookies.keys())
        _LOGGER.debug("PND [%s]: cookies=%s", context, cookie_names)
        has_jsessionid = "JSESSIONID" in cookies
        if not has_jsessionid:
            all_cookies = {c.name: c.domain for c in self._session.cookies}
            _LOGGER.warning(
                "PND [%s]: no JSESSIONID found. All cookies: %s", context, all_cookies
            )

    def _post_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to the PND data endpoint with auto-login retry."""
        for attempt in range(LOGIN_RETRIES + 1):
            response = self._session.post(
                PND_DATA_URL,
                json=payload,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": "https://pnd.cezdistribuce.cz",
                    "Referer": PND_DASHBOARD_URL,
                },
            )
            _LOGGER.debug(
                "PND data POST: status=%s content-type=%s len=%s",
                response.status_code,
                response.headers.get("content-type", "?"),
                len(response.content),
            )

            if response.status_code in (401, 403):
                _LOGGER.debug("PND: got %s, re-authenticating (attempt %d)", response.status_code, attempt)
                self.login()
                continue

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type or response.text.strip().startswith("<!DOCTYPE"):
                _LOGGER.warning(
                    "PND: got HTML instead of JSON (status=%s). "
                    "Session may be expired. Re-authenticating (attempt %d)",
                    response.status_code, attempt,
                )
                if attempt < LOGIN_RETRIES:
                    self.login()
                    continue
                raise Exception(
                    f"PND returned HTML instead of JSON after {LOGIN_RETRIES + 1} attempts. "
                    f"Authentication flow may need adjustment. "
                    f"Response URL: {response.url}, Status: {response.status_code}"
                )

            response.raise_for_status()
            return response.json()
        raise Exception("Unable to fetch PND data after retries")

    @staticmethod
    def _format_date(date: dt.date, time_str: str = "00:00") -> str:
        return f"{date.strftime('%d.%m.%Y')} {time_str}"

    def get_interval_data(
        self,
        electrometer_id: str,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Dict[str, Any]:
        """Fetch 15-minute interval power data (kW) for the given date range."""
        payload = {
            "format": "chart",
            "idAssembly": PND_ID_ASSEMBLY_INTERVAL,
            "idDeviceSet": None,
            "intervalFrom": self._format_date(date_from, "00:00"),
            "intervalTo": self._format_date(date_to, "00:00"),
            "compareFrom": None,
            "opmId": None,
            "electrometerId": electrometer_id,
        }
        return self._post_data(payload)

    def get_daily_data(
        self,
        electrometer_id: str,
        date_from: dt.date,
        date_to: dt.date,
    ) -> Dict[str, Any]:
        """Fetch daily cumulative meter readings (kWh) with NT/VT breakdown."""
        payload = {
            "format": "chart",
            "idAssembly": PND_ID_ASSEMBLY_DAILY,
            "idDeviceSet": None,
            "intervalFrom": self._format_date(date_from, "00:00"),
            "intervalTo": self._format_date(date_to, "00:00"),
            "compareFrom": None,
            "opmId": None,
            "electrometerId": electrometer_id,
        }
        return self._post_data(payload)

    @staticmethod
    def parse_interval_series(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse 15-min interval response into list of {timestamp, kw, status} dicts."""
        result: List[Dict[str, Any]] = []
        if not response.get("hasData"):
            return result
        for series in response.get("series", []):
            for entry in series.get("data", []):
                if len(entry) >= 2:
                    result.append({
                        "timestamp": entry[0],
                        "kw": float(entry[1]),
                        "status": entry[2] if len(entry) > 2 else None,
                    })
        return result

    @staticmethod
    def parse_daily_series(response: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """Parse daily response into {total, nt, vt, export} values.

        Returns the latest available reading for each series type.
        """
        values: Dict[str, Optional[float]] = {
            "total": None,
            "nt": None,
            "vt": None,
            "export": None,
        }
        if not response.get("hasData"):
            return values
        for series in response.get("series", []):
            name = series.get("name", "")
            data = series.get("data", [])
            if not data:
                continue
            latest = data[-1]
            val = float(latest[1]) if len(latest) >= 2 else None
            if "+E_NT/" in name:
                values["nt"] = val
            elif "+E_VT/" in name:
                values["vt"] = val
            elif "-E/" in name:
                values["export"] = val
            elif "+E/" in name:
                values["total"] = val
        return values
