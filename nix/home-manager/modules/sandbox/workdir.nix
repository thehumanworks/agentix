# SSH sessions start in the home directory. This moves them to the working
# directory of the sandbox, or to the one it was started with.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (config.agentix.sandbox) paths;

  script = pkgs.replaceVars ../scripts/workdir-profile.sh {
    inherit (paths) workdir workdirFile;
  };
in
{
  imports = [ ./options.nix ];

  options.agentix.sandbox.workdir.enable =
    lib.mkEnableOption "the start of SSH sessions in the sandbox working directory"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config.programs.bash = lib.mkIf config.agentix.sandbox.workdir.enable {
    # Login shells, as in an interactive SSH login.
    profileExtra = lib.mkAfter ''
      . ${script}
    '';

    # Bash reads .bashrc without a terminal only for `ssh HOST COMMAND`. Start
    # that command in the working directory too, as a login does.
    bashrcExtra = lib.mkAfter ''
      if [[ $- != *i* ]]; then
        . ${script}
      fi
    '';
  };
}
