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

    # Search, structured data, and HTTP.
    ripgrep
    jq
    fzf
    curl

    # General scripting, Python projects, and shell validation.
    python3
    uv
    bun
    shellcheck

    # Nix language servers, available directly on PATH.
    nixd
    nil

    herdr
  ];
}
