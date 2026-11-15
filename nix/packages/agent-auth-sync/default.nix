# agent-auth-sync, which keeps coding agents' sign-in files in step with
# 1Password; see ./README.md. The script needs only a POSIX shell, `op`, and
# `jq`, and runs without Nix too. This package fixes jq, and op, the files,
# and the token file when given; the environment still overrides each.
# `nix run .#agent-auth-sync -- status` runs it with the `op` on PATH.
{
  lib,
  stdenvNoCC,
  makeWrapper,
  coreutils,
  gnugrep,
  gnused,
  jq,
  procps,
  util-linux,
  # The op command. The 1Password CLI is unfree, so by default the `op` on
  # PATH runs.
  op ? null,
  # Lines of "op://VAULT/ITEM/FIELD PATH"; null keeps the script's defaults.
  files ? null,
  # A file holding a service account token, read when the environment has none.
  tokenFile ? null,
}:

let
  # ps tells a live watcher from a later process with its PID; setsid lets the
  # one that `ensure` starts outlive its caller. macOS has ps and lacks setsid.
  runtime = [
    coreutils
    gnugrep
    gnused
  ]
  ++ lib.optionals stdenvNoCC.hostPlatform.isLinux [
    procps
    util-linux
  ];
  setDefault = name: value: "--set-default ${name} ${lib.escapeShellArg value}";
in
stdenvNoCC.mkDerivation {
  pname = "agent-auth-sync";
  version = "1.0.0";
  src = ./agent-auth-sync;
  dontUnpack = true;
  nativeBuildInputs = [ makeWrapper ];
  installPhase = ''
    runHook preInstall
    install -Dm755 $src $out/libexec/agent-auth-sync
    patchShebangs $out/libexec
    makeWrapper $out/libexec/agent-auth-sync $out/bin/agent-auth-sync \
      --prefix PATH : ${lib.makeBinPath runtime} \
      ${lib.concatStringsSep " " (
        [ (setDefault "AGENT_AUTH_SYNC_JQ" (lib.getExe jq)) ]
        ++ lib.optional (op != null) (setDefault "AGENT_AUTH_SYNC_OP" (lib.getExe op))
        ++ lib.optional (files != null) (setDefault "AGENT_AUTH_SYNC_FILES" files)
        ++ lib.optional (tokenFile != null) (setDefault "AGENT_AUTH_SYNC_TOKEN_FILE" tokenFile)
      )}
    runHook postInstall
  '';

  meta = {
    description = "Keep Codex's and pi's auth files in step with 1Password";
    mainProgram = "agent-auth-sync";
    platforms = lib.platforms.unix;
  };
}
