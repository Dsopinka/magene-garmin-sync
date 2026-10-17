"""Configuration.

Settings can come from two places, merged with the stored file winning:
  1. Environment variables (good for pure-headless / docker-compose use).
  2. A JSON file at $DATA_DIR/config.json, which the web control panel writes.

This lets you run the container with no secrets in the compose file and type
your credentials into the web UI instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


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
    onelap_account: str = ""
    onelap_password: str = ""
    onelap_host: str = ""
    onelap_hash_password: bool = False
    onelap_auth_scheme: str = ""

    garmin_email: str = ""
    garmin_password: str = ""
    garmin_mfa: str = ""

    poll_seconds: int = 3600
    lookback_pages: int = 1
    auto_sync: bool = False

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
    def timezone_name(self) -> str:
        return os.environ.get("TZ") or "UTC"

    @property
    def language(self) -> str:
        return os.environ.get("ONELAP_LANGUAGE", "en")

    @property
    def device_id(self) -> str:
        """A stable per-install device id (persisted), so the backend sees one device."""
        path = os.path.join(self.data_dir, "device_id")
        try:
            with open(path) as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        import uuid

        value = str(uuid.uuid4())
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(value)
        except OSError:
            pass
        return value

    @property
    def onelap_ready(self) -> bool:
        return bool(self.onelap_account and self.onelap_password)

    @property
    def garmin_ready(self) -> bool:
        return bool(self.garmin_email and self.garmin_password)

    # -- persistence --------------------------------------------------------

    # Keys the web UI is allowed to write back to config.json.
    EDITABLE = (
        "onelap_account",
        "onelap_password",
        "onelap_host",
        "onelap_hash_password",
        "onelap_auth_scheme",
        "garmin_email",
        "garmin_password",
        "garmin_mfa",
        "poll_seconds",
        "lookback_pages",
        "auto_sync",
    )

    def redacted(self) -> dict:
        """Config for the UI: passwords replaced by a 'set / not set' flag."""
        out = asdict(self)
        for secret in ("onelap_password", "garmin_password", "garmin_mfa"):
            out[f"{secret}_set"] = bool(out.pop(secret))
        return out

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {k: getattr(self, k) for k in self.EDITABLE}
        tmp = config_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, config_path())
        try:
            os.chmod(config_path(), 0o600)
        except OSError:
            pass

    def update(self, changes: dict) -> None:
        for key in self.EDITABLE:
            if key not in changes:
                continue
            value = changes[key]
            # Blank password fields mean "leave unchanged".
            if key in ("onelap_password", "garmin_password", "garmin_mfa") and value == "":
                continue
            if key in ("onelap_hash_password", "auto_sync"):
                value = _bool(value)
            elif key in ("poll_seconds", "lookback_pages"):
                try:
                    value = max(1, int(value))
                except (TypeError, ValueError):
                    continue
            setattr(self, key, value)

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        # 1. environment
        env_map = {
            "onelap_account": os.environ.get("ONELAP_ACCOUNT"),
            "onelap_password": os.environ.get("ONELAP_PASSWORD"),
            "onelap_host": os.environ.get("ONELAP_HOST"),
            "onelap_hash_password": os.environ.get("ONELAP_HASH_PASSWORD"),
            "onelap_auth_scheme": os.environ.get("ONELAP_AUTH_SCHEME"),
            "garmin_email": os.environ.get("GARMIN_EMAIL"),
            "garmin_password": os.environ.get("GARMIN_PASSWORD"),
            "garmin_mfa": os.environ.get("GARMIN_MFA"),
            "poll_seconds": os.environ.get("POLL_SECONDS"),
            "lookback_pages": os.environ.get("LOOKBACK_PAGES"),
            "auto_sync": os.environ.get("AUTO_SYNC"),
        }
        cfg.update({k: v for k, v in env_map.items() if v not in (None, "")})
        # 2. stored file (wins)
        try:
            with open(config_path()) as fh:
                cfg.update(json.load(fh))
        except (FileNotFoundError, ValueError):
            pass
        return cfg
