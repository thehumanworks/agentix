# pi, the coding agent CLI (pi.dev, github.com/earendil-works/pi, formerly
# badlogic/pi-mono), from nixpkgs' pi-coding-agent, behind a `pi` wrapper;
# see ./op-run.nix. pi signs in with the providers in ~/.pi/agent/auth.json,
# which agent-auth-sync keeps in step with 1Password (./auth-sync.nix); the
# wrapper starts its watcher. The nixpkgs build turns off pi's update check
# and telemetry.
#
# The settings choose the default and scoped models, and list the pi packages
# of ./pi-packages, which the image holds already installed: pi finds them
# where `pi install` puts them and does not fetch them at start.
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
  inherit (config.programs.pi-coding-agent) configDir;

  # pi's user packages, as `pi install` lists them in settings.json. Unpinned,
  # as given: ./pi-packages/package-lock.json pins what the image installs,
  # and `pi update --extensions` can move them on later.
  packages = [
    "npm:pi-open-tui"
    "npm:@nothumanwork/pi-tps"
    "npm:@nothumanwork/pi-background-compaction"
    "npm:@nothumanwork/pi-idle-compaction"
    "npm:@nothumanwork/pi-fast"
    "npm:@nothumanwork/pi-worktree"
  ];
  packageJson = lib.importJSON ./pi-packages/package.json;

  # The packages and their dependencies as npm installs them, fetched by the
  # hashes in the lock file. --legacy-peer-deps as pi passes it: pi supplies
  # its own packages, the peers, to extensions.
  nodeModules = pkgs.importNpmLock.buildNodeModules {
    npmRoot = ./pi-packages;
    inherit (pkgs) nodejs;
    derivationArgs = {
      npmFlags = [ "--legacy-peer-deps" ];
      # npm's record of the install names each tarball, which would keep them
      # all in the closure; npm rebuilds it from node_modules.
      postInstall = "rm $out/node_modules/.package-lock.json";
    };
  };
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

    assertions = [
      {
        assertion =
          lib.sort lib.lessThan (map (lib.removePrefix "npm:") packages)
          == lib.attrNames packageJson.dependencies;
        message = "The pi packages in pi.nix and pi-packages/package.json differ.";
      }
    ];

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

      # pi writes settings.json itself, for `pi install`, `/settings`, and a
      # default model saved from `/model`. Declared values win on activation.
      mutableSettings = true;
      settings = {
        defaultProvider = "openai";
        defaultModel = "gpt-6-astra";
        # The models Ctrl+P cycles through, as `/scoped-models` saves them.
        enabledModels = [
          "openai/gpt-6-astra"
          "openai/gpt-6.1-sol"
        ];
        inherit packages;
      };

      # pi's catalog gives both models a context window of 272000 tokens.
      models.providers.openai.modelOverrides =
        lib.genAttrs
          [
            "gpt-6-astra"
            "gpt-6.1-sol"
          ]
          (_: {
            contextWindow = 400000;
          });
    };

    # The packages, where `pi install` would put them; see
    # ./scripts/pi-packages.sh.
    home.activation.piPackages = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${
        lib.getExe (
          pkgs.writeShellApplication {
            name = "pi-packages";
            runtimeInputs = [
              pkgs.coreutils
              pkgs.findutils
              pkgs.jq
            ];
            text = builtins.readFile ./scripts/pi-packages.sh;
          }
        )
      } ${
        lib.escapeShellArgs [
          "${configDir}/npm"
          "${nodeModules}"
        ]
      }
    '';
  };
}
