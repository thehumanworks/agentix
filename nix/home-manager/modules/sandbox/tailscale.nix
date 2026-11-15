{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [
    ./options.nix
    ../1password.nix
  ];

  # Sandbox service only. The Mac's native Tailscale app stays in charge there.
  options.agentix.sandbox.tailscale.enable =
    lib.mkEnableOption "Tailscale with the `ts` wrapper, which runs it without systemd"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config.agentix.commands = lib.mkIf config.agentix.sandbox.tailscale.enable [
    "ts"
    "tailscale"
  ];

  # Tailscale SSH's server is part of tailscaled and needs nothing from
  # OpenSSH, sftp included. sshd, ssh-keygen, and the ssh client on PATH come
  # from ./openssh.nix, which has a switch of its own.
  config.home.packages = lib.mkIf config.agentix.sandbox.tailscale.enable [
    pkgs.tailscale
    (pkgs.writeShellApplication {
      name = "ts";
      runtimeInputs = [
        pkgs.tailscale
        config.agentix.op.package
        pkgs.coreutils
        pkgs.util-linux
        pkgs.jq
        # `ts ssh` runs `tailscale ssh`, which runs the OpenSSH client with
        # tailscale as its ProxyCommand. This puts the client on the PATH of
        # ts alone, so `ts ssh` works with agentix.sandbox.openssh off.
        pkgs.openssh
      ];
      text = builtins.readFile ../scripts/ts.sh;
    })
  ];
}
