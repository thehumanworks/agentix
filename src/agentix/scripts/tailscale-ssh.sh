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
  prefs=$(tailscale --socket="$socket" debug prefs)
  enabled=$(jq -er '.RunSSH | tostring' <<<"$prefs")
}

read_status
case "$action" in
  start)
    if [[ $(jq -r '.BackendState' <<<"$data") == Running ]]; then
      args=(--ssh=true)
      if [[ -n $hostname && $(jq -r '.Hostname // ""' <<<"$prefs") != "$hostname" ]]; then
        args+=(--hostname="$hostname")
      fi
      if [[ $enabled != true || ${#args[@]} -gt 1 ]]; then
        tailscale --socket="$socket" set "${args[@]}"
        read_status
      fi
    else
      args=()
      if [[ -n $hostname ]]; then args+=("$hostname"); fi
      ts up "${args[@]}" >/dev/null
      read_status
    fi
    # The control plane updates Self asynchronously after `set`. Do not return
    # the old DNS name while the requested rename is still propagating.
    if [[ -n $hostname ]]; then
      for (( attempt=0; attempt<50; attempt++ )); do
        if [[ $(jq -r '.Self.HostName // ""' <<<"$data") == "$hostname" ]]; then
          break
        fi
        sleep 0.2
        read_status
      done
      if [[ $(jq -r '.Self.HostName // ""' <<<"$data") != "$hostname" ]]; then
        echo "Tailscale has not confirmed hostname $hostname yet. Retry SSH start." >&2
        exit 1
      fi
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
