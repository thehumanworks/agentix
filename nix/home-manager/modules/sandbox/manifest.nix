# A JSON file in the profile that lists the commands the configuration
# provides and the paths it shares with the agentix command on the host. The
# image build reads it to check the image; see src/agentix/image.py.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.sandbox;
in
{
  imports = [
    ../core.nix
    ./options.nix
  ];

  options.agentix.sandbox.manifest.enable =
    lib.mkEnableOption "the manifest of commands and shared paths"
    // {
      default = cfg.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config.home.packages = lib.mkIf cfg.manifest.enable [
    (pkgs.writeTextDir cfg.manifestFile (
      builtins.toJSON {
        commands = lib.sort lib.lessThan (lib.unique config.agentix.commands);
        inherit (cfg) paths;
      }
    ))
  ];
}
