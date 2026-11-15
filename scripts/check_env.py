#!/usr/bin/env python3
"""Fail when a committed .env file holds a plaintext value.

The repository tracks .env, so every value in it must be a 1Password
reference (op://...) or dotenvx ciphertext (encrypted:...). The Envfile at the
repository root declares the keys. This script:

- reads every line of each env file itself. A line must be blank, a comment,
  or a strict KEY=VALUE assignment on one line; anything else fails, so a
  pasted secret on a line of its own cannot slip past a dotenv parser. Each
  value, and each value in a commented-out assignment, must be empty, an
  op://VAULT/ITEM[/SECTION]/FIELD reference, or dotenvx ciphertext.
  DOTENV_PUBLIC_KEY* values must be public keys.
- holds the root Envfile to a strict subset: `strict true` and
  `env "KEY"` declarations, with no `encrypted: false`, `redacted: false`,
  defaults, profiles, or file blocks. No other Envfile may exist.
- requires every key of a file other than a template (*.example, *.sample,
  *.template) to be declared, and `dotenvx check` to pass, so a declared key
  cannot go missing.
- runs `dotenvx protect --git-file`, dotenvx's own Git filter check, as a
  second opinion.

dotenvx runs in a scratch copy that holds only the root Envfile and the file
being checked, with no access to 1Password, key stores, Armor, or the
caller's environment.

Usage:
  scripts/check_env.py            tracked and untracked, not ignored, files
  scripts/check_env.py --staged   the staged versions, and the staged Envfile
  scripts/check_env.py FILE...    the named files, as they are on disk

dotenvx 2.32.4 to 2.38.x comes from PATH, or from $DOTENVX. No value is
printed: messages name keys and lines, and dotenvx redacts what it shows.
Python 3.9 or newer, standard library only, so it runs on a Mac's python3.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ENVFILE = "Envfile"
# The dotenvx releases that read an Envfile and have `check` and `protect
# --git-file`. The release after 2.38 renames the file to Dotenvxfile.
DOTENVX_MIN = (2, 32, 4)
DOTENVX_BELOW = (2, 39, 0)

# The files dotenvx's Git filter covers (src/lib/helpers/precommitEnvPatterns.js
# in dotenvx 2.38), matched without regard to case (.ENV loads as .env on a
# Mac), plus any file under a directory named .env or .env.d. direnv's .envrc
# is a shell script, not an env file.
PATTERNS = (".env*", "*.env", ".flaskenv", ".dev.vars*")
NOT_ENV = (".envrc",)
ENV_DIRS = (".env", ".env.d")
TEMPLATE_SUFFIXES = (".example", ".sample", ".template")

KEY = r"[A-Za-z_][A-Za-z0-9_]*"
# KEY=VALUE on one line: optional `export`, no spaces around `=`, and a value
# that is unquoted or quoted and closed on the same line. Nothing may follow
# the value: an inline comment could hide a secret, so it is refused.
ASSIGNMENT = re.compile(
    rf"^(?:export\s+)?({KEY})="
    r"(?:'([^'\n]*)'|\"((?:[^\"\\\n]|\\.)*)\"|`([^`\n]*)`|([^\s#'\"`][^\s#]*)?)$"
)
# A comment that is a commented-out assignment: `# KEY=...`, `#KEY=...`, or
# `# KEY: ...`.
COMMENTED = re.compile(rf"^#\s*((?:export\s+)?{KEY}\s*[=:].*)$")

# A 1Password secret reference: op://VAULT/ITEM[/SECTION]/FIELD, each part of
# the characters 1Password allows (spaces inside only), and an optional
# ?attribute= or ?ssh-format= query.
SEGMENT = r"[A-Za-z0-9._-](?:[A-Za-z0-9._ -]*[A-Za-z0-9._-])?"
OP_REFERENCE = re.compile(
    rf"^op://{SEGMENT}(?:/{SEGMENT}){{2,3}}"
    r"(?:\?(?:attribute|ssh-format)=[A-Za-z0-9._-]+)?$"
)
CIPHERTEXT = re.compile(r"^encrypted:([A-Za-z0-9+/]{120,}={0,2})$")
PUBLIC_KEY = re.compile(r"^0[23][0-9a-f]{64}$")

# The Envfile grammar this repository allows. `encrypted: false` would let a
# key be committed as plaintext; `redacted: false` would print its value in
# public CI logs; a default would be a value in the Envfile itself.
DECLARATION = re.compile(rf"^env\s+\"({KEY})\"((?:\s*,\s*[a-z]+:\s*[^,]+?)*)\s*$")
OPTION = re.compile(r"([a-z]+):\s*([^,]+?)\s*(?:,|$)")
ALLOWED_OPTIONS = {
    "redacted": {"true"},
    "encrypted": {"true"},
    "optional": {"true", "false"},
    "required": {"true", "false"},
    "type": {'"string"', '"url"', '"port"', '"number"', '"boolean"', '"email"'},
}

# Isolated from the caller's machine: no Armor (including event delivery),
# OS key store, or Bitwarden lookups (dotenvx tests these for "true"), no
# spinner, and no shell values standing in for missing ones (`check` lets the
# environment override a file).
DOTENVX_ENV = {
    "CI": "1",
    "DOTENVX_NO_ARMOR": "true",
    "DOTENVX_NO_NATIVE": "true",
    "DOTENVX_NO_BITWARDEN": "true",
}
KEPT_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME")


def is_env_file(path: str) -> bool:
    """Whether PATH is an env file the check covers."""
    parts = [part.lower() for part in PurePosixPath(path).parts]
    name = parts[-1]
    if name in NOT_ENV:
        return False
    if any(part in ENV_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in PATTERNS)


def is_envfile(path: str) -> bool:
    return PurePosixPath(path).name.lower() in ("envfile", "dotenvxfile")


def is_template(path: str) -> bool:
    return PurePosixPath(path).name.lower().endswith(TEMPLATE_SUFFIXES)


def lines(content: str) -> list[str]:
    return content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def value_problem(key: str, value: str) -> str | None:
    """Why VALUE may not be committed for KEY, or None when it may."""
    if key.startswith("DOTENV_PUBLIC_KEY"):
        return None if PUBLIC_KEY.match(value) else "is not a dotenvx public key"
    if value == "" or OP_REFERENCE.match(value):
        return None
    match = CIPHERTEXT.match(value)
    if match:
        try:
            raw = base64.b64decode(match.group(1), validate=True)
        except ValueError:  # binascii.Error is one
            raw = b""
        # An ephemeral uncompressed public key, a nonce, a tag, and data.
        if len(raw) >= 97 and raw[0] == 4:
            return None
        return "is not dotenvx ciphertext"
    return "is plaintext (use an op:// reference or dotenvx encryption)"


def parse(content: str) -> tuple[list[str], list[str]]:
    """The keys assigned in an env file, and its problems by line number.

    Messages name the key and the line, never the value.
    """
    keys: list[str] = []
    problems: list[str] = []
    for number, raw in enumerate(lines(content), 1):
        line = raw.strip()
        if not line:
            continue
        commented = False
        if line.startswith("#"):
            match = COMMENTED.match(line)
            if not match:
                continue
            commented = True
            line = match.group(1).strip()
            # A commented-out assignment is held to the same rule.
            line = re.sub(rf"^((?:export\s+)?{KEY})\s*[=:]\s*", r"\1=", line)
        match = ASSIGNMENT.match(line)
        if not match:
            what = "commented-out line" if commented else "line"
            problems.append(
                f"{what} {number} is not a comment or a KEY=VALUE line"
                " (no spaces around =, no inline comment)"
            )
            continue
        key = match.group(1)
        value = next((g for g in match.groups()[1:] if g is not None), "")
        problem = value_problem(key, value)
        if problem:
            where = "commented-out " if commented else ""
            problems.append(f"{where}{key} on line {number} {problem}")
        if not commented and key not in keys:
            keys.append(key)
    return keys, problems


def envfile_problems(envfile: str) -> list[str]:
    """Why the Envfile is outside the allowed grammar; empty when it is not."""
    problems = []
    for number, raw in enumerate(lines(envfile), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "strict true":
            continue
        match = DECLARATION.match(line)
        if not match:
            problems.append(f'{ENVFILE} line {number} is not `env "KEY", ...`')
            continue
        for name, value in OPTION.findall(match.group(2).lstrip(" ,")):
            if value not in ALLOWED_OPTIONS.get(name, ()):
                problems.append(
                    f"{ENVFILE} line {number}: {name}: {value} is not allowed"
                    if name in ALLOWED_OPTIONS
                    else f"{ENVFILE} line {number}: option {name} is not allowed"
                )
    return problems


def declared_keys(envfile: str) -> set[str]:
    return {
        m.group(1) for m in (DECLARATION.match(x.strip()) for x in lines(envfile)) if m
    }


def undeclared(keys: list[str], declared: set[str]) -> list[str]:
    return [
        key
        for key in keys
        if key not in declared and not key.startswith("DOTENV_PUBLIC_KEY")
    ]


def dotenvx_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in KEPT_ENV if name in os.environ}
    env.update(DOTENVX_ENV)
    return env


def dotenvx_problem(dotenvx: str) -> str | None:
    """Why this dotenvx cannot run the check, or None when it can."""
    try:
        result = subprocess.run(
            [dotenvx, "--version"],
            env=dotenvx_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        return f"cannot run {dotenvx}: {error.strerror}"
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
    version = tuple(int(n) for n in match.groups()) if match else None
    if version is None or not DOTENVX_MIN <= version < DOTENVX_BELOW:
        found = ".".join(map(str, version)) if version else "an unknown version"
        return (
            f"{dotenvx} is dotenvx {found}; the check needs 2.32.4 to 2.38.x,"
            " which reads an Envfile. Put the Home Manager one first on PATH,"
            " set DOTENVX, or run: nix shell --inputs-from ./nix nixpkgs#dotenvx"
        )
    return None


class Checker:
    def __init__(self, dotenvx: str, envfile: str):
        self.dotenvx = dotenvx
        self.envfile = envfile
        self.declared = declared_keys(envfile)
        self.failures: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"check-env: {message}", file=sys.stderr)

    def run(self, args: list[str], cwd: Path, stdin: bytes | None = None) -> int:
        result = subprocess.run(
            [self.dotenvx, *args],
            cwd=cwd,
            input=stdin,
            stdout=subprocess.DEVNULL if stdin is not None else None,
            env=dotenvx_env(),
        )
        return result.returncode

    def check(self, path: str, content: bytes) -> None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            self.fail(f"{path}: is not UTF-8 text")
            return
        if PurePosixPath(path).name.lower().startswith(".env.keys"):
            self.fail(f"{path}: private keys must never be committed")
            return
        keys, problems = parse(text)
        for problem in problems:
            self.fail(f"{path}: {problem}")
        spec = not is_template(path)
        if spec:
            for key in undeclared(keys, self.declared):
                self.fail(f"{path}: {key} is not declared in {ENVFILE}")
        if problems:
            # dotenvx would add nothing, and might quote the offending line.
            return
        # dotenvx reads files from disk, and protect reads the Envfile beside
        # the file or at the Git root: give it a scratch repository with only
        # the root Envfile and this file.
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=copy, check=True)
            (copy / ENVFILE).write_text(self.envfile)
            (copy / path).parent.mkdir(parents=True, exist_ok=True)
            (copy / path).write_bytes(content)
            if self.run(["protect", "--git-file", path], copy, content) != 0:
                self.fail(f"{path}: dotenvx protect refused it (see above)")
            if spec:
                args = [
                    "check",
                    "--no-1password",
                    "--no-armor",
                    "--no-native",
                    # Ciphertext cannot be decrypted without .env.keys, which
                    # is never committed; its format is checked above.
                    "--ignore",
                    "DECRYPTION_FAILED",
                    "-f",
                    path,
                ]
                if self.run(args, copy) != 0:
                    self.fail(f"{path}: does not satisfy {ENVFILE} (see above)")


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, stdout=subprocess.PIPE
    ).stdout


def names(output: bytes) -> list[str]:
    return [p for p in output.decode().split("\0") if p]


def relative(file: str, root: Path) -> str:
    """FILE relative to ROOT, or its name when it lies outside ROOT."""
    path = os.path.relpath(Path(file).resolve(), root.resolve())
    return Path(file).name if path.startswith("..") else Path(path).as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check the index")
    parser.add_argument("files", nargs="*", help="files to check, on disk")
    options = parser.parse_args(argv)
    if options.staged and options.files:
        parser.error("--staged takes no files")

    dotenvx = os.environ.get("DOTENVX") or shutil.which("dotenvx")
    if not dotenvx:
        print(
            "check-env: dotenvx not found. The agentix Home Manager profiles"
            " install it; or run: nix shell --inputs-from ./nix nixpkgs#dotenvx",
            file=sys.stderr,
        )
        return 2
    problem = dotenvx_problem(dotenvx)
    if problem:
        print(f"check-env: {problem}", file=sys.stderr)
        return 2

    files: list[str] = []
    if options.files:
        root = Path.cwd()
        selected = [(relative(f, root), Path(f).read_bytes()) for f in options.files]
        envfile_bytes = (
            (root / ENVFILE).read_bytes() if (root / ENVFILE).is_file() else None
        )
    else:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        if options.staged:
            # The index, Envfile included: what the commit will hold.
            files = names(git(root, "ls-files", "-z"))
            staged = names(git(root, "diff", "--cached", "--name-only", "-z"))
            if ENVFILE in staged:
                # A new policy applies to every env file, not just staged ones.
                listed = [p for p in files if is_env_file(p)]
            else:
                changed = git(
                    root, "diff", "--cached", "--name-only", "-z", "--diff-filter=d"
                )
                listed = [p for p in names(changed) if is_env_file(p)]
            selected = [(p, git(root, "show", f":{p}")) for p in listed]
            envfile_bytes = (
                git(root, "show", f":{ENVFILE}") if ENVFILE in files else None
            )
        else:
            files = names(
                git(
                    root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
                )
            )
            selected = [
                (p, (root / p).read_bytes())
                for p in files
                if is_env_file(p) and (root / p).is_file()
            ]
            path = root / ENVFILE
            envfile_bytes = path.read_bytes() if path.is_file() else None

    if envfile_bytes is None:
        print(f"check-env: {root / ENVFILE} is missing", file=sys.stderr)
        return 1
    envfile = envfile_bytes.decode("utf-8", "replace")

    checker = Checker(dotenvx, envfile)
    for problem in envfile_problems(envfile):
        checker.fail(problem)
    for path in files:
        if is_envfile(path) and path != ENVFILE:
            checker.fail(f"{path}: only the root {ENVFILE} may set env policy")
    if not checker.failures:
        # dotenvx quotes a malformed Envfile line, which may hold a value, so
        # it runs only on an Envfile that passed.
        for path, content in selected:
            checker.check(path, content)
    if checker.failures:
        print(
            f"check-env: {len(checker.failures)} problem(s). Commit values as"
            " op:// references or dotenvx ciphertext (`dotenvx set KEY VALUE`),"
            f" and declare each key in {ENVFILE}.",
            file=sys.stderr,
        )
        return 1
    print(f"check-env: {len(selected)} env file(s) redacted and match {ENVFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
