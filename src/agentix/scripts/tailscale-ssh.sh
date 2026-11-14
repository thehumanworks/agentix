#!/usr/bin/env bash
set -euo pipefail

action=$1
hostname=${2:-}
socket=/run/agentix-tailscale/tailscaled.sock

read_status() {
  if [[ ! -S $socket ]]; then
    data='{"BackendState":"Stopped","TailscaleIPs":[]}'
    enabled=false
    return
  fi
  data=$(tailscale --socket="$socket" status --json)
  enabled=$(tailscale --socket="$socket" debug prefs | jq -er '.RunSSH | tostring')
}

read_status
case "$action" in
  start)
    if [[ $(jq -r '.BackendState' <<<"$data") == Running ]]; then
      if [[ $enabled != true ]]; then
        tailscale --socket="$socket" set --ssh=true
        read_status
      fi
    else
      args=()
      if [[ -n $hostname ]]; then args+=("$hostname"); fi
      ts up "${args[@]}" >/dev/null
      read_status
    fi
    ;;
  stop)
    if [[ $enabled == true ]]; then
      tailscale --socket="$socket" set --ssh=false
      read_status
    fi
    ;;
  status) ;;
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac

jq -cn --argjson data "$data" --argjson enabled "$enabled" '{
  status: (if $enabled and $data.BackendState == "Running" then "running" else "stopped" end),
  magicdns: (($data.Self.DNSName // "") | rtrimstr(".")),
  ipv4: ([$data.TailscaleIPs[]? | select(contains(":") | not)] | first // "")
}'
