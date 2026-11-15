# Who commits. flake.nix names this file for each home it builds; another
# person adds a file of their own here and points a home at it.
{
  agentix.identity = {
    name = "Tomas Roda";
    email = "rodat.dev@proton.me";
  };

  # Commits to work repositories use the work address. Match stored remote
  # URLs, independent of where the repository lives.
  programs.git.includes =
    map
      (url: {
        condition = "hasconfig:remote.*.url:${url}";
        contents.user.email = "rodat@leadforensics.com";
      })
      [
        "https://leadforensics.ghe.com/**"
        "git@leadforensics.ghe.com:*/**"
        "ssh://git@leadforensics.ghe.com/**"
      ];
}
