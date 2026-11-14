{ pkgs, op, ... }:

let
  # Each command resolves its host and token. op must already be authenticated.
  mkGh = name: host: token:
    pkgs.writeShellApplication {
      inherit name;
      text = ''
        export GH_HOST="${host}"
        export GH_TOKEN="${token}"
        export GH_NO_UPDATE_NOTIFIER=1
        exec ${op}/bin/op run -- ${pkgs.gh}/bin/gh "$@"
      '';
    };

  personalGh = mkGh "gh" "op://agentix/gh/dev/host" "op://agentix/gh/dev/token";
  workGh = mkGh "gh-lf" "op://agentix/gh/work/host" "op://agentix/gh/work/token";
in
{
  programs.git = {
    enable = true;
    settings = {
      user = {
        name = "Tomas Roda";
        email = "rodat.dev@proton.me";
      };
      init.defaultBranch = "main";
      push.autoSetupRemote = true;
      push.default = "simple";
      remote.pushDefault = "origin";
      pull.ff = "only";
      fetch.prune = true;

      # Host-specific helpers work for ordinary git clone/fetch/push over HTTPS.
      credential."https://leadforensics.ghe.com".helper = [
        ""
        "${workGh}/bin/gh-lf auth git-credential"
      ];
    };

    # Match stored remote URLs, independent of where the repository lives.
    includes = map (url: {
      condition = "hasconfig:remote.*.url:${url}";
      contents.user.email = "rodat@leadforensics.com";
    }) [
      "https://leadforensics.ghe.com/**"
      "git@leadforensics.ghe.com:*/**"
      "ssh://git@leadforensics.ghe.com/**"
    ];
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
      hosts = [ "https://github.com" "https://gist.github.com" ];
    };
  };

  home.packages = [
    workGh
  ];
}
