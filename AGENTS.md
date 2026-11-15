# Agentix project instructions

Agentix builds and publishes a Modal image from the flake's OCI image
(`nix build .#oci` in `nix/`), with the build in `src/agentix/image.py`. New
sandboxes use the published image; `sandbox up` does not rebuild it. Run
project commands from this directory.

## Own image rebuilds

When your changes require a Modal image rebuild, you must perform the rebuild
and verify the result before calling the task complete. This is part of the
implementation, not a follow-up task for the user. Do not stop at "the image
needs rebuilding" or ask for routine permission to run the required build.
An explicit user instruction to defer or skip the build takes precedence.

A rebuild is required when a change affects the image's contents or build:

- Nix flakes, pinned inputs, Home Manager modules, packages, settings, or scripts
  used by the `modal` profile, including `nix/home-manager/modules/scripts/`.
- The OCI image in `nix/oci/`, and `nix/sandbox.toml`, which holds the
  image's paths and environment.
- The builder pin (`NIX_BUILDER`), the build script, the image's command, or
  the checks in `src/agentix/image.py`.
- Any other input consumed when constructing the image. Trace the actual build
  inputs if the effect of a change is unclear.

Documentation-only changes, tests, and host-side CLI or runtime orchestration
changes do not require a rebuild unless they also change an image input or
required image behavior.

## Required completion steps

1. Run the checks relevant to the change, then execute
   `uv run agentix image build`. Use the existing image target, currently
   `determinate-nix:latest`, unless the user specified another target. The command
   builds and publishes the image. Wait for success and record its returned
   `image_id`. Keep normal layer caching; use `--force-build` only when an
   uncached build is necessary.
2. Start a fresh verification sandbox from that published image with
   `uv run agentix sandbox up --ephemeral --timeout=600` and record its
   `sandbox_id`. Use the same image target as the build. Use the configured
   runtime secret when the check needs it; `--secret-name=''` is available for
   checks that need no secrets.
3. Verify the affected behavior inside the fresh sandbox. Use
   `uv run agentix sandbox exec SANDBOX_ID 'COMMAND'` for command checks. Exercise
   the actual terminal or SSH flow when the change affects interactive behavior.
   A successful build, syntax check, or version command alone does not prove a
   behavioral fix. Do not print secret values in commands, logs, or reports.
4. Remove only the verification sandbox you created with
   `uv run agentix sandbox down SANDBOX_ID`, including after a failed check.
   An ephemeral sandbox leaves no snapshot or record behind.
   Existing sandboxes retain their old image. Do not terminate or replace them
   without user authorization. Applying Home Manager inside an existing sandbox
   does not update the published image.
5. Report the published image ID, the behavior verified, and verification cleanup.
   If the build or check fails, diagnose and fix it within the task scope, then
   retry. If authentication, access, or an external failure prevents completion,
   report the exact blocker and what remains unverified. Do not claim the image
   is rebuilt or the task is complete without the corresponding evidence.

See `README.md` and `nix/README.md` for the current image and sandbox workflow.
