"""The deployed Modal app that refreshes Codex's and pi's logins. See
docs/auth-refresher.md.

A refresh token works once, so a login that several machines refresh dies on
all but one of them. This app is the one place that refreshes: it keeps the
real logins, with their refresh tokens, in 1Password fields that only it
uses (`STORE`), refreshes each well before it expires, and writes copies
without the refresh tokens to the fields that every machine pulls
(`PUBLIC`). A machine can then use the access tokens but never refresh, so
it never spends the refresh token that everyone else depends on; agent-auth-
sync on each machine sees the empty refresh tokens and stops pushing.

`agentix auth refresher deploy` deploys it; Modal runs `refresh` on
`SCHEDULE`. Every run is safe to repeat and to cut short: a rotated refresh
token is written to the store before anything else, and a public copy is
never replaced by an older one.

This module is imported in the function's container, which has Modal, the
standard library, and `op`, and nothing else of agentix: keep its imports so.
Nothing here prints or logs a token, and values reach `op` through files,
never its command line.
"""

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import modal

APP_NAME = os.environ.get("AGENTIX_REFRESHER_APP") or "agentix-auth-refresher"
FUNCTION_NAME = "refresh"
# The Modal secret with OP_SERVICE_ACCOUNT_TOKEN; by default the sandboxes'.
SECRET_NAME = os.environ.get("AGENTIX_REFRESHER_SECRET") or "1password"
# Twice an hour, at minutes Modal's other scheduled jobs are less likely to use.
SCHEDULE = "17,47 * * * *"
STATUS_DICT = f"{APP_NAME}-status"

# The fields, as op:// references. STORE holds the real logins and is read
# only by this app; PUBLIC is what agent-auth-sync syncs on every machine.
SETTINGS = {
    "AGENTIX_REFRESHER_CODEX_STORE": "op://agentix/codex-refresher/auth",
    "AGENTIX_REFRESHER_PI_STORE": "op://agentix/pi-refresher/auth",
    "AGENTIX_REFRESHER_CODEX_PUBLIC": "op://agentix/codex/auth",
    "AGENTIX_REFRESHER_PI_PUBLIC": "op://agentix/pi/auth",
}


def setting(name: str) -> str:
    return os.environ.get(name) or SETTINGS[name]


# How long before expiry a token is refreshed: half its lifetime when the
# token says when it was issued, kept between these two, else DEFAULT_AHEAD.
# With a run every 30 minutes, a machine always has an hour or more left.
MIN_AHEAD = 60 * 60
MAX_AHEAD = 24 * 60 * 60
DEFAULT_AHEAD = 2 * 60 * 60
# Codex refreshes a login whose last_refresh is 8 days old when its access
# token has no expiry it can read; this stays ahead of that.
CODEX_MAX_AGE = 7 * 24 * 60 * 60

HTTP_TIMEOUT = 45
OP_TIMEOUT = 60
USER_AGENT = "agentix-auth-refresher"

# The token endpoints and public OAuth client IDs, as Codex and pi use them.
# Codex: codex-rs/login/src/auth/manager.rs (REFRESH_TOKEN_URL, CLIENT_ID).
# pi: pi-ai/src/auth/oauth/{openai-chatgpt,openai-codex,anthropic}.ts.
TOKEN_URLS = {
    "codex": "https://auth.openai.com/oauth/token",
    "openai": "https://auth.openai.com/api/accounts/oauth/token",
    "openai-codex": "https://auth.openai.com/oauth/token",
    "anthropic": "https://platform.claude.com/v1/oauth/token",
}
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# pi keeps Anthropic's client ID base64-encoded; so does this.
ANTHROPIC_CLIENT_ID = base64.b64decode(
    "OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl"
).decode()
OPENAI_RESOURCE = "https://api.openai.com/v1"
OPENAI_DIRECT_SCOPE = "chatgpt.tokens.use.direct"

# The 1Password CLI the container runs, as the image's profile pins it.
OP_VERSION = "2.42.0"
OP_SHA256 = "4432a5de3ef8c00e0f1e341d5d65df7ac0152ff0bc56ebfd6e47de60219f1840"
OP_URL = (
    f"https://cache.agilebits.com/dist/1P/op2/pkg/v{OP_VERSION}/"
    f"op_linux_amd64_v{OP_VERSION}.zip"
)


class RefreshError(Exception):
    """A refresh failed. `permanent` when the refresh token was refused, so
    trying again with it cannot work; the message never holds a token."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class StoreError(Exception):
    """1Password could not be read or written."""


# --- 1Password ---------------------------------------------------------------


class OpStore:
    """Reads and writes op:// fields with the 1Password CLI, the way
    agent-auth-sync does: `op read`, then `op item get` and `op item edit
    --template`, or `op item create` for a missing item. The value goes
    through a private temporary file, never the command line."""

    def __init__(self, op: str = "", timeout: float = OP_TIMEOUT):
        self.op = op or os.environ.get("AGENTIX_REFRESHER_OP") or "op"
        self.timeout = timeout

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.op, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise StoreError(f"op did not answer within {self.timeout} s") from None
        except OSError as error:
            raise StoreError(f"cannot run op: {error.strerror}") from None

    @staticmethod
    def _complaint(result: subprocess.CompletedProcess) -> str:
        line = (result.stderr.strip().splitlines() or ["op failed"])[0]
        return re.sub(r"^\[ERROR\] [0-9/]+ [0-9:]+ ", "", line)

    @staticmethod
    def _missing(result: subprocess.CompletedProcess) -> bool:
        return bool(
            re.search(
                r"isn't an item|could not find item|does not have a field|no item found",
                result.stderr,
            )
        )

    @staticmethod
    def parse(ref: str) -> tuple[str, str, str]:
        match = re.fullmatch(r"op://([^/]+)/([^/]+)/([^/]+)", ref)
        if not match:
            raise StoreError(f"{ref}: not an op://VAULT/ITEM/FIELD reference")
        return match.group(1), match.group(2), match.group(3)

    def read(self, ref: str) -> str | None:
        """The field's value, or None when the item or field does not exist."""
        self.parse(ref)
        result = self._run("read", ref)
        if result.returncode == 0:
            return result.stdout.rstrip("\n")
        if self._missing(result):
            return None
        raise StoreError(f"{ref}: reading 1Password failed: {self._complaint(result)}")

    def write(self, ref: str, value: str) -> None:
        """Puts `value` in the field, creating the field or the item."""
        vault, item, field = self.parse(ref)
        work = tempfile.mkdtemp(prefix="agentix-refresher.")
        try:
            os.chmod(work, 0o700)
            got = self._run("item", "get", item, "--vault", vault, "--format", "json")
            if got.returncode == 0:
                current = json.loads(got.stdout)
                # op deletes a field the template leaves out and writes an
                # empty date as the epoch, so empty dates are left out.
                fields = [
                    f
                    for f in current.get("fields", [])
                    if f.get("type") != "DATE" or (f.get("value") or "") != ""
                ]
                for entry in fields:
                    if field in (entry.get("label"), entry.get("id")):
                        entry["value"] = value
                        break
                else:
                    fields.append({"label": field, "type": "CONCEALED", "value": value})
                current["fields"] = fields
                command = ["item", "edit", item, "--vault", vault]
            elif self._missing(got):
                current = {
                    "title": item,
                    "category": "API_CREDENTIAL",
                    "fields": [{"label": field, "type": "CONCEALED", "value": value}],
                }
                command = ["item", "create", "--vault", vault]
            else:
                raise StoreError(
                    f"{ref}: reading the item failed: {self._complaint(got)}"
                )
            template = os.path.join(work, "item.json")
            fd = os.open(template, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(current, handle)
            result = self._run(*command, "--template", template)
            if result.returncode != 0:
                raise StoreError(
                    f"{ref}: writing 1Password failed: {self._complaint(result)}"
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)


# --- tokens ------------------------------------------------------------------


def jwt_claims(token: Any) -> dict | None:
    """The claims of a JWT, unverified, or None when it is not one."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except ValueError:
        return None
    return claims if isinstance(claims, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(seconds: float) -> str:
    return (
        dt.datetime.fromtimestamp(seconds, dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def due(expires: float | None, issued: float | None, now: float) -> bool:
    """Whether a token that expires at `expires` (and was issued at
    `issued`, when known) is to be refreshed at `now`."""
    if expires is None:
        return True
    if issued is not None and expires > issued:
        ahead = min(MAX_AHEAD, max(MIN_AHEAD, (expires - issued) / 2))
    else:
        ahead = DEFAULT_AHEAD
    return expires - now < ahead


def _post(url: str, fields: dict[str, str], form: bool, client: str) -> dict[str, Any]:
    """POSTs a token request and returns its JSON. A failure says only the
    status and the OAuth error code: the body may echo credentials."""
    if form:
        body = urllib.parse.urlencode(fields).encode()
        kind = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(fields).encode()
        kind = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": kind,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        code = _error_code(error)
        permanent = error.code in (400, 401) and (
            error.code == 401
            or code
            in (
                "invalid_grant",
                "refresh_token_expired",
                "refresh_token_reused",
                "refresh_token_invalidated",
            )
        )
        detail = f" ({code})" if code else ""
        raise RefreshError(
            f"{client}: the token endpoint answered {error.code}{detail}",
            permanent=permanent,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise RefreshError(
            f"{client}: cannot reach the token endpoint: {reason}"
        ) from None
    except ValueError:
        raise RefreshError(
            f"{client}: the token endpoint did not answer JSON"
        ) from None
    if not isinstance(data, dict):
        raise RefreshError(f"{client}: the token endpoint did not answer an object")
    return data


def _error_code(error: urllib.error.HTTPError) -> str:
    """The OAuth error code of a refused request, if it is a plain word."""
    try:
        data = json.loads(error.read(65536))
    except ValueError, OSError:
        return ""
    if not isinstance(data, dict):
        return ""
    code = data.get("error")
    if isinstance(code, dict):
        code = code.get("code") or code.get("type")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
        return ""
    return code


def _token(data: dict, name: str, client: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RefreshError(f"{client}: the token response has no {name}")
    return value


def _lifetime(data: dict, client: str) -> float:
    value = _number(data.get("expires_in"))
    if value is None or value <= 0:
        raise RefreshError(f"{client}: the token response has no valid expires_in")
    return value


# --- Codex -------------------------------------------------------------------
# ~/.codex/auth.json: {"auth_mode", "OPENAI_API_KEY", "tokens": {"id_token",
# "access_token", "refresh_token", "account_id", ...}, "last_refresh"}.
# Codex requires tokens.refresh_token to be a string (token_data.rs), so the
# public copy has "" there; deleting the key makes Codex reject the file.


def codex_tokens(login: Any) -> dict | None:
    if isinstance(login, dict) and isinstance(login.get("tokens"), dict):
        return login["tokens"]
    return None


def codex_real(login: Any) -> bool:
    tokens = codex_tokens(login)
    return bool(
        tokens
        and isinstance(tokens.get("refresh_token"), str)
        and tokens["refresh_token"]
    )


def codex_expiry(login: Any) -> tuple[float | None, float | None]:
    """When the access token expires and was issued, from its claims."""
    claims = jwt_claims((codex_tokens(login) or {}).get("access_token")) or {}
    return _number(claims.get("exp")), _number(claims.get("iat"))


def codex_age(login: Any) -> tuple[float, float]:
    """What orders two Codex logins: access token expiry, then last_refresh."""
    expires, _ = codex_expiry(login)
    return (expires or 0.0, _time((login or {}).get("last_refresh")) or 0.0)


def codex_due(login: dict, now: float) -> bool:
    expires, issued = codex_expiry(login)
    last = _time(login.get("last_refresh"))
    return due(expires, issued, now) or last is None or now - last > CODEX_MAX_AGE


def codex_strip(login: dict) -> dict:
    stripped = json.loads(json.dumps(login))
    stripped["tokens"]["refresh_token"] = ""
    return stripped


def codex_refresh(login: dict, now: float, url: str = "") -> dict:
    """Refreshes a Codex ChatGPT login as Codex does
    (refresh_and_persist_chatgpt_token), and returns the new login."""
    tokens = codex_tokens(login) or {}
    provenance = tokens.get("oauth_provenance") or {}
    issuer = provenance.get("issuer") if isinstance(provenance, dict) else None
    endpoint = url or (
        f"{issuer.rstrip('/')}/oauth/token"
        if isinstance(issuer, str) and issuer
        else TOKEN_URLS["codex"]
    )
    client_id = (
        provenance.get("client_id") if isinstance(provenance, dict) else None
    ) or CODEX_CLIENT_ID
    data = _post(
        endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "refresh_reason": "scheduled",
        },
        form=False,
        client="codex",
    )
    access = _token(data, "access_token", "codex")
    new = json.loads(json.dumps(login))
    new_tokens = new["tokens"]
    new_tokens["access_token"] = access
    if isinstance(data.get("id_token"), str) and data["id_token"]:
        new_tokens["id_token"] = data["id_token"]
    if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
        new_tokens["refresh_token"] = data["refresh_token"]
    new["last_refresh"] = (
        dt.datetime.fromtimestamp(now, dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return new


# --- pi ----------------------------------------------------------------------
# ~/.pi/agent/auth.json: {PROVIDER: {"type": "oauth", "access", "refresh",
# "expires" (ms), ...} or {"type": "api_key", ...}}. pi's read-only store
# requires "refresh" to be a string, so the public copy has "" there.


def _pi_oauth(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("type") == "oauth"


def pi_real(entry: Any) -> bool:
    return (
        _pi_oauth(entry)
        and isinstance(entry.get("refresh"), str)
        and bool(entry["refresh"])
    )


def pi_expires(entry: Any) -> float:
    value = _number(entry.get("expires")) if isinstance(entry, dict) else None
    return value / 1000 if value is not None else 0.0


def pi_due(entry: dict, now: float) -> bool:
    claims = jwt_claims(entry.get("access")) or {}
    expires = pi_expires(entry) or None
    issued = _number(claims.get("iat"))
    if issued is not None and expires is not None:
        # pi subtracts a margin from expires; the claims know the lifetime.
        expires = min(expires, _number(claims.get("exp")) or expires)
    return due(expires, issued, now)


def pi_strip(entry: dict) -> dict:
    stripped = dict(entry)
    stripped["refresh"] = ""
    return stripped


def _pi_openai(entry: dict, now: float, url: str) -> dict:
    """pi's "openai" provider (Sign in with ChatGPT): openai-chatgpt.ts."""
    client_id = entry.get("clientId")
    if not isinstance(client_id, str) or not client_id.strip():
        raise RefreshError(
            "pi openai: the login has no clientId; sign in again", permanent=True
        )
    data = _post(
        url or TOKEN_URLS["openai"],
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": entry["refresh"],
            "resource": OPENAI_RESOURCE,
        },
        form=True,
        client="pi openai",
    )
    access = _token(data, "access_token", "pi openai")
    refresh = _token(data, "refresh_token", "pi openai")
    lifetime = _lifetime(data, "pi openai")
    scopes = entry.get("scopes")
    if isinstance(data.get("scope"), str) and data["scope"].strip():
        scopes = data["scope"].split()
        if OPENAI_DIRECT_SCOPE not in scopes:
            raise RefreshError(f"pi openai: the grant lacks {OPENAI_DIRECT_SCOPE}")
    new = {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": int((now + lifetime - 3 * 60) * 1000),
        "clientId": client_id,
    }
    if scopes is not None:
        new["scopes"] = scopes
    return new


def _pi_openai_codex(entry: dict, now: float, url: str) -> dict:
    """pi's "openai-codex" provider (ChatGPT Plus/Pro): openai-codex.ts."""
    data = _post(
        url or TOKEN_URLS["openai-codex"],
        {
            "grant_type": "refresh_token",
            "refresh_token": entry["refresh"],
            "client_id": CODEX_CLIENT_ID,
        },
        form=True,
        client="pi openai-codex",
    )
    access = _token(data, "access_token", "pi openai-codex")
    refresh = _token(data, "refresh_token", "pi openai-codex")
    lifetime = _lifetime(data, "pi openai-codex")
    claims = jwt_claims(access) or {}
    auth = claims.get("https://api.openai.com/auth")
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not isinstance(account, str) or not account:
        raise RefreshError("pi openai-codex: the new access token names no account")
    return {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": int((now + lifetime) * 1000),
        "accountId": account,
    }


def _pi_anthropic(entry: dict, now: float, url: str) -> dict:
    """pi's "anthropic" provider (Claude Pro/Max): anthropic.ts."""
    data = _post(
        url or TOKEN_URLS["anthropic"],
        {
            "grant_type": "refresh_token",
            "client_id": ANTHROPIC_CLIENT_ID,
            "refresh_token": entry["refresh"],
        },
        form=False,
        client="pi anthropic",
    )
    access = _token(data, "access_token", "pi anthropic")
    lifetime = _lifetime(data, "pi anthropic")
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = entry["refresh"]  # not rotated: the old one stays good
    return {
        "type": "oauth",
        "refresh": refresh,
        "access": access,
        "expires": int((now + lifetime - 5 * 60) * 1000),
    }


PI_REFRESHERS: dict[str, Callable[[dict, float, str], dict]] = {
    "openai": _pi_openai,
    "openai-codex": _pi_openai_codex,
    "anthropic": _pi_anthropic,
}


# --- one run -------------------------------------------------------------------


def _parse(raw: str | None, what: str, check: Callable[[Any], bool]) -> Any:
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise StoreError(f"{what} is not JSON; left alone") from None
    if not check(value):
        raise StoreError(f"{what} is not a login; left alone")
    return value


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _line(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


class Run:
    """One pass over Codex's and pi's logins. `log` takes lines that hold no
    secrets; `token_urls` replaces endpoints, for tests."""

    def __init__(
        self,
        store: OpStore,
        now: float | None = None,
        force: bool = False,
        token_urls: dict[str, str] | None = None,
        log: Callable[[str], None] = print,
        write_tries: int = 3,
        write_pause: float = 2.0,
    ):
        self.store = store
        self.now = time.time() if now is None else now
        self.force = force
        self.urls = token_urls or {}
        self.log = log
        self.write_tries = write_tries
        self.write_pause = write_pause

    def _persist(self, ref: str, value: Any, rescue: Callable[[], None]) -> None:
        """Writes a login with a new refresh token to the store, trying a few
        times. When that cannot be done, `rescue` writes it to the public
        field instead, where the next run takes it back (a machine pulling it
        meanwhile holds a refresh token, as every machine did before), so
        the rotated token is never lost; the run still fails."""
        for attempt in range(self.write_tries):
            try:
                self.store.write(ref, _line(value))
                return
            except StoreError as error:
                last = error
                if attempt + 1 < self.write_tries:
                    time.sleep(self.write_pause)
        self.log(f"ERROR {last}; saving the new login in the public field instead")
        rescue()
        raise StoreError(f"{ref}: the new login could not be saved here: {last}")

    def _publish(self, ref: str, before: Any, after: Any, what: str) -> bool:
        """Writes `after` to the public field unless it is what is there, or
        the field changed since it was read as `before`."""
        if before is not None and _canon(before) == _canon(after):
            return False
        now_raw = self.store.read(ref)
        current = None
        if now_raw is not None and now_raw.strip():
            try:
                current = json.loads(now_raw)
            except ValueError:
                current = now_raw
        if _canon(current) != _canon(before):
            self.log(f"{what}: {ref} changed during this run; the next run publishes")
            return False
        self.store.write(ref, _line(after))
        self.log(f"{what}: published to {ref}")
        return True

    def codex(self) -> dict[str, Any]:
        out: dict[str, Any] = {"refreshed": False, "published": False, "adopted": False}
        store_ref = setting("AGENTIX_REFRESHER_CODEX_STORE")
        public_ref = setting("AGENTIX_REFRESHER_CODEX_PUBLIC")
        is_login = lambda v: codex_tokens(v) is not None  # noqa: E731
        login = _parse(self.store.read(store_ref), store_ref, is_login)
        public = _parse(self.store.read(public_ref), public_ref, is_login)

        # A real login in the public field that is newer than the store's was
        # pushed by a machine that refreshed or signed in again: the store's
        # refresh token is spent or superseded, so the store takes it.
        if codex_real(public) and (
            login is None or codex_age(public) > codex_age(login)
        ):
            self.store.write(store_ref, _line(public))
            login = public
            out["adopted"] = True
            self.log(f"codex: took the newer login in {public_ref} into {store_ref}")
        if login is None:
            raise StoreError(
                f"no Codex login in {store_ref} or {public_ref}: sign in on a machine "
                "and run `agentix auth refresher seed`"
            )
        if not codex_real(login):
            raise StoreError(
                f"the Codex login in {store_ref} has no refresh token: sign in on a "
                "machine and run `agentix auth refresher seed`"
            )

        if self.force or codex_due(login, self.now):
            new = codex_refresh(login, self.now, self.urls.get("codex", ""))
            self._persist(
                store_ref, new, lambda: self.store.write(public_ref, _line(new))
            )
            login = new
            out["refreshed"] = True
            self.log("codex: refreshed")

        stripped = codex_strip(login)
        if (
            public is not None
            and not codex_real(public)
            and codex_age(public) > codex_age(stripped)
        ):
            self.log(
                f"codex: {public_ref} holds newer tokens than the store; left alone"
            )
        else:
            out["published"] = self._publish(public_ref, public, stripped, "codex")
        expires, _ = codex_expiry(login)
        out["expires_at"] = _iso(expires) if expires else None
        return out

    def pi(self) -> dict[str, Any]:
        out: dict[str, Any] = {"providers": {}, "published": False, "errors": []}
        store_ref = setting("AGENTIX_REFRESHER_PI_STORE")
        public_ref = setting("AGENTIX_REFRESHER_PI_PUBLIC")
        is_auth = lambda v: isinstance(v, dict)  # noqa: E731
        logins = _parse(self.store.read(store_ref), store_ref, is_auth) or {}
        public = _parse(self.store.read(public_ref), public_ref, is_auth)

        # As for Codex, per provider: a newer real entry in the public field
        # replaces the store's.
        taken = [
            name
            for name, entry in (public or {}).items()
            if pi_real(entry)
            and (name not in logins or pi_expires(entry) > pi_expires(logins[name]))
        ]
        if taken:
            logins = {**logins, **{name: public[name] for name in taken}}
            self.store.write(store_ref, _line(logins))
            self.log(
                f"pi: took the newer {', '.join(sorted(taken))} from {public_ref} into {store_ref}"
            )

        for name in sorted(logins):
            entry = logins[name]
            if not _pi_oauth(entry):
                continue
            state: dict[str, Any] = {"refreshed": False, "adopted": name in taken}
            out["providers"][name] = state
            if name not in PI_REFRESHERS:
                state["error"] = (
                    "this provider is not supported: kept only in the store"
                )
                continue
            if not pi_real(entry):
                state["error"] = (
                    "no refresh token: sign in and run `agentix auth refresher seed`"
                )
                out["errors"].append(f"pi {name}: {state['error']}")
                continue
            if self.force or pi_due(entry, self.now):
                try:
                    new = PI_REFRESHERS[name](entry, self.now, self.urls.get(name, ""))
                except RefreshError as error:
                    state["error"] = str(error)
                    state["permanent"] = error.permanent
                    out["errors"].append(str(error))
                    self.log(f"ERROR {error}")
                    continue
                logins = {**logins, name: new}
                rescue_copy = {**(public or {}), name: new}
                self._persist(
                    store_ref,
                    logins,
                    lambda copy=rescue_copy: self.store.write(public_ref, _line(copy)),
                )
                state["refreshed"] = True
                self.log(f"pi {name}: refreshed")
            state["expires_at"] = (
                _iso(pi_expires(logins[name])) if pi_expires(logins[name]) else None
            )

        # The public copy: what is there, with each supported provider's entry
        # replaced by the store's without its refresh token, unless the one
        # there is newer, and no refresh token left in it at all.
        published = dict(public or {})
        for name, entry in logins.items():
            if not _pi_oauth(entry) or name not in PI_REFRESHERS:
                continue
            stripped = pi_strip(entry)
            there = published.get(name)
            if (
                _pi_oauth(there)
                and not pi_real(there)
                and pi_expires(there) > pi_expires(stripped)
            ):
                self.log(f"pi {name}: {public_ref} holds a newer entry; left alone")
                continue
            published[name] = stripped
        for name in [n for n, e in published.items() if pi_real(e)]:
            del published[name]
            self.log(
                f"pi {name}: not supported here, so it is left out of {public_ref}"
            )
        if published:
            out["published"] = self._publish(public_ref, public, published, "pi")
        return out

    def __call__(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"ran_at": _iso(self.now), "forced": self.force}
        ok = True
        for name, step in (("codex", self.codex), ("pi", self.pi)):
            try:
                summary[name] = step()
                if summary[name].get("errors"):
                    ok = False
            except (RefreshError, StoreError) as error:
                summary[name] = {"error": str(error)}
                if isinstance(error, RefreshError):
                    summary[name]["permanent"] = error.permanent
                self.log(f"ERROR {name}: {error}")
                ok = False
        summary["ok"] = ok
        return summary


def seed(store: OpStore, codex_path: str = "", pi_path: str = "") -> dict[str, Any]:
    """Puts the real logins in these local files into the refresher's store,
    for after a new sign-in. pi's providers are merged into the store's."""
    done: dict[str, Any] = {}
    if codex_path:
        with open(codex_path) as handle:
            login = json.load(handle)
        if not codex_real(login):
            raise StoreError(f"{codex_path} holds no Codex login with a refresh token")
        ref = setting("AGENTIX_REFRESHER_CODEX_STORE")
        store.write(ref, _line(login))
        done["codex"] = ref
    if pi_path:
        with open(pi_path) as handle:
            local = json.load(handle)
        real = {name: e for name, e in (local or {}).items() if pi_real(e)}
        if not real:
            raise StoreError(f"{pi_path} holds no pi login with a refresh token")
        ref = setting("AGENTIX_REFRESHER_PI_STORE")
        current = _parse(store.read(ref), ref, lambda v: isinstance(v, dict)) or {}
        store.write(ref, _line({**current, **real}))
        done["pi"] = {"field": ref, "providers": sorted(real)}
    return done


# --- the Modal app -------------------------------------------------------------

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim()
    .apt_install("curl", "unzip", "ca-certificates")
    .run_commands(
        f"curl -fsSL -o /tmp/op.zip {OP_URL}"
        f" && echo '{OP_SHA256}  /tmp/op.zip' | sha256sum -c -"
        " && unzip -o /tmp/op.zip op -d /usr/local/bin && rm /tmp/op.zip"
        " && op --version"
    )
)


@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name(SECRET_NAME, required_keys=["OP_SERVICE_ACCOUNT_TOKEN"])
    ],
    schedule=modal.Cron(SCHEDULE),
    # One run at a time: a run-now waits for a scheduled run, and the reverse.
    max_containers=1,
    cpu=0.125,
    memory=256,
    timeout=10 * 60,
    env={name: setting(name) for name in SETTINGS},
    name=FUNCTION_NAME,
)
def refresh(force: bool = False) -> dict:
    """Refresh what is due (everything with `force`), publish the stripped
    copies, and keep the summary for `agentix auth refresher status`."""
    summary = Run(OpStore(), force=force)()
    try:
        modal.Dict.from_name(STATUS_DICT, create_if_missing=True)["last"] = summary
    except modal.exception.Error as error:
        print(f"agentix: could not keep the summary: {error}")
    return summary
