{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.tools;
in
{
  imports = [ ./core.nix ];

  options.agentix.tools = {
    enable = lib.mkEnableOption "the command line tools and direnv" // {
      default = true;
    };

    packages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.htop ]";
      description = ''
        The tools to install. This module defines the standard set; a list in
        another module adds to it, and `lib.mkForce` replaces it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = cfg.packages;

    # The main command of each tool, so a tool added to or removed from the
    # list is checked with it. bind (dig), coreutils, and PostgreSQL (psql)
    # name none, and Python's is versioned.
    agentix.commands =
      lib.concatMap (pkg: lib.optional (pkg.meta ? mainProgram) pkg.meta.mainProgram) cfg.packages
      ++ [
        "dig"
        "psql"
        "python3"
        "direnv"
      ];

    agentix.tools.packages = with pkgs; [
      # Consistent shell and text tools on Linux and macOS.
      bash
      coreutils
      findutils
      gnugrep
      gnused
      gawk
      diffutils
      patch
      file
      tree

      # Search and structured data.
      ripgrep
      fd
      fzf
      ast-grep
      jq
      yq-go
      sqlite
      # psql, with the server's commands; initdb and postgres refuse to run
      # as root.
      postgresql

      # Network and transfer.
      curl
      wget
      rsync
      dnsutils
      lsof

      # Archives.
      zip
      unzip
      zstd

      # Builds and task runners.
      gnumake
      just
      pkg-config

      # Benchmarks, rerunning on change, and structural diffs (difft).
      hyperfine
      watchexec
      difftastic

      # Python, JavaScript, and shell.
      python3
      uv
      bun
      nodejs
      shellcheck
      shfmt

      # Nix formatting and linting; the flake's checks run the same tools.
      # nom shows a build's progress: `nom build`, or `nix build ... |& nom`.
      nixfmt
      statix
      deadnix
      nix-output-monitor

      herdr
    ];

    # Loads a project's .envrc. `use flake` environments are cached and kept from
    # garbage collection by nix-direnv.
    programs.direnv = {
      enable = true;
      nix-direnv.enable = true;
    };
  };
}
