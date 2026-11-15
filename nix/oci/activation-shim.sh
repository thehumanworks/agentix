# Stands in for nix, nix-build, nix-env, and nix-store while the image build
# runs Home Manager's activation script. That build cannot write to the Nix
# store and needs no store writes: the image already holds every store path and
# root's Nix profile, home-path included. What remains are the links Nix would
# make. Every other call fails the build, so a Home Manager update that needs
# more from Nix is noticed here and not in a container.
#
# default.nix sets home_path before this text.
: "${home_path:?}"

fail() {
  echo "oci: unexpected call during image activation: $command $*" >&2
  exit 1
}

# Like `nix-store --add-root`: link ROOT to PATH and register ROOT as an
# indirect GC root, as Nix does for a root outside /nix/var/nix/gcroots.
add_root() {
  mkdir -p "$(dirname "$2")" /nix/var/nix/gcroots/auto
  ln -sfn "$1" "$2"
  ln -sfn "$2" "/nix/var/nix/gcroots/auto/$(printf %s "$2" | sha256sum | cut -c 1-32)"
  printf '%s\n' "$2"
}

command=${0##*/}
case "$command $*" in
"nix-build --quiet --expr {} --no-out-link") ;; # Sanity check.
"nix-env -q") ;;                                 # Creates the profile directory.
"nix-env --profile "*" --set /nix/store/"*)
  # The first generation of the Home Manager profile, as Nix names it.
  (($# == 4)) || fail "$@"
  [[ ! -e $2 && ! -L $2 ]] || fail "$@"
  ln -s "$4" "$2-1-link"
  ln -s "${2##*/}-1-link" "$2"
  ;;
"nix-store --realise /nix/store/"*" --add-root "*)
  (($# == 4)) && [[ -e $2 ]] || fail "$@"
  add_root "$2" "$4"
  ;;
"nix profile list --json") cat "$HOME/.nix-profile/manifest.json" ;;
# The activation replaces home-path in the profile. The image's profile holds
# this home-path already, so there is nothing to replace.
"nix profile remove $home_path" | "nix profile install $home_path") ;;
*) fail "$@" ;;
esac
