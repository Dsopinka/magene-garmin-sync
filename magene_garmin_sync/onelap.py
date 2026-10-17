"""Client for the OnelapFit (Magene international) cloud.

Independent implementation of the small read-only slice we need — sign in, list
the athlete's own rides, download each ride's FIT — against OnelapFit's observed
REST interface. Ships none of the app's code.

The international backend rejects requests that lack the app's standard headers
(it answers 503), so every request carries them. Endpoint paths, query names and
the header set are facts about the wire protocol.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger("onelap")

# International backend. Confirmed base URL.
DEFAULT_HOST = "https://rfs-fitness.rfsvr.com"
APP_VERSION = "1.8.2"

PATH_LOGIN = "/api/v1/app/login"
PATH_REFRESH = "/api/v1/app/refresh_token"
PATH_RECORD_LIST = "/api/v1/app/record/riding/list"
PATH_RECORD_DETAIL = "/api/v1/app/record/riding/detail"

# Fields in a ride list item / detail that can carry the downloadable FIT.
FIT_FIELDS = ("fileAddr", "fileKey", "fit_url", "fitUrl", "durl")
# Fields that can carry the ride's id.
ID_FIELDS = ("did", "id", "activity_id", "recordId")
# Fields that can carry the ride's start time.
TIME_FIELDS = ("start_time", "start_riding_time", "created_at")


class OnelapError(RuntimeError):
    pass


class AuthError(OnelapError):
    pass


@dataclass
class Ride:
    ride_id: str
    start_time: datetime | None
    name: str
    raw: dict

    def fit_reference(self) -> str | None:
        for key in FIT_FIELDS:
            value = str(self.raw.get(key) or "").strip()
            if value:
                return value
        return None

    @property
    def date_stamp(self) -> str:
        if self.start_time:
            return self.start_time.strftime("%Y-%m-%d_%H%M%S")
        return self.ride_id


def _as_datetime(value) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        seconds = int(value)
        if seconds > 10_000_000_000:  # milliseconds
            seconds //= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[: len(fmt) + 2], fmt)
        except ValueError:
            continue
    return None


def _first(mapping: dict, keys) -> str:
    for key in keys:
        value = str(mapping.get(key) or "").strip()
        if value:
            return value
    return ""


class OnelapClient:
    def __init__(
        self,
        account: str,
        password: str,
        host: str | None = None,
        *,
        hash_password: bool = False,
        device_id: str | None = None,
        timezone_name: str = "UTC",
        language: str = "en",
        timeout: int = 30,
        auth_scheme: str = "",  # kept for config compatibility; unused
    ):
        self.account = account
        self._password = password
        self.host = (host or DEFAULT_HOST).rstrip("/")
        self.hash_password = hash_password
        self.timeout = timeout
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._uid = 0

        device_id = device_id or str(uuid.uuid4())
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Timezone": timezone_name,
                "UserId": "0",
                "Nid": "0",
                "App-Name": "onelapfit",
                "App-Version": APP_VERSION,
                "Version": APP_VERSION,
                "App-Area": "China",
                "App-Language": language,
                "Language": language,
                "Platform": "44",
                "DeviceId": device_id,
                "SessionId": str(uuid.uuid4()),
                "ShowId": str(uuid.uuid4()),
            }
        )

    # -- auth ---------------------------------------------------------------

    def _password_value(self) -> str:
        if self.hash_password:
            return hashlib.md5(self._password.encode()).hexdigest()
        return self._password

    def _set_token(self, token: str) -> None:
        self._token = token
        if token:
            self._session.headers["Authorization"] = token
        else:
            self._session.headers.pop("Authorization", None)

    def _store_login(self, payload: dict) -> None:
        data = payload.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            raise AuthError(f"login response had no data object: {payload}")
        token = str(data.get("token") or "").strip()
        if not token:
            raise AuthError("login response carried no token")
        self._set_token(token)
        self._refresh_token = str(data.get("refresh_token") or "").strip() or None
        userinfo = data.get("userinfo")
        if isinstance(userinfo, dict) and userinfo.get("uid"):
            self._uid = int(userinfo["uid"])
            self._session.headers["UserId"] = str(self._uid)

    def login(self) -> None:
        body = {"account": self.account, "password": self._password_value()}
        resp = self._session.post(self.host + PATH_LOGIN, json=body, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise AuthError(f"login rejected ({resp.status_code})")
        resp.raise_for_status()
        payload = resp.json()
        code = payload.get("code")
        if code not in (0, 200, None):
            raise AuthError(f"login failed (code={code}): {payload.get('error') or payload.get('msg') or payload}")
        self._store_login(payload)
        log.info("signed in to %s as %s", self.host, self.account)

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        # Refresh posts an empty body with the refresh token in Authorization.
        headers = dict(self._session.headers)
        headers["Authorization"] = self._refresh_token
        resp = self._session.post(
            self.host + PATH_REFRESH, data="", headers=headers, timeout=self.timeout
        )
        if resp.status_code != 200:
            return False
        try:
            payload = resp.json()
        except ValueError:
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("token"):
            return False
        self._set_token(str(data["token"]).strip())
        if data.get("refresh_token"):
            self._refresh_token = str(data["refresh_token"]).strip()
        return True

    def _relogin(self) -> bool:
        self._set_token("")
        self._refresh_token = None
        try:
            self.login()
            return True
        except OnelapError:
            return False

    def _get_json(self, path: str, params: dict) -> dict:
        if not self._token:
            self.login()
        url = self.host + path
        resp = self._session.get(url, params=params, timeout=self.timeout)
        if resp.status_code in (401, 403):
            if self._refresh() or self._relogin():
                resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # -- rides --------------------------------------------------------------

    def list_rides(self, page: int = 1, limit: int = 50) -> list[Ride]:
        params = {
            "start_time": 0,
            "end_time": 4102444800,  # year 2100, seconds
            "p": page,
            "source": "",
            "data_type": "",
        }
        payload = self._get_json(PATH_RECORD_LIST, params)
        if isinstance(payload, dict) and payload.get("code") not in (0, 200, None):
            raise OnelapError(
                f"ride list failed (code={payload.get('code')}): {payload.get('error') or payload.get('msg')}"
            )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            items = data.get("list") or data.get("records") or data.get("items")
        else:
            items = data
        if not isinstance(items, list):
            raise OnelapError(f"unexpected ride-list shape: keys={list(data) if isinstance(data, dict) else type(data)}")
        rides: list[Ride] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            rid = _first(raw, ID_FIELDS)
            if not rid:
                continue
            start = _as_datetime(next((raw.get(k) for k in TIME_FIELDS if raw.get(k)), None))
            rides.append(Ride(rid, start, str(raw.get("name") or "").strip(), raw))
            if len(rides) >= limit:
                break
        return rides

    def ride_detail(self, ride_id: str) -> dict:
        payload = self._get_json(PATH_RECORD_DETAIL, {"did": ride_id, "from": ""})
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})

    def resolve_fit_url(self, ride: Ride) -> str | None:
        ref = ride.fit_reference()
        if not ref:
            detail = self.ride_detail(ride.ride_id)
            ref = _first(detail, FIT_FIELDS)
        if not ref:
            return None
        if ref.startswith(("http://", "https://")):
            return ref
        return f"{self.host}/{ref.lstrip('/')}"

    def download_fit(self, ride: Ride, dest_path: str) -> bool:
        url = self.resolve_fit_url(ride)
        if not url:
            log.warning("ride %s has no FIT reference; skipping", ride.ride_id)
            return False
        if not self._token:
            self.login()
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        content = resp.content
        if len(content) < 14 or content[8:12] != b".FIT":
            log.warning("ride %s download was not a FIT file (%d bytes)", ride.ride_id, len(content))
            return False
        with open(dest_path, "wb") as fh:
            fh.write(content)
        return True
