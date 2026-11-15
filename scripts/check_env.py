#!/usr/bin/env python3
"""Fail when a committed .env file holds a plaintext value.

The repository tracks .env, so every value in it must be a 1Password
reference (op://...) or dotenvx ciphertext (encrypted:...). The Envfile at the
repository root declares the keys. For each env file this runs:

- `dotenvx protect --git-file`, the check dotenvx's own Git filter runs. It
  allows empty values, op:// and bw:// references, ciphertext, and
  DOTENV_PUBLIC_KEY, refuses anything else, and refuses .env.keys files.
- for files other than templates (*.example, *.sample, *.template): every key
  must be declared in the Envfile, and `dotenvx check` must pass, so a
  declared key cannot go missing.

Usage:
  scripts/check_env.py            tracked and untracked, not ignored, files
  scripts/check_env.py --staged   the staged versions (the pre-commit hook)
  scripts/check_env.py FILE...    the named files, as they are on disk

dotenvx comes from PATH, or from $DOTENVX. No value is printed: dotenvx names
the offending keys and redacts values, and this script reads only key names.
Python 3.9 or newer, standard library only, so it runs on a Mac's python3.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ENVFILE = "Envfile"

# The files dotenvx's Git filter covers (src/lib/helpers/precommitEnvPatterns.js
# in dotenvx 2.38), less direnv's .envrc, which is a shell script.
PATTERNS = (".env*", "*.env", ".flaskenv", ".dev.vars*")
NOT_ENV = (".envrc",)
TEMPLATE_SUFFIXES = (".example", ".sample", ".template")

# One assignment, as dotenv parses it (lib/main.js LINE in motdotla/dotenv):
# optional `export`, a key, `=` or `: `, then a quoted value that may span
# lines, or an unquoted one up to a comment.
LINE = re.compile(
    r"^\s*(?:export\s+)?([\w.-]+)(?:\s*=\s*?|:\s+?)"
    r"(\s*'(?:\\'|[^'])*'|\s*\"(?:\\\"|[^\"])*\"|\s*`(?:\\`|[^`])*`|[^#\r\n]+)?"
    r"\s*(?:#.*)?$",
    re.MULTILINE,
)
DECLARATION = re.compile(r"^\s*env\s+[\"']([^\"']+)[\"']", re.MULTILINE)

# Isolated from the caller's machine: no Armor, OS key store, or Bitwarden
# lookups, no 1Password resolution, and no shell values standing in for
# missing ones (`check` lets the environment override a file).
DOTENVX_ENV = {
    "CI": "1",
    "DOTENVX_NO_ARMOR": "1",
    "DOTENVX_NO_NATIVE": "1",
    "DOTENVX_NO_BITWARDEN": "1",
}
KEPT_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME")


def is_env_file(path: str) -> bool:
    """Whether dotenvx's Git filter would cover PATH."""
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name in NOT_ENV:
        return False
    if len(parts) > 1 and parts[-2] == ".env.d":
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in PATTERNS)


def is_template(path: str) -> bool:
    return PurePosixPath(path).name.endswith(TEMPLATE_SUFFIXES)


def env_keys(content: str) -> list[str]:
    """The keys assigned in an env file, in order, without duplicates."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    keys = []
    for match in LINE.finditer(text):
        if match.group(1) not in keys:
            keys.append(match.group(1))
    return keys


def declared_keys(envfile: str) -> set[str]:
    """The keys an Envfile declares with `env "KEY"`, in any block."""
    return set(DECLARATION.findall(envfile))


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


class Checker:
    def __init__(self, dotenvx: str, root: Path):
        self.dotenvx = dotenvx
        self.root = root
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

    def protect(self, path: str, content: bytes) -> None:
        if self.run(["protect", "--git-file", path], self.root, content) != 0:
            self.fail(f"{path}: plaintext value or private key (see above)")

    def spec(self, path: str, content: bytes, envfile: str) -> None:
        keys = env_keys(content.decode("utf-8", "replace"))
        for key in undeclared(keys, declared_keys(envfile)):
            self.fail(f"{path}: {key} is not declared in {ENVFILE}")
        # `check` reads files from disk; give it the content being checked,
        # at the same path beside a copy of the Envfile.
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory)
            (copy / ENVFILE).write_text(envfile)
            (copy / path).parent.mkdir(parents=True, exist_ok=True)
            (copy / path).write_bytes(content)
            args = [
                "check",
                "--no-1password",
                "--no-armor",
                "--no-native",
                # Ciphertext cannot be decrypted without .env.keys, which is
                # never committed; its presence is what the check needs.
                "--ignore",
                "DECRYPTION_FAILED",
                "-f",
                path,
            ]
            if self.run(args, copy) != 0:
                self.fail(f"{path}: does not satisfy {ENVFILE} (see above)")

    def check(self, path: str, content: bytes, envfile: str) -> None:
        self.protect(path, content)
        if not is_template(path) and not PurePosixPath(path).name.startswith(
            ".env.keys"
        ):
            self.spec(path, content, envfile)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, stdout=subprocess.PIPE
    ).stdout


def paths(output: bytes) -> list[str]:
    return [p for p in output.decode().split("\0") if p and is_env_file(p)]


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
            " install it; or run: nix shell nixpkgs#dotenvx",
            file=sys.stderr,
        )
        return 2

    if options.files:
        root = Path.cwd()
        selected = [(relative(f, root), Path(f).read_bytes()) for f in options.files]
    else:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        if options.staged:
            staged = git(root, "diff", "--cached", "--name-only", "-z")
            names = staged.decode().split("\0")
            if ENVFILE in names:
                # A new policy applies to every env file, not just staged ones.
                listed = paths(git(root, "ls-files", "-z"))
            else:
                listed = paths(
                    git(
                        root, "diff", "--cached", "--name-only", "-z", "--diff-filter=d"
                    )
                )
            selected = [(p, git(root, "show", f":{p}")) for p in listed]
        else:
            listed = paths(
                git(
                    root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
                )
            )
            selected = [
                (p, (root / p).read_bytes()) for p in listed if (root / p).is_file()
            ]

    envfile_path = root / ENVFILE
    if not envfile_path.is_file():
        print(f"check-env: {envfile_path} is missing", file=sys.stderr)
        return 1
    envfile = envfile_path.read_text()

    checker = Checker(dotenvx, root)
    for path, content in selected:
        checker.check(path, content, envfile)
    if checker.failures:
        print(
            f"check-env: {len(checker.failures)} problem(s). Commit values as"
            " op:// references or with `dotenvx set KEY VALUE` (encrypted),"
            f" and declare each key in {ENVFILE}.",
            file=sys.stderr,
        )
        return 1
    print(f"check-env: {len(selected)} env file(s) redacted and match {ENVFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
