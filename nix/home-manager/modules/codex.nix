# The Codex CLI from nixpkgs, as a `codex` wrapper that gets its API key from
# 1Password each time it starts; see ./op-run.nix.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.codex;
  opRun = import ./op-run.nix { inherit config lib pkgs; };

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
  imports = [ ./1password.nix ];

  options.agentix.codex = {
    enable = lib.mkEnableOption "the Codex CLI, with its settings and `codex` wrapper" // {
      default = true;
    };

    secrets = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default.CODEX_API_KEY = "op://agentix/openai/api/key";
      example.CODEX_API_KEY = "op://Private/OpenAI/credential";
      description = ''
        Environment variables that `codex` gets from 1Password, as op://
        references that `op run` resolves when it starts. `codex exec` reads
        its API key from CODEX_API_KEY; an interactive `codex` uses the
        login that `codex login` stores in ~/.codex.
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
        }).overrideAttrs
          { inherit (codex) version; };
    };
  };
}
