#!/usr/bin/env bash
set -euo pipefail

# Modal sandboxes start with /tmp as 0777, and a mode set during the image
# build does not survive into them. Claude refuses to put its cross-session
# messaging socket in a world-writable directory that lacks the sticky bit, so
# restore the standard 1777 when we own /tmp.
if [[ -O /tmp && ! -k /tmp ]]; then
  chmod +t /tmp || true
fi

export CLAUDE_CODE_OAUTH_TOKEN="op://agentix/claude/oauth/token"
# Preserve terminal streams so Claude starts its interactive UI.
exec "@op@" run --no-masking -- "@claude@" "$@"
