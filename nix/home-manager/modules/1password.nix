{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.op;
in
{
  imports = [ ./core.nix ];

  options.agentix.op = {
    enable = lib.mkEnableOption "the 1Password CLI, `op`, on PATH" // {
      default = true;
    };

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs._1password-cli;
      defaultText = lib.literalExpression "pkgs._1password-cli";
      description = ''
        The `op` command on PATH. The `claude`, `gh`, `gh-lf`, and `ts` wrappers
        call this package, so a profile that replaces it changes how all of them
        authenticate.
      '';
    };

    tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = lib.literalExpression "config.agentix.sandbox.paths.opTokenFile";
      description = ''
        A file from which the profile's `op` loads its service account token.
        When the file is readable, `claude` starts without the token in its
        environment, and the commands it runs get secrets through `op`.
      '';
    };
  };

  config = lib.mkMerge [
    # The wrappers call the CLI even when `op` itself is not on PATH.
    { agentix.unfreePackages = [ "1password-cli" ]; }
    (lib.mkIf cfg.enable {
      home.packages = [ cfg.package ];
      agentix.commands = [ "op" ];
    })
  ];
}
