# Options that several agentix modules contribute to or read.
{ lib, ... }:

{
  options.agentix = {
    commands = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "rg" ];
      description = ''
        Commands that the enabled modules put on PATH and that agents rely on.
        A module lists the commands it installs. The sandbox manifest lists
        them, and the image build fails if one is missing.
      '';
    };

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
