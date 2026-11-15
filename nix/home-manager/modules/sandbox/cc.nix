# A C and C++ compiler, so native extensions, `cc` crates, and Makefiles build
# without a Nix shell: the GCC of the pinned nixpkgs, as `cc`, `gcc`, `c++`,
# and `g++`, which finds glibc's headers and libraries itself, and binutils for
# `ld`, `ar`, `nm`, and the other tools build scripts call. A project's own
# Nix shell still sets its compiler and libraries over these.
{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [
    ../core.nix
    ./options.nix
  ];

  options.agentix.sandbox.cc.enable =
    lib.mkEnableOption "the C and C++ compiler, `cc`, and binutils"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf config.agentix.sandbox.cc.enable {
    home.packages = [
      pkgs.stdenv.cc
      pkgs.binutils
    ];
    agentix.commands = [
      "ar"
      "c++"
      "cc"
      "g++"
      "gcc"
      "ld"
      "nm"
    ];
  };
}
