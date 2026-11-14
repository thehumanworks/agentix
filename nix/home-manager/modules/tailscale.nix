{ lib, pkgs, op, ... }:

{
  # Sandbox service only. Keep the Mac's native Tailscale app in charge there.
  home.packages = lib.optionals pkgs.stdenv.hostPlatform.isLinux [
    pkgs.tailscale
    pkgs.openssh
    (pkgs.writeShellApplication {
      name = "ts";
      runtimeInputs = [
        pkgs.tailscale
        op
        pkgs.coreutils
        pkgs.util-linux
        pkgs.jq
        pkgs.openssh
      ];
      text = builtins.readFile ./scripts/ts.sh;
    })
  ];
}
