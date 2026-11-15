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

  config.home.packages = lib.mkIf config.agentix.sandbox.tailscale.enable [
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
