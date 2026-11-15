# The switch that the modules in this directory follow by default.
{ lib, ... }:

{
  options.agentix.sandbox.enable = lib.mkEnableOption ''
    the modules for root in a Modal sandbox: the 1Password token file,
    Claude's sandbox brief, Rex, and Tailscale. Each has its own
    `agentix.sandbox.<name>.enable`, which defaults to this value
  '';
}
