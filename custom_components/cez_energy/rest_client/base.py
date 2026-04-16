"""Base CAS authentication client for ČEZ services.

Automates a browser-like OAuth2/CAS dance using requests.Session.
All network calls are synchronous; callers must run them off the HA event loop.
"""
import logging
import urllib.parse
from typing import Optional, Sequence

import requests
from bs4 import BeautifulSoup
from requests import Response, Session

_LOGGER = logging.getLogger(__name__)

CAS_BASE_URL = "https://cas.cez.cz/cas"
CLIENT_NAME = "CasOAuthClient"
RESPONSE_TYPE = "code"
SCOPE = "openid"

LOGIN_RETRIES = 2


class AbstractCezRestClient:
    def __init__(
        self,
        redirect_url: str,
        client_id: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        extra_params: Optional[dict] = None,
    ):
        self._service = (
            f"{CAS_BASE_URL}/oauth2.0/callbackAuthorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_url)}"
            f"&response_type={RESPONSE_TYPE}"
            f"&client_name={CLIENT_NAME}"
        )
        self._login_url = f"{CAS_BASE_URL}/login?service={urllib.parse.quote(self._service)}"
        self._authorize_url = (
            f"{CAS_BASE_URL}/oidc/authorize"
            f"?scope={SCOPE}"
            f"&response_type={RESPONSE_TYPE}"
            f"&redirect_uri={urllib.parse.quote(redirect_url)}"
            f"&client_id={client_id}"
        )
        self._username = username
        self._password = password
        if extra_params:
            self._login_url += f"&{urllib.parse.urlencode(extra_params)}"
            self._authorize_url += f"&{urllib.parse.urlencode(extra_params)}"
        self._session = requests.Session()
        self._session.max_redirects = 10
        self._anonymous_session = requests.Session()

    def login(self, username: Optional[str] = None, password: Optional[str] = None):
        if username:
            self._username = username
        if password:
            self._password = password
        response = self._session.get(self._login_url)
        soup = BeautifulSoup(response.text, "html.parser")
        execution_input = soup.find("input", {"name": "execution"})
        if not execution_input:
            raise Exception("CAS login page missing execution field")
        response = self._session.post(
            self._login_url,
            data={
                "username": self._username,
                "password": self._password,
                "execution": execution_input["value"],
                "_eventId": "submit",
                "geolocation": "",
            },
        )
        _LOGGER.debug(log_history(response))
        response = self._session.get(self._authorize_url)
        _LOGGER.debug(log_history(response))

    def _post(self, session: Session, url, data=None, json=None, **kwargs):
        return self._handle_login(lambda: session.post(url, data=data, json=json, **kwargs))

    def _get(self, session: Session, url, **kwargs):
        return self._handle_login(lambda: session.get(url, **kwargs))

    def _handle_login(self, func):
        for _ in range(LOGIN_RETRIES):
            response = func()
            if response.status_code == 401:
                self.login()
                continue
            elif response.status_code == 200:
                return response.json()
        raise Exception("Unable to login after retries")


def is_array(value) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str)


def log_history(response: Response) -> str:
    result = ""
    for resp in response.history:
        result += f"\n{resp.status_code} {resp.is_redirect} {resp.headers} {resp.url}"
    result += f"\n{response.status_code} {response.is_redirect} {response.headers} {response.url}"
    return result
