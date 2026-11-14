{
  description = "Portable Home Manager environments for Modal and macOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    nixvim.url = "github:nix-community/nixvim";
    home-manager = {
      url = "github:nix-community/home-manager/master";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, nixvim, ... }:
    let
      mkHome = system: username: homeDirectory:
        home-manager.lib.homeManagerConfiguration {
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfreePredicate = pkg:
              builtins.elem (nixpkgs.lib.getName pkg) [ "claude-code" "1password-cli" ];
          };
          modules = [
            nixvim.homeModules.nixvim
            ./home-manager/home.nix
            { home = { inherit username homeDirectory; }; }
          ];
        };
    in {
      homeConfigurations = {
        modal = mkHome "x86_64-linux" "root" "/root";
        modal-arm = mkHome "aarch64-linux" "root" "/root";
        mac = mkHome "aarch64-darwin" "tomas" "/Users/tomas";
        mac-intel = mkHome "x86_64-darwin" "tomas" "/Users/tomas";
      };
    };
}
