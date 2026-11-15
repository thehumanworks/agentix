# Checks that the harness wrappers start claude, codex, and pi without the
# 1Password service account token that the profile's `op` loads from the
# token file, as in an SSH session where the token is not exported, and
# when it is exported, and that a different exported token stays. It runs a
# copy of each generated wrapper with `op` replaced by a stand-in that, like
# sandbox/op-token.nix, loads the token file when no token is exported and
# then runs the command, and with the harness replaced by a command that
# prints the token it sees. The codex and pi wrappers' `agent-auth-sync
# ensure`, which would run the real `op` and start a watcher, becomes `true`.
{ pkgs, home }:

let
  inherit (pkgs) lib;
  inherit (home.config) agentix programs;
  wrappers = {
    claude = programs.claude-code.package;
    codex = programs.codex.package;
    pi = programs.pi-coding-agent.package;
  };
  op = pkgs.writeShellScript "op" ''
    if [[ -z ''${OP_SERVICE_ACCOUNT_TOKEN:-} && -r $AGENTIX_OP_TOKEN_FILE ]]; then
      OP_SERVICE_ACCOUNT_TOKEN=$(<"$AGENTIX_OP_TOKEN_FILE")
      export OP_SERVICE_ACCOUNT_TOKEN
    fi
    [[ $1 == run ]] || exit 1
    while [[ $1 != -- ]]; do shift; done
    shift
    exec "$@"
  '';
  harness = pkgs.writeShellScript "harness" ''
    echo "''${OP_SERVICE_ACCOUNT_TOKEN-none}"
  '';
in
assert agentix.op.tokenFile != null;
pkgs.runCommand "agentix-wrapper-token" { } ''
  printf file-token > token
  export AGENTIX_OP_TOKEN_FILE=$PWD/token HOME=$PWD/home
  fail=0
  expect() {
    local name=$1 want=$2 got
    shift 2
    got=$("$@" ./"$name" one two)
    if [[ $got != "$want" ]]; then
      echo "$name with $*: the harness saw '$got', not '$want'" >&2
      fail=1
    fi
  }
  ${lib.concatStrings (
    lib.mapAttrsToList (name: package: ''
      sed -E \
        -e 's|${lib.getExe agentix.op.package}|${op}|g' \
        -e 's|${lib.getExe agentix.authSync.package}|true|g' \
        -e 's|^exec "\$\{launch\[@\]\}" [^ ]+ "\$@"$|exec "''${launch[@]}" ${harness} "$@"|' \
        ${lib.getExe package} > ${name}
      chmod +x ${name}
      grep -q '${harness}' ${name} || { echo "${name}: no exec line to replace" >&2; exit 1; }
      if grep -q '${agentix.authSync.package}' ${name}; then
        echo "${name}: still runs agent-auth-sync" >&2
        exit 1
      fi
      expect ${name} none env -u OP_SERVICE_ACCOUNT_TOKEN
      expect ${name} none env OP_SERVICE_ACCOUNT_TOKEN=file-token
      expect ${name} other env OP_SERVICE_ACCOUNT_TOKEN=other
    '') wrappers
  )}
  # claude gets its login through `op run`, the path this checks most.
  grep -q '${op} run' claude || { echo "claude does not start through op run" >&2; exit 1; }
  ((fail == 0))
  touch $out
''
