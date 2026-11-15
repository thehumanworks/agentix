# pi in a sandbox: it loads the sandbox brief. pi asks no permissions, so
# there is nothing else to turn off.
{ config, lib, ... }:

{
  imports = [
    ./options.nix
    ../pi.nix
  ];

  options.agentix.sandbox.pi.enable = lib.mkEnableOption "pi's sandbox brief" // {
    default = config.agentix.sandbox.enable;
    defaultText = lib.literalExpression "config.agentix.sandbox.enable";
  };

  config = lib.mkIf (config.agentix.sandbox.pi.enable && config.agentix.pi.enable) {
    # Loaded into every session as ~/.pi/agent/AGENTS.md.
    programs.pi-coding-agent.context = config.agentix.sandbox.brief;
  };
}
