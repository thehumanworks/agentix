# Entry point of the OCI image. It does what the start command in
# src/agentix/sandbox.py does for a Modal sandbox, then runs the command.
#
# default.nix sets coreutils and the paths from nix/sandbox.toml before this
# text. The command keeps the PATH of the container; this adds nothing to it.
: "${coreutils:?}" "${op_token_file:?}" "${workdir:?}" "${workdir_file:?}" "${ready_file:?}"

tmpfs() {
  [[ $("$coreutils/stat" -f -c %T "$1" 2>/dev/null) == tmpfs ]]
}

# Modal's /run is a tmpfs, so the token never reaches a disk or a snapshot.
# Docker and Podman keep /run in the container's writable layer, so keep the
# token's directory in /dev/shm instead, unless /run is a tmpfs
# (`--tmpfs /run`).
token_dir=$("$coreutils/dirname" "$op_token_file")
if [[ ! -d $token_dir ]] && ! tmpfs "$("$coreutils/dirname" "$token_dir")" && tmpfs /dev/shm; then
  shm_dir=/dev/shm/${token_dir##*/}
  (umask 077 && "$coreutils/mkdir" -p "$shm_dir")
  "$coreutils/ln" -sfn "$shm_dir" "$token_dir"
fi
(umask 077 && "$coreutils/mkdir" -p "$token_dir")
"$coreutils/mkdir" -p "$("$coreutils/dirname" "$workdir_file")" "$("$coreutils/dirname" "$ready_file")"

# The image's op loads the token from this root-only file when the variable is
# not set, as in SSH sessions, and claude starts without it in its environment.
if [[ -n ${OP_SERVICE_ACCOUNT_TOKEN:-} ]]; then
  (umask 077 && printf %s "$OP_SERVICE_ACCOUNT_TOKEN" >"$op_token_file")
fi

# `docker run -w PATH`: SSH logins start there too, as with --workdir on Modal.
if [[ $PWD != "$workdir" ]]; then
  printf '%s\n' "$PWD" >"$workdir_file"
fi
: >"$ready_file"

if (($# > 0)); then
  exec "$@"
fi
# `docker run -it IMAGE` opens a login shell. Without a terminal the container
# stays up for `docker exec`, as a Modal sandbox does.
if [[ -t 0 ]]; then
  exec /bin/bash -l
fi
exec "$coreutils/sleep" infinity
