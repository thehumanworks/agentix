#!/usr/bin/env bash
set -euo pipefail

action=$1
hostname=${2:-}
socket=/run/agentix-tailscale/tailscaled.sock

if [[ $action == start ]] && { ! command -v ts >/dev/null || ! command -v tailscale >/dev/null; }; then
  echo "This sandbox's image has no Tailscale: agentix.sandbox.tailscale is off in its Home Manager profile." >&2
  exit 1
fi

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

# Start Tailscale, or apply SSH and the hostname to a running one. Returns
# non-zero on failure, so that the caller can look at the state first.
bring_up() {
  if [[ $(jq -r '.BackendState' <<<"$data") == Running ]]; then
    args=(--ssh=true)
    if [[ -n $hostname && $(jq -r '.Hostname // ""' <<<"$prefs") != "$hostname" ]]; then
      args+=(--hostname="$hostname")
    fi
    if [[ $enabled != true || ${#args[@]} -gt 1 ]]; then
      tailscale --socket="$socket" set "${args[@]}" || return 1
      read_status
    fi
  else
    args=()
    if [[ -n $hostname ]]; then args+=("$hostname"); fi
    ts up "${args[@]}" >/dev/null || return 1
    read_status
  fi
}

# "online" once the node is connected to the tailnet, "login" when the
# control server asks this node to log in, or "" while neither is known.
connection() {
  jq -r 'if .Self.Online == true then "online"
    elif (.AuthURL // "") != "" then "login" else "" end' <<<"$data"
}

wait_connected() {
  for (( attempt=0; attempt<80; attempt++ )); do
    state=$(connection)
    if [[ -n $state ]]; then return; fi
    sleep 0.25
    read_status
  done
}

# The first label of the node's MagicDNS name.
dns_label() {
  jq -r '(.Self.DNSName // "") | split(".")[0]' <<<"$data"
}

# Whether label $1 is the hostname with the suffix of a name that was taken.
suffixed() {
  [[ $1 == "$hostname"-* && ${1#"$hostname"-} =~ ^[0-9]+$ ]]
}

# Wait until the DNS label starts with $1, if $2 is true, or no longer does.
wait_dns_label() {
  local label
  for (( attempt=0; attempt<50; attempt++ )); do
    label=$(dns_label)
    if [[ $2 == true && $label == "$1"* ]] || [[ $2 == false && $label != "$1"* ]]; then
      return
    fi
    sleep 0.2
    read_status
  done
}

read_status
case "$action" in
  start)
    state=""
    if bring_up; then
      wait_connected
    else
      read_status
      state=$(connection)
      # Any other failure is reported as it is.
      if [[ $state != login ]]; then exit 1; fi
    fi
    # A restored sandbox holds the node key of the snapshot. Once the tailnet
    # has removed that node, as it removes an ephemeral node some time after
    # it went offline, tailscaled still reports Running from its saved state,
    # but the node stays offline and the control server asks for a login.
    # Logging out drops only that key, so that ts enrolls the sandbox again,
    # under the same hostname. The SSH host keys stay.
    if [[ $state == login ]]; then
      tailscale --socket="$socket" logout >&2
      read_status
      bring_up
      wait_connected
    fi
    if [[ $state != online ]]; then
      echo "Tailscale did not connect to the tailnet. Run ts status in the sandbox." >&2
      exit 1
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
      # Tailscale names a node NAME-1 while another node has NAME, and keeps
      # that name after the other node is gone, until the hostname changes.
      # Renaming the node away and back takes NAME if it is free by now.
      if suffixed "$(dns_label)"; then
        away=${hostname:0:55}-rename
        tailscale --socket="$socket" set --hostname="$away"
        wait_dns_label "$away" true
        tailscale --socket="$socket" set --hostname="$hostname"
        wait_dns_label "$away" false
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
