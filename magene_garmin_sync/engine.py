"""Shared probe/sync logic for the Strava -> Garmin bridge.

Used by both the CLI and the web control panel. Every function takes a ``log``
callback so callers can stream progress.
"""
from __future__ import annotations

import os
from typing import Callable

from .config import Config
from .strava import StravaAuthError, StravaClient
from . import tcx

Log = Callable[[str], None]


def _strava(cfg: Config) -> StravaClient:
    return StravaClient(
        cfg.strava_client_id,
        cfg.strava_client_secret,
        cfg.strava_refresh_token,
        cfg.strava_access_token,
        cfg.strava_expires_at,
        on_token=cfg.set_strava_tokens,
    )


def _wanted(cfg: Config, act) -> bool:
    if cfg.rides_only and not act.is_ride:
        return False
    if act.is_virtual and not cfg.include_virtual:
        return False
    if cfg.device_filter and cfg.device_filter.lower() not in (act.device_name or "").lower():
        return False
    return True


def probe(cfg: Config, log: Log) -> dict:
    """Confirm the Strava connection and show a few recent rides with device names."""
    if not cfg.strava_ready:
        return {"ok": False, "error": "Strava client ID/secret not set"}
    if not cfg.strava_connected:
        return {"ok": False, "error": "not connected to Strava yet (use Connect Strava)"}

    client = _strava(cfg)
    try:
        who = client.athlete_name()
    except StravaAuthError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc}"}

    log(f"connected to Strava as {who}.")
    activities = client.list_activities(per_page=8)
    rides = []
    for act in activities[:8]:
        try:
            detail = client.activity_detail(act.id)
        except Exception:  # noqa: BLE001
            detail = act
        rides.append(
            {
                "id": detail.id,
                "when": detail.date_stamp,
                "name": detail.name,
                "sport": detail.sport_type,
                "device": detail.device_name or "?",
                "would_sync": _wanted(cfg, detail),
            }
        )
        log(f"  {detail.date_stamp}  {detail.sport_type:12s}  device={detail.device_name or '?'}  "
            f"{'[sync]' if _wanted(cfg, detail) else '[skip]'}  {detail.name}")
    return {"ok": True, "athlete": who, "rides": rides}


def sync_once(cfg: Config, log: Log) -> dict:
    from .garmin_sync import GarminUploader
    from .state import SyncState

    if not cfg.strava_connected:
        return {"ok": False, "error": "not connected to Strava"}
    if not cfg.garmin_ready:
        return {"ok": False, "error": "garmin credentials missing"}

    os.makedirs(cfg.fit_dir, exist_ok=True)
    state = SyncState(cfg.state_file)
    strava = _strava(cfg)

    log("listing recent Strava activities ...")
    activities = strava.list_activities(per_page=cfg.scan_count)
    summary = {"ok": True, "listed": len(activities), "new": 0,
               "uploaded": 0, "duplicate": 0, "skipped": 0, "failed": 0}

    garmin = None
    for act in activities:
        if state.is_synced(act.id):
            continue
        # fetch detail (device name) to decide
        try:
            detail = strava.activity_detail(act.id)
        except Exception as exc:  # noqa: BLE001
            log(f"activity {act.id}: detail fetch failed: {exc}")
            summary["failed"] += 1
            continue
        if not _wanted(cfg, detail):
            continue
        summary["new"] += 1
        try:
            streams = strava.streams(detail.id)
            if not streams.get("time"):
                log(f"activity {detail.id}: no streams (manual entry?), skipping")
                summary["skipped"] += 1
                state.mark(detail.id, "skipped")
                continue
            data = tcx.build_tcx(detail, streams)
            path = os.path.join(cfg.fit_dir, f"strava_{detail.date_stamp}_{detail.id}.tcx")
            with open(path, "wb") as fh:
                fh.write(data)

            if garmin is None:
                log("connecting to Garmin Connect ...")
                garmin = GarminUploader(cfg.garmin_email, cfg.garmin_password,
                                        cfg.tokenstore, cfg.garmin_mfa or None)
                garmin.connect()
            result = garmin.upload(path)
            state.mark(detail.id, result)
            summary[result] = summary.get(result, 0) + 1
            log(f"{detail.date_stamp}  {detail.name}  -> {result}")
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            log(f"activity {detail.id} FAILED: {exc}")
    log(f"done: {summary}")
    return summary
