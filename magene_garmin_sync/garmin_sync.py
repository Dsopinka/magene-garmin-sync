"""Garmin Connect upload side, wrapping the maintained python-garminconnect.

Auth uses Garmin's mobile SSO flow via the library. Tokens are cached in a
token store directory so day-to-day runs do not re-enter the password. A first
login may require an MFA code; set GARMIN_MFA in the environment for headless
runs with 2FA enabled, or run `cli garmin-login` once interactively.
"""
from __future__ import annotations

import logging
import os

from garminconnect import Garmin
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

log = logging.getLogger("garmin")


class GarminUploader:
    def __init__(self, email: str, password: str, tokenstore: str, mfa_code: str | None = None):
        self.email = email
        self._password = password
        self.tokenstore = tokenstore
        self._mfa_code = mfa_code
        self._client: Garmin | None = None

    def _mfa_prompt(self) -> str:
        if self._mfa_code:
            return self._mfa_code
        env = os.environ.get("GARMIN_MFA")
        if env:
            return env
        # Only reached in an interactive session.
        return input("Garmin MFA code: ").strip()

    def connect(self) -> None:
        client = Garmin(
            email=self.email,
            password=self._password,
            prompt_mfa=self._mfa_prompt,
        )
        # Resume from cached tokens when possible; fall back to full login.
        try:
            client.login(self.tokenstore)
        except (FileNotFoundError, GarminConnectAuthenticationError):
            client.login(self.tokenstore)
        self._client = client
        log.info("connected to Garmin Connect as %s", self.email)

    def upload(self, fit_path: str) -> str:
        """Upload one activity file. Returns 'uploaded', 'duplicate', or raises."""
        if self._client is None:
            self.connect()
        try:
            self._client.upload_activity(fit_path)
            return "uploaded"
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as exc:  # noqa: BLE001 - library raises plain Exception on 409
            message = str(exc).lower()
            if "409" in message or "conflict" in message or "duplicate" in message:
                return "duplicate"
            raise
