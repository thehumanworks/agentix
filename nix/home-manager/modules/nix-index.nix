# comma and nix-locate with a prebuilt index, so `, COMMAND` runs a command
# that is not installed without indexing nixpkgs first. The index comes from
# nix-index-database, a weekly build of nixpkgs-unstable that the flake pins
# like any other input; `nix flake update nix-index-database` moves it.
#
# Both commands read the small index, of the files in each package's bin/,
# which is all comma and the shell's command-not-found hook need. comma runs
# the package from the `nixpkgs` the registry names: in a sandbox, the image's
# own revision.
{ inputs }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.nixIndex;
  index = import inputs.nix-index-database { inherit pkgs; };

  # comma asks which package to run when several have the command, with fzy,
  # which needs a terminal; agents have none, and fzy fails. Without one, this
  # picks the package named after the command (hello for `, hello`, not
  # haskellPackages.hello or tests.*), says which on standard error, and lists
  # the others. With a terminal, fzy asks as before.
  picker = pkgs.writeShellApplication {
    name = "comma-picker";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gawk
      pkgs.gnugrep
    ];
    text = ''
      candidates=$(cat)
      if { : < /dev/tty; } 2> /dev/null; then
        exec ${lib.getExe pkgs.fzy} <<< "$candidates"
      fi
      choice=$(printf '%s\n' "$candidates" | awk -v cmd="''${COMMA_COMMAND:-}" '
        NF {
          attr = $0; sub(/\.[^.]*$/, "", attr); n = split(attr, part, ".")
          rank = (attr == cmd) ? 0 : (part[n] == cmd) ? 1 : 2
          printf "%d %d %03d %s\n", rank, (attr ~ /^tests\./), n, $0
        }' | sort -k1,1n -k2,2n -k3,3n -k4,4 | awk 'NR == 1 { print $4 }')
      others=$(printf '%s\n' "$candidates" | grep -vxF "$choice" | tr '\n' ' ' || true)
      echo "comma: running ''${COMMA_COMMAND:-it} from $choice''${others:+ (also in: $others)}" >&2
      printf '%s\n' "$choice"
    '';
  };
  comma = pkgs.symlinkJoin {
    name = "comma-agentix";
    paths = [ index.comma-with-db ];
    postBuild = ''
      for name in , comma; do
        rm "$out/bin/$name"
        cat > "$out/bin/$name" <<SH
      #!${pkgs.runtimeShell}
      for arg; do case \$arg in -*) ;; *) export COMMA_COMMAND=\$arg; break ;; esac; done
      export COMMA_PICKER=\''${COMMA_PICKER:-${lib.getExe picker}}
      exec ${index.comma-with-db}/bin/$name "\$@"
      SH
        chmod +x "$out/bin/$name"
      done
    '';
  };
in
{
  imports = [
    ./core.nix
    inputs.nix-index-database.homeModules.nix-index
  ];

  options.agentix.nixIndex.enable =
    lib.mkEnableOption "comma (`, COMMAND`) and nix-locate, with a prebuilt index of nixpkgs"
    // {
      default = true;
    };

  config = lib.mkIf cfg.enable {
    # nix-locate, and the command-not-found hook in the shells Home Manager
    # manages, which suggests the package of a missing command.
    programs.nix-index = {
      enable = true;
      package = index.nix-index-with-small-db;
    };
    home.packages = [ comma ];
    agentix.commands = [
      ","
      "comma"
      "nix-locate"
    ];
  };
}
