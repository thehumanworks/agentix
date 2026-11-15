{ config, lib, ... }:

let
  inherit (config.agentix.sandbox.paths) workdir;
in
{
  imports = [ ./options.nix ];

  options.agentix.sandbox.claude.enable =
    lib.mkEnableOption "Claude's sandbox brief and trusted directories"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.claude.enable {
    # Claude starts in these directories without asking.
    agentix.claude.trustedDirectories = [
      config.home.homeDirectory
      workdir
    ];

    # Loaded into every session as ~/.claude/CLAUDE.md.
    programs.claude-code.context = builtins.replaceStrings [ "@workdir@" ] [ workdir ] (
      builtins.readFile ./claude-context.md
    );
  };
}
