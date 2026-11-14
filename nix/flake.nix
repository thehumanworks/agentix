{
  description = "Portable Home Manager environments for Modal sandboxes and macOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager/master";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Nixvim tests against its own nixpkgs pin and advises against `follows`.
    # One package set keeps the image half the size; `nix flake check` and the
    # image build's editor smoke test catch a mismatch after an update.
    nixvim = {
      url = "github:nix-community/nixvim";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Claude Code plugin marketplace. Not a flake, only a source tree. Move to
    # its newest commit with `nix flake update claude-mods`.
    claude-mods = {
      url = "github:thehumanworks/claude-mods";
      flake = false;
    };
    # Rex, the terminal multiplexer: the published release binary, on the
    # snapshot channel the nixos box uses. Both the flake and the binary
    # download without credentials. Rex's flake allows its own unfree licence.
    # It needs only glibc, PAM, and patchelf from nixpkgs, so it follows this
    # flake's pin instead of bringing a second package set into the image.
    # Move to the newest snapshot with `uv run agentix flake update rex`.
    rex = {
      url = "https://pkgs.superlogical.com/rex/nix/snapshot.tar.gz";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      home-manager,
      nixvim,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      # One entry per machine type. The profile holds everything that differs.
      homes = {
        modal = {
          system = "x86_64-linux";
          profile = ./home-manager/profiles/sandbox.nix;
        };
        modal-arm = {
          system = "aarch64-linux";
          profile = ./home-manager/profiles/sandbox.nix;
        };
        mac = {
          system = "aarch64-darwin";
          profile = ./home-manager/profiles/mac.nix;
        };
      };

      forAllSystems = lib.genAttrs (lib.unique (lib.mapAttrsToList (_: home: home.system) homes));

      pkgsFor = forAllSystems (
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate =
            pkg:
            builtins.elem (lib.getName pkg) [
              "claude-code"
              "1password-cli"
            ];
        }
      );

      mkHome =
        { system, profile }:
        home-manager.lib.homeManagerConfiguration {
          pkgs = pkgsFor.${system};
          extraSpecialArgs = { inherit inputs; };
          modules = [
            nixvim.homeModules.nixvim
            profile
          ];
        };
    in
    {
      homeConfigurations = lib.mapAttrs (_: mkHome) homes;

      # `nix fmt` formats every Nix file under this directory. The root is tied
      # to flake.nix: outside a Git checkout treefmt otherwise picks its own
      # root, and as root in a sandbox that rewrote files in /nix/store.
      formatter = forAllSystems (
        system: pkgsFor.${system}.nixfmt-tree.override { settings.tree-root-file = "flake.nix"; }
      );

      # `nix flake check` builds this system's profiles and lints the sources.
      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor.${system};
          lint =
            name: tools: command:
            pkgs.runCommand "agentix-${name}" { nativeBuildInputs = tools; } ''
              cd ${self}
              ${command}
              touch $out
            '';
        in
        {
          format = lint "format" [
            pkgs.nixfmt
            pkgs.findutils
          ] "find . -name '*.nix' -exec nixfmt --check {} +";
          statix = lint "statix" [ pkgs.statix ] "statix check .";
          deadnix = lint "deadnix" [ pkgs.deadnix ] "deadnix --fail .";
        }
        // lib.mapAttrs' (
          name: _: lib.nameValuePair "home-${name}" self.homeConfigurations.${name}.activationPackage
        ) (lib.filterAttrs (_: home: home.system == system) homes)
      );
    };
}
