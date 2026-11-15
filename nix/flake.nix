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
    # Rex, the terminal multiplexer: the published release binary, from the
    # snapshot channel the nixos box uses. Both the flake and the binary
    # download without credentials. Rex's flake allows its own unfree licence.
    # It needs only glibc, PAM, and patchelf from nixpkgs, so it follows this
    # flake's pin instead of bringing a second package set into the image.
    # The URL names one snapshot, and the server never changes what it holds.
    # The channel's own URL, snapshot.tar.gz, would lock as whatever its Link
    # header names, which has been an older snapshot than the one it served.
    # `nix flake update` leaves Rex here; `uv run agentix flake update rex`
    # moves this URL to the newest snapshot.
    rex = {
      url = "https://pkgs.superlogical.com/rex/nix/snapshot/0.0.1794719511-gf91cd7.tar.gz";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      home-manager,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      # One entry per machine type. The profile holds everything that differs;
      # the user file says who commits.
      homes = {
        modal = {
          system = "x86_64-linux";
          profile = ./home-manager/profiles/sandbox.nix;
          user = ./home-manager/users/tomas.nix;
        };
        modal-arm = {
          system = "aarch64-linux";
          profile = ./home-manager/profiles/sandbox.nix;
          user = ./home-manager/users/tomas.nix;
        };
        mac = {
          system = "aarch64-darwin";
          profile = ./home-manager/profiles/mac.nix;
          user = ./home-manager/users/tomas.nix;
        };
      };

      forAllSystems = lib.genAttrs (lib.unique (lib.mapAttrsToList (_: home: home.system) homes));

      # The homes allow unfree packages through the modules' own list; see
      # home-manager/home.nix.
      pkgsFor = forAllSystems (system: nixpkgs.legacyPackages.${system});

      mkHome =
        {
          system,
          profile,
          user,
          # Settings on top of the profile, for the checks below.
          modules ? [ ],
        }:
        home-manager.lib.homeManagerConfiguration {
          pkgs = pkgsFor.${system};
          extraSpecialArgs = { inherit inputs; };
          modules = [
            self.homeManagerModules.default
            profile
            user
          ]
          ++ modules;
        };

      # The sandbox home of each Linux system, which the OCI image holds.
      sandboxHomes = {
        x86_64-linux = "modal";
        aarch64-linux = "modal-arm";
      };

      # Each SSH engine has a module of its own. These checks evaluate the
      # sandbox profile with each engine's module on and off, and fail when a
      # module that is off still installs its package, lists its commands in
      # the manifest, or adds its accounts to the image, or when one that is on
      # does not. The profile itself must have both engines. Evaluating the
      # generation and the OCI image is enough; nothing of them is built.
      sshEngineChecks =
        system:
        let
          pkgs = pkgsFor.${system};
          name = sandboxHomes.${system};
          engines = {
            tailscale = {
              package = pkgs.tailscale;
              commands = [
                "tailscale"
                "ts"
              ];
              accounts = [ ];
            };
            openssh = {
              package = pkgs.openssh;
              commands = [
                "scp"
                "sftp"
                "ssh"
                "ssh-keygen"
                "sshd"
              ];
              accounts = [ "sshd" ];
            };
          };
          check =
            variant: on: home:
            let
              inherit (home.config.agentix) commands sandbox;
              installed = map (package: package.outPath) home.config.home.packages;
              expect =
                engine: what: found:
                let
                  enabled = lib.elem engine on;
                in
                lib.assertMsg (found == enabled) (
                  "${variant}: with the ${engine} module "
                  + (if enabled then "on, ${what} is missing" else "off, ${what} is still there")
                );
              results = lib.concatLists (
                lib.mapAttrsToList (
                  engine: spec:
                  [ (expect engine spec.package.name (lib.elem spec.package.outPath installed)) ]
                  ++ map (command: expect engine "the command ${command}" (lib.elem command commands)) spec.commands
                  ++ map (
                    account: expect engine "the account ${account}" (sandbox.accounts ? ${account})
                  ) spec.accounts
                ) engines
              );
            in
            assert lib.all lib.id results;
            pkgs.writeText "agentix-ssh-${variant}" (
              builtins.unsafeDiscardStringContext ''
                ${home.activationPackage.drvPath}
                ${(import ./oci { inherit pkgs home; }).drvPath}
              ''
            );
          variant =
            on:
            mkHome (
              homes.${name}
              // {
                modules = [
                  {
                    agentix.sandbox = lib.mapAttrs (engine: _: { enable = lib.elem engine on; }) engines;
                  }
                ];
              }
            );
        in
        {
          ssh-engines = check "profile" [
            "openssh"
            "tailscale"
          ] self.homeConfigurations.${name};
          ssh-openssh-only = check "openssh-only" [ "openssh" ] (variant [ "openssh" ]);
          ssh-tailscale-only = check "tailscale-only" [ "tailscale" ] (variant [ "tailscale" ]);
          ssh-none = check "none" [ ] (variant [ ]);
        };
    in
    {
      homeConfigurations = lib.mapAttrs (_: mkHome) homes;

      # For other flakes; see README.md.
      homeManagerModules = import ./home-manager/exports.nix { inherit inputs lib; };

      # The sandbox profile as an OCI image for Docker, Podman, and other
      # hosts: `nix build .#oci` on Linux. See oci/default.nix.
      packages = lib.mapAttrs (system: name: {
        oci = import ./oci {
          pkgs = pkgsFor.${system};
          home = self.homeConfigurations.${name};
        };
      }) sandboxHomes;

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
        # Builds the image's activated root and layer list, not its layers.
        // self.packages.${system} or { }
        // lib.optionalAttrs (sandboxHomes ? ${system}) (sshEngineChecks system)
      );
    };
}
