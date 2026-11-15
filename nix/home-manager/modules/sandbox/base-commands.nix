# Commands that a Debian system provides and that agents, the Home Manager
# scripts, and `agentix sandbox ssh` expect: archives, a pager, `which`,
# `flock`, the terminal and locale tools. Taken from the pinned nixpkgs, they
# follow flake.lock, and the image build checks that each one on the list is in
# the profile. The sandbox image has no distribution under it, so they come
# only from here.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.sandbox.baseCommands;

  # Only these of glibc's commands. On a distribution that uses this module,
  # its ldconfig and ldd would shadow the system's: dpkg runs ldconfig from
  # PATH, and Nix's ldd misreports Debian's programs. The OCI image adds the
  # rest itself.
  glibcCommands = pkgs.runCommand "glibc-commands" { } ''
    mkdir -p $out/bin
    ln -s ${lib.getBin pkgs.glibc}/bin/iconv ${lib.getBin pkgs.glibc}/bin/locale $out/bin/
  '';
in
{
  imports = [
    ../core.nix
    ./options.nix
  ];

  options.agentix.sandbox.baseCommands.enable =
    lib.mkEnableOption "the archive, pager, terminal, locale, and lock commands of a Linux base system"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf cfg.enable {
    # A fallback: any other package with a command of the same name wins.
    # nixpkgs gives coreutils priority 10 and procps 11, and both have `kill`,
    # as util-linux does; lowPrio would tie with coreutils.
    home.packages = map (lib.setPrio 20) [
      pkgs.bzip2
      pkgs.getent
      glibcCommands # iconv, locale
      pkgs.gnutar
      pkgs.gzip
      pkgs.hostname
      pkgs.less
      pkgs.ncurses # clear, infocmp, reset, tput
      pkgs.openssl
      pkgs.util-linux # flock, column, more, script, setsid
      pkgs.which
      pkgs.xz
    ];

    agentix.commands = [
      "bzip2"
      "clear"
      "flock"
      "getent"
      "gzip"
      "hostname"
      "iconv"
      "infocmp"
      "less"
      "locale"
      "openssl"
      "tar"
      "tput"
      "which"
      "xz"
    ];
  };
}
