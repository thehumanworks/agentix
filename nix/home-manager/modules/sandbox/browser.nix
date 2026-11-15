# A headless Chromium for web testing: Playwright's chrome-headless-shell from
# nixpkgs, without the full browser, Firefox, or WebKit, which a sandbox has no
# display for. Two ways in:
#
# - `chrome-headless-shell URL` on PATH, with `--no-sandbox`, which Chromium
#   requires as root, for `--dump-dom`, `--screenshot`, and `--print-to-pdf`.
# - Playwright, which finds the same build through PLAYWRIGHT_BROWSERS_PATH.
#   The client must come from the minor release nixpkgs packages, which the
#   module writes to share/playwright-browsers/VERSION, such as 1.64; another
#   release looks for a build of its own and fails. Playwright adds
#   `--no-sandbox` itself.
#
# Neither has fonts of its own or a system fontconfig to find, so the browser
# starts with a configuration that names the DejaVu fonts.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.agentix.sandbox.browser;
  driver = pkgs.playwright-driver;
  browsers = driver.browsers.override {
    withChromium = false;
    withChromiumHeadlessShell = true;
    withFirefox = false;
    withWebkit = false;
    withFfmpeg = false;
  };
  fontsConf = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };

  # The browsers directory as Playwright lays it out, with each build's binary
  # replaced by a wrapper that names the fonts. Chromium finds its resources
  # next to the real binary, so the rest stays links into the original.
  wrapped = pkgs.runCommand "playwright-browsers-${driver.version}" { } ''
    for build in ${browsers}/*; do
      name=''${build##*/}
      mkdir -p "$out/$name"
      for dir in "$build"/*; do
        mkdir -p "$out/$name/''${dir##*/}"
        for file in "$dir"/*; do
          ln -s "$file" "$out/$name/''${dir##*/}/"
        done
      done
    done
    binary=$(echo "$out"/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell)
    test -L "$binary"
    real=$(readlink "$binary")
    rm "$binary"
    cat > "$binary" <<SH
    #!${pkgs.runtimeShell}
    export FONTCONFIG_FILE=\''${FONTCONFIG_FILE:-${fontsConf}}
    exec $real "\$@"
    SH
    chmod +x "$binary"
    echo ${lib.versions.majorMinor driver.version} > "$out/VERSION"
  '';

  # The directory in the profile, a path that stays put across rebuilds.
  browsersPath = "${config.home.profileDirectory}/share/playwright-browsers";
  profileDir = pkgs.runCommand "playwright-browsers-profile" { } ''
    mkdir -p $out/share
    ln -s ${wrapped} $out/share/playwright-browsers
  '';

  shell = pkgs.writeShellScriptBin "chrome-headless-shell" ''
    exec ${wrapped}/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell \
      --no-sandbox "$@"
  '';
in
{
  imports = [
    ../core.nix
    ./options.nix
  ];

  options.agentix.sandbox.browser.enable =
    lib.mkEnableOption "a headless Chromium, as `chrome-headless-shell` and for Playwright"
    // {
      default = config.agentix.sandbox.enable;
      defaultText = lib.literalExpression "config.agentix.sandbox.enable";
    };

  config = lib.mkIf cfg.enable {
    home.packages = [
      shell
      profileDir
    ];
    agentix.commands = [ "chrome-headless-shell" ];

    # For login shells and SSH sessions. nix/sandbox.toml gives the image the
    # same values, for commands started without a login shell.
    home.sessionVariables = {
      PLAYWRIGHT_BROWSERS_PATH = browsersPath;
      # Playwright checks for the libraries of an Ubuntu install, which the
      # Nix build does not need.
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "1";
    };
  };
}
