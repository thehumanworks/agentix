# OpenSSH, for `agentix sandbox ssh start --engine=openssh`: that command runs
# src/agentix/scripts/openssh-ssh.sh in the sandbox, which starts this sshd
# behind Modal's TCP tunnel and makes its host key with this ssh-keygen. The
# package holds the client too, so ssh, scp, and sftp come with it. With the
# module off, the image has none of these commands and no sshd account, and
# that engine refuses to start.
{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [
    ../core.nix
    ./options.nix
  ];

  options.agentix.sandbox.openssh.enable =
    lib.mkEnableOption "OpenSSH: the sshd that `sandbox ssh start --engine=openssh` runs, and the ssh client"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.openssh.enable {
    home.packages = [ pkgs.openssh ];
    agentix.commands = [
      "scp"
      "sftp"
      "ssh"
      "ssh-keygen"
      "sshd"
    ];

    # Before authentication, sshd drops to this account in /var/empty, the
    # directory nixpkgs builds it with; `sshd -t` fails without either. The
    # image has no useradd, so it writes the account itself.
    agentix.sandbox.accounts.sshd = {
      uid = 74;
      description = "SSH privilege separation";
      home = "/var/empty";
    };
  };
}
