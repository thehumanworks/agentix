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
  # which needs a terminal; agents have none, and fzy fails. The wrapper below
  # tells this picker whether someone can answer: with a terminal on standard
  # input and error, and without COMMA_NONINTERACTIVE=1, fzy asks as before.
  # Otherwise it takes the package whose attribute is the command's name, as
  # hello for `, hello`, and says so on standard error. With no such package
  # it guesses nothing: it lists the candidates, prints nothing, and comma
  # exits with an error.
  picker = pkgs.writeShellApplication {
    name = "comma-picker";
    runtimeInputs = [
      pkgs.coreutils
    ];
    text = ''
      candidates=$(cat)
      if [[ ''${COMMA_INTERACTIVE:-} == 1 ]]; then
        exec ${lib.getExe pkgs.fzy} <<< "$candidates"
      fi
      command=''${COMMA_COMMAND:-}
      # Each candidate is ATTRIBUTE.OUTPUT, such as hello.out.
      while IFS= read -r candidate; do
        if [[ -n $command && ''${candidate%.*} == "$command" ]]; then
          echo "comma: running $command from nixpkgs#$command" >&2
          printf '%s\n' "$candidate"
          exit 0
        fi
      done <<< "$candidates"
      {
        echo "comma: several packages have bin/$command, and none is named $command:"
        while IFS= read -r candidate; do
          echo "  ''${candidate%.*}"
        done <<< "$candidates"
        echo "Run one of them with: nix shell nixpkgs#ATTRIBUTE -c $command ARGS"
      } >&2
      exit 1
    '';
  };

  # Finds the command in comma's arguments for the picker: the first word that
  # is not an option or an option's value, or the word after `--`.
  wrapper =
    name:
    pkgs.writeShellScript "comma-wrapper" ''
      command=
      skip=
      dashes=
      for arg; do
        if [[ -n $dashes ]]; then
          command=$arg
          break
        elif [[ -n $skip ]]; then
          skip=
          continue
        fi
        case $arg in
          --) dashes=1 ;;
          -P | --picker | -F | --nixpkgs-flake | -c | --print-completions | --cache-level) skip=1 ;;
          -*) ;;
          *)
            command=$arg
            break
            ;;
        esac
      done
      export COMMA_COMMAND=$command
      if [[ -t 0 && -t 2 && ''${COMMA_NONINTERACTIVE:-} != 1 ]]; then
        export COMMA_INTERACTIVE=1
      else
        export COMMA_INTERACTIVE=0
      fi
      export COMMA_PICKER=''${COMMA_PICKER:-${lib.getExe picker}}
      exec ${index.comma-with-db}/bin/${name} "$@"
    '';

  comma = pkgs.symlinkJoin {
    name = "comma-agentix";
    paths = [ index.comma-with-db ];
    postBuild = ''
      rm "$out/bin/," "$out/bin/comma"
      ln -s ${wrapper ","} "$out/bin/,"
      ln -s ${wrapper "comma"} "$out/bin/comma"
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
