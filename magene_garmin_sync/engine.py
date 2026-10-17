"""Shared probe/sync logic, used by both the CLI and the web control panel.

Every function takes a ``log`` callback so callers can stream progress (the web
UI pipes it to the live log; the CLI pipes it to the logger).
"""
from __future__ import annotations

import os
from typing import Callable

from .config import Config
from .onelap import DEFAULT_HOST, AuthError, OnelapClient

Log = Callable[[str], None]


def _onelap(cfg: Config, host: str | None = None, hash_pw: bool | None = None) -> OnelapClient:
    return OnelapClient(
        cfg.onelap_account,
        cfg.onelap_password,
        host or cfg.onelap_host or None,
        hash_password=cfg.onelap_hash_password if hash_pw is None else hash_pw,
        device_id=cfg.device_id,
        timezone_name=cfg.timezone_name,
        language=cfg.language,
    )


def probe(cfg: Config, log: Log) -> dict:
    """Find a host/auth combo that logs in and list a few recent rides.

    Returns {ok, host, hash_password, rides:[...]} and, on success, writes the
    discovered host/hash_password back into cfg (caller may persist).
    """
    if not cfg.onelap_ready:
        log("Onelap account/password not set yet.")
        return {"ok": False, "error": "onelap credentials missing"}

    hosts = [cfg.onelap_host] if cfg.onelap_host else [DEFAULT_HOST]
    first_hash = cfg.onelap_hash_password
    for host in hosts:
        for hash_pw in (first_hash, not first_hash):
            log(f"trying host={host} hash_password={hash_pw} ...")
            client = _onelap(cfg, host, hash_pw)
            try:
                client.login()
            except AuthError as exc:
                log(f"  rejected: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"  error: {exc}")
                continue
            log(f"  signed in. host={host} hash_password={hash_pw}")
            cfg.onelap_host = host
            cfg.onelap_hash_password = hash_pw
            try:
                rides = client.list_rides(limit=5)
            except Exception as exc:  # noqa: BLE001
                log(f"  login worked but listing rides failed: {exc}")
                return {"ok": True, "host": host, "hash_password": hash_pw,
                        "rides": [], "warning": str(exc)}
            log(f"  found {len(rides)} recent ride(s).")
            return {
                "ok": True,
                "host": host,
                "hash_password": hash_pw,
                "rides": [
                    {
                        "id": r.ride_id,
                        "when": r.date_stamp,
                        "name": r.name,
                        "has_fit": bool(r.fit_reference()),
                    }
                    for r in rides
                ],
            }
    log("Could not sign in to any candidate host.")
    return {"ok": False, "error": "login failed on all candidate hosts"}


def sync_once(cfg: Config, log: Log) -> dict:
    """Pull new Onelap rides and upload them to Garmin Connect. Returns a summary."""
    from .garmin_sync import GarminUploader
    from .state import SyncState

    if not cfg.onelap_ready:
        return {"ok": False, "error": "onelap credentials missing"}
    if not cfg.garmin_ready:
        return {"ok": False, "error": "garmin credentials missing"}

    os.makedirs(cfg.fit_dir, exist_ok=True)
    state = SyncState(cfg.state_file)

    onelap = _onelap(cfg)
    log("signing in to Onelap ...")
    onelap.login()

    rides = []
    for page in range(1, cfg.lookback_pages + 1):
        rides.extend(onelap.list_rides(page=page))
    new_rides = [r for r in rides if not state.is_synced(r.ride_id)]
    log(f"{len(rides)} ride(s) listed, {len(new_rides)} new.")
    summary = {"ok": True, "listed": len(rides), "new": len(new_rides),
               "uploaded": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    if not new_rides:
        return summary

    log("connecting to Garmin Connect ...")
    garmin = GarminUploader(cfg.garmin_email, cfg.garmin_password, cfg.tokenstore,
                            cfg.garmin_mfa or None)
    garmin.connect()

    for ride in sorted(new_rides, key=lambda r: r.date_stamp):
        fit_path = os.path.join(cfg.fit_dir, f"onelap_{ride.date_stamp}_{ride.ride_id}.fit")
        try:
            if not onelap.download_fit(ride, fit_path):
                log(f"ride {ride.ride_id}: no usable FIT, skipped")
                summary["skipped"] += 1
                continue
            result = garmin.upload(fit_path)
            state.mark(ride.ride_id, result)
            summary[result] = summary.get(result, 0) + 1
            log(f"ride {ride.ride_id} ({ride.date_stamp}) -> {result}")
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            log(f"ride {ride.ride_id} FAILED: {exc}")
    return summary
