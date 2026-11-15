# Any user on any Linux machine or container: a distribution, a bare image,
# or the sandbox, which ./sandbox.nix builds on this. It names no user;
# `lib.mkLinuxHome` in flake.nix sets home.username and home.homeDirectory.
#
# The sandbox modules that work for any user are on: the base commands, which
# only fill gaps a distribution leaves, the manifest, Rex, and OpenSSH. The
# ones that need root or a sandbox that `agentix sandbox up` started stay off,
# since nothing else writes the files that nix/sandbox.toml names: the
# 1Password token file (`op` signs in as usual, or reads
# OP_SERVICE_ACCOUNT_TOKEN), the start in the working directory, Claude's
# sandbox brief, and Tailscale's `ts`, which runs as root.
# `agentix.sandbox.enable = true` turns them on.
{
  config,
  inputs,
  lib,
  pkgs,
  ...
}:

{
  imports = [ ../home.nix ];

  agentix.sandbox = {
    baseCommands.enable = lib.mkDefault true;
    manifest.enable = lib.mkDefault true;
    openssh.enable = lib.mkDefault true;
    rex.enable = lib.mkDefault true;
  };

  agentix.commands = [
    "ps"
    "strace"
  ];

  home = {
    language.base = lib.mkDefault "C.UTF-8";

    # Process and syscall inspection; these packages are Linux-only.
    packages = [
      pkgs.procps
      pkgs.strace
    ];
  };

  # Login shells, which SSH sessions use, load the session variables and the
  # direnv hook from the files this writes.
  programs.bash = {
    enable = true;

    # `ssh HOST COMMAND` starts a shell that reads only .bashrc, so load the
    # session variables there too. The file guards against loading twice.
    bashrcExtra = ''
      . "${config.home.sessionVariablesPackage}/etc/profile.d/hm-session-vars.sh"
    '';

    # A distribution's /etc/profile, such as Debian's, resets PATH, and the
    # Nix profile script runs once per session. A login shell started from
    # another shell, as in `ssh HOST 'bash -lc COMMAND'`, would otherwise lose
    # the Nix profile.
    profileExtra = ''
      case ":$PATH:" in
        *":$HOME/.nix-profile/bin:"*) ;;
        *) PATH="$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH" ;;
      esac
    '';
  };

  # C.UTF-8 needs no locale archive; skip the 220 MB set of every locale.
  i18n.glibcLocales = lib.mkDefault pkgs.glibcLocalesUtf8;

  # Headless by default: the Wayland clipboard tools would bring in D-Bus and
  # systemd. A desktop sets this back to true.
  programs.nixvim.waylandSupport = lib.mkDefault false;

  # `nix run nixpkgs#x`, `nix shell nixpkgs#x`, and `nix-shell -p x` resolve to
  # the revision this configuration was built from, so they reuse the store and
  # agree with the installed tools.
  nix = {
    registry.nixpkgs.to = {
      type = "github";
      owner = "NixOS";
      repo = "nixpkgs";
      inherit (inputs.nixpkgs) rev;
    };
    nixPath = [ "nixpkgs=flake:nixpkgs" ];
  };
}
