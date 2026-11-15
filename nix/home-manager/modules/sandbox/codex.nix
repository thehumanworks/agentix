# Codex as a sandbox runs it: no approval prompts and no sandbox of its own,
# as Claude runs in auto mode there. The sandbox is a disposable VM, and
# Codex's Linux sandbox (bubblewrap) does not run in it. It trusts the home
# and working directories, and loads the sandbox brief.
{ config, lib, ... }:

let
  inherit (config.agentix.sandbox.paths) workdir;
in
{
  imports = [
    ./options.nix
    ../codex.nix
  ];

  options.agentix.sandbox.codex.enable =
    lib.mkEnableOption "Codex's sandbox settings: full access, trusted directories, and the brief"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf (config.agentix.sandbox.codex.enable && config.agentix.codex.enable) {
    programs.codex = {
      settings = {
        approval_policy = "never";
        sandbox_mode = "danger-full-access";
        notice.hide_full_access_warning = true;
        projects = lib.genAttrs [ config.home.homeDirectory workdir ] (_: {
          trust_level = "trusted";
        });
      };
      # Loaded into every session as ~/.codex/AGENTS.md.
      context = config.agentix.sandbox.brief;
    };
  };
}
