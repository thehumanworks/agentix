# The sandbox profile as an OCI image, built by Nix alone: no base image and no
# package manager. It runs under Docker, Podman, a devcontainer, or any other
# OCI runtime. See ../README.md for building, loading, and running it.
#
# The image is the Modal sandbox without Debian: root's Home Manager
# generation, activated while the image is built, a Nix that can add to the
# store, and the files and variables that src/agentix/image.py and the start
# command in src/agentix/sandbox.py provide there.
{
  pkgs,
  # A Home Manager configuration of the sandbox profile for this system.
  home,
}:

let
  inherit (pkgs) lib;

  generation = home.activationPackage;
  homePath = home.config.home.path;
  # From nix/sandbox.toml, which the agentix command reads too.
  inherit (home.config.agentix.sandbox) paths manifestFile;
  # Where other distributions keep the dynamic loader.
  loaderDir = if pkgs.stdenv.hostPlatform.isx86_64 then "lib64" else "lib";

  # Commands the Modal image gets from Debian and that agents, the Home
  # Manager scripts, and `agentix sandbox ssh` expect. Most are in the closure
  # already. A Home Manager package of the same name wins.
  base = lib.lowPrio (
    pkgs.buildEnv {
      name = "oci-base";
      paths = [
        pkgs.bzip2
        pkgs.getent
        pkgs.glibc.bin # iconv, ldd, locale
        pkgs.gnutar
        pkgs.gzip
        pkgs.hostname
        pkgs.less
        pkgs.ncurses # clear, infocmp, reset, tput
        pkgs.openssl.bin
        pkgs.util-linux # flock, mount, script, more
        pkgs.which
        pkgs.xz
      ];
    }
  );

  # Root's Nix profile, as `nix profile add` writes it in the Modal image:
  # Nix, the Home Manager packages, CA certificates, and the base commands. A
  # later `home-manager switch` finds home-path in the manifest and replaces it.
  profilePackages = [
    pkgs.nix.out
    homePath
    pkgs.cacert.out
    base
  ];
  profile = pkgs.buildEnv {
    name = "profile";
    paths = profilePackages;
    postBuild = ''
      cp ${
        pkgs.writeText "manifest.json" (
          builtins.toJSON {
            version = 3;
            elements = lib.listToAttrs (
              map (
                package:
                lib.nameValuePair (lib.getName package) {
                  active = true;
                  priority = package.meta.priority or 5;
                  storePaths = [ package.outPath ];
                }
              ) profilePackages
            );
          }
        )
      } $out/manifest.json
    '';
  };

  entrypoint = pkgs.writeShellApplication {
    name = "agentix-entrypoint";
    text = ''
      coreutils=${pkgs.coreutils}/bin
      op_token_file=${paths.opTokenFile}
      workdir=${paths.workdir}
      workdir_file=${paths.workdirFile}
      ready_file=${paths.readyFile}
    ''
    + builtins.readFile ./entrypoint.sh;
  };

  # The files outside /nix and /root. Every file is copied into the image, so
  # the ones in /etc stay writable; links into the store stay links.
  rootfs =
    let
      # The sshd account lets `agentix sandbox ssh start --engine=openssh`
      # skip useradd, which the image does not have. `*` keeps root unlocked
      # for public-key SSH logins, as on Debian.
      passwd = pkgs.writeText "passwd" ''
        root:x:0:0:root:/root:/bin/bash
        sshd:x:74:74:SSH privilege separation:/var/empty:/bin/false
        nobody:x:65534:65534:Nobody:/var/empty:/bin/false
      '';
      group = pkgs.writeText "group" ''
        root:x:0:
        tty:x:5:
        sshd:x:74:
        nogroup:x:65534:
      '';
      shadow = pkgs.writeText "shadow" ''
        root:*:1::::::
        sshd:*:1::::::
        nobody:*:1::::::
      '';
      nsswitch = pkgs.writeText "nsswitch.conf" ''
        passwd: files
        group: files
        shadow: files
        hosts: files dns
        networks: files
        protocols: files
        services: files
      '';
      shells = pkgs.writeText "shells" ''
        /bin/sh
        /bin/bash
      '';
      # Login shells read the profile.d scripts, as on Debian. Home Manager's
      # .profile puts the Nix profile on PATH.
      profileScript = pkgs.writeText "profile" ''
        for script in /etc/profile.d/*.sh; do
          if [ -r "$script" ]; then
            . "$script"
          fi
        done
        unset script
      '';
      # Shells that read no profile, such as `ssh HOST COMMAND`, read this
      # and .bashrc. The Nix installer adds the same to Debian's bash.bashrc.
      bashrc = pkgs.writeText "bashrc" ''
        case ":$PATH:" in
          *":$HOME/.nix-profile/bin:"*) ;;
          *) PATH="$HOME/.nix-profile/bin:/nix/var/nix/profiles/default/bin:$PATH" ;;
        esac
      '';
      # No build users and no build sandbox: containers seldom permit the
      # namespaces it needs, and Modal's Nix runs without one too.
      nixConf = pkgs.writeText "nix.conf" ''
        build-users-group =
        experimental-features = nix-command flakes
        max-jobs = auto
        sandbox = false
      '';
    in
    pkgs.runCommand "oci-rootfs" { } ''
      mkdir -p $out/bin $out/usr/bin $out/etc/nix $out/etc/profile.d $out/etc/ssl/certs
      ln -s ${pkgs.bashInteractive}/bin/bash $out/bin/bash
      ln -s ${pkgs.bashInteractive}/bin/bash $out/bin/sh
      ln -s ${pkgs.coreutils}/bin/false $out/bin/false
      ln -s ${pkgs.coreutils}/bin/env $out/usr/bin/env
      # Binaries built for other distributions, such as the Pythons uv
      # downloads, look for the loader and terminfo at the usual paths.
      mkdir -p $out/${loaderDir} $out/usr/share
      ln -s ${pkgs.stdenv.cc.bintools.dynamicLinker} $out/${loaderDir}/
      ln -s ${pkgs.ncurses}/share/terminfo $out/usr/share/terminfo
      ln -s ${pkgs.cacert.out}/etc/ssl/certs/ca-bundle.crt $out/etc/ssl/certs/ca-certificates.crt
      ln -s ${pkgs.iana-etc}/etc/services ${pkgs.iana-etc}/etc/protocols $out/etc/
      cp ${passwd} $out/etc/passwd
      cp ${group} $out/etc/group
      cp ${shadow} $out/etc/shadow
      cp ${nsswitch} $out/etc/nsswitch.conf
      cp ${shells} $out/etc/shells
      cp ${profileScript} $out/etc/profile
      cp ${bashrc} $out/etc/bashrc
      cp ${nixConf} $out/etc/nix/nix.conf
    '';

  # Everything the image refers to from outside the store. The Nix database
  # registers their closure, and a GC root keeps each one, so Nix in the
  # container neither downloads nor collects what the image holds.
  roots = [
    rootfs
    profile
    generation
    entrypoint
    pkgs.tini
  ];

  closure = pkgs.closureInfo { rootPaths = roots; };

  # Stand-ins for Nix's commands while Home Manager activates; see
  # activation-shim.sh. One directory of real files: the activation looks for
  # its Nix commands in the directory of nix-env, after resolving links.
  activationShim =
    let
      script = pkgs.writeShellApplication {
        name = "nix";
        runtimeInputs = [ pkgs.coreutils ];
        text = ''
          home_path=${homePath}
        ''
        + builtins.readFile ./activation-shim.sh;
      };
    in
    pkgs.runCommand "oci-activation-shim" { } ''
      mkdir -p $out/bin
      for name in nix nix-build nix-env nix-store; do
        cp ${lib.getExe script} $out/bin/$name
      done
    '';
in
pkgs.dockerTools.streamLayeredImage {
  name = "agentix-sandbox";
  tag = "latest";

  extraCommands = ''
    export NIX_REMOTE=local?root=$PWD USER=nobody
    ${lib.getExe' pkgs.nix "nix-store"} --load-db < ${closure}/registration
    # A fixed registration time keeps the image reproducible.
    ${lib.getExe pkgs.sqlite} nix/var/nix/db/db.sqlite \
      "UPDATE ValidPaths SET registrationTime = $SOURCE_DATE_EPOCH"
  '';

  # Runs with the image's root as /, so the activation writes /root and makes
  # the links Home Manager makes in a container.
  enableFakechroot = true;
  fakeRootCommands = ''
    cp -rP ${rootfs}/. /
    chmod -R u+w /bin /etc /usr /${loaderDir}
    chmod 600 /etc/shadow
    mkdir -p /root /tmp /var/tmp /var/empty /run ${paths.workdir} /opt/agentix \
      /nix/var/nix/profiles/per-user/root /nix/var/nix/gcroots/agentix
    chmod 700 /root
    chmod 1777 /tmp /var/tmp

    # Root's profile, laid out as Nix lays it out, with the profiles and the
    # image's own paths as GC roots.
    ln -s ${profile} /nix/var/nix/profiles/per-user/root/profile-1-link
    ln -s profile-1-link /nix/var/nix/profiles/per-user/root/profile
    ln -s /nix/var/nix/profiles/per-user/root/profile /nix/var/nix/profiles/default
    ln -s /nix/var/nix/profiles/per-user/root/profile /root/.nix-profile
    ln -s /nix/var/nix/profiles /nix/var/nix/gcroots/profiles
    ${lib.concatMapStrings (root: ''
      ln -s ${root} /nix/var/nix/gcroots/agentix/${baseNameOf root}
    '') roots}

    # This flake, so `home-manager switch --flake /root/.config/home-manager`
    # works as in a Modal sandbox.
    mkdir -p /root/.config
    cp -r ${lib.cleanSource ../.} /root/.config/home-manager
    chmod -R u+w /root/.config/home-manager

    # Home Manager's own activation, with Nix's commands replaced.
    (
      export HOME=/root USER=root LOGNAME=root PATH=${activationShim}/bin:$PATH
      cd /root
      nix-store --realise ${generation} --add-root /opt/agentix/home-generation > /dev/null
      ${generation}/activate
    )
    # The activation removes its temporary root; drop the link to it.
    find /nix/var/nix/gcroots/auto -xtype l -delete

    test -L /root/.bashrc
    test -L /root/.claude/CLAUDE.md
    test -x /root/.nix-profile/bin/claude
    test -x /root/.nix-profile/bin/nix
    test -s /root/.claude.json
    ${lib.getExe pkgs.jq} -e --arg rule "Read(/${paths.opTokenFile})" \
      '.permissions.deny | index($rule)' /root/.claude/settings.json > /dev/null
    # Every command the configuration lists is in root's profile.
    for name in $(${lib.getExe pkgs.jq} -er '.commands | join(" ")' /root/.nix-profile/${manifestFile}); do
      test -x "/root/.nix-profile/bin/$name" || { echo "missing: $name" >&2; exit 1; }
    done
    test "$(readlink /root/.local/state/home-manager/gcroots/current-home)" = ${generation}
  '';

  config = {
    Entrypoint = [
      (lib.getExe pkgs.tini)
      "--"
      (lib.getExe entrypoint)
    ];
    WorkingDir = paths.workdir;
    # src/agentix/image.py sets the same for Modal; keep them in step. Commands
    # run without a login shell get them from here. IS_SANDBOX lets Claude
    # skip permission prompts as root; sandbox.py sets it on Modal.
    Env = [
      "PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      "HOME=/root"
      "USER=root"
      "LOGNAME=root"
      "EDITOR=nvim"
      "VISUAL=nvim"
      "LANG=C.UTF-8"
      "NIX_PATH=nixpkgs=flake:nixpkgs"
      "IS_SANDBOX=1"
    ];
    Labels."org.opencontainers.image.title" = "agentix-sandbox";
  };
}
