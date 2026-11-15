# agent-auth-sync

Keeps the sign-in files of coding agents in step with 1Password on every
machine that runs it. By default these are:

| File | 1Password field |
| --- | --- |
| `~/.codex/auth.json` (Codex's ChatGPT login; `$CODEX_HOME/auth.json`) | `op://agentix/codex/auth` |
| `~/.pi/agent/auth.json` (pi's providers; `$PI_CODING_AGENT_DIR/auth.json`) | `op://agentix/pi/auth` |

Each field holds its file as one line of JSON.

## Why

Codex and pi refresh their OAuth tokens on their own, and a refresh token
works once. When one machine refreshes, every other copy of the file is
dead: Codex then reports "your refresh token was already used". So every
machine that runs the agents pushes its file when it changes, and pulls the
field when another machine changed it. Both Codex and pi read the file
again before they refresh, so a running agent picks up a pulled file.

## How it decides

For each file, it compares three versions as canonical JSON: the file, the
field, and the version both last agreed on, kept in
`~/.local/state/agent-auth-sync`.

- Only the file changed: it pushes. The field is created, with its item,
  when missing.
- Only the field changed, or the file is missing or `{}`: it pulls. The file
  is replaced in one rename, with mode 600.
- Both changed: the newer tokens win. For Codex, that is the login with the
  later `last_refresh`. For pi, each provider entry with the later
  `expires` wins, and an entry on one side only is kept.
- A file that is not JSON, or a pi `auth.json.lock` (pi is writing), is left
  alone until the next pass. A lock untouched for a minute counts as left
  behind.
- A pull replaces the file only if it is still what was read: it takes
  `auth.json.lock` (so pi waits), checks the file again, and renames. If an
  agent refreshed in between, the pull stops, and the next look (within
  `AGENT_AUTH_SYNC_INTERVAL` seconds) pushes or merges the refresh.
- After a push, it reads the field again. If another machine pushed at the
  same moment, the newer tokens of the two go to both sides. Two pushes
  can still cross unseen when each reads the field back before the other
  writes; the later one then wins, as it did before.

Values pass through files in a private temporary directory, never through a
command line, and nothing prints them.

## Requirements

A POSIX shell (macOS `/bin/sh`, bash, dash, or busybox), the 1Password CLI
`op`, and `jq`, which macOS 15 and later include. No Nix needed.

`sync` and `status` use whatever `op` is signed in to. `watch` and `ensure`
run unattended, so they use only a service account token: from
`OP_SERVICE_ACCOUNT_TOKEN`, or from the file that `save-token` writes. The
service account needs write access to the vault to push.

## Use

```sh
export OP_SERVICE_ACCOUNT_TOKEN=...
./agent-auth-sync install    # or: uv run agentix auth install
```

`install` saves the token in `~/.config/agent-auth-sync/op-token` (mode
600), copies the script to `~/.local/bin/agent-auth-sync`, writes the paths
of `op` and `jq` to `~/.config/agent-auth-sync/config`, and starts
`agent-auth-sync watch`:

- on macOS, as the launchd agent `dev.agentix.agent-auth-sync`
  (`~/Library/LaunchAgents`), which starts at login and restarts if it ends;
  the log is `~/Library/Logs/agent-auth-sync.log`;
- on Linux, as the systemd user service `agent-auth-sync.service`, with
  `Restart=always`; `loginctl enable-linger` keeps it running while you are
  logged out.

| Command | What it does |
| --- | --- |
| `sync` | Compare each file with its field once, and copy the newer over the other |
| `watch` | Sync now, then within seconds of a file changing, and every 2 minutes for changes elsewhere; a second watcher waits for the first to stop, or with `--no-wait` exits |
| `ensure` | Unless a watcher runs, start one and wait up to 2 seconds for its first sync; does nothing without a token |
| `status` | Whether each file and its field agree, and whether a watcher runs; prints no secrets |
| `stop` | Stop the watcher |
| `save-token` | Save `OP_SERVICE_ACCOUNT_TOKEN` from this shell for the service |
| `install` / `uninstall` | Set up or remove the service, the copy, the config, and the saved token |

`ensure` returns once the watcher's first sync is done, or after
`AGENT_AUTH_SYNC_ENSURE_WAIT` seconds, so `codex` and `pi` start within that
even when 1Password does not answer; the sync goes on in the background and
writes to `~/.local/state/agent-auth-sync/watch.log`. On a fresh machine
whose first pull takes longer, the agent starts before its file arrives;
raise the wait to avoid that. Each call of `op` is stopped after
`AGENT_AUTH_SYNC_OP_TIMEOUT` seconds.

A new login on one machine reaches the others. Logging out does not stick:
a deleted file, or pi's `{}` after `/logout`, is pulled back from
1Password, and a cleared field is pushed back by any machine that still has
the file. To end a login everywhere, revoke it with the provider (for
Codex, the ChatGPT account's sessions; for pi, each provider's console),
then delete the field.

## Settings

From the environment, then from `~/.config/agent-auth-sync/config`:

| Variable | Default |
| --- | --- |
| `AGENT_AUTH_SYNC_FILES` | The two lines above, as `op://VAULT/ITEM/FIELD PATH` per line |
| `AGENT_AUTH_SYNC_OP`, `AGENT_AUTH_SYNC_JQ` | `op`, `jq` |
| `AGENT_AUTH_SYNC_INTERVAL` | `5`: seconds between looks at the files |
| `AGENT_AUTH_SYNC_REMOTE_INTERVAL` | `120`: seconds between reads of 1Password; each read of each field counts against the service account's rate limit |
| `AGENT_AUTH_SYNC_ENSURE_WAIT` | `2`: seconds `ensure` waits for the first sync |
| `AGENT_AUTH_SYNC_OP_TIMEOUT` | `30`: seconds before a call of `op` is stopped |
| `AGENT_AUTH_SYNC_TOKEN_FILE` | `~/.config/agent-auth-sync/op-token`: the token `save-token` writes and `uninstall` removes |
| `AGENT_AUTH_SYNC_PROFILE_TOKEN_FILE` | None; the Nix package sets the profile's `agentix.op.tokenFile` (in a sandbox, `/run/agentix/op-token`). Read when neither the environment nor the saved token has one; never written or removed |
| `AGENT_AUTH_SYNC_STATE_DIR` | `~/.local/state/agent-auth-sync` |
| `AGENT_AUTH_SYNC_CONFIG_DIR` | `~/.config/agent-auth-sync` |

## With Nix

`nix run .#agent-auth-sync -- status` in `nix/` runs it with the `op` on
PATH. The Home Manager module `auth-sync.nix` (`agentix.authSync`) installs
it with the profile's `op`. The `codex` and `pi` wrappers run
`agent-auth-sync ensure` before they start (`startWithAgents`), which is how
sandboxes keep their files in step. On a computer,
`agentix.authSync.service.enable = true` adds the launchd agent or systemd
user service; run `agent-auth-sync save-token` once from a shell that has
the token.

What the Home Manager modules change on a Mac or Linux computer too, not
only in sandboxes:

- Codex keeps its login in `~/.codex/auth.json`
  (`cli_auth_credentials_store = "file"`), not the macOS Keychain or another
  keyring. A login kept in the keyring before is not read; run `codex login`
  once.
- `agentix.codex.secrets` and `agentix.pi.secrets` are empty by default:
  `codex` and `pi` no longer get `CODEX_API_KEY` or `OPENAI_API_KEY` from
  1Password and use the logins in their files instead. Add a key back with
  those options to bill an API key.
