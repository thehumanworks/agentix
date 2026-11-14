{
  config,
  lib,
  pkgs,
  ...
}:

let
  # Each command resolves its host and token. op must already be authenticated.
  mkGh =
    name: host: token:
    pkgs.writeShellApplication {
      inherit name;
      text = ''
        export GH_HOST="${host}"
        export GH_TOKEN="${token}"
        export GH_NO_UPDATE_NOTIFIER=1
        exec ${lib.getExe config.agentix.op.package} run -- ${lib.getExe pkgs.gh} "$@"
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
