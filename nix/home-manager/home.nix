# Shared by every profile. Machine-specific settings live in ./profiles.
{
  imports = [
    ./modules/1password.nix
    ./modules/claude-code.nix
    ./modules/git.nix
    ./modules/language-servers.nix
    ./modules/nixvim.nix
    ./modules/tools.nix
  ];

  # Compatibility baseline for stateful settings; do not bump on routine updates.
  home.stateVersion = "26.05";

  programs.home-manager.enable = true;
}
