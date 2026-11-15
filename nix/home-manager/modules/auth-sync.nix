# agent-auth-sync, which keeps the sign-in files of Codex and pi in step with
# 1Password; see ../../packages/agent-auth-sync/README.md. A refresh token
# works once, so every machine that runs the agents runs it too: in a sandbox,
# `codex` and `pi` start its watcher; on a computer, a launchd agent or a
# systemd user service can keep it running instead.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.authSync;
  inherit (config.home) homeDirectory;
  # Where Home Manager's codex module puts Codex's directory.
  codexHome =
    if config.home.preferXdgDirectories then
      "${config.xdg.configHome}/codex"
    else
      "${homeDirectory}/.codex";
  exe = lib.getExe cfg.package;
in
{
  imports = [ ./1password.nix ];

  options.agentix.authSync = {
    enable =
      lib.mkEnableOption "agent-auth-sync, which keeps Codex's and pi's sign-in files in step with 1Password"
      // {
        default = true;
      };

    files = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {
        "op://agentix/codex/auth" = "${codexHome}/auth.json";
        "op://agentix/pi/auth" = "${config.programs.pi-coding-agent.configDir}/auth.json";
      };
      defaultText = lib.literalExpression ''
        {
          "op://agentix/codex/auth" = "~/.codex/auth.json";
          "op://agentix/pi/auth" = "''${config.programs.pi-coding-agent.configDir}/auth.json";
        }
      '';
      description = ''
        The files to keep in step, by the op:// reference of the 1Password
        field that holds each as one line of JSON. The field, and its item,
        are created when missing.
      '';
    };

    startWithAgents = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether `codex` and `pi` run `agent-auth-sync ensure` before they
        start: unless a watcher runs, it syncs once and leaves one running.
        It does nothing without a service account token, so `op` never asks
        to sign in from the background.
      '';
    };

    service.enable = lib.mkEnableOption ''
      a launchd agent on macOS, or a systemd user service on Linux, that keeps
      `agent-auth-sync watch` running. It reads the token that
      `agent-auth-sync save-token` keeps, so run that once from a shell that
      has OP_SERVICE_ACCOUNT_TOKEN
    '';

    package = lib.mkOption {
      type = lib.types.package;
      readOnly = true;
      description = "agent-auth-sync, built with this profile's `op`, files, and token file.";
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        agentix.authSync.package = pkgs.callPackage ../../packages/agent-auth-sync {
          op = config.agentix.op.package;
          files = lib.concatStringsSep "\n" (lib.mapAttrsToList (ref: path: "${ref} ${path}") cfg.files);
          inherit (config.agentix.op) tokenFile;
        };
        home.packages = [ cfg.package ];
        agentix.commands = [ "agent-auth-sync" ];
      }

      (lib.mkIf (cfg.service.enable && pkgs.stdenv.hostPlatform.isDarwin) {
        launchd.agents.agent-auth-sync = {
          enable = true;
          config = {
            ProgramArguments = [
              exe
              "watch"
            ];
            RunAtLoad = true;
            KeepAlive = true;
            ThrottleInterval = 30;
            ProcessType = "Background";
            StandardOutPath = "${homeDirectory}/Library/Logs/agent-auth-sync.log";
            StandardErrorPath = "${homeDirectory}/Library/Logs/agent-auth-sync.log";
          };
        };
      })

      (lib.mkIf (cfg.service.enable && pkgs.stdenv.hostPlatform.isLinux) {
        systemd.user.services.agent-auth-sync = {
          Unit.Description = "Keep coding agents' auth files in step with 1Password";
          Service = {
            ExecStart = "${exe} watch";
            Restart = "always";
            RestartSec = 30;
          };
          Install.WantedBy = [ "default.target" ];
        };
      })
    ]
  );
}
