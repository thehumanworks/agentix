# agentix-sync, which `agentix sandbox sync` runs on the host and in the
# sandbox. The host's agentix command runs the same script from the
# repository, so both sides have one copy of the code; `nix run
# .#agentix-sync` runs this package.
{
  writeShellApplication,
  coreutils,
  findutils,
  gawk,
  git,
  gnused,
}:

writeShellApplication {
  name = "agentix-sync";
  runtimeInputs = [
    coreutils
    findutils
    gawk
    git
    gnused
  ];
  text = builtins.readFile ../home-manager/modules/scripts/agentix-sync.sh;
}
