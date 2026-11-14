{
  config,
  lib,
  pkgs,
  ...
}:

{
  options.agentix.op = {
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
      example = "/run/agentix/op-token";
      description = ''
        A file from which the profile's `op` loads its service account token.
        When the file is readable, `claude` starts without the token in its
        environment, and the commands it runs get secrets through `op`.
      '';
    };
  };

  config.home.packages = [ config.agentix.op.package ];
}
