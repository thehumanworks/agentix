#!/usr/bin/env bash
set -euo pipefail

target=$1
shift

# Seed a private, writable file once; preserve all existing Claude state.
if [[ ! -e "$target" && ! -L "$target" ]]; then
  umask 077
  set -o noclobber
  printf '%s\n' '{"hasCompletedOnboarding":true}' > "$target"
  set +o noclobber
fi

# Accept the workspace trust dialog for the remaining arguments. Rewrite the
# file only when an entry is missing, and keep everything else Claude stored.
if (( $# > 0 )) && [[ -f "$target" && ! -L "$target" ]]; then
  # shellcheck disable=SC2016 # $root and $ARGS are jq variables.
  trusted='. as $root | all($ARGS.positional[]; $root.projects[.].hasTrustDialogAccepted == true)'
  # shellcheck disable=SC2016
  trust='reduce $ARGS.positional[] as $dir (.; .projects[$dir].hasTrustDialogAccepted = true)'
  if ! jq -e "$trusted" --args "$@" < "$target" > /dev/null; then
    tmp=$(mktemp "$target.XXXXXX")
    trap 'rm -f "$tmp"' EXIT
    jq "$trust" --args "$@" < "$target" > "$tmp"
    mv "$tmp" "$target"
  fi
fi
