# The Codex CLI from nixpkgs, behind a `codex` wrapper; see ./op-run.nix.
# Codex signs in with the ChatGPT login in ~/.codex/auth.json, which
# agent-auth-sync keeps in step with 1Password (./auth-sync.nix); the
# wrapper starts its watcher. agentix.codex.secrets can add an API key.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.codex;
  opRun = import ./op-run.nix { inherit config lib pkgs; };
  authSync = config.agentix.authSync;

  # nixpkgs' build leaves the symbol tables in the binaries. Stripping them
  # takes about 155 MiB off; logs_client, a log viewer for Codex's
  # developers, goes too. Only on Linux, where strip -s is GNU's.
  codex =
    if pkgs.stdenv.hostPlatform.isLinux then
      pkgs.runCommand "codex-${pkgs.codex.version}"
        {
          inherit (pkgs.codex) version meta;
          nativeBuildInputs = [ pkgs.binutils ];
        }
        ''
          cp -a ${pkgs.codex} $out
          chmod -R u+w $out
          rm $out/bin/logs_client
          strip -s $out/bin/codex $out/bin/codex-code-mode-host
        ''
    else
      pkgs.codex;
in
{
  imports = [
    ./1password.nix
    ./auth-sync.nix
  ];

  options.agentix.codex = {
    enable = lib.mkEnableOption "the Codex CLI, with its settings and `codex` wrapper" // {
      default = true;
    };

    secrets = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example.CODEX_API_KEY = "op://Private/OpenAI/credential";
      description = ''
        Environment variables that `codex` gets from 1Password, as op://
        references that `op run` resolves when it starts. None by default:
        Codex uses the ChatGPT login in ~/.codex/auth.json. A CODEX_API_KEY
        here makes `codex exec` bill that API key instead.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    agentix.commands = [ "codex" ];

    programs.codex = {
      enable = true;
      # Let Codex save what it learns, such as trusted projects; declared
      # values win on activation.
      mutableSettings = true;
      settings = {
        # Nix installs and updates it.
        check_for_update_on_startup = false;
        # The login stays in ~/.codex/auth.json, not a keyring, so
        # agent-auth-sync can keep it in step on every machine.
        cli_auth_credentials_store = "file";
      };
      # The version is kept so Home Manager picks the settings format for it.
      package =
        (opRun {
          name = "codex";
          program = lib.getExe codex;
          inherit (cfg) secrets;
          bypass = [
            "--version"
            "-V"
            "--help"
            "-h"
          ];
          beforeLaunch = lib.optionalString (authSync.enable && authSync.startWithAgents) ''
            ${lib.getExe authSync.package} ensure ${lib.escapeShellArg authSync.codexFile} || true
          '';
        }).overrideAttrs
          { inherit (codex) version; };
    };
  };
}
