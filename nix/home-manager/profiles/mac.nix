# Apple Silicon Mac. Shell startup files and system settings stay unmanaged.
{
  imports = [
    ../home.nix
    ./identity.nix
  ];

  home = {
    username = "tomas";
    homeDirectory = "/Users/tomas";
  };
}
