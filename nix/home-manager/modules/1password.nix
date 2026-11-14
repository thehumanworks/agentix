{ pkgs, ... }:

let
  # In a sandbox, op loads the service account token that `agentix sandbox up`
  # stored, so SSH sessions need no token forwarding. The Mac keeps plain op.
  op =
    if pkgs.stdenv.hostPlatform.isLinux then
      pkgs.writeShellApplication {
        name = "op";
        text = builtins.replaceStrings
          [ "@op@" ]
          [ "${pkgs._1password-cli}/bin/op" ]
          (builtins.readFile ./scripts/op.sh);
      }
    else
      pkgs._1password-cli;
in
{
  # The other modules call this package, so their wrappers find the token too.
  _module.args.op = op;

  home.packages = [ op ];
}
