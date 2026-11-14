{
  config,
  lib,
  pkgs,
  ...
}:

let
  # Each command resolves its host and token. op must already be authenticated.
  #
  # op run replaces a secret in the child's output with a placeholder. Git
  # reads the token from the output of `gh auth git-credential`, so that one
  # command runs unmasked; in any other output the token stays concealed.
  mkGh =
    name: host: token:
    pkgs.writeShellApplication {
      inherit name;
      text = ''
        export GH_HOST="${host}"
        export GH_TOKEN="${token}"
        export GH_NO_UPDATE_NOTIFIER=1
        op_run=(${lib.getExe config.agentix.op.package} run)
        if [[ ''${1:-} == auth && ''${2:-} == git-credential ]]; then
          op_run+=(--no-masking)
        fi
        exec "''${op_run[@]}" -- ${lib.getExe pkgs.gh} "$@"
      '';
    };

  personalGh = mkGh "gh" "op://agentix/gh/dev/host" "op://agentix/gh/dev/token";
  workGh = mkGh "gh-lf" "op://agentix/gh/work/host" "op://agentix/gh/work/token";
in
{
  # The committer identity is set by the profile; see ../profiles/identity.nix.
  programs.git = {
    enable = true;
    lfs.enable = true;
    settings = {
      init.defaultBranch = "main";
      push.autoSetupRemote = true;
      push.default = "simple";
      remote.pushDefault = "origin";
      pull.ff = "only";
      fetch.prune = true;

      # Host-specific helpers work for ordinary git clone/fetch/push over HTTPS.
      credential."https://leadforensics.ghe.com".helper = [
        ""
        "${lib.getExe workGh} auth git-credential"
      ];
    };
  };

  programs.gh = {
    enable = true;
    package = personalGh;
    settings = {
      git_protocol = "https";
      prompt = "enabled";
    };
    gitCredentialHelper = {
      enable = true;
      hosts = [
        "https://github.com"
        "https://gist.github.com"
      ];
    };
  };

  home.packages = [ workGh ];
}
