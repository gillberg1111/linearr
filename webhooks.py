"""Outbound webhook delivery for Linearr events."""
from __future__ import annotations

import datetime
import json
import logging
import threading
import urllib.error
import urllib.request

import db

log = logging.getLogger(__name__)


def _build_payload(
    event: str,
    playlist: dict | None = None,
    data: dict | None = None,
) -> dict:
    p: dict = {
        "event": event,
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if playlist:
        p["playlist"] = playlist
    if data:
        p["data"] = data
    return p


def _playlist_info(row: dict) -> dict:
    return {
        "id":        row["id"],
        "name":      row["name"],
        "backend":   row.get("backend", "plex"),
        "sort_mode": row.get("sort_mode", "rotation"),
        "type":      row.get("playlist_type", "manual"),
    }


def _post(url: str, payload: dict) -> bool:
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Linearr-Webhook/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        log.warning("Webhook %s returned HTTP %s", url, exc.code)
    except urllib.error.URLError as exc:
        log.warning("Webhook %s unreachable: %s", url, exc.reason)
    except Exception as exc:
        log.warning("Webhook %s failed: %s", url, exc)
    return False


def fire(event: str, playlist: dict | None = None, data: dict | None = None) -> None:
    try:
        hooks = db.list_webhooks()
    except Exception:
        return
    if not hooks:
        return

    payload = _build_payload(event, playlist=playlist, data=data)

    def _deliver() -> None:
        for hook in hooks:
            ok = _post(hook["url"], payload)
            log.log(
                logging.DEBUG if ok else logging.WARNING,
                "Webhook [%s] %s → %s",
                hook.get("label") or hook["url"],
                event,
                "OK" if ok else "FAILED",
            )

    threading.Thread(target=_deliver, daemon=True, name="webhook-fire").start()


def fire_test(url: str) -> tuple[bool, str]:
    payload = _build_payload(
        "test",
        data={"message": "This is a test webhook from Linearr."},
    )
    ok = _post(url, payload)
    return ok, ("Delivered successfully." if ok else "Delivery failed — check the URL and try again.")
