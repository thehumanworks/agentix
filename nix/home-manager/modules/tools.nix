{ pkgs, ... }:

{
  home.packages = with pkgs; [
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

    # Python, JavaScript, and shell.
    python3
    uv
    bun
    nodejs
    shellcheck
    shfmt

    # Nix formatting and linting; the flake's checks run the same tools.
    nixfmt
    statix
    deadnix

    herdr
  ];

  # Loads a project's .envrc. `use flake` environments are cached and kept from
  # garbage collection by nix-direnv.
  programs.direnv = {
    enable = true;
    nix-direnv.enable = true;
  };
}
