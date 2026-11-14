{ lib, pkgs, ... }:

{
  # Sandbox service only. Keep the Mac's native Tailscale app in charge there.
  home.packages = lib.optionals pkgs.stdenv.hostPlatform.isLinux [
    pkgs.tailscale
    pkgs.openssh
    (pkgs.writeShellApplication {
      name = "ts";
      runtimeInputs = [
        pkgs.tailscale
        pkgs._1password-cli
        pkgs.coreutils
        pkgs.util-linux
        pkgs.jq
        pkgs.openssh
      ];
      text = builtins.readFile ./scripts/ts.sh;
    })
  ];
}
