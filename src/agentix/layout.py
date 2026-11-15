"""Paths that the command line and the sandbox image agree on.

They are defined once, in nix/sandbox.toml. The Home Manager modules read the
same file as the agentix.sandbox.* options and write the paths, with the
commands the image provides, into a manifest in the image.

Keep this module free of heavy imports: the command line reads it on every run.
"""

import tomllib
from pathlib import Path

NIX_DIR = Path(__file__).resolve().parents[2] / "nix"

_SHARED = tomllib.loads((NIX_DIR / "sandbox.toml").read_text())

# Every path below, as the manifest in the image lists them.
PATHS: dict[str, str] = _SHARED["paths"]
# The default working directory of sandboxes.
WORKDIR = PATHS["workdir"]
# Modal gives the 1Password token only to the processes it starts. SSH sessions
# do not have it, so the image's `op` wrapper reads it from this root-only file.
OP_TOKEN_FILE = PATHS["opTokenFile"]
# SSH logins read this to start in the override instead of WORKDIR.
WORKDIR_FILE = PATHS["workdirFile"]
# The last thing the start command of a sandbox writes.
READY_FILE = PATHS["readyFile"]
# The manifest, relative to the Home Manager profile.
MANIFEST = _SHARED["manifest"]
