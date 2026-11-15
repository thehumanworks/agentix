"""Names and defaults shared by every adapter.

Keep this module free of heavy imports: the command line reads it on every run.
"""

import os

APP_NAME = "agentix"
IMAGE_NAME = "determinate-nix:latest"
SECRET_NAME = "1password"
SANDBOX_TIMEOUT = 3600
# Modal's default reservation leaves a sandbox with about 1 GiB, which Nix
# evaluation, language servers, and test suites exhaust.
SANDBOX_MEMORY_MIB = 8192
# The Modal Volume that keeps the record of every named sandbox (see
# recordfiles.py), and the Dict that caches it. AGENTIX_STORE keeps them under
# another name, for example to try a change without touching the real records.
STORE_NAME = os.environ.get("AGENTIX_STORE") or "agentix-sandboxes"
CACHE_NAME = f"{STORE_NAME}-cache"
# Earlier versions kept the records in a Dict of the store's name. Records
# still there are merged into the Volume when they are read.
LEGACY_STORE_NAME = STORE_NAME
