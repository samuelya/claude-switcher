"""csw: switch Claude Code between saved accounts, by hand or on a usage limit."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from . import api, core
from . import storage as st


def _until(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        secs = (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return "?"
    if secs <= 0:
        return "now"
    h, m = divmod(int(secs) // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if d else f"{h}h{m:02d}m"


def cmd_add(args) -> int:
    e = core.add_current(args.alias)
    print(f"Saved {e['email']} ({e.get('plan') or 'unknown plan'}).")
    print("To add another account: in Claude Code run /login with it (do not /logout first), then `csw add` again.")
    return 0


def cmd_list(args) -> int:
    reg = st.registry_get()
    if not reg["accounts"]:
        print("No accounts saved yet. Log in with `claude`, then run `csw add`.")
        return 0
    cur = core.active_uuid()
    names = [a["email"] + (f" ({a['alias']})" if a.get("alias") else "") for a in reg["accounts"]]
    w = max(len("account"), *map(len, names))  # widest name sets the column
    print(f"{'':2}{'#':>2}  {'account':<{w}}  {'plan':<6} {'5h':>5} {'resets':>7} {'7d':>5} {'resets':>7}")
    for i, (a, name) in enumerate(zip(reg["accounts"], names), 1):
        mark = "* " if a["uuid"] == cur else "  "
        row = f"{mark}{i:>2}  {name:<{w}}  {a.get('plan', ''):<6}"
        if args.no_usage:
            print(row)
            continue
        try:
            u = core.usage_for(a["uuid"])
            (p5, r5), (p7, r7) = u["five_hour"], u["seven_day"]
            print(f"{row} {p5:>4.0f}% {_until(r5):>7} {p7:>4.0f}% {_until(r7):>7}")
        except api.ApiError as e:
            print(f"{row} usage unavailable: {e}")
    if cur and not any(a["uuid"] == cur for a in reg["accounts"]):
        print("\nThe account Claude Code is using now is not saved. Run `csw add` to save it.")
    return 0


def cmd_current(args) -> int:
    acct = st.config_get().get("oauthAccount") or {}
    print(acct.get("emailAddress", "not logged in"))
    return 0


def cmd_switch(args) -> int:
    reg = st.registry_get()
    cur = core.active_uuid()
    if args.target in ("next", "best"):
        others = [a for a in reg["accounts"] if a["uuid"] != cur and not a.get("disabled")]
        if not others:
            print("No other account to switch to.")
            return 2
        if args.target == "next":
            uuids = [a["uuid"] for a in reg["accounts"]]
            start = uuids.index(cur) + 1 if cur in uuids else 0
            ring = reg["accounts"][start:] + reg["accounts"][:start]
            entry = next(a for a in ring if a in others)
        else:
            usages = {}
            for a in others:
                try:
                    usages[a["uuid"]] = core.usage_for(a["uuid"])
                except api.ApiError:
                    usages[a["uuid"]] = None
            best = core.pick_best(usages, threshold=100.1, exclude=cur)
            if not best:
                print("Could not read usage for any other account.")
                return 1
            entry = next(a for a in others if a["uuid"] == best)
    else:
        entry = core.find(reg, args.target)

    if entry["uuid"] == cur:
        print(f"Already on {entry['email']}.")
        return 0
    core.switch_to(entry["uuid"])
    print(f"Switched to {entry['email']}.")
    if st.IS_MAC:
        print("Running Claude Code sessions pick it up within about 30 seconds; restart one to apply it now.")
    return 0


def cmd_remove(args) -> int:
    e = core.remove(args.target)
    print(f"Removed {e['email']} from the switcher. Claude Code's own login is unchanged.")
    return 0


def cmd_alias(args) -> int:
    reg = st.registry_get()
    e = core.find(reg, args.target)
    e["alias"] = args.name
    st.registry_set(reg)
    print(f"{e['email']} is now '{args.name}'.")
    return 0


def cmd_toggle(args) -> int:
    reg = st.registry_get()
    e = core.find(reg, args.target)
    e["disabled"] = args.cmd == "disable"
    st.registry_set(reg)
    print(f"{e['email']} {'excluded from' if e['disabled'] else 'included in'} auto-switch.")
    return 0


def cmd_exec(args) -> int:
    entry = core.find(st.registry_get(), args.target)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    command = command or ["claude"]
    env = core.exec_env(entry["uuid"], dict(os.environ))
    print(f"csw: running {command[0]} as {entry['email']}", file=sys.stderr, flush=True)
    try:
        os.execvpe(command[0], command, env)  # replaces csw; exit code and signals pass through
    except OSError as e:
        print(f"error: cannot run {command[0]}: {e.strerror}", file=sys.stderr)
        return 127


def cmd_auto(args) -> int:
    print(f"Auto-switch: threshold {args.threshold:.0f}%, check every {args.interval}s, "
          f"cooldown {args.cooldown}s{', dry run' if args.dry_run else ''}. Ctrl-C to stop.", flush=True)
    codes = {"switched": 0, "error": 1, "unmanaged": 1}
    while True:
        try:
            t = core.auto_tick(args.threshold, args.cooldown, args.dry_run)
        except (core.SwitchError, api.ApiError, st.StorageError) as e:
            t = core.Tick("error", str(e))
        print(f"[{datetime.now():%H:%M:%S}] {t.message}", flush=True)
        if args.once:
            return codes.get(t.status, 2)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="csw", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="save the account Claude Code is logged in as")
    s.add_argument("--alias")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("list", aliases=["ls"], help="saved accounts with 5-hour and 7-day usage")
    s.add_argument("--no-usage", action="store_true", help="skip the usage lookup")
    s.set_defaults(fn=cmd_list)

    sub.add_parser("current", help="print the active account").set_defaults(fn=cmd_current)

    s = sub.add_parser("switch", aliases=["sw"], help="switch to an account")
    s.add_argument("target", help="list number, alias, email, 'next' or 'best'")
    s.set_defaults(fn=cmd_switch)

    s = sub.add_parser("remove", aliases=["rm"], help="forget a saved account")
    s.add_argument("target")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("alias", help="name an account")
    s.add_argument("target")
    s.add_argument("name")
    s.set_defaults(fn=cmd_alias)

    for name in ("disable", "enable"):
        s = sub.add_parser(name, help=f"{name} an account for auto-switch")
        s.add_argument("target")
        s.set_defaults(fn=cmd_toggle)

    s = sub.add_parser("exec", help="run a command (default: claude) as an account, leaving the login alone",
                       description="Run a command as a saved account without switching Claude Code's login. "
                                   "Example: csw exec personal -- claude -p --model claude-fable-5-1 'hello'")
    s.add_argument("target", help="list number, alias or email")
    s.add_argument("command", nargs=argparse.REMAINDER, help="command to run (default: claude)")
    s.set_defaults(fn=cmd_exec)

    s = sub.add_parser("auto", help="watch usage and switch when the limit is near")
    s.add_argument("--threshold", type=float, default=90, help="switch at this usage %% (default 90)")
    s.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    s.add_argument("--cooldown", type=int, default=300, help="minimum seconds between switches (default 300)")
    s.add_argument("--once", action="store_true", help="check once and exit (0 switched, 1 error, 2 no change)")
    s.add_argument("--dry-run", action="store_true", help="report the switch without making it")
    s.set_defaults(fn=cmd_auto)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (core.SwitchError, api.ApiError, st.StorageError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
