# pi-packages ROOT STORE
#
# Lays out the pi packages that STORE holds, a node_modules tree built from
# ../pi-packages, in ROOT, pi's npm directory (~/.pi/agent/npm), where
# `pi install` puts packages. Each entry of node_modules is a link into the
# store, so the image holds the packages once, and the directories stay
# writable for `pi install` and `pi update`, which replace the links with
# copies. Runs when Home Manager activates; does nothing while STORE is the
# one laid out last.
#
# What the user added stays. Only links into the Nix store, the previous
# layout, are replaced; a package that `pi install` or `pi update` put there
# as a directory is kept, even where STORE has one of that name. The
# dependencies that `pi install` added to package.json stay in it, and the
# lock file is replaced only while it is the one laid out last.

root=$1
store=$2
modules=$root/node_modules
previous=$(cat "$root/.agentix-store" 2> /dev/null || true)
if [[ -d $modules && $previous == "$store" ]]; then
  exit 0
fi

mkdir -p "$modules"
# Links into the store directory that holds STORE, such as /nix/store.
find "$modules" -mindepth 1 -maxdepth 2 -type l -lname "$(dirname "$store")/*" -delete
find "$modules" -mindepth 1 -maxdepth 1 -type d -name '@*' -empty -delete

link() {
  [[ -e $2 || -L $2 ]] || ln -s "$1" "$2"
}
for entry in "$store"/node_modules/* "$store"/node_modules/.bin; do
  name=$(basename "$entry")
  if [[ $name == @* ]]; then
    mkdir -p "$modules/$name"
    for scoped in "$entry"/*; do
      link "$scoped" "$modules/$name/$(basename "$scoped")"
    done
  else
    link "$entry" "$modules/$name"
  fi
done

# Keep the dependencies that `pi install` added. A package.json that is not
# a JSON object with an object of dependencies is replaced, with a warning,
# rather than stopping the activation.
if [[ -s $root/package.json ]] &&
  merged=$(jq -se '
    if (.[0] | type == "object" and ((.dependencies // {}) | type == "object"))
    then .[0] + {dependencies: ((.[0].dependencies // {}) + .[1].dependencies)}
    else false end' "$root/package.json" "$store/package.json" 2> /dev/null); then
  printf '%s\n' "$merged" > "$root/package.json"
else
  if [[ -e $root/package.json ]]; then
    echo "pi-packages: $root/package.json is not a valid package.json; writing it again." >&2
  fi
  install -m 644 "$store/package.json" "$root/package.json"
fi
if [[ ! -e $root/package-lock.json ]] ||
  { [[ -n $previous ]] && cmp -s "$root/package-lock.json" "$previous/package-lock.json"; }; then
  install -m 644 "$store/package-lock.json" "$root/package-lock.json"
fi
[[ -e $root/.gitignore ]] || printf '*\n!.gitignore\n' > "$root/.gitignore"
printf '%s\n' "$store" > "$root/.agentix-store"
