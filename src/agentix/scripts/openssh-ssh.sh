#!/usr/bin/env bash
set -euo pipefail
umask 077

action=$1
public_key=${2:-}
runtime=/root/.local/state/agentix/openssh
config=$runtime/sshd_config
pidfile=$runtime/sshd.pid

mkdir -p "$runtime"
chmod 700 "$runtime"
exec 9>"$runtime/lock"
flock -w 30 9

running() {
  [[ -f $pidfile ]] || return 1
  pid=$(cat "$pidfile")
  [[ $pid =~ ^[0-9]+$ ]] && [[ $pid -gt 1 ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Refuse to signal a reused PID belonging to another process.
  [[ -r /proc/$pid/cmdline ]] || return 1
  [[ $(tr '\0' ' ' <"/proc/$pid/cmdline") == *"sshd"*"$config"* ]]
}

case "$action" in
  start)
    if ! running; then
      if [[ -z $public_key ]]; then echo "An SSH public key is required." >&2; exit 1; fi
      printf '%s\n' "$public_key" >"$runtime/authorized_keys"
      ssh-keygen -l -f "$runtime/authorized_keys" >/dev/null
      if [[ ! -f $runtime/host_key ]]; then
        ssh-keygen -q -t ed25519 -N '' -f "$runtime/host_key"
      fi
      mkdir -p /run/sshd /var/empty
      if ! id sshd >/dev/null 2>&1; then
        /usr/sbin/useradd --system --no-create-home --home-dir /run/sshd \
          --shell /bin/false sshd
      fi
      cat >"$config" <<EOF
Port 22
ListenAddress 0.0.0.0
HostKey $runtime/host_key
PidFile $pidfile
AuthorizedKeysFile $runtime/authorized_keys
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AcceptEnv OP_SERVICE_ACCOUNT_TOKEN
UsePAM no
AllowUsers root
PrintMotd no
Subsystem sftp internal-sftp
EOF
      sshd=$(command -v sshd)
      "$sshd" -t -f "$config"
      # Do not expose injected credentials or shell hooks to SSH sessions.
      env -i PATH="$PATH" HOME=/root USER=root LOGNAME=root \
        "$sshd" -f "$config" -E "$runtime/sshd.log" 9>&-
      for ((attempt=0; attempt<50; attempt++)); do
        if running; then break; fi
        sleep 0.1
      done
      if ! running; then
        echo "OpenSSH failed to start. See $runtime/sshd.log" >&2
        exit 1
      fi
    fi
    ;;
  stop)
    if running; then
      kill "$pid"
      for ((attempt=0; attempt<50; attempt++)); do
        if ! running; then break; fi
        sleep 0.1
      done
      if running; then echo "OpenSSH did not stop." >&2; exit 1; fi
    fi
    ;;
  status) ;;
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac

state=stopped
if running; then state=running; fi
fingerprint=""
if [[ -f $runtime/host_key.pub ]]; then
  fingerprint=$(ssh-keygen -l -f "$runtime/host_key.pub" | cut -d ' ' -f 2)
fi
jq -cn --arg status "$state" --arg host_key_fingerprint "$fingerprint" \
  '{status: $status, host_key_fingerprint: $host_key_fingerprint}'
