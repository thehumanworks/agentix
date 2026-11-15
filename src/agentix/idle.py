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

from . import durations
from .errors import InvalidRequest

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

# Runs as `sh -c WATCHER agentix-idle MODE SECONDS POLL PROBE [PROC]`. MODE
# "watch" exits IDLE_EXIT_CODE once nothing has run for SECONDS, looking every
# POLL seconds; "check" prints busy or idle once. PROBE is the argv, joined by
# spaces, of Modal's readiness probe, which Modal runs now and then. PROC is
# /proc, or a stand-in for tests.
#
# Every process counts, except: PID 1 (Modal's agent) and kernel threads;
# the watcher and what it starts; zombies; the SSH listeners, which run
# whether or not anyone is logged in (sshd that PID 1 holds, and tailscaled,
# but not its `be-child` session processes); and agents that a login leaves
# behind. Commands from `sandbox exec`, with or without --resume, and the
# commands of detached runs are children of PID 1, and SSH sessions are
# children of a listener, so each counts while it runs.
#
# The clock is /proc/uptime, which a wall clock change does not move.
WATCHER = r"""set -u
mode=$1 limit=$2 interval=$3 probe="$4 " proc=${5:-/proc}
self=$$

busy() {
  for dir in "$proc"/[0-9]*; do
    pid=${dir##*/}
    [ "$pid" = 1 ] && continue
    [ "$pid" = "$self" ] && continue
    line=
    { IFS= read -r line < "$dir/stat"; } 2>/dev/null
    [ -n "$line" ] || continue
    comm=${line#*\(}
    comm=${comm%\)*}
    # shellcheck disable=SC2086
    set -- ${line##*\) }
    state=$1 ppid=$2
    [ "$state" = Z ] && continue
    [ "$pid" = 2 ] || [ "$ppid" = 2 ] && continue
    [ "$ppid" = "$self" ] && continue
    case $comm in
      ssh-agent|gpg-agent|keyboxd|scdaemon|dirmngr) continue ;;
      sshd) [ "$ppid" = 1 ] && continue ;;
    esac
    args=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
    [ -n "$args" ] || continue
    [ "$args" = "$probe" ] && continue
    if [ "$comm" = tailscaled ]; then
      case $args in *be-child*) ;; *) continue ;; esac
    fi
    return 0
  done
  return 1
}

clock() {
  read -r up rest < /proc/uptime
  now=${up%.*}
}

if [ "$mode" = check ]; then
  if busy; then echo busy; else echo idle; fi
  exit 0
fi
clock
last=$now
while :; do
  sleep "$interval"
  clock
  if busy; then
    last=$now
  elif [ $((now - last)) -ge "$limit" ]; then
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
