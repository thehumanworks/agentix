# Who commits. Shared by the profiles, kept apart from the reusable modules.
{
  programs.git = {
    settings.user = {
      name = "Tomas Roda";
      email = "rodat.dev@proton.me";
    };

    # Match stored remote URLs, independent of where the repository lives.
    includes =
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
  };
}
