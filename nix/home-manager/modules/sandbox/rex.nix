# Rex, the terminal multiplexer, set up as on the nixos box with the module
# from Rex's own flake.
#
# The nixos box also runs the server as a systemd user service with its own
# Tailscale node. A sandbox has no systemd, so it uses Rex's built-in start
# instead: the first rex command starts a server in the background, which
# keeps its sessions across commands and SSH logins and exits once the last
# session closes. `rex server start` starts one that stays until the sandbox
# ends. No Tailscale node is enrolled; reach the server over SSH, as
# `rex -S ssh://root@HOST`.
{ inputs }:
{ config, lib, ... }:

{
  imports = [
    ./options.nix
    inputs.rex.homeManagerModules.default
  ];

  options.agentix.sandbox.rex.enable =
    lib.mkEnableOption "Rex, its terminfo entry, and its ssh guard"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.rex.enable {
    # Installs rex, writes its xterm-rex terminfo entry to ~/.terminfo, and adds
    # its ssh guard to ~/.ssh/config.
    programs.rex.enable = true;

    # Home Manager writes ~/.ssh/config only when this is on. The guard there
    # keeps ssh from a Rex terminal from sending TERM=xterm-rex to hosts that
    # lack the entry. With both the entry and the guard in place, Rex terminals
    # use xterm-rex; otherwise they fall back to xterm-256color.
    programs.ssh = {
      enable = true;
      enableDefaultConfig = false;
    };
  };
}
