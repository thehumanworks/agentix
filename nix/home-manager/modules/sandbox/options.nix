# What the modules in this directory share: the switch they follow by default,
# and the paths that the agentix command on the host uses too.
{ lib, ... }:

let
  shared = lib.importTOML ../../../sandbox.toml;

  # Read-only, so a profile cannot move a path away from the one the host uses.
  sharedPath =
    name: description:
    lib.mkOption {
      type = lib.types.str;
      default = shared.paths.${name};
      readOnly = true;
      description = "${description} Set in nix/sandbox.toml, which the agentix command reads too.";
    };
in
{
  options.agentix.sandbox = {
    enable = lib.mkEnableOption ''
      the modules for root in a Modal sandbox: the 1Password token file,
      Claude's sandbox brief, the SSH working directory, the manifest, Rex, and
      Tailscale. Each has its own `agentix.sandbox.<name>.enable`, which
      defaults to this value
    '';

    paths = {
      workdir = sharedPath "workdir" "The working directory of the image, where SSH logins start.";
      opTokenFile = sharedPath "opTokenFile" "The root-only file in which a sandbox stores the 1Password token.";
      workdirFile = sharedPath "workdirFile" "The file that names the `--workdir` a sandbox was started with.";
      readyFile = sharedPath "readyFile" "The file a sandbox writes last when it starts.";
    };

    manifestFile = lib.mkOption {
      type = lib.types.str;
      default = shared.manifest;
      readOnly = true;
      description = ''
        Where the manifest goes, relative to the profile. Set in
        nix/sandbox.toml, which the image build reads too.
      '';
    };
  };
}
