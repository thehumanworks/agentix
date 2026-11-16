# The login refresher: one place that refreshes Codex's and pi's logins

A refresh token works once. Today every machine that runs Codex or pi
(sandboxes, the Mac) holds the same login and refreshes it when it decides
to, and agent-auth-sync passes the new tokens around. When two machines
refresh at about the same time, or one refreshes while another is offline,
the others hold a spent refresh token and Codex reports "your refresh token
was already used". The sync only narrows that race; it cannot close it.

The login refresher closes it by being the only holder of the refresh
tokens. It is a Modal app with one scheduled function:

```sh
uv run agentix auth refresher deploy          # once; deploying again is safe
uv run agentix auth refresher status          # deployed? how did the last run go? no secrets
uv run agentix auth refresher run-now         # run now; --force refreshes every login
uv run agentix auth refresher seed            # hand this computer's logins in (agent-auth-sync does it by itself)
uv run agentix auth refresher stop            # stop it (logins then expire within days)
```

## What a run does

Modal runs `refresh` at minutes 17 and 47 of every hour (UTC). Each run:

1. Checks that the store belongs to this app: the field `owner` of the
   store's meta item (`op://agentix/codex-refresher-meta/owner`) names the
   app that first used it, and a second app (another
   `AGENTIX_REFRESHER_APP`) refuses to refresh the same logins.
2. Reads the real logins from the refresher's own fields, the store
   (`op://agentix/codex-refresher/auth`, `op://agentix/pi-refresher/auth`),
   every intake slot (each item in the vault named `ITEM-intake-...` after
   a public field's item, such as `codex-intake-mac-3f9a0c1e`), where
   machines hand in newer logins, and the public fields that every machine
   syncs (`op://agentix/codex/auth`, `op://agentix/pi/auth`).
3. Takes over the newest login with a refresh token among the slots and
   the public field, when it is newer than the store's (for pi, per
   provider, so two slots can each give one): a machine that signed in
   again or refreshed anyway, or one that still runs an older
   agent-auth-sync. A slot that `seed --force` wrote is taken once, whatever
   its age; its fingerprint is kept in the meta item's `taken` field. The
   first run takes the current logins over this way, so no seeding is
   needed at first. Logins are ordered by their access tokens' own `exp`
   and `iat` claims where they have them, not by times a machine's clock
   wrote (`last_refresh`, pi's `expires`); Anthropic's tokens are not JWTs,
   so pi's `expires` orders those.
4. Refreshes each login that is due, the way Codex and pi themselves do it
   (same endpoints, client IDs, and request bodies): Codex's ChatGPT login,
   and pi's `openai` (Sign in with ChatGPT), `openai-codex` (ChatGPT
   Plus/Pro), and `anthropic` (Claude Pro/Max) entries. A login is due when
   less than half its lifetime is left, kept between 1 and 24 hours (2 hours
   when the token does not say when it was issued); a Codex login also when
   its `last_refresh` is 7 days old, and one whose expiry cannot be read 2
   hours after its last refresh.
   Before it spends a refresh token it rewrites the store's current value,
   so a store it cannot write costs nothing, and it starts a refresh only
   while the run has time left for the request, every save attempt, and
   the rescue: a run stops starting refreshes 25 minutes in, and Modal
   stops the function at 30, so it is never stopped holding an unsaved
   token. Each call of `op` is given 20 seconds for a read and 30 for each
   half of a write.
5. Writes each new login to the store at once, before anything else, so a
   rotated refresh token is never lost. If the store cannot be written after
   three tries (whatever the error), it writes the new login to its own
   rescue slot (`codex-intake-refresher`, `pi-intake-refresher`), then the
   public field, again and again until the deadline; the
   next run takes it back from there. The run is reported as failed.
6. Writes copies without refresh tokens to the public fields, unless what is
   there is already the same or newer, or changed during the run, or the
   run has too little time left to read and write it (the next run
   publishes then).

Each run keeps a summary (expiry times, what was refreshed or published,
errors; never a token) in the Modal Dict `agentix-auth-refresher-status`,
which `status` prints, with the pi providers it leaves out of the public
field (`pi_left_out`). Runs never overlap (`max_containers=1`). A failed
refresh changes nothing; a refused refresh token is reported as permanent.

### The copies

Codex and pi accept a login whose refresh token is empty, use its access
token, and fail only locally when they try to refresh it. Checked offline
with fake tokens and a local token server:

- Codex 0.167.1 requires `tokens.refresh_token` to be a string: an empty one
  is accepted (`codex login status`: "Logged in using ChatGPT"; requests
  carry the access token), a missing key makes it reject the file
  (`codex-rs/login/src/token_data.rs`). Codex refreshes only when the access
  token expires within 5 minutes or after a 401; it reads `last_refresh` only
  when the access token has no readable expiry
  (`auth/manager.rs`, `proactive_refresh_reason`). A failed refresh never
  writes `auth.json`. After a 401 it first re-reads the file, so a copy that
  agent-auth-sync pulled meanwhile is picked up.
- pi 1.5.2 refreshes an entry only when its `expires` is within 5 minutes,
  under its file lock (`pi-ai/src/auth/resolve.ts`); a failed refresh
  writes nothing. Its read-only store requires `refresh` to be a string
  (`dist/core/auth-storage.js`), so the copies keep `"refresh": ""`. Each
  OAuth entry needs `type: "oauth"`, `access`, `refresh`, and `expires`
  (milliseconds); `openai` also keeps `clientId` and `scopes`,
  `openai-codex` keeps `accountId`.

So the copies keep every key and only empty the refresh token. A machine
that does try to refresh (because the refresher was down until the access
token nearly expired) gets an error on that machine only.

Other pi providers with OAuth logins (GitHub Copilot, xAI, ...) are not
refreshed: the refresher keeps their real entries in the store only and
leaves them out of the public field, since a copy that every machine
refreshes on its own would break as before. API-key entries pass through.

## The machines: agent-auth-sync pulls only

agent-auth-sync has a setting, `AGENT_AUTH_SYNC_MODE`:

| Value | What it does |
| --- | --- |
| `auto` (default) | Pulls only a field whose refresh tokens are all empty, which is what the refresher writes; syncs every other field both ways, as before |
| `pull` | Never pushes, never creates a field |
| `sync` | Always both ways, as before |

So the switch comes with the refresher: until it publishes, nothing
changes; from its first publish, every machine with this version only
pulls. A pulled copy replaces the same login with its refresh token on the
machine (the field wins a tie), so no machine keeps a refresh token it could
spend. A login on the machine that has a refresh token and is strictly newer
than the field (a `codex login`, pi's `/login`, or a refresh that happened
anyway) stays there and is handed to the refresher: agent-auth-sync writes
it to this machine's own intake slot, `op://VAULT/ITEM-intake-MACHINE/auth`
(for `op://agentix/codex/auth` on the Mac, say
`op://agentix/codex-intake-mac-3f9a0c1e/auth`), which no machine pulls.
MACHINE is the host name and a random part, kept in agent-auth-sync's state
directory (`~/.local/state/agent-auth-sync/machine-id`); the item is created
on the first hand-in. It writes the slot again whenever the slot does not
hold that login, as long as the login is still newer than the field. The
refresher's next run takes it over and publishes it stripped, and the
machine then pulls that like any other copy. So signing in again needs no
extra step. `AGENT_AUTH_SYNC_INTAKE=off` turns this off.

Each machine writes only its own slot, and nothing but the refresher reads
or writes the store and meta items, so a hand-in can never write an old
login back over a new one. The store items hold only `auth`. Slots are not
cleared: the refresher takes a slot's login only while it is newer than the
store's, so an old one is ignored. A slot item stays for each machine that
ever handed a login in; delete those of machines that are gone whenever
convenient.

A machine with an older agent-auth-sync still pushes. That is safe: it
pulls the copy like any other change, and if it pushes a real login that is
newer, the refresher takes it over on its next run.

## Setup (once)

Nothing to create in 1Password: the refresher creates the items
`codex-refresher` and `pi-refresher` (field `auth`: the real logins) and
`codex-refresher-meta` and `pi-refresher-meta` (fields `owner` and `taken`)
in the `agentix` vault on its first write, with the service account of the
Modal secret `1password` (the sandboxes'), which can already write that
vault; each machine creates its own `codex-intake-MACHINE` and
`pi-intake-MACHINE` on its first hand-in.

1. Update the Mac's agent-auth-sync from a clone at this version, so it goes
   pull-only at the switch and hands later sign-ins to the refresher:

   ```sh
   export OP_SERVICE_ACCOUNT_TOKEN=...
   uv run agentix auth install
   ```

2. Deploy the refresher, and run it once to see it work:

   ```sh
   uv run agentix auth refresher deploy
   uv run agentix auth refresher run-now
   uv run agentix auth refresher status
   ```

   The first run takes the logins in `op://agentix/codex/auth` and
   `op://agentix/pi/auth` over and publishes them without refresh tokens.
3. Rebuild the image (`uv run agentix image build`) so new sandboxes have
   this agent-auth-sync. Sandboxes from older images keep the old script and
   converge as described above; replace them when convenient.

If a login is refused later (the summary says `permanent`), sign in again on
the Mac (`codex login`, pi's `/login`). agent-auth-sync hands the new login
in within seconds; `uv run agentix auth refresher run-now` takes it at once
instead of at the next half hour. On a computer without agent-auth-sync,
`uv run agentix auth refresher seed` (with the 1Password CLI signed in)
hands the files in, through this computer's seed slots (`ITEM-intake-MACHINE-seed`).
It never reads the store: it compares against the public field, whose
copies carry the store's access tokens, and refuses a login older than
that, which would put back a spent refresh token; `--force` hands it in
anyway, and the next run takes it once.

To go back: `uv run agentix auth refresher stop`; set
`AGENT_AUTH_SYNC_MODE=sync` on the machines (environment, or a line in
`~/.config/agent-auth-sync/config`); then sign in again on one of them
(`codex login`, pi's `/login`), which pushes real logins as before.

## Settings

Read where the app is deployed; `deploy` bakes them into the function.

| Variable | Default |
| --- | --- |
| `AGENTIX_REFRESHER_APP` | `agentix-auth-refresher` |
| `AGENTIX_REFRESHER_SECRET` | `1password`: the Modal secret with `OP_SERVICE_ACCOUNT_TOKEN` |
| `AGENTIX_REFRESHER_CODEX_STORE`, `AGENTIX_REFRESHER_PI_STORE` | `op://agentix/codex-refresher/auth`, `op://agentix/pi-refresher/auth` |
| `AGENTIX_REFRESHER_CODEX_PUBLIC`, `AGENTIX_REFRESHER_PI_PUBLIC` | `op://agentix/codex/auth`, `op://agentix/pi/auth` |

The meta items are named after the store's (`STOREITEM-meta`), and the intake
slots after the public field's (`ITEM-intake-MACHINE`), in the same vaults.

## Cost

One small container (0.125 CPU, 256 MiB, debian-slim with the pinned `op`
2.42.0) for about 10 to 30 seconds, 48 times a day (the 30-minute timeout
is only a ceiling): at the rates in
`src/agentix/cost.py` and 20 seconds a run, about $0.20 a month.

## Limits

- The store is kept apart by convention: the sandboxes' service account can
  read the `agentix` vault, so it could read the store items too; nothing in
  the sandboxes or on the Mac does (agent-auth-sync and `seed` touch only
  the public fields and their own slots). Each machine can also write any
  slot, so the slots are trusted as the public fields were before. For a hard boundary, put the store in another vault
  with another service account, in its own Modal secret
  (`AGENTIX_REFRESHER_SECRET` and the `*_STORE` settings).
- If the refresher stops, machines keep working until the access tokens
  expire (about 10 days for OpenAI's, hours for Anthropic's), then fail
  until it runs again.
- Logging out still does not stick, as before; revoke the login with the
  provider.
- The refresh requests copy Codex's and pi's own (endpoints, client IDs,
  fields) from their sources at Codex 0.167.1 and pi 1.5.2; a later version
  may change them. They were tested against a local stand-in only, never
  against the real endpoints.
