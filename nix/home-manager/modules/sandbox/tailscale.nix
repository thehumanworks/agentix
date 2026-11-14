{ config, pkgs, ... }:

{
  # Sandbox service only. The Mac's native Tailscale app stays in charge there.
  home.packages = [
    pkgs.tailscale
    pkgs.openssh
    (pkgs.writeShellApplication {
      name = "ts";
      runtimeInputs = [
        pkgs.tailscale
        config.agentix.op.package
        pkgs.coreutils
        pkgs.util-linux
        pkgs.jq
        pkgs.openssh
      ];
      text = builtins.readFile ../scripts/ts.sh;
    })
  ];
}
