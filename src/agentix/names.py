"""Sandbox names. They need no Modal, so the command line checks them first."""

import re
import secrets

from .errors import InvalidRequest

# Lowercase letters, digits, and dashes, starting with a letter. Modal accepts
# them as sandbox names: those must be shorter than 64 characters.
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
# A DNS label also ends with a letter or a digit. A new name must be one, so
# that it works as a Tailscale hostname. Names from before this rule can end
# with a dash; they are still found.
_HOSTNAME = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
# Modal sandbox IDs start with this. A name that did too would be ambiguous.
_ID_PREFIX = "sb-"

_ADJECTIVES = (
    "amber agile bold brave brisk calm clever cosmic crisp dapper eager fancy "
    "gentle glad golden happy hazy jolly keen lively lucky mellow merry nimble "
    "noble plucky proud quick quiet rapid ready rosy rustic shiny silent sleek "
    "snowy solar steady sunny swift tidy vivid warm witty zesty"
).split()
_NOUNS = (
    "badger beacon bison canyon cedar comet coral crane dune falcon fern finch "
    "fjord gecko glacier harbor heron ibis lagoon lark lynx maple marten meadow "
    "mesa moose orca otter owl panda pebble pine puffin quail raven reef robin "
    "sparrow summit tern tiger tundra walrus willow wren yak"
).split()


def is_sandbox_id(target: str) -> bool:
    return target.startswith(_ID_PREFIX)


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name) or is_sandbox_id(name):
        raise InvalidRequest(
            f"Invalid sandbox name: {name!r}. Use up to 63 lowercase letters, "
            "digits, and dashes, starting with a letter and not with 'sb-'."
        )
    return name


def check_new_name(name: str) -> str:
    """Check the name of a new sandbox, which must also be a hostname."""
    if not _HOSTNAME.fullmatch(name) or is_sandbox_id(name):
        raise InvalidRequest(
            f"Invalid sandbox name: {name!r}. Use up to 63 lowercase letters, "
            "digits, and dashes, starting with a letter but not with 'sb-', "
            "and ending with a letter or digit."
        )
    return name


def is_hostname(name: str) -> bool:
    """Whether `name` is a DNS label, which Tailscale keeps as it is."""
    return bool(_HOSTNAME.fullmatch(name))


def check_ephemeral(name: str, ephemeral: bool) -> None:
    """An ephemeral sandbox keeps no record, and a name is found by its record."""
    if ephemeral and name:
        raise InvalidRequest(
            "--ephemeral keeps no record, so the sandbox cannot have a name. "
            "Drop --name, and use the sandbox ID that up prints."
        )


def generate() -> str:
    """A readable name such as `brisk-otter`. The caller checks that it is free."""
    return f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_NOUNS)}"
