{ pkgs, ... }:

{
  imports = [
    ./modules/1password.nix
    ./modules/claude-code.nix
    ./modules/git.nix
    ./modules/tools.nix
    ./modules/nixvim.nix
    ./modules/tailscale.nix
  ];

  # Compatibility baseline for stateful settings; do not bump on routine updates.
  home.stateVersion = "26.05";

  programs.home-manager.enable = true;
}
