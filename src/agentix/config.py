"""Defaults for `sandbox up` from the user's config file.

The file is ~/.config/agentix/config.toml, or config.toml in
$XDG_CONFIG_HOME/agentix/ when that is set. A flag given on the command line
wins over the file, and the file wins over the built-in default. A missing
file changes nothing.

Keep this module free of heavy imports: the command line reads it on every
`sandbox up`.
"""

import math
import os
import tomllib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import idle
from .defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT, SECRET_NAME
from .errors import InvalidRequest

# The table of the file that holds the defaults of `sandbox up`.
UP_TABLE = ("sandbox", "up")


class ConfigError(InvalidRequest):
    """The config file cannot be read, or holds something it should not."""


@dataclass(frozen=True)
class Key:
    name: str
    types: tuple[type, ...]
    type_name: str
    default: Any
    # Checks the value further, given the value and where it came from, and
    # raises InvalidRequest if it is wrong.
    check: Callable[[Any, str], object] | None = None


# The keys of [sandbox.up], each named as its `sandbox up` flag (in the
# underscore form that help prints). Name, hostname, and --ephemeral belong
# to one sandbox, so the file cannot set them.
UP_KEYS: dict[str, Key] = {
    key.name: key
    for key in (
        Key("image_name", (str,), "string", IMAGE_NAME),
        Key("secret_name", (str,), "string", SECRET_NAME),
        Key("timeout", (int,), "integer", SANDBOX_TIMEOUT),
        Key("memory", (int,), "integer", SANDBOX_MEMORY_MIB),
        Key("cpu", (int, float), "number", 0),
        Key("workdir", (str,), "string", ""),
        Key("tailscale", (bool,), "boolean", False),
        Key("no_snapshot", (bool,), "boolean", False),
        Key("idle_stop", (str,), "duration", idle.OFF, idle.seconds),
    )
}


def path() -> Path:
    """Where the config file is, whether or not it exists."""
    # The XDG spec says to ignore a relative XDG_CONFIG_HOME.
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    if not base.is_absolute():
        base = Path("~/.config").expanduser()
    return base / "agentix" / "config.toml"


def _article(word: str) -> str:
    return "an" if word[0] in "aeiou" else "a"


def _check_type(key: Key, value: Any, where: str) -> Any:
    # TOML booleans are Python bools, which are ints too.
    if isinstance(value, bool) and bool not in key.types:
        ok = False
    else:
        ok = isinstance(value, key.types)
    if not ok:
        if bool in key.types and isinstance(value, str):
            where += " (write True or False)"
        raise ConfigError(
            f"{where}: {key.name} must be {_article(key.type_name)} {key.type_name}, "
            f"not {type(value).__name__} {value!r}."
        )
    if key.check is not None:
        try:
            key.check(value, where)
        except InvalidRequest as error:
            raise ConfigError(str(error)) from None
    if key.type_name == "number":
        value = float(value)
        if not math.isfinite(value):
            raise ConfigError(
                f"{where}: {key.name} must be a finite number, not {value!r}."
            )
    return value


def load(file: Path | None = None) -> dict[str, Any]:
    """The `sandbox up` defaults that the file sets, checked; {} without a file."""
    file = path() if file is None else file
    try:
        with file.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise ConfigError(f"Cannot read the config file {file}: {detail}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{file} is not valid TOML: {error}") from None

    table: Any = data
    prefix = ""
    for part in UP_TABLE:
        for key in table:
            if key != part:
                raise ConfigError(
                    f"{file}: unknown key {prefix}{key}. The file holds only "
                    f"[{'.'.join(UP_TABLE)}]; see `agentix config show`."
                )
        table = table.get(part, {})
        prefix += f"{part}."
        if not isinstance(table, dict):
            raise ConfigError(f"{file}: {prefix.rstrip('.')} must be a table.")

    values = {}
    for name, value in table.items():
        key = UP_KEYS.get(name)
        if key is None:
            hint = ""
            if name.replace("-", "_") in UP_KEYS:
                hint = f" Write it as {name.replace('-', '_')}."
            raise ConfigError(
                f"{file}: unknown key {prefix}{name}.{hint} "
                f"Known keys: {', '.join(UP_KEYS)}."
            )
        values[name] = _check_type(key, value, f"{file}: {prefix}{name}")
    return values


def resolve(
    flags: dict[str, Any],
    file: Path | None = None,
    ignore_file: Collection[str] = (),
) -> dict[str, tuple[Any, str]]:
    """Each `sandbox up` option's value and where it came from.

    `flags` holds what the command line gave, with None for a flag it did
    not give. The source is "flag", "config FILE", or "default". A key in
    `ignore_file` skips the file's value (it is still checked), for a flag
    that replaces that option, as --repo replaces workdir.
    """
    file = path() if file is None else file
    unknown = (set(flags) | set(ignore_file)) - set(UP_KEYS)
    if unknown:
        raise ValueError(f"Not keys of the config file: {sorted(unknown)}")
    configured = load(file)
    result = {}
    for name, key in UP_KEYS.items():
        if flags.get(name) is not None:
            value = flags[name]
            if key.types == (str,):
                # Fire reads --image-name=7 as a number.
                value = str(value)
            flag = "--" + name.replace("_", "-")
            result[name] = (_check_type(key, value, flag), "flag")
        elif name in configured and name not in ignore_file:
            result[name] = (configured[name], f"config {file}")
        else:
            result[name] = (key.default, "default")
    return result


def up_options(ignore_file: Collection[str] = (), **flags: Any) -> dict[str, Any]:
    """The values `sandbox up` runs with: flag, then config file, then default."""
    resolved = resolve(flags, ignore_file=ignore_file)
    return {name: value for name, (value, _) in resolved.items()}


def show() -> list[dict[str, Any]]:
    """One row per key: its effective value, where it came from, and its type."""
    return [
        {
            "key": name,
            "value": value,
            "source": source,
            "type": UP_KEYS[name].type_name,
        }
        for name, (value, source) in resolve({}).items()
    ]
