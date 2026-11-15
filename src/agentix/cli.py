"""The click layer of the command line: a command tree built from plain functions.

`__main__` keeps one plain function per command and a table that names its
arguments with `arg`, `opt`, and `switch`. This module turns the table into
click commands, accepts the spellings the old python-fire command line took
(see `legacy_args`), and prints results as Fire did.
"""

import json
import sys
from collections.abc import Callable, Sequence
from types import GeneratorType
from typing import Any

import click

try:  # Wrap help at spaces only, so that --no-snapshot stays whole.
    from click._textwrap import TextWrapper

    TextWrapper.wordsep_re = TextWrapper.wordsep_simple_re
except ImportError:  # pragma: no cover
    pass

# Values a boolean flag that Fire typed (`--tailscale=False`) still takes.
BOOLEAN_WORDS = {
    "true": True,
    "false": False,
    "1": True,
    "0": False,
    "yes": True,
    "no": False,
}


class NoneIsNotGiven(click.ParamType):
    """A value where the literal None means "not given", as it did with Fire."""

    def __init__(self, inner: click.ParamType):
        self.inner = inner
        self.name = inner.name

    def convert(self, value, param, ctx):
        if value == "None":
            return None
        return self.inner.convert(value, param, ctx)


def arg(name: str, *short: str, required: bool = True, nargs: int = 1) -> list:
    """A positional argument. Except a variadic one, it is also --NAME=VALUE.

    Fire took every positional as a flag too, as `--sandbox-id=sb-...`.
    """
    metavar = name.upper()
    if nargs != 1:
        return [click.Argument([name], nargs=nargs, metavar=f"[{metavar}]...")]
    shown = metavar if required else f"[{metavar}]"
    argument = click.Argument([name], required=False, metavar=shown)
    argument.positional_required = required
    flag = click.Option(
        ["--" + name.replace("_", "-"), *short, name + "__flag"], hidden=True
    )
    return [argument, flag]


def opt(name: str, *short: str, type: Any = str, default: Any = "", **attrs) -> list:
    """An option that takes a value, as --NAME=VALUE or --NAME VALUE.

    A default of None marks an option that the config file can set: None
    means "not given", and so does the literal value None, as with Fire.
    """
    kind = click.types.convert_type(type)
    if default is None:
        kind = NoneIsNotGiven(kind)
    return [click.Option([name, *short], type=kind, default=default, **attrs)]


def switch(
    name: str,
    *short: str,
    off: str = "",
    default: bool | None = False,
    values: str = "words",
    **attrs,
) -> list:
    """A boolean flag, with an off spelling such as --no-tailscale if `off` names it.

    Fire took `--NAME=True` and `--NAME=False` (and `--NAME True`), so these
    still work. With `values="words"` it also takes true/false, 1/0 and
    yes/no in any case; with `values="literal"` only True and False, and
    click refuses any other value.
    """
    decls = [f"{name}/{off}" if off else name, *short]
    option = click.Option(decls, is_flag=True, default=default, **attrs)
    option.legacy_values = values
    return [option]


def command(
    function: Callable[..., Any],
    *params: list,
    call: Callable[..., Any] | None = None,
    help: str | None = None,
) -> click.Command:
    """A click command that calls `function` (or `call`) with the parsed values."""
    flat = [param for group in params for param in group]
    required = {
        p.name: p.positional_required for p in flat if hasattr(p, "positional_required")
    }
    positional = list(required)

    def callback(**values: Any) -> Any:
        given = [values.pop(name) for name in positional]
        flagged = {name: values.pop(name + "__flag") for name in positional}
        free = iter([value for value in given if value is not None])
        for name in positional:
            value = flagged[name] if flagged[name] is not None else next(free, None)
            if value is None:
                if required[name]:
                    raise click.UsageError(f"Missing argument '{name.upper()}'.")
                value = ""
            values[name] = value
        extra = list(free)
        if extra:
            raise click.UsageError(f"Got unexpected extra argument ({' '.join(extra)})")
        return (call or function)(**values)

    uses_h = any("-h" in getattr(p, "opts", ()) for p in flat)
    return click.Command(
        None,
        params=flat,
        callback=callback,
        help=help if help is not None else function.__doc__,
        context_settings=_help_settings(uses_h),
    )


def _help_settings(uses_h: bool) -> dict[str, Any]:
    # Fire took -h for help, except where a flag such as --hostname took it.
    names = ["--help"] if uses_h else ["-h", "--help"]
    return {"help_option_names": names, "max_content_width": 100}


class Group(click.Group):
    """Commands listed in the order given, and help with exit 0 when none is named.

    Fire did both.
    """

    def __init__(self, help: str, commands: dict[str, click.Command]):
        super().__init__(
            commands=commands,
            help=help,
            callback=self._without_command,
            invoke_without_command=True,
            no_args_is_help=False,
            context_settings=_help_settings(False),
        )

    @staticmethod
    def _without_command() -> None:
        ctx = click.get_current_context()
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)


def group(help: str, commands: dict[str, click.Command]) -> Group:
    return Group(help, commands)


def _resolve(root: click.Group, args: list[str]) -> tuple[click.Command, int]:
    """The command that `args` name, and how many of them name it."""
    command: click.Command = root
    i = 0
    while (
        isinstance(command, click.Group)
        and i < len(args)
        and args[i] in command.commands
    ):
        command, i = command.commands[args[i]], i + 1
    return command, i


def _context(root: click.Group, path: list[str]) -> click.Context:
    """A context for the command that `path` names, for a usage message."""
    command: click.Command = root
    ctx = click.Context(root, info_name="agentix", **root.context_settings)
    for name in path:
        command = command.commands[name]  # type: ignore[attr-defined]
        ctx = click.Context(
            command, parent=ctx, info_name=name, **command.context_settings
        )
    return ctx


def legacy_args(root: click.Group, args: Sequence[str]) -> list[str]:
    """Rewrite the spellings python-fire took into ones click takes.

    Only options of the command that `args` name are rewritten; anything
    else is left for click to accept or refuse. The rewrites:
    `--a_b` to `--a-b`; `--noX` to the off spelling of a boolean X;
    `--X=True|False` (and `--X True|False`) for a boolean that took values;
    `-x=V` to `-x V`; `--resume=CMD` to `--resume-with=CMD`; and
    `COMMAND -- --help` to `COMMAND --help`.
    """
    args = list(args)
    command, depth = _resolve(root, args)
    head, rest = args[:depth], args[depth:]
    tail: list[str] = []
    if "--" in rest:
        split = rest.index("--")
        rest, tail = rest[:split], rest[split:]
        if tail[1:] in (["--help"], ["-h"]):
            rest, tail = rest + ["--help"], []
    options: dict[str, click.Option] = {}
    for param in command.params:
        if isinstance(param, click.Option):
            for spelling in param.opts + param.secondary_opts:
                options[spelling] = param

    def toggle(option: click.Option, on: bool) -> list[str]:
        if on:
            return [option.opts[0]]
        if option.secondary_opts:
            return [option.secondary_opts[0]]
        return []  # A plain switch that is off is a switch not given.

    out: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        i += 1
        if not token.startswith("-") or token == "-" or token.startswith("---"):
            out.append(token)
            continue
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            name = "--" + name[2:].replace("_", "-")
        elif len(token) > 2 and token[2] == "=":
            name, eq, value = token[:2], "=", token[3:]
        elif len(token) == 2 and token in options:
            name, eq, value = token, "", ""
        else:
            out.append(token)
            continue
        option = options.get(name)
        on = True
        if option is None and name.startswith("--no") and not name.startswith("--no-"):
            option = options.get("--" + name[4:])
            if option is not None and option.is_flag:
                on = False
            else:
                option = None
        if option is None:
            out.append(name + eq + value)
            continue
        if name in option.secondary_opts:
            on = not on
        if not option.is_flag:
            if not eq:
                out.append(name)
            elif name.startswith("--"):
                out.append(f"{name}={value}")
            else:
                out += [name, value]
            if not eq and i < len(rest):
                out.append(rest[i])  # its value, kept as it is
                i += 1
            continue
        alias = getattr(option, "value_alias", "")
        if eq and alias and value not in ("True", "False"):
            out.append(f"{alias}={value}")
            continue
        mode = getattr(option, "legacy_values", "")
        if eq and mode != "words" and value not in ("True", "False"):
            out.append(name + eq + value)  # for click to refuse
            continue
        if eq:
            word = BOOLEAN_WORDS.get(value.lower())
            if word is None:
                raise click.UsageError(
                    f"{name} takes True or False, or no value, not {value!r}.",
                    ctx=_context(root, head),
                )
            out += toggle(option, word == on)
        elif i < len(rest) and rest[i] in ("True", "False"):
            out += toggle(option, (rest[i] == "True") == on)
            i += 1
        else:
            out += toggle(option, on)
    return head + out + tail


def run(root: click.Group, args: Sequence[str]) -> None:
    """Parse `args`, call the command, and print what it returns, as Fire did.

    A usage error prints click's message and exits 2. An error that the
    command raises is not caught here.
    """
    try:
        with root.make_context("agentix", legacy_args(root, args)) as ctx:
            result = root.invoke(ctx)
    except click.exceptions.Exit as done:
        if done.exit_code:
            sys.exit(done.exit_code)
        return
    except click.ClickException as error:
        error.show()
        sys.exit(error.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted!", err=True)
        sys.exit(1)
    print_result(result)


def print_result(result: Any) -> None:
    """Print a command's result as python-fire did.

    A dict prints as `key: value` lines, aligned, without keys that start
    with `_`; a list as one line per item, JSON where it can be; None as
    nothing; anything else as its str.
    """
    if result is None:
        return
    if isinstance(result, (list, set, frozenset, GeneratorType)):
        for item in result:
            print(_one_line(item))
    elif isinstance(result, dict):
        visible = {
            key: value
            for key, value in result.items()
            if not (isinstance(key, str) and key.startswith("_"))
        }
        if not visible:
            print("{}")
            return
        width = max(len(str(key)) for key in visible) + 1
        print(
            "\n".join(
                f"{f'{key}:':{width}s} {_one_line(value)}"
                for key, value in visible.items()
            )
        )
    elif isinstance(result, tuple):
        print(_one_line(result))
    else:
        print(result)


def _one_line(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\n", " ")
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError, ValueError:
        return str(value).replace("\n", " ")
