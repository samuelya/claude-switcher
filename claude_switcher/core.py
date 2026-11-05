"""Account operations: add, remove, switch, usage, exec, and the auto-switch tick."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from . import api
from . import storage as st


class SwitchError(RuntimeError):
    pass


# ------------------------------------------------------------------ lookups

def active_uuid() -> str | None:
    return (st.config_get().get("oauthAccount") or {}).get("accountUuid")


def find(reg: dict, target: str) -> dict:
    """Match by list number (1-based), alias, email or uuid."""
    accounts = reg["accounts"]
    if target.isdigit() and 1 <= int(target) <= len(accounts):
        return accounts[int(target) - 1]
    for a in accounts:
        if target in (a.get("alias"), a["email"], a["uuid"]):
            return a
    raise SwitchError(f"no saved account matches '{target}' (see `csw list`)")


# --------------------------------------------------------------- add/remove

def add_current(alias: str | None = None) -> dict:
    live = st.live_creds_get() or {}
    oauth = live.get("claudeAiOauth")
    acct = st.config_get().get("oauthAccount")
    if not oauth or not acct:
        raise SwitchError("Claude Code is not logged in. Run `claude`, then /login.")
    uuid = acct["accountUuid"]
    st.saved_set(uuid, {"claudeAiOauth": oauth, "oauthAccount": acct})

    reg = st.registry_get()
    entry = next((a for a in reg["accounts"] if a["uuid"] == uuid), None)
    if entry is None:
        entry = {"uuid": uuid}
        reg["accounts"].append(entry)
    entry["email"] = acct.get("emailAddress", "?")
    entry["org"] = acct.get("organizationName", "")
    entry["plan"] = oauth.get("subscriptionType", "")
    if alias:
        entry["alias"] = alias
    st.registry_set(reg)
    return entry


def remove(target: str) -> dict:
    reg = st.registry_get()
    entry = find(reg, target)
    reg["accounts"].remove(entry)
    st.saved_delete(entry["uuid"])
    st.registry_set(reg)
    return entry


# ------------------------------------------------------------ saved tokens

def _fresh_saved(uuid: str, margin_ms: int = api.REFRESH_MARGIN_MS) -> dict | None:
    """Saved blob for an inactive account, refreshed if it expires within margin_ms.

    Reads inside the lock, so a token another csw process has just rotated is
    picked up instead of being refreshed a second time.
    """
    with st.lock():
        blob = st.saved_get(uuid)
        if blob and api.needs_refresh(blob["claudeAiOauth"], margin_ms):
            blob["claudeAiOauth"] = api.refresh(blob["claudeAiOauth"])
            st.saved_set(uuid, blob)
        return blob


# ------------------------------------------------------------------- switch

def _snapshot_active(reg: dict) -> None:
    """Save the live login back to its slot before we overwrite it.

    Claude Code refreshes tokens on its own and the refresh token rotates,
    so our saved copy of the active account goes stale while it is in use.
    """
    cur = active_uuid()
    if not cur or not any(a["uuid"] == cur for a in reg["accounts"]):
        return
    live = st.live_creds_get() or {}
    acct = st.config_get().get("oauthAccount")
    if live.get("claudeAiOauth") and acct:
        st.saved_set(cur, {"claudeAiOauth": live["claudeAiOauth"], "oauthAccount": acct})


def switch_to(uuid: str) -> None:
    reg = st.registry_get()
    if uuid == active_uuid():
        return
    blob = _fresh_saved(uuid)
    if not blob:
        raise SwitchError("saved credentials are missing; log in to that account and `csw add` again")
    oauth = blob["claudeAiOauth"]

    _snapshot_active(reg)

    live = st.live_creds_get() or {}
    live["claudeAiOauth"] = oauth          # mcpOAuth and other keys stay as they are
    st.live_creds_set(live)
    st.config_set_oauth_account(blob["oauthAccount"])

    reg["last_switch"] = st.now()
    st.registry_set(reg)


# -------------------------------------------------------------------- usage

def usage_for(uuid: str) -> dict:
    """Usage for one account. Refreshes saved (inactive) tokens when needed.

    The active account's token is read live and never refreshed here: Claude
    Code owns it, and two refreshes with the same token would kill one of them.
    """
    if uuid == active_uuid():
        oauth = (st.live_creds_get() or {}).get("claudeAiOauth") or {}
        if api.needs_refresh(oauth):
            raise api.ApiError(None, "live token expired; Claude Code refreshes it on its next request")
        return api.usage(oauth["accessToken"])

    blob = _fresh_saved(uuid)
    if not blob:
        raise api.ApiError(None, "saved credentials missing")
    return api.usage(blob["claudeAiOauth"]["accessToken"])


# --------------------------------------------------------------------- exec

EXEC_MIN_VALID_MS = 60 * 60 * 1000  # a child cannot refresh, so start it with an hour or more
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
# Variables that would make the child authenticate as someone else.
CONFLICTING_AUTH = (TOKEN_VAR, "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def exec_env(uuid: str, base: dict[str, str]) -> dict[str, str]:
    """Environment for a child process that should run as the given account.

    The child gets an access token only. It cannot rotate the refresh token,
    so the copy csw saved stays valid. For the active account the token
    variables are removed and the child uses Claude Code's own login.
    """
    env = {k: v for k, v in base.items() if k not in CONFLICTING_AUTH}
    if uuid == active_uuid():
        return env
    blob = _fresh_saved(uuid, EXEC_MIN_VALID_MS)
    if not blob:
        raise SwitchError("saved credentials are missing; log in to that account and `csw add` again")
    env[TOKEN_VAR] = blob["claudeAiOauth"]["accessToken"]
    return env


def score(u: dict) -> float:
    """The tighter of the two windows, as a percentage."""
    return max(u["five_hour"][0], u["seven_day"][0])


def pick_best(usages: dict[str, dict | None], threshold: float, exclude: str | None) -> str | None:
    """Account with the most headroom below the threshold, or None."""
    ok = [(score(u), uid) for uid, u in usages.items()
          if uid != exclude and u is not None and score(u) < threshold]
    return min(ok)[1] if ok else None


# --------------------------------------------------------------- auto tick

@dataclass
class Tick:
    status: str      # "ok" | "switched" | "cooldown" | "exhausted" | "error" | "unmanaged"
    message: str
    target: str | None = None


def auto_tick(threshold: float, cooldown_s: float, dry_run: bool = False) -> Tick:
    reg = st.registry_get()
    cur = active_uuid()
    by_uuid = {a["uuid"]: a for a in reg["accounts"]}
    if cur not in by_uuid:
        return Tick("unmanaged", "active account is not saved; run `csw add` first")

    try:
        u = usage_for(cur)
    except api.ApiError as e:
        return Tick("error", f"{by_uuid[cur]['email']}: {e}")

    line = f"{by_uuid[cur]['email']}  5h {u['five_hour'][0]:.0f}%  7d {u['seven_day'][0]:.0f}%"
    if score(u) < threshold:
        return Tick("ok", line)

    if st.now() - reg["last_switch"] < cooldown_s:
        return Tick("cooldown", f"{line}  (over {threshold:.0f}%, in cooldown)")

    usages: dict[str, dict | None] = {}
    for uid in by_uuid:
        if uid == cur or by_uuid[uid].get("disabled"):
            continue
        try:
            usages[uid] = usage_for(uid)
        except api.ApiError:
            usages[uid] = None
    best = pick_best(usages, threshold, exclude=cur)
    if best is None:
        return Tick("exhausted", f"{line}  (over {threshold:.0f}%, no other account has headroom)")

    target = by_uuid[best]["email"]
    if not dry_run:
        switch_to(best)
        notify(f"Switched to {target}", f"{by_uuid[cur]['email']} reached {score(u):.0f}%")
    return Tick("switched", f"{line}  -> {'would switch' if dry_run else 'switched'} to {target}", best)


def notify(title: str, body: str) -> None:
    if not st.IS_MAC:
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'display notification "{esc(body)}" with title "{esc(title)}"'],
        capture_output=True,
    )
