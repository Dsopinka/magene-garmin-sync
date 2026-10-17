"""Client for the OnelapFit (Magene international) cloud.

The OnelapFit app talks to a plain JSON REST backend. This client implements
only the read side we need: sign in, list the athlete's own ride records, and
download each ride's FIT file. It is an independent implementation against the
observed REST interface; it ships none of the app's code.

Only the endpoint *paths* are hard-coded. The host and a couple of auth details
are configurable because Onelap runs several regional backends, and the easiest
way to pin them down is to probe with a real account (see ``cli probe``).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger("onelap")

# Candidate international hosts, best guess first. `probe` tries each in order.
DEFAULT_HOSTS = [
    "https://rfs-fitness.rfsvr.com",
    "https://rfs-fitness.oversea.wonlap.cn",
]

# REST paths observed in the shipped app.
PATH_LOGIN = "/api/v1/app/login"
PATH_REFRESH = "/api/v1/app/refresh_token"
PATH_RECORD_LIST = "/api/v1/app/record/riding/list"
PATH_RECORD_DETAIL = "/api/v1/app/record/riding/detail"

# Response fields that may carry the downloadable FIT location, in priority order.
FIT_URL_FIELDS = ("fit_url", "fitUrl", "durl", "fileKey")


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
        for key in FIT_URL_FIELDS:
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
    """Best-effort parse of the many time shapes an activity list can use."""
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


class OnelapClient:
    def __init__(
        self,
        account: str,
        password: str,
        host: str | None = None,
        *,
        hash_password: bool = False,
        auth_scheme: str = "",
        timeout: int = 30,
    ):
        self.account = account
        self._password = password
        self.host = (host or DEFAULT_HOSTS[0]).rstrip("/")
        self.hash_password = hash_password
        self.auth_scheme = auth_scheme  # "" => send bare token; e.g. "Bearer "
        self.timeout = timeout
        self._token: str | None = None
        self._refresh_token: str | None = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "magene-garmin-sync/0.1",
            }
        )

    # -- auth ---------------------------------------------------------------

    def _password_value(self) -> str:
        if self.hash_password:
            return hashlib.md5(self._password.encode()).hexdigest()
        return self._password

    def _auth_headers(self) -> dict:
        if not self._token:
            return {}
        return {"Authorization": f"{self.auth_scheme}{self._token}"}

    def _store_tokens(self, payload: dict) -> None:
        data = payload.get("data")
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            raise AuthError(f"login response had no data object: {payload}")
        token = str(data.get("token") or data.get("access_token") or "").strip()
        if not token:
            raise AuthError(f"login response carried no token: {data}")
        self._token = token
        self._refresh_token = str(
            data.get("refresh_token") or data.get("refreshToken") or ""
        ).strip() or None

    def login(self) -> None:
        body = {"account": self.account, "password": self._password_value()}
        resp = self._session.post(
            self.host + PATH_LOGIN, json=body, timeout=self.timeout
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"login rejected ({resp.status_code}): check account/password")
        resp.raise_for_status()
        payload = resp.json()
        code = payload.get("code")
        if code not in (0, 200, None):
            raise AuthError(f"login failed (code={code}): {payload.get('msg') or payload}")
        self._store_tokens(payload)
        log.info("signed in to %s as %s", self.host, self.account)

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        resp = self._session.post(
            self.host + PATH_REFRESH,
            json={"refresh_token": self._refresh_token},
            headers=self._auth_headers(),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return False
        try:
            self._store_tokens(resp.json())
            return True
        except (AuthError, ValueError):
            return False

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        if not self._token:
            self.login()
        url = self.host + path
        resp = self._session.get(
            url, params=params, headers=self._auth_headers(), timeout=self.timeout
        )
        if resp.status_code in (401, 403):
            if self._refresh() or self._relogin():
                resp = self._session.get(
                    url, params=params, headers=self._auth_headers(), timeout=self.timeout
                )
        resp.raise_for_status()
        return resp.json()

    def _relogin(self) -> bool:
        self._token = self._refresh_token = None
        try:
            self.login()
            return True
        except OnelapError:
            return False

    # -- rides --------------------------------------------------------------

    def list_rides(self, page: int = 1, limit: int = 50) -> list[Ride]:
        payload = self._get_json(PATH_RECORD_LIST, {"page": page, "limit": limit})
        data = payload.get("data")
        items = data.get("list") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise OnelapError(f"unexpected ride-list shape: {payload}")
        rides: list[Ride] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("id") or raw.get("activity_id") or raw.get("recordId") or "").strip()
            if not rid:
                continue
            start = _as_datetime(
                raw.get("start_time") or raw.get("start_riding_time") or raw.get("created_at")
            )
            rides.append(Ride(rid, start, str(raw.get("name") or "").strip(), raw))
        return rides

    def ride_detail(self, ride_id: str) -> dict:
        payload = self._get_json(PATH_RECORD_DETAIL, {"id": ride_id})
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("ridingRecord") if isinstance(data.get("ridingRecord"), dict) else data
        return payload

    def resolve_fit_url(self, ride: Ride) -> str | None:
        ref = ride.fit_reference()
        if not ref:
            detail = self.ride_detail(ride.ride_id)
            for key in FIT_URL_FIELDS:
                value = str(detail.get(key) or "").strip()
                if value:
                    ref = value
                    break
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
        resp = self._session.get(
            url, headers=self._auth_headers(), timeout=self.timeout, stream=True
        )
        resp.raise_for_status()
        content = resp.content
        # A real FIT starts with ".FIT" at byte offset 8; anything else is an
        # error page the server returned with a 200.
        if len(content) < 14 or content[8:12] != b".FIT":
            log.warning("ride %s download was not a FIT file (%d bytes)", ride.ride_id, len(content))
            return False
        with open(dest_path, "wb") as fh:
            fh.write(content)
        return True
