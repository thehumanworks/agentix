# Root in a Modal sandbox: a Debian container with no systemd and no Nix daemon.
{
  config,
  inputs,
  pkgs,
  ...
}:

{
  imports = [ ../home.nix ];

  # The 1Password token file, Claude's sandbox brief, the SSH working
  # directory, the manifest, Rex, and Tailscale.
  agentix.sandbox.enable = true;
  agentix.commands = [
    "ps"
    "strace"
  ];

  home = {
    username = "root";
    homeDirectory = "/root";
    language.base = "C.UTF-8";

    # Process and syscall inspection; these packages are Linux-only.
    packages = [
      pkgs.procps
      pkgs.strace
    ];

    # Replace the Debian defaults for root instead of failing on them. The
    # sandbox modules add the start in the working directory to both.
    file = {
      ".bashrc".force = true;
      ".profile".force = true;
    };
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

    # Debian's /etc/profile resets PATH, and the Nix profile script runs once
    # per session. A login shell started from another shell, as in
    # `ssh HOST 'bash -lc COMMAND'`, would otherwise lose the Nix profile.
    profileExtra = ''
      case ":$PATH:" in
        *":$HOME/.nix-profile/bin:"*) ;;
        *) PATH="$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH" ;;
      esac
    '';
  };

  # C.UTF-8 needs no locale archive; skip the 220 MB set of every locale.
  i18n.glibcLocales = pkgs.glibcLocalesUtf8;

  # No user systemd here; skip the unit files and their reload step.
  systemd.user.enable = false;

  # Headless: the Wayland clipboard tools would bring in D-Bus and systemd.
  programs.nixvim.waylandSupport = false;

  # `nix run nixpkgs#x`, `nix shell nixpkgs#x`, and `nix-shell -p x` resolve to
  # the revision this image was built from, so they reuse the store and agree
  # with the installed tools.
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
