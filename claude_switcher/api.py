"""The two Anthropic endpoints we call: token refresh and usage."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client
BETA = "oauth-2025-04-20"
UA = "claude-switcher/0.1"
REFRESH_MARGIN_MS = 5 * 60 * 1000


class ApiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def _call(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise ApiError(e.code, f"HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(None, f"network error: {e}") from None


def needs_refresh(oauth: dict, margin_ms: int = REFRESH_MARGIN_MS) -> bool:
    return oauth.get("expiresAt", 0) - time.time() * 1000 < margin_ms


def refresh(oauth: dict) -> dict:
    """Return a copy of `oauth` with a fresh access token.

    The refresh token rotates, so the caller must save the result, or the
    stored login stops working.
    """
    if not oauth.get("refreshToken"):
        raise ApiError(None, "no refresh token stored; log in again and re-add")
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    data = _call(req)
    out = dict(oauth)
    out["accessToken"] = data["access_token"]
    out["expiresAt"] = int(time.time() * 1000) + data["expires_in"] * 1000
    if data.get("refresh_token"):
        out["refreshToken"] = data["refresh_token"]
    if data.get("scope"):
        out["scopes"] = data["scope"].split()
    return out


def usage(access_token: str) -> dict:
    """Return {"five_hour": (pct, resets_at), "seven_day": (pct, resets_at)}."""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": BETA,
        "User-Agent": UA,
    })
    raw = _call(req)
    out = {}
    for key in ("five_hour", "seven_day"):
        w = raw.get(key) or {}
        out[key] = (float(w.get("utilization") or 0), w.get("resets_at"))
    return out
