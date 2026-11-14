#!/usr/bin/env bash
set -euo pipefail

# Modal gives the service account token only to the processes it starts, so
# SSH sessions do not have it. `agentix sandbox up` stores it in this root-only
# file. A token that is already exported takes precedence.
token_file=${AGENTIX_OP_TOKEN_FILE:-/run/agentix/op-token}
if [[ -z ${OP_SERVICE_ACCOUNT_TOKEN:-} && -r $token_file ]]; then
  OP_SERVICE_ACCOUNT_TOKEN=$(<"$token_file")
  export OP_SERVICE_ACCOUNT_TOKEN
fi
exec "@op@" "$@"
