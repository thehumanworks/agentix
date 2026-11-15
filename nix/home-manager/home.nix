# Shared by every profile. flake.nix adds the agentix modules, which start
# enabled, and the user file; machine-specific settings live in ./profiles.
{ config, lib, ... }:

{
  # Compatibility baseline for stateful settings; do not bump on routine updates.
  home.stateVersion = "26.05";

  programs.home-manager.enable = true;

  # Allow the unfree packages that the enabled modules list, and no others.
  nixpkgs.config.allowUnfreePredicate =
    pkg: builtins.elem (lib.getName pkg) config.agentix.unfreePackages;
}
