# The Home Manager modules that flake.nix exports as `homeManagerModules`.
# Each one has an `agentix.<name>.enable` option. `default` imports them all:
# the shared modules start enabled, and the sandbox ones follow
# `agentix.sandbox.enable`, which is off.
{ inputs, lib }:

let
  # A module that uses this flake's inputs gets them here, not from the
  # configuration that imports it, whose `inputs` are its own. The key lets
  # several exports import it without declaring its options twice.
  withInputs = file: {
    key = toString file;
    imports = [ (lib.modules.importApply file { inherit inputs; }) ];
  };

  claude-code = withInputs ./modules/claude-code.nix;
  nix-index = withInputs ./modules/nix-index.nix;
  nixvim = withInputs ./modules/nixvim.nix;
  sandbox-rex = withInputs ./modules/sandbox/rex.nix;
in
{
  "1password" = ./modules/1password.nix;
  inherit
    claude-code
    nix-index
    nixvim
    sandbox-rex
    ;
  git = ./modules/git.nix;
  language-servers = ./modules/language-servers.nix;
  sync = ./modules/sync.nix;
  tools = ./modules/tools.nix;

  # Claude's sandbox settings extend the Claude module, so it comes along.
  sandbox-claude.imports = [
    ./modules/sandbox/claude.nix
    claude-code
  ];
  sandbox-base-commands = ./modules/sandbox/base-commands.nix;
  sandbox-browser = ./modules/sandbox/browser.nix;
  sandbox-cc = ./modules/sandbox/cc.nix;
  sandbox-manifest = ./modules/sandbox/manifest.nix;
  sandbox-op-token = ./modules/sandbox/op-token.nix;
  sandbox-openssh = ./modules/sandbox/openssh.nix;
  sandbox-tailscale = ./modules/sandbox/tailscale.nix;
  sandbox-workdir = ./modules/sandbox/workdir.nix;

  default.imports = [
    ./modules/sandbox/base-commands.nix
    ./modules/sandbox/browser.nix
    ./modules/sandbox/cc.nix
    ./modules/sandbox/claude.nix
    ./modules/sandbox/manifest.nix
    ./modules/sandbox/op-token.nix
    ./modules/sandbox/openssh.nix
    sandbox-rex
    ./modules/sandbox/tailscale.nix
    ./modules/sandbox/workdir.nix
    ./modules/1password.nix
    claude-code
    ./modules/git.nix
    ./modules/language-servers.nix
    nix-index
    nixvim
    ./modules/sync.nix
    ./modules/tools.nix
  ];
}
