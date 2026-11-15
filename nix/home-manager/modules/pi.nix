# pi, the coding agent CLI (pi.dev, github.com/earendil-works/pi, formerly
# badlogic/pi-mono), from nixpkgs' pi-coding-agent, as a `pi` wrapper that
# gets its provider's API key from 1Password each time it starts; see
# ./op-run.nix. The nixpkgs build turns off pi's update check and telemetry.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.pi;
  opRun = import ./op-run.nix { inherit config lib pkgs; };
in
{
  imports = [ ./1password.nix ];

  options.agentix.pi = {
    enable = lib.mkEnableOption "the pi coding agent and its `pi` wrapper" // {
      default = true;
    };

    secrets = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default.OPENAI_API_KEY = "op://agentix/openai/api/key";
      example.ANTHROPIC_API_KEY = "op://agentix/anthropic/api/key";
      description = ''
        Environment variables that `pi` gets from 1Password, as op://
        references that `op run` resolves when it starts. Each provider reads
        its own variable (`pi --help` lists them), so this chooses the
        providers pi can use.
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
      };
    };
  };
}
