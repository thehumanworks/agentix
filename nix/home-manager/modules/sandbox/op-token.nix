{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (config.agentix.op) tokenFile;
in
{
  imports = [
    ./options.nix
    ../1password.nix
  ];

  options.agentix.sandbox.opToken.enable =
    lib.mkEnableOption "an `op` that loads the token file a sandbox stores at start"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.opToken.enable {
    agentix.op.tokenFile = config.agentix.sandbox.paths.opTokenFile;

    # Modal gives the service account token only to the processes it starts, so
    # SSH sessions do not have it. `agentix sandbox up` stores it in a root-only
    # file, which this op loads. A token that is already exported takes precedence.
    agentix.op.package = pkgs.writeShellApplication {
      name = "op";
      text = ''
        token_file=''${AGENTIX_OP_TOKEN_FILE:-${tokenFile}}
        if [[ -z ''${OP_SERVICE_ACCOUNT_TOKEN:-} && -r $token_file ]]; then
          OP_SERVICE_ACCOUNT_TOKEN=$(<"$token_file")
          export OP_SERVICE_ACCOUNT_TOKEN
        fi
        exec ${lib.getExe pkgs._1password-cli} "$@"
      '';
    };

    # The claude wrapper keeps the token out of Claude's environment; this stops
    # Claude from reading the file. Tools that need a secret call op.
    # A permission rule marks an absolute path with a second leading slash.
    #
    # Do not add CLAUDE_CODE_SUBPROCESS_ENV_SCRUB for this. It forces Claude's
    # permission mode to `default`, and op fails in the commands Claude runs.
    programs.claude-code.settings.permissions.deny = [ "Read(/${tokenFile})" ];
  };
}
