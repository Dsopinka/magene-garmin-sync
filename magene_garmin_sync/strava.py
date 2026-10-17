"""Strava API client (official v3 API, OAuth2).

Reads the authenticated athlete's own activities and per-second streams. Used as
the source for the Garmin bridge: your Magene rides already sync to Strava, so
we read them here (with your authorization) and rebuild them for Garmin.

Tokens: the app uses a refresh token (obtained once via the Connect flow) to
mint short-lived access tokens. A callback persists refreshed tokens.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

log = logging.getLogger("strava")

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
SCOPE = "activity:read_all"

STREAM_KEYS = "time,latlng,altitude,heartrate,cadence,watts,distance,temp,velocity_smooth,moving"


class StravaError(RuntimeError):
    pass


class StravaAuthError(StravaError):
    pass


@dataclass
class Activity:
    id: str
    name: str
    sport_type: str
    start_date: str          # ISO8601 UTC, e.g. 2026-10-14T16:48:30Z
    device_name: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start_date.replace("Z", "+00:00"))

    @property
    def date_stamp(self) -> str:
        return self.start_dt.strftime("%Y-%m-%d_%H%M%S")

    @property
    def is_ride(self) -> bool:
        return self.sport_type in (
            "Ride", "GravelRide", "MountainBikeRide", "EBikeRide", "VirtualRide",
        )

    @property
    def is_virtual(self) -> bool:
        return "Virtual" in self.sport_type


def build_authorize_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    from urllib.parse import urlencode

    q = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "approval_prompt": "auto",
    }
    if state:
        q["state"] = state
    return f"{AUTH_URL}?{urlencode(q)}"


class StravaClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        access_token: str = "",
        expires_at: int = 0,
        on_token: Callable[[dict], None] | None = None,
        timeout: int = 30,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.expires_at = int(expires_at or 0)
        self.on_token = on_token
        self.timeout = timeout
        self._session = requests.Session()

    # -- oauth --------------------------------------------------------------

    @classmethod
    def exchange_code(cls, client_id: str, client_secret: str, code: str, timeout: int = 30) -> dict:
        """Trade an authorization code for tokens. Returns the token payload."""
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise StravaAuthError(f"code exchange failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json()

    def _ensure_token(self) -> None:
        if self.access_token and time.time() < self.expires_at - 60:
            return
        if not self.refresh_token:
            raise StravaAuthError("not connected to Strava (no refresh token)")
        resp = self._session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise StravaAuthError(f"token refresh failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        self.access_token = data["access_token"]
        self.expires_at = int(data.get("expires_at") or (time.time() + 21600))
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        if self.on_token:
            self.on_token(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at,
                }
            )

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        self._ensure_token()
        resp = self._session.get(
            API + path,
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            self.access_token = ""
            self._ensure_token()
            resp = self._session.get(
                API + path,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=self.timeout,
            )
        if resp.status_code == 429:
            raise StravaError("Strava rate limit reached; try again later")
        resp.raise_for_status()
        return resp

    # -- reads --------------------------------------------------------------

    def athlete_name(self) -> str:
        data = self._get("/athlete").json()
        return " ".join(x for x in (data.get("firstname"), data.get("lastname")) if x) or "athlete"

    def list_activities(self, after_epoch: int = 0, per_page: int = 30, page: int = 1) -> list[Activity]:
        params = {"per_page": per_page, "page": page}
        if after_epoch:
            params["after"] = after_epoch
        items = self._get("/athlete/activities", params).json()
        out: list[Activity] = []
        for raw in items:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            out.append(
                Activity(
                    id=str(raw["id"]),
                    name=str(raw.get("name") or ""),
                    sport_type=str(raw.get("sport_type") or raw.get("type") or ""),
                    start_date=str(raw.get("start_date") or ""),
                    device_name="",  # only on detail
                    raw=raw,
                )
            )
        return out

    def activity_detail(self, activity_id: str) -> Activity:
        raw = self._get(f"/activities/{activity_id}", {"include_all_efforts": "false"}).json()
        return Activity(
            id=str(raw["id"]),
            name=str(raw.get("name") or ""),
            sport_type=str(raw.get("sport_type") or raw.get("type") or ""),
            start_date=str(raw.get("start_date") or ""),
            device_name=str(raw.get("device_name") or ""),
            raw=raw,
        )

    def streams(self, activity_id: str) -> dict:
        """Return {key: [values...]} for the activity's streams, aligned by index."""
        data = self._get(
            f"/activities/{activity_id}/streams",
            {"keys": STREAM_KEYS, "key_by_type": "true"},
        ).json()
        out = {}
        if isinstance(data, dict):
            for key, obj in data.items():
                if isinstance(obj, dict) and "data" in obj:
                    out[key] = obj["data"]
        return out
