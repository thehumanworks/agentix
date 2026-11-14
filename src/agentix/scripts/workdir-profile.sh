# shellcheck shell=sh
# Installed as /etc/profile.d/agentix-workdir.sh by the image build.
# SSH logins start in the home directory; move them to the sandbox working
# directory. `sandbox up --workdir=PATH` records an override in /run/agentix.
if [ -n "${SSH_CONNECTION:-}" ] && [ "$PWD" = "$HOME" ]; then
  agentix_workdir=$(cat /run/agentix/workdir 2>/dev/null || echo /workdir)
  if [ -d "$agentix_workdir" ]; then
    cd "$agentix_workdir" || true
  fi
  unset agentix_workdir
fi
