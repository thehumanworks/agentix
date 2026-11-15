# Root in a sandbox: the image that ../../oci builds, with no distribution
# under it, no systemd, and no Nix daemon. Modal runs it, as do Docker and
# Podman. The Linux settings it shares with any other machine are in
# ./linux.nix.
{
  imports = [ ./linux.nix ];

  # The base commands, the 1Password token file, Claude's sandbox brief, the
  # SSH working directory, the manifest, Rex, OpenSSH, and Tailscale.
  agentix.sandbox.enable = true;

  home = {
    username = "root";
    homeDirectory = "/root";

    # Replace a distribution's defaults for root, when there are any, instead
    # of failing on them. The sandbox modules add the start in the working
    # directory to both.
    file = {
      ".bashrc".force = true;
      ".profile".force = true;
    };
  };

  # The prompt that Debian's /etc/bash.bashrc sets, which the image lacks:
  # user, host, and directory.
  programs.bash.initExtra = ''
    PS1='\u@\h:\w\$ '
  '';

  # No user systemd here; skip the unit files and their reload step.
  systemd.user.enable = false;
}
