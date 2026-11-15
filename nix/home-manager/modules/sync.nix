# agentix-sync, the side of `agentix sandbox sync` that runs in the sandbox:
# it copies uncommitted changes between two checkouts of one repository with
# Git. The host runs the same script; see ../../packages/agentix-sync.nix.
{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [ ./core.nix ];

  options.agentix.sync.enable =
    lib.mkEnableOption "agentix-sync, which copies uncommitted changes between checkouts"
    // {
      default = true;
    };

  config = lib.mkIf config.agentix.sync.enable {
    home.packages = [ (pkgs.callPackage ../../packages/agentix-sync.nix { }) ];
    agentix.commands = [ "agentix-sync" ];
  };
}
