#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) != 0 ]]; then
  echo "Run ts as root inside the Linux sandbox." >&2
  exit 1
fi

umask 077
state_dir=${TAILSCALE_STATE_DIR:-$HOME/.local/state/agentix/tailscale}
runtime_dir=/run/agentix-tailscale
socket=$runtime_dir/tailscaled.sock
command=${1:-up}
if (( $# > 0 )); then shift; fi

if [[ $command != up ]]; then
  exec tailscale --socket="$socket" "$command" "$@"
fi
if (( $# > 1 )); then
  echo "Usage: ts up [hostname] | status | ip -4 | ssh user@host" >&2
  exit 2
fi

mkdir -p "$state_dir" "$runtime_dir"
chmod 700 "$state_dir" "$runtime_dir"
# Serialize starts, including concurrent invocations from separate terminals.
exec 9>"$runtime_dir/start.lock"
flock -w 60 9

status() {
  tailscale --socket="$socket" status --json 2>/dev/null || true
}

if ! status | jq -e '.BackendState' >/dev/null 2>&1; then
  # Do not pass the 1Password token or shell startup hooks into the daemon.
  nohup env -i PATH="$PATH" HOME="$HOME" USER=root LOGNAME=root \
    tailscaled --tun=userspace-networking --socket="$socket" \
    --statedir="$state_dir" --state="$state_dir/tailscaled.state" \
    >>"$state_dir/daemon.log" 2>&1 </dev/null 9>&- &
  daemon_pid=$!
  ready=false
  for (( attempt=0; attempt<50; attempt++ )); do
    if status | jq -e '.BackendState' >/dev/null 2>&1; then
      ready=true
      break
    fi
    if ! kill -0 "$daemon_pid" 2>/dev/null; then break; fi
    sleep 0.2
  done
  if [[ $ready != true ]]; then
    echo "Tailscale daemon did not become ready. See $state_dir/daemon.log" >&2
    exit 1
  fi
fi

backend=$(status | jq -r '.BackendState')
up_args=(--ssh --accept-dns=false)
if (( $# == 1 )); then up_args+=(--hostname="$1"); fi

case "$backend" in
  NeedsLogin|NoState)
    # Only enrollment needs 1Password. Existing identities work without it.
    auth_ref=${TAILSCALE_AUTHKEY:-op://agentix/tailscale/auth/key}
    if [[ $auth_ref == op://* ]]; then
      # The credential helper may start a cache daemon; it must not inherit
      # the startup lock, or later starts will block after this command exits.
      TAILSCALE_AUTHKEY=$(op read "$auth_ref" 9>&-)
    else
      TAILSCALE_AUTHKEY=$auth_ref
    fi
    export -n TAILSCALE_AUTHKEY
    if [[ -z $TAILSCALE_AUTHKEY ]]; then
      echo "The Tailscale auth key is empty." >&2
      exit 1
    fi
    # Pass the key through an inherited descriptor, never argv or a disk file.
    tailscale --socket="$socket" up --timeout=30s \
      "${up_args[@]}" --auth-key=file:/dev/fd/3 3<<<"$TAILSCALE_AUTHKEY"
    unset TAILSCALE_AUTHKEY auth_ref
    ;;
  Running)
    tailscale --socket="$socket" set "${up_args[@]}"
    ;;
  Stopped|Starting)
    tailscale --socket="$socket" up --timeout=30s "${up_args[@]}"
    ;;
  *)
    echo "Tailscale needs attention: $backend. Run ts status." >&2
    exit 1
    ;;
esac

status | jq -er '
  select(.BackendState == "Running") |
  "MagicDNS: \(.Self.DNSName | rtrimstr("."))",
  "IPv4: \(.TailscaleIPs[] | select(contains(":") | not))"
'
