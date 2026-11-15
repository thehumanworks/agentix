# Options that several agentix modules contribute to or read.
{ lib, ... }:

{
  options.agentix = {
    unfreePackages = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "claude-code" ];
      description = ''
        Names of the unfree packages that the agentix modules install, as
        `lib.getName` gives them. A module that adds an unfree package lists it
        here. The modules do not change nixpkgs settings themselves; the
        configuration that imports them allows these names, for example with
        `nixpkgs.config.allowUnfreePredicate`.
      '';
    };

    identity = {
      name = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "Ada Lovelace";
        description = "The name Git records as the author of commits.";
      };

      email = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "ada@example.com";
        description = "The email address Git records as the author of commits.";
      };
    };
  };
}
