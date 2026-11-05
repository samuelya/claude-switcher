"""Where credentials live: Claude Code's own store, and our saved copies.

Claude Code keeps the logged-in account in two places:
  * the OAuth tokens: macOS Keychain item "Claude Code-credentials", or
    ~/.claude/.credentials.json on Linux. The JSON holds "claudeAiOauth"
    (the account login) plus other keys such as "mcpOAuth", which we keep.
  * the account identity: the "oauthAccount" key in ~/.claude.json.

A switch rewrites both. Saved copies go in the Keychain under our own
service name on macOS, or 0600 files under ~/.claude-switcher/creds elsewhere.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path

IS_MAC = platform.system() == "Darwin"
HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_CREDS_FILE = HOME / ".claude" / ".credentials.json"
APP_DIR = Path(os.environ.get("CSW_HOME", HOME / ".claude-switcher"))
REGISTRY = APP_DIR / "accounts.json"
LOCK = APP_DIR / ".refresh.lock"

CC_SERVICE = "Claude Code-credentials"
OUR_SERVICE = "claude-switcher"
_STDIN_LIMIT = 4096 - 64  # `security -i` truncates longer lines


class StorageError(RuntimeError):
    pass


# --------------------------------------------------------------------- files

def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


# ------------------------------------------------------------------ keychain

def _user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def kc_get(service: str, account: str) -> str | None:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 44:  # item not found
        return None
    if r.returncode != 0:
        raise StorageError(f"keychain read failed: {r.stderr.strip()}")
    return r.stdout.rstrip("\n")


def kc_set(service: str, account: str, value: str) -> None:
    hex_value = value.encode().hex()
    line = f"add-generic-password -U -a {_quote(account)} -s {_quote(service)} -X {hex_value}\n"
    if len(line) <= _STDIN_LIMIT:
        # stdin keeps the secret out of the process list
        r = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=15)
    else:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account, "-s", service, "-X", hex_value],
            capture_output=True, text=True, timeout=15,
        )
    # `security -i` can exit 0 after a failed command, so read it back.
    if r.returncode != 0 or kc_get(service, account) != value:
        raise StorageError(f"keychain write failed: {r.stderr.strip() or 'value did not persist'}")


def kc_delete(service: str, account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", service],
        capture_output=True, text=True, timeout=15,
    )


# ------------------------------------------- Claude Code's live credentials

def live_creds_get() -> dict | None:
    raw = kc_get(CC_SERVICE, _user()) if IS_MAC else _read_text(CLAUDE_CREDS_FILE)
    return json.loads(raw) if raw else None


def live_creds_set(data: dict) -> None:
    text = json.dumps(data)
    if IS_MAC:
        kc_set(CC_SERVICE, _user(), text)
    else:
        atomic_write(CLAUDE_CREDS_FILE, text)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def config_get() -> dict:
    return _read_json(CLAUDE_JSON, {})


def config_set_oauth_account(oauth_account: dict) -> None:
    """Replace only "oauthAccount" in ~/.claude.json; every other key stays."""
    cfg = config_get()
    cfg["oauthAccount"] = oauth_account
    mode = CLAUDE_JSON.stat().st_mode & 0o777 if CLAUDE_JSON.exists() else 0o600
    atomic_write(CLAUDE_JSON, json.dumps(cfg, indent=2), mode)


# ------------------------------------------------------- our saved accounts

def saved_get(uuid: str) -> dict | None:
    if IS_MAC:
        raw = kc_get(OUR_SERVICE, uuid)
    else:
        raw = _read_text(APP_DIR / "creds" / f"{uuid}.json")
    return json.loads(raw) if raw else None


def saved_set(uuid: str, blob: dict) -> None:
    text = json.dumps(blob)
    if IS_MAC:
        kc_set(OUR_SERVICE, uuid, text)
    else:
        atomic_write(APP_DIR / "creds" / f"{uuid}.json", text)


def saved_delete(uuid: str) -> None:
    if IS_MAC:
        kc_delete(OUR_SERVICE, uuid)
    else:
        (APP_DIR / "creds" / f"{uuid}.json").unlink(missing_ok=True)


# ------------------------------------------------------------------ registry

def registry_get() -> dict:
    reg = _read_json(REGISTRY, {})
    reg.setdefault("accounts", [])
    reg.setdefault("last_switch", 0)
    return reg


def registry_set(reg: dict) -> None:
    atomic_write(REGISTRY, json.dumps(reg, indent=2))


@contextlib.contextmanager
def lock():
    """Serialise token refreshes across csw processes.

    A refresh rotates the refresh token, so two processes refreshing the same
    saved account at once would leave one of them holding a dead token.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def now() -> float:
    return time.time()
