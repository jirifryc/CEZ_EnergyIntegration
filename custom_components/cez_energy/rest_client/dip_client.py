"""REST client for the ČEZ Distribuce DIP portal.

Handles authenticated access for HDO signals and supply-point data,
plus anonymous access for outage searches.
"""
import logging

from .base import AbstractCezRestClient, LOGIN_RETRIES, is_array

_LOGGER = logging.getLogger(__name__)

CEZ_DISTRIBUCE_CLIENT_ID = "fjR3ZL9zrtsNcDQF.onpremise.dip.sap.dipcezdistribucecz.prod"
CEZ_DISTRIBUCE_BASE_URL = "https://dip.cezdistribuce.cz/irj/portal"


class CezDistribuceRestClient(AbstractCezRestClient):
    def __init__(
        self,
        base_url: str = CEZ_DISTRIBUCE_BASE_URL,
        client_id: str = CEZ_DISTRIBUCE_CLIENT_ID,
    ):
        self._base_url = base_url
        super().__init__(self._base_url, client_id)

    def login(self, username=None, password=None):
        super().login(username, password)
        self.refresh_api_token()
        self.refresh_anon_api_token()

    def refresh_api_token(self):
        api_token = self._get(self._session, f"{self._base_url}/rest-auth-api?path=/token/get")
        self._session.headers.update({"X-Request-Token": api_token})

    def refresh_anon_api_token(self):
        api_token = self._get(
            self._anonymous_session,
            f"{self._base_url}/anonymous/rest-auth-api?path=/token/get",
        )
        self._anonymous_session.headers.update({"X-Request-Token": api_token})

    def common_header(self):
        return self._get(self._session, f"{self._base_url}/common-api?path=/common/header")

    def get_supply_points(self):
        return self._post(
            self._session,
            f"{self._base_url}/vyhledani-om?path=/vyhledaniom/zakladniInfo/50/PREHLED_OM_CELEK",
            json={"nekontrolovatPrislusnostOM": False},
        )

    def get_supply_point_detail(self, uid: str):
        return self._get(self._session, f"{self._base_url}/prehled-om?path=supply-point-detail/{uid}")

    def get_readings(self, uid: str):
        return self._post(
            self._session,
            f"{self._base_url}/prehled-om?path=supply-point-detail/meter-reading-history/{uid}/false",
            json={},
        )

    def get_signals(self, ean: str):
        return self._get(self._session, f"{self._base_url}/prehled-om?path=supply-point-detail/signals/{ean}")

    def get_outages(self, ean=None, meter_number=None, psc=None, mesto=None, ulice=None):
        if ean:
            payload = {"eans": ean if is_array(ean) else [ean]}
        elif meter_number:
            payload = {"meterNumbers": meter_number if is_array(meter_number) else [meter_number]}
        elif psc and mesto:
            payload = {"psc": psc, "mesto": mesto, "ulice": ulice}
        else:
            raise ValueError("Either ean, meter_number, or psc+mesto must be provided")
        return self._post(
            self._anonymous_session,
            f"{self._base_url}/anonymous/vyhledani-odstavek?path=shutdown-search",
            json=payload,
        )

    def _get(self, session, url, **kwargs):
        return self._handle_token(
            session == self._anonymous_session,
            lambda: AbstractCezRestClient._get(self, session, url, **kwargs),
        )

    def _post(self, session, url, data=None, json=None, **kwargs):
        return self._handle_token(
            session == self._anonymous_session,
            lambda: AbstractCezRestClient._post(self, session, url, data=data, json=json, **kwargs),
        )

    def _handle_token(self, is_anonymous, func):
        for _ in range(LOGIN_RETRIES):
            json_response = func()
            if "statusCode" in json_response:
                if json_response["statusCode"] == 401:
                    if is_anonymous:
                        self.refresh_anon_api_token()
                    else:
                        self.refresh_api_token()
                    continue
                elif json_response["statusCode"] == 200:
                    return json_response["data"]
            else:
                return json_response.get("data", json_response)
        raise Exception("Unable to get DIP REST token after retries")
