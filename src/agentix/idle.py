"""Stop a sandbox by itself once nobody uses it. See docs/idle-stop.md.

`sandbox up --idle-stop=20m` makes the sandbox's main command a watcher,
WATCHER below, instead of `sleep infinity`. The watcher looks at the
sandbox's processes every few seconds. Once none but its own and a few idle
daemons has run for the whole duration, it exits with IDLE_EXIT_CODE. A
sandbox ends when its main command ends, and Modal then saves its exit
snapshot as it does for `sandbox down`, so `sandbox restore` continues it.

Keep this module free of heavy imports: the command line reads it on every
`sandbox up`.
"""

import posixpath
from collections.abc import Sequence

from . import durations
from .errors import InvalidRequest
from .layout import READY_FILE

# The exit code of the watcher, which Modal reports as the sandbox's.
IDLE_EXIT_CODE = 75
# Tags a sandbox started with an idle stop, with the seconds, for listings.
IDLE_TAG = "agentix-idle-stop"
# Turns off an idle stop that the config file sets.
OFF = "off"
# Shorter would stop a sandbox between `sandbox up` and the first login.
MINIMUM = 60
# The watcher looks this often at most, and at least.
_LONGEST_POLL = 30
_SHORTEST_POLL = 5

# In /run, a tmpfs, so that no snapshot keeps them. The start command makes
# FLAG when the sandbox has an idle stop; while it is there, each command
# that agentix starts leaves a file named MARKS and its PID, which the
# watcher removes when it sees it. runner.py spells these paths too.
RUN_DIR = posixpath.dirname(READY_FILE)
FLAG = f"{RUN_DIR}/idle-stop"
MARKS = f"{RUN_DIR}/used."
# OpenSSH's log, where sshd notes each login on a line that starts with
# "Accepted METHOD for". A client cannot write the start of a line, so a
# scanner cannot fake one. The path is the one scripts/openssh-ssh.sh
# starts sshd with.
SSHD_LOG = "/root/.local/state/agentix/openssh/sshd.log"
# Runs as `sh -c EXEC_WRAPPER agentix-exec COMMAND...`: leaves a mark, then
# becomes COMMAND, so its output, exit code, and PID are COMMAND's. A first
# word that starts with - is refused as Modal refused it before (bash's exec
# would read it as an option, and dash's exec has no --). The shell adds PWD
# to the environment, which Modal's commands lack, so it drops it. bash also
# adds SHLVL=0 at every exec, which no unset prevents; that one stays. `true`,
# not `:`: a failed redirection of a special builtin would end the shell.
EXEC_WRAPPER = f"""[ -e {FLAG} ] && {{ true > "{MARKS}$$"; }} 2>/dev/null
case $1 in
  -*) echo "agentix-exec: $1: not found" >&2; exit 127 ;;
esac
unset PWD
exec "$@"
"""


def wrap(args: Sequence[str]) -> list[str]:
    """`args` as agentix runs it in a sandbox, so that the watcher sees it."""
    return ["sh", "-c", EXEC_WRAPPER, "agentix-exec", *args]


# Runs as `sh -c WATCHER agentix-idle MODE SECONDS POLL PROBE MARKS SSHLOG
# [PROC]`.
# MODE "watch" exits IDLE_EXIT_CODE once nothing has run for SECONDS, looking
# every POLL seconds; "check" prints busy or idle once. PROBE is the argv,
# joined by spaces, of Modal's readiness probe, which Modal runs now and then.
# MARKS is the prefix of the files that EXEC_WRAPPER leaves; SSHLOG is
# SSHD_LOG; PROC is /proc, or a stand-in for tests.
#
# A look is busy if a mark is there, or if OpenSSH logged a login since the
# last look, so that a command or an SSH session (ssh HOST CMD, scp, sftp,
# rsync, git) shorter than POLL is seen too; or if a process runs that
# counts. Tailscale SSH logs nowhere the watcher reads, so only its sessions'
# processes count. Every process counts, except:
# PID 1 (Modal's agent) and kernel threads; the watcher and what it starts;
# zombies; the SSH listeners, which run whether or not anyone is logged in
# (sshd that PID 1 holds, and tailscaled, but not its `be-child` session
# processes); and agents that a login leaves behind. Names are matched on the
# kernel's name and on argv[0], because Nix wraps some programs, so that
# tailscaled runs as `.tailscaled-wra`. Commands from `sandbox exec`, with or
# without --resume, and the commands of detached runs are children of PID 1,
# and SSH sessions are children of a listener, so each counts while it runs.
# A process that cannot be read counts, so a fault keeps the sandbox up.
#
# The clock is /proc/uptime, which a wall clock change does not move.
WATCHER = r"""set -u
mode=$1 limit=$2 interval=$3 probe="$4 " marks=$5 sshlog=$6 proc=${7:-/proc}
self=$$

marked() {
  set -- "$marks"*
  [ -e "$1" ] || return 1
  if [ "$mode" = watch ]; then rm -f "$@"; fi
  return 0
}

# How many logins OpenSSH has logged; 0 before it has a log.
logins() {
  count=$(grep -c '^Accepted [a-z-]* for ' "$sshlog" 2>/dev/null)
  count=${count:-0}
}

logged_in() {
  logins
  [ "$count" = "$seen" ] && return 1
  seen=$count
  return 0
}

busy() {
  marked && return 0
  logged_in && return 0
  for dir in "$proc"/[0-9]*; do
    pid=${dir##*/}
    [ "$pid" = 1 ] && continue
    [ "$pid" = "$self" ] && continue
    line=
    { IFS= read -r line < "$dir/stat"; } 2>/dev/null
    # It ended since the listing.
    [ -n "$line" ] || continue
    comm=${line#*\(}
    comm=${comm%\)*}
    # shellcheck disable=SC2086
    set -- ${line##*\) }
    state=$1 ppid=$2
    [ "$state" = Z ] && continue
    [ "$pid" = 2 ] || [ "$ppid" = 2 ] && continue
    [ "$ppid" = "$self" ] && continue
    args=$({ tr '\0' ' ' < "$dir/cmdline"; } 2>/dev/null) || return 0
    [ -n "$args" ] || return 0
    [ "$args" = "$probe" ] && continue
    # The kernel's name and argv[0], without what Nix's wrappers add.
    c=${comm#.}
    c=${c%-wrapped}
    c=${c%-wra}
    n=${args%% *}
    n=${n##*/}
    n=${n#.}
    n=${n%-wrapped}
    for x in "$c" "$n"; do
      case $x in
        ssh-agent|gpg-agent|keyboxd|scdaemon|dirmngr) continue 2 ;;
        sshd) [ "$ppid" = 1 ] && continue 2 ;;
        tailscaled) case $args in *be-child*) ;; *) continue 2 ;; esac ;;
      esac
    done
    return 0
  done
  return 1
}

clock() {
  read -r up rest < /proc/uptime
  now=${up%.*}
}

logins
seen=$count
if [ "$mode" = check ]; then
  if busy; then echo busy; else echo idle; fi
  exit 0
fi
# Idle time counts from the first look that saw nothing, so the stop comes
# between SECONDS and SECONDS + 2 * POLL after the last use (a mark or a
# login is seen at the look after it), never sooner.
clock
since=$now
while :; do
  sleep "$interval"
  clock
  if busy; then
    since=
  elif [ -z "$since" ]; then
    since=$now
  elif [ $((now - since)) -ge "$limit" ]; then
    echo "agentix: nothing ran for ${limit}s; stopping the sandbox" >&2
    exit @IDLE_EXIT_CODE@
  fi
done
""".replace("@IDLE_EXIT_CODE@", str(IDLE_EXIT_CODE))


def seconds(text: object, flag: str = "--idle-stop") -> int:
    """The idle stop of `text`, such as 20m or 1h30m, in seconds; 0 for off."""
    if isinstance(text, str) and text.strip().lower() == OFF:
        return 0
    if isinstance(text, bool) or not isinstance(text, str):
        raise InvalidRequest(
            f"Invalid {flag}: {text!r}. Give a duration such as 20m or 1h, or {OFF}."
        )
    value = int(durations.parse(text, flag).total_seconds())
    if value < MINIMUM:
        raise InvalidRequest(
            f"Invalid {flag}: {text!r}. It must be at least {MINIMUM // 60}m, "
            "so that a new sandbox is not stopped before the first login."
        )
    return value


def poll_interval(limit: int) -> int:
    """How often the watcher looks: a sixth of `limit`, from 5 s to 30 s."""
    return max(_SHORTEST_POLL, min(_LONGEST_POLL, limit // 6))


def describe(limit: int) -> str:
    """`limit` as `sandbox ls` shows it, such as 20m or 1h30m; "off" for 0."""
    if not limit:
        return OFF
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, limit = divmod(limit, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)
