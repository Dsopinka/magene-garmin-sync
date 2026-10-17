"""magene-garmin-sync CLI / entrypoint.

Commands:
  serve         Start the web control panel (default; what the container runs).
  probe         Try logging in to Onelap and list recent rides.
  garmin-login  Do the one-time Garmin sign-in and cache tokens (handles MFA).
  sync          Pull new Onelap rides and upload them to Garmin Connect once.
  run           Loop `sync` every poll_seconds (headless, no web UI).

Credentials come from the stored config.json (written by the web UI) and/or
environment variables.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import engine
from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cli")


def cmd_serve(cfg: Config) -> int:
    from .webapp import serve

    serve(os.environ.get("WEB_HOST", "0.0.0.0"), int(os.environ.get("WEB_PORT", "8787")))
    return 0


def cmd_probe(cfg: Config) -> int:
    result = engine.probe(cfg, log.info)
    if result.get("ok"):
        cfg.save()
        log.info("saved: ONELAP_HOST=%s hash_password=%s", cfg.onelap_host, cfg.onelap_hash_password)
        return 0
    return 1


def cmd_garmin_login(cfg: Config) -> int:
    from .garmin_sync import GarminUploader

    if not cfg.garmin_ready:
        raise SystemExit("set GARMIN_EMAIL and GARMIN_PASSWORD first")
    os.makedirs(cfg.data_dir, exist_ok=True)
    GarminUploader(cfg.garmin_email, cfg.garmin_password, cfg.tokenstore, cfg.garmin_mfa or None).connect()
    log.info("Garmin tokens cached in %s", cfg.tokenstore)
    return 0


def cmd_sync(cfg: Config) -> int:
    result = engine.sync_once(cfg, log.info)
    log.info("summary: %s", result)
    return 0 if result.get("ok") and not result.get("failed") else 1


def cmd_run(cfg: Config) -> int:
    log.info("headless sync loop; interval %ds", cfg.poll_seconds)
    while True:
        try:
            engine.sync_once(cfg, log.info)
        except Exception as exc:  # noqa: BLE001
            log.error("sync cycle error: %s", exc)
        time.sleep(max(300, cfg.poll_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="magene-garmin-sync")
    parser.add_argument(
        "command",
        choices=["serve", "probe", "garmin-login", "sync", "run"],
        nargs="?",
        default="serve",
    )
    args = parser.parse_args(argv)
    cfg = Config.load()
    return {
        "serve": cmd_serve,
        "probe": cmd_probe,
        "garmin-login": cmd_garmin_login,
        "sync": cmd_sync,
        "run": cmd_run,
    }[args.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
