# shellcheck shell=sh
# SSH sessions start in the home directory; move them to the sandbox working
# directory. `sandbox up --workdir=PATH` records an override in @workdirFile@.
# Home Manager fills in both paths from nix/sandbox.toml. Login shells read
# this from ~/.profile, and the .bashrc of `ssh HOST COMMAND` reads it too.
if [ -n "${SSH_CONNECTION:-}" ] && [ "$PWD" = "$HOME" ]; then
  agentix_workdir=$(cat @workdirFile@ 2>/dev/null || echo @workdir@)
  if [ -d "$agentix_workdir" ]; then
    cd "$agentix_workdir" || true
  fi
  unset agentix_workdir
fi
