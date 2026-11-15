# Choosing a replacement for python-fire

Status: proposal (phase 1: compare and recommend). Nothing here changes the CLI yet.

## The problem

python-fire makes the command line forgiving in ways that cost real sandboxes:

- A mistyped flag still runs the command. `sandbox up --nmae=dev` starts a
  sandbox under a generated name and only then exits 2 with `Cannot find key`.
  `sandbox up --no-tailscale` is ignored the same way (Fire's spelling is
  `--notailscale`).
- Values are guessed, not typed. `--tailscale=false` is the string `"false"`,
  a bare `--repo` is `True`, `--name=7` is the integer 7, `--workdir=None` reads
  as "not given".
- Every flag can also be filled by position: `sandbox up img 3` sets
  `image_name=img`, `secret_name="3"`. `sandbox prune` has to refuse `*extra`
  by hand for this reason.
- Help prints `Type: Optional[Union]` and `Default: None` for every option that
  the config file can set, and needs `sandbox up -- --help`.
- The code carries workarounds: `config.resolve` coerces strings back
  (`--image-name=7`), `repo.py` refuses a bare `--repo`, `_prune` re-checks the
  types of `--yes`, `--keep-latest` and `--name`, and `main` splits off what
  follows `--` for `sandbox shell` before Fire sees it.

## What the CLI exposes today

On main (f7e4cf6, with `sandbox prune`): 16 commands, 33 flags (29 of them
with a Fire short alias), 13 positionals. Taken from Fire's own help for every
command.

| command | positionals | flags (Fire short alias) and defaults |
| --- | --- | --- |
| `image build` | | `-f --force-build` (False), `-i --image-name` (determinate-nix:latest) |
| `flake update` | `[INPUTS]...` | `-b --build` (False), `-i --image-name` |
| `config show` | | |
| `sandbox up` | | `-i --image-name`, `-s --secret-name`, `--timeout`, `-w --workdir`, `-m --memory`, `-c --cpu`, `--tailscale`, `--no-snapshot` (all None: flag, then `[sandbox.up]` in the config file, then built-in default); `--name` (""), `-h --hostname` (""), `-e --ephemeral` (False), `-r --repo` ("") |
| `sandbox down` | `SANDBOX_ID` | |
| `sandbox exec` | `SANDBOX_ID COMMAND` | |
| `sandbox ls` | | |
| `sandbox snapshot` | `NAME` | |
| `sandbox snapshots` | `NAME` | |
| `sandbox restore` | `NAME` | `-s --snapshot` (""), `-t --tailscale` (False), `-h --hostname` ("") |
| `sandbox purge` | `NAME` | `-t --terminate` (False) |
| `sandbox prune` | (refuses any) | `-o --older-than` (required), `-k --keep-latest`, `-n --name` (glob), `-y --yes` |
| `sandbox shell` | `[NAME]`, `-- COMMAND...` | `-e --engine` (tailscale), `-h --hostname`, `-p --public-key` |
| `sandbox ssh start` | `SANDBOX_ID` | `-e --engine` (tailscale), `-h --hostname`, `-p --public-key` |
| `sandbox ssh stop` | `SANDBOX_ID` | `-e --engine` |
| `sandbox ssh status` | `SANDBOX_ID` | `-e --engine` |

In flight:

- auto-resume-on-timeout: `sandbox exec ... --resume[=COMMAND]` (`-r`; typed
  `bool | str`) and `sandbox resume NAME`.
- detached-runs: `sandbox exec --detach`, `sandbox logs RUN`, `sandbox wait RUN`
  (exits with the run's code), `sandbox runs [TARGET] [--clean]`,
  `runner deploy`.

Every flag also works today as `--a_b` and `--a-b`, as `--x=V` and `--x V`;
booleans as `--x`, `--nox`, `--x=True`/`--x=False`; positionals as
`--sandbox-id=V` (tests rely on this); help as `CMD -- --help`. A result prints
as `key: value` lines, a list as one JSON object per line.

## Candidates, measured

Each candidate got the same slice: the top level, `sandbox up` with every
option above (typed, `--tailscale/--no-tailscale`, config-backed options
defaulting to None), `sandbox ls`, and `config show`. `sandbox up` resolves
flag > config > default through `config.resolve` and prints it, so the
"no-op" runs do the real parsing work without Modal. `sandbox ls` imports the
backend (`agentix.local`, which imports Modal) and stops before the network.

Machine: this cloud container, 4 vCPU, Python 3.14.6, hyperfine 2.0.0, no
shell (`-N`). Warm: 5 warm-ups, 30 runs. Cold: 8 runs, each after deleting
every `__pycache__` in the venv and `src/` and dropping the page cache (the
first run after `uv sync`, since uv does not compile bytecode by default).
Mean wall time in ms, **warm / cold**:

| library (version) | `--help` | `sandbox up --help` | `config show` | `sandbox up` no-op | `sandbox ls` (Modal import) |
|---|---|---|---|---|---|
| fire 0.7.1 (today) | 98 / 189 | 96 / 163 | 105 / 177 | 99 / 163 | 391 / 1129 |
| cyclopts 5.5.0 | 196 / 635 | 227 / 632 | 103 / 239 | 109 / 264 | 444 / 1214 |
| typer 0.29.1 | 190 / 578 | 302 / 866 | 187 / 478 | 197 / 492 | 471 / 1547 |
| click 8.5.1 | 60 / 155 | 56 / 167 | 60 / 156 | 64 / 164 | 398 / 1161 |
| argparse (3.14 stdlib) | 51 / 82 | 51 / 81 | 68 / 90 | 63 / 89 | 369 / 1104 |

Imports alone (warm / cold): `python -c pass` 19 / 30; `import fire` 95 / 160
(Fire pulls in asyncio); `import cyclopts` 100 / 235; `import typer` 82 / 174
(then pydantic at parse time); `import click` 50 / 133; `import argparse`
23 / 61; `import agentix.__main__` 30 / 58; `import agentix.config` 45 / 75;
`import modal` 394 / 1124. The installed `agentix --help` matches the Fire
prototype (100 / 180); `uv run` adds about 33 ms on top.

Other runs: cyclopts with `help_formatter="plain", help_format="plaintext"`
cuts `sandbox up --help` to 164-170 ms warm, still 1.7x Fire. The click
prototype with the compatibility shim below costs 66-70 ms warm.

What this means:

- Modal dominates every command that does work: 390 ms warm, 1.1 s cold, then
  the network. The library is 30-80 ms of that. Our own imports are already
  lazy (`agentix.__main__` costs 11 ms over bare Python), and Modal is loaded
  only by commands that need it; no further lazy import changes that, and
  Python 3.14 has no lazy-import switch for Modal's own imports.
- So "faster" shows up in help, `config show`, and every error caught before
  Modal (a mistyped flag fails in 60 ms instead of starting a sandbox).
  click and argparse are 1.5-1.9x faster than Fire there. cyclopts is as fast
  on commands but 2x slower on help (rich + markdown rendering). typer is
  2x slower everywhere (pydantic).

## Behaviour, measured on the prototypes

| case | fire | cyclopts | typer | click | argparse |
|---|---|---|---|---|---|
| `--nmae=dev` | **runs**, then exit 2 | exit 2, "Did you mean --name?" | exit 2, suggests | exit 2, suggests | exit 2, no suggestion |
| `sandbox upp` | exit 2, lists commands | exit 2, did you mean | exit 2, did you mean | exit 2, did you mean | exit 2, "maybe you meant" |
| `--no-tailscale` | **ignored**, exit 2 after running | ok | ok | ok | ok |
| `--notailscale` | ok | exit 2 | exit 2 | exit 2 | exit 2 |
| `--tailscale=false` | refused by config.py | ok | exit 2 | exit 2 | exit 2 |
| `--tailscale=False`, `--no-snapshot=False` | ok | ok | exit 2 | exit 2 | exit 2 |
| `--timeout=abc` | traceback from config.py | exit 2, clear | exit 2 | exit 2 | exit 2 |
| bare `--repo` | `True` | exit 2 | exit 2 | exit 2 | exit 2 |
| `--secret_name=x` | ok | ok | exit 2 | exit 2 | exit 2 |
| `--workdir=None` | not given | the string "None" | "None" | "None" | "None" |
| `--timeout=3600` (= default) | source: flag | flag | flag | flag | flag |
| `sandbox up` (no flags) | default | default | default | default | default |
| `-- --help` | help | exit 2 | exit 2 | exit 2 | exit 2 |

Every candidate distinguishes "not given" (None) from "given equal to the
default", including tri-state booleans, which is what `config.resolve` needs.

## Scores

1 (poor) to 5 (best).

| | cyclopts | typer | click | argparse |
|---|---|---|---|---|
| Unknown flag/command: exit 2 + did you mean | 5 | 5 | 5 | 3 (commands only) |
| `--x/--no-x` booleans | 5 | 5 | 5 | 5 |
| Typed values | 5 | 5 | 5 | 5 |
| Not given vs. given-as-default | 5 | 5 | 5 | 5 |
| Help quality | 5 (docstring params, panels) | 3 (rich markup ate `[sandbox.up]`) | 4 | 3 |
| Legacy spellings without a shim | 4 (all but `--notailscale`, `-- --help`) | 2 | 2 | 2 |
| Speed vs. today | 2 (help 2x slower) | 1 | 5 | 5 |
| Added dependencies | 3 (docstring-parser, rich-rst; rich and attrs already there) | 1 (pydantic + pydantic-core, 7 MB; shellingham, 3 more) | 5 (none: Modal already requires click~=8.1) | 5 (stdlib) |
| Maintenance | 4 (active, one main author) | 4 (active) | 5 (Pallets) | 5 (stdlib) |
| Migration size | 4 (signatures stay, add keyword-only) | 3 | 3 (decorators per command) | 2 (a parser per command) |

## Recommendation: click

click is the only candidate that is both faster than Fire (1.5-1.9x on help,
`config show` and every early error) and fixes every listed pain: unknown
flags and commands exit 2 with a suggestion before anything starts,
`--tailscale/--no-tailscale` with a None default, real `int`/`float`, a bare
`--repo` is an error, and help shows each config-backed option as
`[default: 3600, or timeout in [sandbox.up] of the config file]`. It adds no
package (Modal already depends on it; we would declare `click>=8.1,<9`
ourselves), it is the most widely used Python CLI library, and its
`CliRunner` and `standalone_mode=False` make the CLI tests simple.

Runner-up: cyclopts. It is the most modern (type hints and docstrings define
the CLI, so `__main__.py` changes least) and accepts most Fire spellings
natively (`--secret_name`, `--tailscale=False`, `--sandbox-id=V`). The
trade-off is speed: `--help` is 2x slower than Fire today (196 vs 98 ms warm,
635 vs 189 ms cold), commands are no faster, and it adds two packages. If
"modern" matters more than "faster", pick cyclopts; the migration plan below
is the same except that the shim shrinks to two rules.

argparse is fastest but has no suggestion for a mistyped flag, needs
`allow_abbrev=False` on every parser, and needs the most code. typer is the
slowest and now pulls in pydantic.

## Migration plan (click)

One branch, one commit per step, tests green at each step.

1. Add `src/agentix/cli_compat.py` (about 60 lines, prototyped): before click
   parses, find the command the arguments name and rewrite Fire spellings for
   that command's own options only:
   `--a_b` to `--a-b`; `--nox` to `--no-x` for a known boolean;
   `--x=True|False` (any case, also 1/0, yes/no) to `--x`/`--no-x`, and
   `--x=False` on a plain switch to nothing; `CMD -- --help` to
   `CMD --help` (not for `sandbox shell`). A token it does not recognise is
   left alone, so click still rejects it.
2. Add `positional_or_flag(...)` (prototyped): each positional also accepts a
   hidden `--name=V`, so `sandbox ssh status --sandbox-id=dev` keeps working.
3. Rewrite `main()` as click groups mirroring the table above, with the same
   function bodies. Keep every Fire short alias (`-i`, `-s`, `-w`, `-m`, `-c`,
   `-h` = hostname, `-e`, `-r`, `-f`, `-b`, `-t`, `-o`, `-k`, `-n`, `-y`,
   `-p`); help stays `--help` only, as in click's default, so `-h` keeps
   meaning hostname. `--no-snapshot` becomes `--no-snapshot/--snapshot`, so a
   config file's `no_snapshot = true` can be overridden.
4. Keep the output: a `_print_result` helper (about 15 lines) reproduces
   Fire's `key: value` lines for a dict and one JSON object per line for a
   list. `main(argv)` calls click with `standalone_mode=False` so tests keep
   calling `main([...])`; usage errors print click's message and exit 2.
5. Delete the Fire workarounds: the string coercion in `config.resolve`, the
   bare-`--repo` check, the type checks and `*extra` in `_prune`, and
   `_shell_command` (`sandbox shell NAME -- CMD` keeps the split at `--`).
   Drop `fire` from pyproject.toml and the README's Fire notes and known
   limitation.
6. Tests: one table-driven test runs every spelling in "What the CLI exposes
   today" and asserts the parsed call, plus tests for each rejection (exit 2,
   suggestion, nothing started).
7. In-flight branches: `--resume` should become a plain flag plus
   `--resume-with=COMMAND`, with the shim mapping `--resume=COMMAND` to it.
   An optional-value flag is ambiguous in any parser:
   `sandbox exec dev --resume 'claude -p x'` takes the command as the resume
   value (click then exits 2, "Missing argument COMMAND"). detached-runs'
   commands are plain typed arguments and port as they are.

Spellings that change, and why:

- `--workdir=None` (and `=None` on any config-backed option): today it means
  "not given". New: refused with a message that says to leave the flag out,
  or to pass `--workdir=` for `/workdir`. Keeping it silently would make
  `None` a reserved path.
- Filling a flag by position (`sandbox up img 3`,
  `sandbox restore dev im-x`): undocumented and the cause of `_prune`'s
  `*extra`; now exit 2, "unexpected extra argument".
- Values Fire parsed as Python literals (`--name=7` as an int, `--name=[a]`
  as a list): now plain strings, or a type error with exit 2.
- Fire's own flags after `--` (`-- --trace`, `--interactive`,
  `--completion`): gone. click can generate shell completion instead.
- `--tailscale=false` (lower case), `--no-tailscale`, and a typo such as
  `--nmae` stop being errors or silent no-ops: the first two now work, the
  typo fails before anything starts.
