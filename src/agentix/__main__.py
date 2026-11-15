"""The command line. It only parses input and prints results.

Modal is slow to import, so only a command that needs it loads it.
"""

import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .defaults import IMAGE_NAME
from .errors import PublicKeyRequired


def _backend():
    from .local import Local

    return Local()


def _build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    from .image import build_image

    image = build_image(force_build=force_build, image_name=image_name)
    return {"image_id": image.object_id}


def _flake_update(*inputs: str, build: bool = False, image_name: str = IMAGE_NAME):
    """Run `nix flake update` in a sandbox and write nix/flake.lock on this host.

    Name inputs to move only those, for example `flake update claude-mods`;
    with no names every input moves except Rex, which moves only when named,
    as in `flake update rex`: it then points nix/flake.nix at the newest Rex
    snapshot. The lock is written only once the image's home profile
    evaluates with it. With `--build`, the image is built and published if an
    input moved; otherwise run `image build` afterwards.
    """
    from .flake import update
    from .image import get_app

    result = update(get_app(), image_name, inputs)
    if build and result["changed"]:
        result |= _build_image(image_name=image_name)
    return result


def _create_sandbox(
    image_name: str | None = None,
    secret_name: str | None = None,
    timeout: int | None = None,
    workdir: str | None = None,
    memory: int | None = None,
    cpu: float | None = None,
    name: str = "",
    tailscale: bool | None = None,
    hostname: str = "",
    no_snapshot: bool | None = None,
    ephemeral: bool = False,
    repo: str = "",
):
    """Start a sandbox under NAME, or under a new name such as brisk-otter.

    A flag not given takes its value from [sandbox.up] in
    ~/.config/agentix/config.toml, else the built-in default; see
    `agentix config show`.

    Its filesystem is saved when it ends, for `sandbox restore`, unless
    --no-snapshot is given. --ephemeral starts a sandbox with no name, no
    record, and no snapshot: `sandbox down ID` leaves nothing behind. With
    --tailscale, Tailscale SSH starts too, as with `sandbox ssh start`.
    --repo=OWNER/NAME[@REF] clones that GitHub repository with the sandbox's
    credentials into NAME in the image's working directory, checks out REF,
    runs `direnv allow` if it has an .envrc, and starts commands and logins
    there. It replaces --workdir.
    """
    from . import config, names, ssh_input

    options = config.up_options(
        image_name=image_name,
        secret_name=secret_name,
        timeout=timeout,
        workdir=workdir,
        memory=memory,
        cpu=cpu,
        tailscale=tailscale,
        no_snapshot=no_snapshot,
        ignore_file=("workdir",) if repo else (),
    )
    # Refuse before anything is created.
    names.check_ephemeral(name, ephemeral)
    if name:
        names.check_new_name(name)
    ssh_input.check_tailscale_start(
        options["tailscale"], hostname, options["secret_name"]
    )
    return _backend().create_sandbox(
        image_name=options["image_name"],
        secret_name=options["secret_name"],
        timeout=options["timeout"],
        workdir=options["workdir"],
        memory=options["memory"],
        cpu=options["cpu"],
        name=name,
        tailscale=options["tailscale"],
        hostname=hostname,
        exit_snapshot=not (options["no_snapshot"] or ephemeral),
        ephemeral=ephemeral,
        repo=repo,
    )


def _config_show():
    """The defaults `sandbox up` uses, and where each comes from.

    Each key is a `sandbox up` flag. A flag given on the command line wins
    over [sandbox.up] in the config file, which wins over the built-in
    default. Prints one JSON object per key, with the file's path.
    """
    from . import config

    return config.show()


def _terminate_sandbox(sandbox_id: str):
    """Stop a sandbox, given its ID or name, and record its snapshot."""
    return _backend().terminate(sandbox_id)


def _execute(sandbox_id: str, command: str, resume: bool | str = False):
    """Run COMMAND in a sandbox, given its ID or name.

    With --resume, if a named sandbox reaches its timeout while COMMAND runs,
    it is restored from its exit snapshot and COMMAND continues, as its resume
    rule says (claude resumes its own session with a neutral prompt), up
    to 3 times.
    --resume='OTHER' names the command that continues it instead. A command
    with no rule is refused. See docs/resume.md.
    """
    if resume is False:
        result = _backend().execute(sandbox_id, shlex.split(command))
        if result["exit_code"] != 0:
            raise RuntimeError(f"Failed to execute command: {result['stderr']}")
        return result["stdout"]
    result = _backend().execute_resumable(
        sandbox_id, shlex.split(command), resume, _notify, _output
    )
    _check_runs(result)


def _resume(name: str):
    """Restore NAME from its latest snapshot and continue what was cut off.

    Commands started with `sandbox exec --resume` that were still running when
    the sandbox ended continue as their resume rule says, watched as by
    `exec --resume`. With none, this is `sandbox restore NAME`.
    """
    result = _backend().resume(name, _notify, _output)
    _check_runs(result)
    return result


def _notify(message: str) -> None:
    print(f"agentix: {message}", file=sys.stderr, flush=True)


def _output(run: dict[str, Any], result: dict[str, Any]) -> None:
    sys.stdout.write(result["stdout"])
    sys.stdout.flush()
    sys.stderr.write(result["stderr"])
    # 128 is what Modal reports when the sandbox ends; the watcher says more.
    if result["exit_code"] not in (0, 128):
        _notify(
            f"`{shlex.join(run['command'])}` exited with code {result['exit_code']}."
        )


def _check_runs(result: dict[str, Any]) -> None:
    if any(run["exit_code"] != 0 for run in result.get("runs", [])):
        raise RuntimeError(
            "Not every command finished: "
            + ", ".join(
                f"`{shlex.join(run['command'])}` ({run['exit_code']})"
                for run in result.get("runs", [])
            )
        )


def _list_sandboxes():
    """Named sandboxes, running or not, and running sandboxes without a name."""
    return _backend().list_sandboxes()


def _snapshot(name: str):
    """Save the filesystem of the running sandbox of NAME now."""
    return _backend().snapshot(name)


def _snapshots(name: str):
    """The snapshots of NAME, oldest first, with the time each was taken."""
    return _backend().snapshots(name)


def _restore(
    name: str, snapshot: str = "", tailscale: bool = False, hostname: str = ""
):
    """Start a new sandbox for NAME from its latest snapshot, or from --snapshot=ID.

    The sandbox NAME has now must have ended; stop it with `sandbox down`.
    """
    return _backend().restore(name, snapshot, tailscale, hostname)


def _purge(name: str, terminate: bool = False):
    """Delete the snapshots and the record of NAME.

    A running sandbox is refused, unless --terminate stops it first.
    """
    return _backend().purge(name, terminate)


def _ssh_start(
    sandbox_id: str, engine: str = "tailscale", hostname: str = "", public_key: str = ""
):
    """Start SSH in a sandbox, given its ID or name.

    The Tailscale hostname defaults to the sandbox name, so that a restored
    sandbox keeps its address, and to the lowercase sandbox ID for a sandbox
    without a name. An explicit hostname also applies when Tailscale is
    already running.
    """
    from . import ssh_input

    ssh_input.check_arguments(engine, hostname, public_key)
    return _with_public_key(
        engine,
        public_key,
        lambda key: _backend().ssh("start", sandbox_id, engine, hostname, key),
    )


def _with_public_key(engine: str, path: str, start: Callable[[str], Any]) -> Any:
    """Call `start` with the text of the public key at `path`, for openssh.

    A listener that already runs needs no key, so a key that cannot be read
    is reported only if the sandbox asks for it.
    """
    from . import ssh_input

    key, key_problem = "", None
    if engine == "openssh":
        try:
            key = ssh_input.read_public_key(path)
        except ValueError as error:
            key_problem = error
    try:
        return start(key)
    except PublicKeyRequired:
        if key_problem is None:
            raise
        raise key_problem from None


def _shell(
    name: str = "",
    engine: str = "tailscale",
    hostname: str = "",
    public_key: str = "",
    command: Sequence[str] = (),
):
    """Log in to a running sandbox with ssh, given its name or ID.

    Without a name, the only sandbox that runs. SSH is started if it does not
    run yet, with --engine=tailscale (the default) or --engine=openssh, and
    ssh starts once the sandbox answers. Run one command instead with
    `agentix sandbox shell NAME -- COMMAND`; it gets a terminal when this one
    has one. ssh accepts only the host keys that the sandbox reports through
    Modal. A stopped sandbox is not restored: restore it first.
    """
    from . import ssh_input

    ssh_input.check_arguments(engine, hostname, public_key)
    details = _with_public_key(
        engine,
        public_key,
        lambda key: _backend().shell(name, engine, hostname, key),
    )
    argv = ["ssh", *details["ssh_options"]]
    identity = Path(public_key).expanduser().with_suffix("") if public_key else None
    if identity is not None and public_key.endswith(".pub") and identity.is_file():
        argv += ["-i", str(identity)]
    if command and sys.stdin.isatty() and sys.stdout.isatty():
        argv.append("-t")
    argv.append(details["destination"])
    if command:
        argv += ["--", *command]
    label = details["name"] or details["sandbox_id"]
    print(
        f"Connecting to {label} as {details['destination']} with {engine}. ssh "
        "trusts only host keys that the sandbox reported through Modal "
        f"({details['host_key_fingerprints'][-1]}).",
        file=sys.stderr,
    )
    if warning := details.get("warning"):
        print(f"Warning: {warning}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("ssh", argv)


def _shell_command(command: Sequence[str]) -> Callable[..., Any]:
    """`sandbox shell` for Fire, with the command that followed `--`."""

    def shell(
        name: str = "",
        engine: str = "tailscale",
        hostname: str = "",
        public_key: str = "",
    ):
        return _shell(name, engine, hostname, public_key, command)

    shell.__doc__ = _shell.__doc__
    return shell


def _ssh_stop(sandbox_id: str, engine: str = "tailscale"):
    """Stop the selected SSH engine without terminating the sandbox."""
    return _backend().ssh("stop", sandbox_id, engine)


def _ssh_status(sandbox_id: str, engine: str = "tailscale"):
    """Show SSH state and connection details for the given sandbox."""
    return _backend().ssh("status", sandbox_id, engine)


def main(argv: Sequence[str] | None = None):
    from fire import Fire

    args = list(sys.argv[1:] if argv is None else argv)
    # Fire reads what follows `--` as flags of its own. After `sandbox shell`,
    # it is the command to run in the sandbox.
    command: list[str] = []
    if args[:2] == ["sandbox", "shell"] and "--" in args:
        split = args.index("--")
        args, command = args[:split], args[split + 1 :]
    Fire(
        {
            "image": {
                "build": _build_image,
            },
            "flake": {
                "update": _flake_update,
            },
            "config": {
                "show": _config_show,
            },
            "sandbox": {
                "up": _create_sandbox,
                "down": _terminate_sandbox,
                "exec": _execute,
                "ls": _list_sandboxes,
                "snapshot": _snapshot,
                "snapshots": _snapshots,
                "restore": _restore,
                "resume": _resume,
                "purge": _purge,
                "shell": _shell_command(command),
                "ssh": {
                    "start": _ssh_start,
                    "stop": _ssh_stop,
                    "status": _ssh_status,
                },
            },
        },
        command=args,
    )


if __name__ == "__main__":
    os.environ["PAGER"] = "cat"
    main()
