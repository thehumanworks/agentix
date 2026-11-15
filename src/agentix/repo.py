"""`sandbox up --repo OWNER/NAME[@REF]`: start a sandbox inside a clone.

The clone runs in the sandbox, so Git uses the sandbox's own GitHub
credentials: the image's `gh` credential helper, through 1Password. Only
`sandbox up` clones; a restored sandbox has the clone in its snapshot.

Keep this module free of heavy imports: the command line reads it on every run.
"""

import re

from .errors import InvalidRequest, OperationFailed
from .layout import WORKDIR

_SPEC = re.compile(r"(?P<owner>[A-Za-z0-9-]+)/(?P<name>[A-Za-z0-9._-]+)(@(?P<ref>.+))?")

# $1 is the URL, $2 the directory, $3 the ref or "". The directory is the
# sandbox's workdir, which Modal creates empty. Git must not prompt: there is
# no terminal, and a repository without access should fail at once. A failed
# step after the clone exits with its code in _STEPS.
CLONE = """set -eu
export GIT_TERMINAL_PROMPT=0
git clone --quiet -- "$1" "$2"
if [ -n "$3" ]; then
  git -C "$2" -c advice.detachedHead=false checkout --quiet "$3" -- || exit 3
fi
if [ -e "$2/.envrc" ]; then
  direnv allow "$2" || exit 4
fi
"""
_STEPS = {3: "check out {ref} in", 4: "run direnv allow in"}


def parse(spec: str) -> tuple[str, str, str]:
    """The clone URL, the directory under the working directory, and the ref."""
    match = _SPEC.fullmatch(spec)
    name = match["name"].removesuffix(".git") if match else ""
    if name in ("", ".", "..") or (match["ref"] or "").startswith("-"):
        raise InvalidRequest(
            f"--repo={spec} is not OWNER/NAME or OWNER/NAME@REF, as in "
            "--repo=thehumanworks/agentix@main."
        )
    url = f"https://github.com/{match['owner']}/{name}.git"
    return url, f"{WORKDIR}/{name}", match["ref"] or ""


def command(spec: str) -> list[str]:
    """The command that clones `spec` into its directory in a sandbox."""
    url, path, ref = parse(spec)
    return ["sh", "-c", CLONE, "agentix-clone", url, path, ref]


def failed(spec: str, exit_code: int, stderr: str) -> OperationFailed:
    """The error for a clone command that exited with `exit_code`."""
    # Git's own lines; a credential helper that fails explains at length.
    lines = stderr.strip().splitlines()
    git = [line for line in lines if line.startswith(("fatal:", "error:", "remote:"))]
    detail = "\n".join(git or lines[-3:]) or "no output"
    if exit_code in _STEPS:
        step = _STEPS[exit_code].format(ref=parse(spec)[2])
        return OperationFailed(
            f"Cloned {spec}, but could not {step} it, so the sandbox was "
            f"removed.\n{detail}"
        )
    return OperationFailed(
        f"Could not clone {spec}, so the sandbox was removed. Check the name "
        f"and that the sandbox's GitHub credentials can read it.\n{detail}"
    )
