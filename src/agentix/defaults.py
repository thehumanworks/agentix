"""Names and defaults shared by every adapter.

Keep this module free of heavy imports: the command line reads it on every run.
"""

APP_NAME = "agentix"
IMAGE_NAME = "determinate-nix:latest"
SECRET_NAME = "1password"
SANDBOX_TIMEOUT = 3600
# Modal's default reservation leaves a sandbox with about 1 GiB, which Nix
# evaluation, language servers, and test suites exhaust.
SANDBOX_MEMORY_MIB = 8192
# The Modal Dict that maps sandbox names to their sandboxes and snapshots.
STORE_NAME = "agentix-sandboxes"
