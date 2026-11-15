# pi, the coding agent CLI (pi.dev, github.com/earendil-works/pi, formerly
# badlogic/pi-mono), from nixpkgs' pi-coding-agent, behind a `pi` wrapper;
# see ./op-run.nix. pi signs in with the providers in ~/.pi/agent/auth.json,
# which agent-auth-sync keeps in step with 1Password (./auth-sync.nix); the
# wrapper starts its watcher. The nixpkgs build turns off pi's update check
# and telemetry.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.pi;
  opRun = import ./op-run.nix { inherit config lib pkgs; };
  authSync = config.agentix.authSync;
in
{
  imports = [
    ./1password.nix
    ./auth-sync.nix
  ];

  options.agentix.pi = {
    enable = lib.mkEnableOption "the pi coding agent and its `pi` wrapper" // {
      default = true;
    };

    secrets = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example.ANTHROPIC_API_KEY = "op://agentix/anthropic/api/key";
      description = ''
        Environment variables that `pi` gets from 1Password, as op://
        references that `op run` resolves when it starts. None by default:
        pi uses the providers in ~/.pi/agent/auth.json. Each provider reads
        its own variable (`pi --help` lists them), so this can add providers.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    agentix.commands = [ "pi" ];

    programs.pi-coding-agent = {
      enable = true;
      package = opRun {
        name = "pi";
        program = lib.getExe pkgs.pi-coding-agent;
        inherit (cfg) secrets;
        bypass = [
          "--version"
          "-v"
          "--help"
          "-h"
        ];
        beforeLaunch = lib.optionalString (authSync.enable && authSync.startWithAgents) ''
          ${lib.getExe authSync.package} ensure || true
        '';
      };
    };
  };
}
