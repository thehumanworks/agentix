# A helper function, not a Home Manager module: the harness modules call it
# as `import ./op-run.nix { inherit config lib pkgs; }`.
#
# Builds the wrapper command of an agent harness, such as `claude` or
# `codex`, that gets its credentials from 1Password each time it starts.
# Nothing secret is in the wrapper, the store, or the image: each variable in
# SECRETS holds an op:// reference, and `op run` replaces it with the secret
# for the harness alone. `--no-masking` keeps the terminal streams, so the
# harness starts its interactive UI.
#
# When the profile's `op` loads a token file (see sandbox/op-token.nix), the
# harness starts without the 1Password token in its environment, and the
# commands it runs get secrets through `op`. A different exported token is an
# override and stays.
#
# With SECRETS empty, the wrapper starts the harness without `op`, and still
# without the token when the token file has it.
#
# A call with exactly one argument from BYPASS, such as `--version`, starts
# the harness directly, so it answers without 1Password. BEFORE_LAUNCH runs
# after that check, so `--version` skips it too.
{
  config,
  lib,
  pkgs,
}:

{
  name,
  program,
  secrets,
  bypass ? [ ],
  # Shell lines that run first.
  preamble ? "",
  # Shell lines that run just before the harness starts.
  beforeLaunch ? "",
}:

let
  inherit (config.agentix.op) tokenFile;

  exports = lib.concatStrings (
    lib.mapAttrsToList (variable: reference: ''
      export ${variable}=${lib.escapeShellArg reference}
    '') secrets
  );

  # Each variable is exported as its reference, which `op run` resolves.
  launch =
    if secrets == { } then
      "launch=()"
    else
      ''
        ${exports}
        launch=(${lib.getExe config.agentix.op.package} run --no-masking --)
      '';

  withoutToken = lib.optionalString (tokenFile != null) ''
    token_file=''${AGENTIX_OP_TOKEN_FILE:-${tokenFile}}
    if [[ -r $token_file ]] &&
      [[ -z ''${OP_SERVICE_ACCOUNT_TOKEN:-} || $OP_SERVICE_ACCOUNT_TOKEN == "$(<"$token_file")" ]]; then
      launch+=(${lib.getExe' pkgs.coreutils "env"} -u OP_SERVICE_ACCOUNT_TOKEN)
    fi
  '';
in
pkgs.writeShellApplication {
  inherit name;
  text = ''
    ${preamble}
    ${lib.optionalString (bypass != [ ]) ''
      if [[ $# -eq 1 ]]; then
        case $1 in
          ${lib.concatMapStringsSep " | " lib.escapeShellArg bypass}) exec ${program} "$@" ;;
        esac
      fi
    ''}
    ${beforeLaunch}
    ${launch}
    ${withoutToken}
    exec "''${launch[@]}" ${program} "$@"
  '';
}
