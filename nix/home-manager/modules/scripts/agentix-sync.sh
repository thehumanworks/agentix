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
#   agentix-sync apply [DIR] [BASE]
#                             standard input: that bundle; merges its changes
#                             into this working tree, or changes nothing
#   agentix-sync mark [DIR] COMMIT
#                             records COMMIT, which the other side applied, as
#                             the last state both sides agreed on
#   agentix-sync undo [DIR]   puts back the working tree from before the last
#                             apply (or undo)
#
# The working tree, untracked files included and ignored files left out,
# becomes a commit whose parent is HEAD. `apply` merges the sender's changes,
# from BASE to that commit, into this side's working tree with
# `git merge-tree`, so the two HEADs may differ. BASE is the last state both
# sides agreed on (refs/agentix-sync/synced on both, which `info` shows), so
# that a file synced before and changed since on one side is not a conflict;
# without it, the sender's HEAD.
#
# apply changes nothing, and exits with 3 and lists the files, if any file
# conflicts, or if writing would remove a file Git ignores here. It changes
# nothing, and exits with 1, if any step before writing fails: reading the
# working tree, merging, or preparing the new files (filters included), or
# if the working tree changed while it ran. Then it saves this side's working
# tree as refs/agentix-sync/backup/NNNNNN-TIME, moves the files it replaces
# aside, and moves the new ones in; if a move fails, it puts the old files
# back. The index and HEAD stay as they are: the changes show as unstaged,
# and new files as untracked.
#
# Every step checks its own result: set -e does nothing inside $(...), in a
# function called from `if` or `||`, or in a process substitution.
set -euo pipefail

PROTOCOL=2
OUTGOING=refs/agentix-sync/outgoing
INCOMING=refs/agentix-sync/incoming
SYNCED=refs/agentix-sync/synced
BACKUPS=refs/agentix-sync/backup
# Snapshot commits are internal; they need no identity from the user's config.
export GIT_AUTHOR_NAME=agentix-sync GIT_AUTHOR_EMAIL=agentix-sync@localhost
export GIT_COMMITTER_NAME=agentix-sync GIT_COMMITTER_EMAIL=agentix-sync@localhost
export GIT_TERMINAL_PROMPT=0

work=
keep_work=

die() {
  printf 'agentix-sync: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n $work && -z $keep_work ]]; then
    rm -rf "$work"
  fi
}

# Enter the top of the checkout at $1. Exit 4: no such directory; 5: not in a
# Git checkout with a commit.
enter() {
  local dir=${1:-.} top gitdir
  if [[ ! -d $dir ]]; then
    printf 'agentix-sync: %s does not exist\n' "$dir" >&2
    exit 4
  fi
  cd "$dir" || die "cannot enter $dir"
  if ! top=$(git rev-parse --show-toplevel 2>/dev/null); then
    printf 'agentix-sync: %s is not in a Git checkout\n' "$dir" >&2
    exit 5
  fi
  cd "$top" || die "cannot enter $top"
  if ! git rev-parse --verify -q HEAD >/dev/null; then
    printf 'agentix-sync: %s has no commit yet\n' "$top" >&2
    exit 5
  fi
  gitdir=$(git rev-parse --absolute-git-dir) || die "cannot find the Git directory of $top"
  work=$(mktemp -d "$gitdir/agentix-sync.XXXXXX") || die "cannot make a temporary directory in $gitdir"
  trap cleanup EXIT
}

# Prints a commit of the working tree, untracked files included, on top of
# HEAD. It starts from a copy of the index, so the real index is untouched.
# The copy keeps the index's time, by which Git tells a file changed just
# after it was indexed; a newer copy would make Git take such a file as
# unchanged. Called in $(...), so each step is checked.
snapshot() {
  local index tree branch commit
  index=$(git rev-parse --git-path index) || return 1
  rm -f "$work/index" || return 1
  if [[ -f $index ]]; then
    cp -p "$index" "$work/index" || return 1
  fi
  GIT_INDEX_FILE=$work/index git -c advice.addEmbeddedRepo=false add -A -- . || return 1
  tree=$(GIT_INDEX_FILE=$work/index git write-tree) || return 1
  branch=$(git rev-parse --abbrev-ref HEAD) || return 1
  commit=$(git commit-tree -p HEAD -m "agentix-sync: the working tree of $branch" "$tree") || return 1
  [[ -n $commit ]] || return 1
  printf '%s\n' "$commit"
}

# Sets $snap to a snapshot of the working tree, or stops.
take_snapshot() {
  snap=$(snapshot) ||
    die "could not read the working tree, so nothing was changed; see Git's message above"
}

tree_of() {
  git rev-parse --verify -q "$1^{tree}" || die "no tree for $1"
}

# The changes from tree $1 to tree $2, as NUL-separated status and path, in
# $work/diff, and the paths alone in $work/touched (all) and $work/write
# (those that $2 has).
list_changes() {
  local status path
  git diff-tree -r -z --no-renames --name-status "$1" "$2" >"$work/diff" ||
    die "could not compare $1 and $2"
  : >"$work/touched"
  : >"$work/write"
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    printf '%s\0' "$path" >>"$work/touched"
    if [[ $status != D ]]; then
      printf '%s\0' "$path" >>"$work/write"
    fi
  done <"$work/diff"
}

# Files that writing the changes in $work/diff would remove although tree $1,
# the working tree as snapshotted, does not have them: files Git ignores
# here. Each would be lost, as it is in no backup. Written to
# $work/blocked: a file where a new file goes, a file where a new file's
# directory goes, and a file inside a directory that a new file replaces.
check_in_the_way() {
  local from=$1 status path prefix found
  : >"$work/blocked"
  while IFS= read -r -d '' status && IFS= read -r -d '' path; do
    [[ $status == D ]] && continue
    prefix=$path
    while [[ $prefix == */* ]]; do
      prefix=${prefix%/*}
      if [[ -L $prefix || (-e $prefix && ! -d $prefix) ]] &&
        ! git cat-file -e "$from:$prefix" 2>/dev/null; then
        printf '%s\n' "$prefix" >>"$work/blocked"
      fi
    done
    if [[ -L $path || (-e $path && ! -d $path) ]]; then
      if ! git cat-file -e "$from:$path" 2>/dev/null; then
        printf '%s\n' "$path" >>"$work/blocked"
      fi
    elif [[ -d $path ]]; then
      find "$path" \( -type f -o -type l \) -print0 >"$work/found" ||
        die "could not list $path"
      while IFS= read -r -d '' found; do
        if ! git cat-file -e "$from:$found" 2>/dev/null; then
          printf '%s\n' "$found" >>"$work/blocked"
        fi
      done <"$work/found"
    fi
  done <"$work/diff"
  awk '!seen[$0]++' "$work/blocked" >"$work/blocked.unique" || die "awk failed"
  mv "$work/blocked.unique" "$work/blocked" || die "mv failed"
}

# Saves commit $1 under a new backup ref, numbered after the newest one.
backup() {
  local commit=$1 last number stamp
  last=$(git for-each-ref --sort=-refname --count=1 --format='%(refname:lstrip=3)' \
    "$BACKUPS/[0-9][0-9][0-9][0-9][0-9][0-9]-*") || die "could not list the backups"
  number=${last%%-*}
  [[ $number =~ ^[0-9]+$ ]] || number=0
  stamp=$(date -u +%Y%m%dT%H%M%SZ) || die "date failed"
  backup_ref=$(printf '%s/%06d-%s' "$BACKUPS" "$((10#$number + 1))" "$stamp")
  git update-ref -m "agentix-sync: $2" "$backup_ref" "$commit" "" ||
    die "could not save the backup $backup_ref"
  printf 'backup %s %s\n' "$backup_ref" "$commit"
}

dir_of() {
  local dir=${1%/*}
  [[ $dir == "$1" ]] && dir=.
  printf '%s' "$dir"
}

# Removes the empty directories above path $1.
prune_dirs() {
  local dir
  dir=$(dir_of "$1")
  while [[ $dir != . ]] && rmdir -- "$dir" 2>/dev/null; do
    dir=$(dir_of "$dir")
  done
}

# Moves the files in $work/touched aside to $work/old, then the prepared
# ones from $work/new into place. Returns 1 at the first failure, with what
# it did in $work/moved and $work/placed.
swap() {
  local path dir
  : >"$work/moved"
  : >"$work/placed"
  while IFS= read -r -d '' path; do
    if [[ -L $path || (-e $path && ! -d $path) ]]; then
      dir=$(dir_of "$path")
      mkdir -p -- "$work/old/$dir" || return 1
      mv -- "$path" "$work/old/$path" || return 1
      printf '%s\0' "$path" >>"$work/moved"
      prune_dirs "$path"
    fi
  done <"$work/touched"
  while IFS= read -r -d '' path; do
    if [[ ${AGENTIX_SYNC_FAIL_AT:-} == "$path" ]]; then
      # For the tests: a write that fails part of the way.
      return 1
    fi
    # A directory that a file replaces is empty now; anything else in it
    # was refused by check_in_the_way.
    if [[ -d $path && ! -L $path ]]; then
      rmdir -- "$path" || return 1
    fi
    dir=$(dir_of "$path")
    mkdir -p -- "$dir" || return 1
    mv -- "$work/new/$path" "$path" || return 1
    printf '%s\0' "$path" >>"$work/placed"
  done <"$work/write"
}

# Undoes what swap did. Returns 1 if anything could not be put back.
unswap() {
  local path dir failed=0
  while IFS= read -r -d '' path; do
    rm -f -- "$path" || failed=1
    prune_dirs "$path"
  done <"$work/placed"
  while IFS= read -r -d '' path; do
    dir=$(dir_of "$path")
    if [[ -d $path && ! -L $path ]]; then
      rmdir -- "$path" || failed=1
    fi
    { mkdir -p -- "$dir" && mv -- "$work/old/$path" "$path"; } || failed=1
  done <"$work/moved"
  return "$failed"
}

# Makes the working tree, which snapshot commit $1 holds, hold tree $2,
# changing only the files that differ. Saves $1 as a backup first, with
# message $3. Stops with nothing changed if any check fails or the working
# tree changed since $1, and puts the old files back if writing fails.
write_tree() {
  local from=$1 to=$2 now
  list_changes "$from" "$to"
  # The new files, filters and all, away from the working tree.
  GIT_INDEX_FILE=$work/write-index git read-tree "$to" ||
    die "could not read the merged tree, so nothing was changed"
  mkdir "$work/new" "$work/old" || die "could not make $work/new"
  GIT_INDEX_FILE=$work/write-index git checkout-index -f -z --prefix="$work/new/" --stdin \
    <"$work/write" || die "could not prepare the new files, so nothing was changed; see Git's message above"
  take_snapshot
  now=$snap
  [[ $(tree_of "$now") == "$(tree_of "$from")" ]] ||
    die "the working tree changed while syncing, so nothing was changed; sync again"
  backup "$from" "$3"
  if swap; then
    return 0
  fi
  if unswap; then
    die "could not write the working tree, so it was put back as it was"
  fi
  keep_work=1
  die "could not write the working tree, nor put all of it back; the files moved aside are in $work/old, and $backup_ref holds the whole working tree from before"
}

report_changes() {
  git diff-tree -r --no-renames --name-status "$1" "$2" >"$work/report" ||
    die "could not compare $1 and $2"
  sed 's/^/changed /' "$work/report" || die "sed failed"
}

cmd_info() {
  enter "${1:-}"
  local head branch origin synced parent
  head=$(git rev-parse HEAD) || die "no HEAD"
  branch=$(git rev-parse --abbrev-ref HEAD) || die "no branch"
  origin=$(git config --get remote.origin.url) || origin=
  printf 'protocol %s\ntoplevel %s\nhead %s\nbranch %s\norigin %s\n' \
    "$PROTOCOL" "$PWD" "$head" "$branch" "$origin"
  git rev-list --max-parents=0 HEAD >"$work/roots" || die "could not list the root commits"
  sed 's/^/root /' "$work/roots"
  if synced=$(git rev-parse -q --verify "$SYNCED^{commit}"); then
    parent=$(git rev-parse "$synced^") || die "no parent of $synced"
    printf 'synced %s %s\n' "$synced" "$parent"
  fi
  # What the other side need not send.
  git for-each-ref --format='known %(objectname)' refs/heads refs/remotes >"$work/known" ||
    die "could not list the refs"
  cat "$work/known"
  printf 'known %s\n' "$head"
}

cmd_pack() {
  enter "${1:-}"
  local sha
  take_snapshot
  git update-ref "$OUTGOING" "$snap" || die "could not record $OUTGOING"
  printf '%s\n' "$OUTGOING" >"$work/revs"
  # Leave out what the other side has; skip what this side does not know.
  while read -r sha; do
    if [[ $sha =~ ^[0-9a-f]{40,64}$ ]] && git cat-file -e "$sha^{commit}" 2>/dev/null; then
      printf '^%s\n' "$sha" >>"$work/revs"
    fi
  done
  git bundle create -q - --stdin <"$work/revs" || die "could not make the bundle"
}

cmd_apply() {
  enter "${1:-}"
  local help theirs sender base mine head merged code=0
  help=$(git merge-tree -h 2>&1) || true
  [[ $help == *merge-base* ]] ||
    die "this needs Git 2.40 or newer for git merge-tree --merge-base; this is $(git --version)"
  cat >"$work/bundle" || die "could not read the bundle"
  if ! git bundle verify -q "$work/bundle" >/dev/null 2>"$work/verify"; then
    cat "$work/verify" >&2
    die "the changes sent need commits this checkout does not have"
  fi
  git fetch -q --no-write-fetch-head "$work/bundle" "+$OUTGOING:$INCOMING" ||
    die "could not fetch the bundle"
  theirs=$(git rev-parse --verify -q "$INCOMING^{commit}") || die "the bundle has no snapshot"
  sender=$(git rev-parse --verify -q "$theirs^") || die "the snapshot has no parent"
  base=$sender
  if [[ -n ${2:-} ]]; then
    base=$(git rev-parse -q --verify "$2^{commit}") || die "no commit $2 to merge from"
  fi
  head=$(git rev-parse HEAD) || die "no HEAD"
  take_snapshot
  mine=$snap
  printf 'incoming %s\nsender-head %s\nhead %s\n' "$theirs" "$sender" "$head"
  git merge-tree --write-tree --name-only --no-messages --merge-base="$base" "$mine" "$theirs" \
    >"$work/merge" || code=$?
  if ((code == 1)); then
    printf 'result conflict\n'
    tail -n +2 "$work/merge" | awk '!seen[$0]++' | sed 's/^/conflict /'
    exit 3
  elif ((code != 0)); then
    die "git merge-tree failed with exit code $code, so nothing was changed"
  fi
  merged=$(head -n 1 "$work/merge") || die "could not read the merge"
  [[ -n $merged ]] || die "git merge-tree gave no tree"
  if [[ $merged == "$(tree_of "$mine")" ]]; then
    git update-ref "$SYNCED" "$theirs" || die "could not record $SYNCED"
    printf 'result unchanged\n'
    return
  fi
  list_changes "$mine" "$merged"
  check_in_the_way "$mine"
  if [[ -s $work/blocked ]]; then
    printf 'result conflict\n'
    sed 's/^/ignored /' "$work/blocked"
    exit 3
  fi
  report_changes "$mine" "$merged" >"$work/changes"
  write_tree "$mine" "$merged" "before applying $theirs"
  cat "$work/changes"
  git update-ref "$SYNCED" "$theirs" || die "could not record $SYNCED"
  printf 'result applied\n'
}

cmd_mark() {
  enter "${1:-}"
  local commit
  commit=$(git rev-parse -q --verify "${2:-}^{commit}") || die "no commit ${2:-} to mark"
  git update-ref "$SYNCED" "$commit" || die "could not record $SYNCED"
}

cmd_undo() {
  enter "${1:-}"
  local latest mine target
  latest=$(git for-each-ref --sort=-refname --count=1 --format='%(refname)' \
    "$BACKUPS/[0-9][0-9][0-9][0-9][0-9][0-9]-*") || die "could not list the backups"
  [[ -n $latest ]] || die "there is no backup to go back to"
  take_snapshot
  mine=$snap
  target=$(tree_of "$latest")
  if [[ $(tree_of "$mine") == "$target" ]]; then
    printf 'result unchanged\n'
    return
  fi
  list_changes "$mine" "$latest"
  check_in_the_way "$mine"
  if [[ -s $work/blocked ]]; then
    printf 'result conflict\n'
    sed 's/^/ignored /' "$work/blocked"
    exit 3
  fi
  report_changes "$mine" "$latest" >"$work/changes"
  # The working tree no longer holds what the other side has, so the two
  # agree on nothing now. The current state is saved first, so that undo can
  # itself be undone.
  write_tree "$mine" "$latest" "before going back to $latest"
  git update-ref -d "$SYNCED" || die "could not clear $SYNCED"
  cat "$work/changes"
  printf 'restored %s\nresult applied\n' "$latest"
}

case ${1:-} in
  info | pack | apply | mark | undo)
    command=$1
    shift
    "cmd_$command" "$@"
    ;;
  version | --version)
    printf 'agentix-sync %s\n' "$PROTOCOL"
    ;;
  *)
    echo "usage: agentix-sync info|pack|apply|mark|undo [DIR], or agentix-sync version" >&2
    exit 2
    ;;
esac
