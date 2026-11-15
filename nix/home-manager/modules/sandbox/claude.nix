{ config, lib, ... }:

{
  imports = [ ./options.nix ];

  options.agentix.sandbox.claude.enable =
    lib.mkEnableOption "Claude's sandbox brief and trusted directories"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.claude.enable {
    # Claude starts in these directories without asking. Keep /workdir in step
    # with WORKDIR in src/agentix/image.py.
    agentix.claude.trustedDirectories = [
      "/root"
      "/workdir"
    ];

    # Loaded into every session as ~/.claude/CLAUDE.md.
    programs.claude-code.context = ./claude-context.md;
  };
}
