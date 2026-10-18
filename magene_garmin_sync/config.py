"""Configuration.

Settings merge from environment variables and a JSON file at
$DATA_DIR/config.json (the file, written by the web UI, wins). This lets you run
the container with no secrets in the compose file and enter everything in the UI.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def data_dir() -> str:
    return os.environ.get("DATA_DIR", "/data")


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


@dataclass
class Config:
    # Strava source (you register a free API app; see README)
    strava_client_id: str = ""
    strava_client_secret: str = ""
    # OAuth tokens, set by the Connect flow (not entered by hand)
    strava_refresh_token: str = ""
    strava_access_token: str = ""
    strava_expires_at: int = 0

    # Which Strava activities to send
    device_filter: str = ""        # substring match on device name; "" = any
    include_virtual: bool = False  # include Zwift/virtual rides
    rides_only: bool = True        # only cycling activities
    skip_garmin_origin: bool = True  # ignore activities that came from Garmin (avoid bounce-back)

    # Garmin Connect destination
    garmin_email: str = ""
    garmin_password: str = ""
    garmin_mfa: str = ""

    poll_seconds: int = 3600
    scan_count: int = 30           # how many recent Strava activities to scan
    auto_sync: bool = False

    # Fields the web UI may write.
    EDITABLE = (
        "strava_client_id",
        "strava_client_secret",
        "device_filter",
        "include_virtual",
        "rides_only",
        "skip_garmin_origin",
        "garmin_email",
        "garmin_password",
        "garmin_mfa",
        "poll_seconds",
        "scan_count",
        "auto_sync",
    )
    # Fields persisted to disk (editable + OAuth tokens).
    PERSIST = EDITABLE + ("strava_refresh_token", "strava_access_token", "strava_expires_at")

    @property
    def data_dir(self) -> str:
        return data_dir()

    @property
    def tokenstore(self) -> str:
        return os.path.join(self.data_dir, "garmin_tokens")

    @property
    def state_file(self) -> str:
        return os.path.join(self.data_dir, "sync_state.json")

    @property
    def fit_dir(self) -> str:
        return os.path.join(self.data_dir, "fit")

    @property
    def strava_ready(self) -> bool:
        return bool(self.strava_client_id and self.strava_client_secret)

    @property
    def strava_connected(self) -> bool:
        return bool(self.strava_refresh_token)

    @property
    def garmin_ready(self) -> bool:
        return bool(self.garmin_email and self.garmin_password)

    # -- persistence --------------------------------------------------------

    def redacted(self) -> dict:
        out = asdict(self)
        for secret in ("strava_client_secret", "garmin_password", "garmin_mfa"):
            out[f"{secret}_set"] = bool(out.pop(secret))
        # never expose raw tokens to the browser
        out.pop("strava_refresh_token", None)
        out.pop("strava_access_token", None)
        out.pop("strava_expires_at", None)
        out["strava_connected"] = self.strava_connected
        return out

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {k: getattr(self, k) for k in self.PERSIST}
        tmp = config_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, config_path())
        try:
            os.chmod(config_path(), 0o600)
        except OSError:
            pass

    def update(self, changes: dict) -> None:
        """Apply user-editable changes (from the web UI)."""
        for key in self.EDITABLE:
            if key not in changes:
                continue
            value = changes[key]
            if key in ("strava_client_secret", "garmin_password", "garmin_mfa") and value == "":
                continue  # blank = keep current
            if key in ("include_virtual", "rides_only", "auto_sync", "skip_garmin_origin"):
                value = _bool(value)
            elif key in ("poll_seconds", "scan_count"):
                try:
                    value = max(1, int(value))
                except (TypeError, ValueError):
                    continue
            setattr(self, key, value)

    def set_strava_tokens(self, tokens: dict) -> None:
        self.strava_refresh_token = str(tokens.get("refresh_token") or self.strava_refresh_token)
        self.strava_access_token = str(tokens.get("access_token") or "")
        self.strava_expires_at = int(tokens.get("expires_at") or 0)
        self.save()

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        env_map = {
            "strava_client_id": os.environ.get("STRAVA_CLIENT_ID"),
            "strava_client_secret": os.environ.get("STRAVA_CLIENT_SECRET"),
            "garmin_email": os.environ.get("GARMIN_EMAIL"),
            "garmin_password": os.environ.get("GARMIN_PASSWORD"),
            "garmin_mfa": os.environ.get("GARMIN_MFA"),
            "device_filter": os.environ.get("DEVICE_FILTER"),
            "poll_seconds": os.environ.get("POLL_SECONDS"),
            "scan_count": os.environ.get("SCAN_COUNT"),
            "auto_sync": os.environ.get("AUTO_SYNC"),
        }
        cfg.update({k: v for k, v in env_map.items() if v not in (None, "")})
        try:
            with open(config_path()) as fh:
                stored = json.load(fh)
            for key in cls.PERSIST:
                if key in stored:
                    setattr(cfg, key, stored[key])
        except (FileNotFoundError, ValueError):
            pass
        return cfg
