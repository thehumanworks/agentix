#!/usr/bin/env bash
# agentix-sync: copy the uncommitted changes of one Git checkout to another,
# moving only what the other side lacks. `agentix sandbox sync` runs it on
# both sides, the host and the sandbox: Nix installs this file in the image,
# and the host runs the same file from the repository (or `nix run
# .#agentix-sync`).
#
#   agentix-sync info [DIR]   what the checkout is: protocol, origin, commits
#   agentix-sync pack [DIR]   standard input: the commits the other side has
#                             (from its `info`); standard output: a Git bundle
#                             of this working tree as a commit on top of HEAD
#   agentix-sync apply [DIR]  standard input: that bundle; merges its changes
#                             into this working tree, or changes nothing
#   agentix-sync undo [DIR]   puts back the working tree from before the last
#                             apply (or undo)
#
# The working tree, untracked files included and ignored files left out,
# becomes a commit whose parent is HEAD. `apply` merges the sender's changes,
# from its HEAD to that commit, into this side's working tree with
# `git merge-tree`, so the two HEADs may differ. If any file conflicts, or a
# file the sender adds exists here but Git ignores it, apply changes nothing
# and lists the files (exit code 3). Otherwise it first saves this side's
# working tree as refs/agentix-sync/backup/TIME, then writes only the files
# that change. The index and HEAD stay as they are: the changes show as
# unstaged, and new files as untracked.
set -euo pipefail

PROTOCOL=1
OUTGOING=refs/agentix-sync/outgoing
INCOMING=refs/agentix-sync/incoming
BACKUPS=refs/agentix-sync/backup
# Snapshot commits are internal; they need no identity from the user's config.
export GIT_AUTHOR_NAME=agentix-sync GIT_AUTHOR_EMAIL=agentix-sync@localhost
export GIT_COMMITTER_NAME=agentix-sync GIT_COMMITTER_EMAIL=agentix-sync@localhost
export GIT_TERMINAL_PROMPT=0

die() {
  printf 'agentix-sync: %s\n' "$*" >&2
  exit 1
}

# Enter the top of the checkout at $1. Exit 4: no such directory; 5: not in a
# Git checkout with a commit.
enter() {
  local dir=${1:-.}
  [[ -d $dir ]] || {
    printf 'agentix-sync: %s does not exist\n' "$dir" >&2
    exit 4
  }
  cd "$dir"
  local top
  top=$(git rev-parse --show-toplevel 2>/dev/null) || {
    printf 'agentix-sync: %s is not in a Git checkout\n' "$dir" >&2
    exit 5
  }
  cd "$top"
  git rev-parse --verify -q HEAD >/dev/null || {
    printf 'agentix-sync: %s has no commit yet\n' "$top" >&2
    exit 5
  }
  work=$(mktemp -d "$(git rev-parse --absolute-git-dir)/agentix-sync.XXXXXX")
  trap 'rm -rf "$work"' EXIT
}

# A commit of the working tree, untracked files included, on top of HEAD. It
# starts from a copy of the index, so the real index is untouched. The copy
# keeps the index's time, by which Git tells a file changed just after it was
# indexed; a newer copy would make Git take such a file as unchanged.
snapshot() {
  local index tree
  index=$(git rev-parse --git-path index)
  if [[ -f $index ]]; then
    cp -p "$index" "$work/index"
  fi
  GIT_INDEX_FILE=$work/index git -c advice.addEmbeddedRepo=false add -A -- .
  tree=$(GIT_INDEX_FILE=$work/index git write-tree)
  git commit-tree -p HEAD -m "agentix-sync: the working tree of $(git rev-parse --abbrev-ref HEAD)" "$tree"
}

# Files that `write_tree` would put where Git ignores an existing file: those
# are in neither tree, so nothing else would notice them being overwritten.
ignored_in_the_way() {
  local from=$1 to=$2 status path
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    # A directory in the way is not overwritten: checkout-index fails on it.
    if [[ $status == A && (-L $path || (-e $path && ! -d $path)) ]]; then
      printf '%s\n' "$path"
    fi
  done < <(git diff-tree -r -z --no-renames --name-status "$from" "$to")
}

# Make the working tree hold tree $2 where it now holds tree $1: delete the
# files that $2 lacks, and write the ones that differ, with their modes and
# symlinks, through a temporary index.
write_tree() {
  local from=$1 to=$2 status path parent
  : >"$work/write"
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    if [[ $status == D ]]; then
      rm -f -- "$path"
      parent=$(dirname -- "$path")
      while [[ $parent != . ]] && rmdir -- "$parent" 2>/dev/null; do
        parent=$(dirname -- "$parent")
      done
    else
      printf '%s\0' "$path" >>"$work/write"
    fi
  done < <(git diff-tree -r -z --no-renames --name-status "$from" "$to")
  GIT_INDEX_FILE=$work/write-index git read-tree "$to"
  GIT_INDEX_FILE=$work/write-index git checkout-index -f -z --stdin <"$work/write"
}

backup() {
  local commit=$1 ref
  ref=$BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short "$commit")
  git update-ref -m "agentix-sync: $2" "$ref" "$commit"
  printf 'backup %s %s\n' "$ref" "$commit"
}

report_changes() {
  git diff-tree -r --no-renames --name-status "$1" "$2" | sed 's/^/changed /'
}

cmd_info() {
  enter "${1:-}"
  printf 'protocol %s\n' "$PROTOCOL"
  printf 'toplevel %s\n' "$PWD"
  printf 'head %s\n' "$(git rev-parse HEAD)"
  printf 'branch %s\n' "$(git rev-parse --abbrev-ref HEAD)"
  printf 'origin %s\n' "$(git config --get remote.origin.url || true)"
  git rev-list --max-parents=0 HEAD | sed 's/^/root /'
  # What the other side need not send.
  git for-each-ref --format='known %(objectname)' refs/heads refs/remotes
  printf 'known %s\n' "$(git rev-parse HEAD)"
}

cmd_pack() {
  enter "${1:-}"
  local commit sha
  commit=$(snapshot)
  git update-ref "$OUTGOING" "$commit"
  {
    printf '%s\n' "$OUTGOING"
    # Leave out what the other side has; skip what this side does not know.
    while read -r sha; do
      if [[ $sha =~ ^[0-9a-f]{40,64}$ ]] && git cat-file -e "$sha^{commit}" 2>/dev/null; then
        printf '^%s\n' "$sha"
      fi
    done
  } >"$work/revs"
  git bundle create -q - --stdin <"$work/revs"
}

cmd_apply() {
  enter "${1:-}"
  [[ $(git merge-tree -h 2>&1 || true) == *merge-base* ]] ||
    die "this needs Git 2.40 or newer for git merge-tree --merge-base; this is $(git --version)"
  cat >"$work/bundle"
  git bundle verify -q "$work/bundle" >/dev/null 2>"$work/verify" || {
    cat "$work/verify" >&2
    die "the changes sent need commits this checkout does not have"
  }
  git fetch -q --no-write-fetch-head "$work/bundle" "+$OUTGOING:$INCOMING"
  local theirs base mine merged code=0
  theirs=$(git rev-parse "$INCOMING^{commit}")
  base=$(git rev-parse "$theirs^")
  mine=$(snapshot)
  printf 'base %s\n' "$base"
  printf 'head %s\n' "$(git rev-parse HEAD)"
  git merge-tree --write-tree --name-only --no-messages --merge-base="$base" "$mine" "$theirs" >"$work/merge" || code=$?
  if ((code == 1)); then
    printf 'result conflict\n'
    tail -n +2 "$work/merge" | awk '!seen[$0]++' | sed 's/^/conflict /'
    exit 3
  elif ((code != 0)); then
    die "git merge-tree failed with exit code $code"
  fi
  merged=$(head -n 1 "$work/merge")
  if [[ $merged == "$(git rev-parse "$mine^{tree}")" ]]; then
    printf 'result unchanged\n'
    return
  fi
  ignored_in_the_way "$mine" "$merged" >"$work/ignored"
  if [[ -s $work/ignored ]]; then
    printf 'result conflict\n'
    sed 's/^/ignored /' "$work/ignored"
    exit 3
  fi
  backup "$mine" "before applying $theirs"
  report_changes "$mine" "$merged"
  write_tree "$mine" "$merged"
  printf 'result applied\n'
}

cmd_undo() {
  enter "${1:-}"
  local latest mine
  latest=$(git for-each-ref --sort=-refname --count=1 --format='%(refname)' "$BACKUPS/")
  [[ -n $latest ]] || die "there is no backup to go back to"
  mine=$(snapshot)
  if [[ $(git rev-parse "$mine^{tree}") == "$(git rev-parse "$latest^{tree}")" ]]; then
    printf 'result unchanged\n'
    return
  fi
  ignored_in_the_way "$mine" "$latest" >"$work/ignored"
  if [[ -s $work/ignored ]]; then
    printf 'result conflict\n'
    sed 's/^/ignored /' "$work/ignored"
    exit 3
  fi
  # Saved first, so that undo can itself be undone.
  backup "$mine" "before going back to $latest"
  report_changes "$mine" "$latest"
  write_tree "$mine" "$latest"
  printf 'restored %s\n' "$latest"
  printf 'result applied\n'
}

case ${1:-} in
  info | pack | apply | undo)
    command=$1
    shift
    "cmd_$command" "$@"
    ;;
  version | --version)
    printf 'agentix-sync %s\n' "$PROTOCOL"
    ;;
  *)
    echo "usage: agentix-sync info|pack|apply|undo [DIR], or agentix-sync version" >&2
    exit 2
    ;;
esac
