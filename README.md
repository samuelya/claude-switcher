# claude-switcher

[![test](https://github.com/samuelya/claude-switcher/actions/workflows/test.yml/badge.svg)](https://github.com/samuelya/claude-switcher/actions/workflows/test.yml)

`csw` switches Claude Code between several Claude accounts. You can switch by hand, or leave `csw auto` running so that it moves to another account when the current one nears its usage limit. The tool uses only the Python standard library.

> **Before you use this tool:** using several accounts to work around usage limits may conflict with Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) or [Usage Policy](https://www.anthropic.com/legal/aup), and could put your accounts at risk. You are responsible for checking the current terms and complying with them. This project is not affiliated with or endorsed by Anthropic.

## Supported platforms

| Platform | Status |
|---|---|
| macOS | Supported and tested |
| Linux | Supported; unit tests run in CI, not yet tested against a live Claude Code login |
| Windows | Not supported yet; contributions and test reports are welcome |

## Install

You need Python 3.10 or later. Install with [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```sh
uv tool install git+https://github.com/samuelya/claude-switcher
# or
pipx install git+https://github.com/samuelya/claude-switcher
```

To upgrade later, run `uv tool upgrade claude-switcher` or `pipx upgrade claude-switcher`.

To work on the code, clone the repository and run `python3 -m claude_switcher --help` from its folder.

## Save your accounts

Each account must be saved once while Claude Code is logged in as that account.

1. Run `claude` and log in to the first account if you have not already done so.
2. Run `csw add --alias personal`.
3. In Claude Code, run `/login` and sign in to the next account. Do not run `/logout` first, because Claude Code can revoke the saved refresh token of the account you leave.
4. Run `csw add --alias work`.
5. Repeat steps 3 and 4 for each further account.

## Switch manually

```sh
csw list              # saved accounts, 5-hour and 7-day usage, time until each window resets
csw switch work       # by alias; a list number or email address also works
csw switch next       # the next account in the list
csw switch best       # the account with the most usage left
csw current           # the account Claude Code is using now
```

On macOS, a running Claude Code session caches its credentials for about 30 seconds, so the new account applies after that delay. Restarting the session applies the new account immediately. On Linux, Claude Code re-reads its credentials file when the file changes.

## Switch automatically

```sh
csw auto                          # check every 60 seconds and switch at 90% usage
csw auto --threshold 85 --interval 120
csw auto --dry-run                # report what it would do without switching
csw auto --once                   # one check, for cron or launchd (exit 0 switched, 1 error, 2 no change)
csw disable personal              # leave an account out of automatic switching
```

Each check reads the active account's 5-hour and 7-day usage and takes the higher of the two. When that figure reaches the threshold, `csw` reads usage for the other enabled accounts and switches to the one with the lowest figure below the threshold. A five-minute cooldown (`--cooldown`) prevents repeated switching between accounts that are all close to the limit. If no account has headroom, `csw` stays on the current account and reports this. On macOS, each switch also raises a desktop notification.

## Run one command as another account

`csw exec` runs a command as a saved account without switching Claude Code's login. Sessions that are already running keep their account, and the command's account applies only to that command and the processes it starts.

```sh
csw exec personal                    # an interactive Claude Code session as "personal"
csw exec personal -- claude -p "Summarise CHANGELOG.md" --model claude-fable-5-1
```

With no command, `csw exec` starts `claude`. The `--` separator is optional. It makes it clear where the `csw` arguments end.

For an inactive account, `csw` refreshes the saved token when less than an hour of it is left. It then passes the access token to the command in `CLAUDE_CODE_OAUTH_TOKEN`. The command receives the access token only, never the refresh token, so it cannot invalidate the copy `csw` keeps. Claude Code removes this variable from the environment of the commands its own tools run. For the active account, `csw` runs the command with Claude Code's normal login.

`csw` also removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and any inherited `CLAUDE_CODE_OAUTH_TOKEN` from the command's environment, so the account you name is the one that is used.

A common use is to let a session on one account hand a task to a model that only another account can use. The child process cannot see the parent conversation, so the prompt must contain everything the task needs. A headless run cannot ask for permission, so pass the tools it may use with `--allowedTools`. Put the prompt before `--allowedTools`, because that option takes several values and would otherwise treat the prompt as one of them.

The command runs under the other account's terms and data settings. Do not use it for work that must stay under the first account, such as client work on a business account.

## How it works

Claude Code stores the logged-in account in two places:

| What | macOS | Linux |
|---|---|---|
| OAuth tokens (`claudeAiOauth`) | Keychain item `Claude Code-credentials` | `~/.claude/.credentials.json` |
| Account identity (`oauthAccount`) | `~/.claude.json` | `~/.claude.json` |

A switch replaces only those two values; `csw exec` changes neither. Other data in the same places, such as MCP server logins and your Claude Code settings, stays as it is.

`csw` keeps its saved copies in the Keychain under the service `claude-switcher` on macOS, or in `~/.claude-switcher/creds/` with file mode 0600 on Linux. The account list is in `~/.claude-switcher/accounts.json`, which holds no secrets.

Usage comes from `https://api.anthropic.com/api/oauth/usage`, the endpoint Claude Code uses for its own `/usage` view. Inactive accounts refresh their tokens through Claude Code's OAuth client when needed. Each refresh issues a new refresh token, so `csw` saves it straight away. A lock file in `~/.claude-switcher` stops two `csw` processes from refreshing the same account at once.

## Limitations

- The usage endpoint and token format are internal to Claude Code and are not a documented public API. A future Claude Code release could change them and break this tool.
- `csw` does not refresh the active account's token, because Claude Code refreshes that token itself and two refreshes of the same token would invalidate one of them. If the live token has expired, usage for the active account shows as unavailable until Claude Code makes its next request.
- When you switch away from an account, `csw` saves that account's latest tokens. However, a Claude Code process that is still running with the old account can refresh the token later. The saved copy then becomes invalid, and you need to `/login` to that account again and run `csw add`.
- `csw` writes `~/.claude.json` with a read, modify and replace sequence. It does not take Claude Code's file lock, so a write by Claude Code at the same moment could be lost.
- Setup tokens from `claude setup-token` and API key logins are not supported, because the usage endpoint does not accept them.
- `CLAUDE_CONFIG_DIR` is not supported. Only the default Claude Code profile is switched.
- A command started with `csw exec` cannot refresh its own token. If it runs for longer than the token lasts (at least one hour from the start), its requests fail with an authentication error.
- `csw exec` does not change `~/.claude.json`, so Claude Code may show the email of the global login, for example in `/status`, while its requests use the account you named. This display has not yet been checked in an interactive session.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests run against an in-memory store and never touch the Keychain, your credentials files or the network.
