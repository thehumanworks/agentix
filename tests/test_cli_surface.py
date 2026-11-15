"""The whole command line that python-fire took still works with click.

Each spelling of each command and flag is run through main() with the
command's plain function replaced, and the call it gets is compared with the
function's defaults plus the one value the spelling sets. (When the surface
moved from Fire to click, the same spellings were run through both, and all
of them gave the same calls.)
"""

import contextlib
import inspect
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from agentix import __main__ as cli_main
from agentix.cli import print_result
from agentix.defaults import IMAGE_NAME

# Each command: its path, its plain function, its positionals as
# (name, sample, short flag), its flags that take a value as
# (flag, short, sample, value it gives), and its boolean flags as (flag, short).
SURFACE = [
    (
        "image build",
        "_build_image",
        [],
        [("--image-name", "-i", "img", "img")],
        [("--force-build", "-f")],
    ),
    (
        "flake update",
        "_flake_update",
        [],
        [("--image-name", "-i", "img", "img")],
        [("--build", "-b")],
    ),
    ("auth install", "_auth_install", [], [], []),
    ("auth uninstall", "_auth_uninstall", [], [], []),
    ("auth status", "_auth_status", [], [], []),
    ("auth sync", "_auth_sync", [], [], []),
    ("config show", "_config_show", [], [], []),
    ("runner deploy", "_runner_deploy", [], [], []),
    (
        "sandbox up",
        "_create_sandbox",
        [],
        [
            ("--image-name", "-i", "img", "img"),
            ("--secret-name", "-s", "sec", "sec"),
            ("--timeout", None, "77", 77),
            ("--workdir", "-w", "/wd", "/wd"),
            ("--memory", "-m", "1024", 1024),
            ("--cpu", "-c", "1.5", 1.5),
            ("--name", None, "dev", "dev"),
            ("--hostname", "-h", "box", "box"),
            ("--repo", "-r", "o/r", "o/r"),
        ],
        [("--tailscale", None), ("--no-snapshot", None), ("--ephemeral", "-e")],
    ),
    ("sandbox down", "_terminate_sandbox", [("sandbox_id", "sb-1", None)], [], []),
    (
        "sandbox exec",
        "_execute",
        [("sandbox_id", "dev", None), ("command", "echo hi", None)],
        [],
        [("--resume", "-r"), ("--detach", "-d")],
    ),
    ("sandbox logs", "_logs", [("run_id", "fc-1", None)], [], []),
    ("sandbox wait", "_wait", [("run_id", "fc-1", None)], [], []),
    ("sandbox runs", "_runs", [("target", "dev", "-t")], [], [("--clean", "-c")]),
    ("sandbox ls", "_list_sandboxes", [], [], []),
    ("sandbox snapshot", "_snapshot", [("name", "dev", None)], [], []),
    ("sandbox snapshots", "_snapshots", [("name", "dev", None)], [], []),
    (
        "sandbox restore",
        "_restore",
        [("name", "dev", None)],
        [("--snapshot", "-s", "im-1", "im-1"), ("--hostname", "-h", "box", "box")],
        [("--tailscale", "-t")],
    ),
    ("sandbox resume", "_resume", [("name", "dev", None)], [], []),
    ("sandbox purge", "_purge", [("name", "dev", None)], [], [("--terminate", "-t")]),
    (
        "sandbox prune",
        "_prune",
        [],
        [("--older-than", "-o", "14d", "14d"), ("--name", "-n", "s*", "s*")],
        [("--keep-latest", "-k"), ("--yes", "-y")],
    ),
    (
        "sandbox sync",
        "_sync",
        [("name", "dev", "-n")],
        [("--path", None, "/p", "/p")],
        [("--pull", None)],
    ),
    (
        "sandbox shell",
        "_shell",
        [("name", "dev", "-n")],
        [
            ("--engine", "-e", "openssh", "openssh"),
            ("--hostname", "-h", "box", "box"),
            ("--public-key", "-p", "k.pub", "k.pub"),
        ],
        [],
    ),
    (
        "sandbox ssh start",
        "_ssh_start",
        [("sandbox_id", "dev", None)],
        [
            ("--engine", "-e", "openssh", "openssh"),
            ("--hostname", "-h", "box", "box"),
            ("--public-key", "-p", "k.pub", "k.pub"),
        ],
        [],
    ),
    (
        "sandbox ssh stop",
        "_ssh_stop",
        [("sandbox_id", "dev", None)],
        [("--engine", "-e", "openssh", "openssh")],
        [],
    ),
    (
        "sandbox ssh status",
        "_ssh_status",
        [("sandbox_id", "dev", None)],
        [("--engine", "-e", "openssh", "openssh")],
        [],
    ),
]

# Flags a command cannot go without, added to each of its spellings.
REQUIRED = {"_prune": (["--older-than=14d"], {"older_than": "14d"})}

GROUPS = ["", "image", "flake", "auth", "config", "runner", "sandbox", "sandbox ssh"]


def _underscored(flag):
    return "--" + flag[2:].replace("-", "_")


def spellings():
    """(argv, function, the arguments it should get beyond its defaults)."""
    for path, function, positionals, values, booleans in SURFACE:
        command = path.split()
        plain = [sample for _, sample, _ in positionals]
        given = {name: sample for name, sample, _ in positionals}
        extra, extra_given = REQUIRED.get(function, ([], {}))
        base = command + plain + extra
        yield base, function, given | extra_given
        for i, (name, sample, short) in enumerate(positionals):
            rest = plain[:i] + plain[i + 1 :]
            forms = []
            for flag in ("--" + name.replace("_", "-"), "--" + name):
                forms += [[f"{flag}={sample}"], [flag, sample]]
            if short:
                forms += [[short, sample], [f"{short}={sample}"]]
            for form in forms:
                yield command + form + rest + extra, function, given | extra_given
        for flag, short, sample, value in values:
            underscored = _underscored(flag)
            forms = [
                [f"{flag}={sample}"],
                [flag, sample],
                [f"{underscored}={sample}"],
                [underscored, sample],
            ]
            if short:
                forms += [[short, sample], [f"{short}={sample}"]]
            key = flag[2:].replace("-", "_")
            for form in forms:
                start = command + plain if key in extra_given else base
                yield start + form, function, given | extra_given | {key: value}
        for flag, short in booleans:
            key = flag[2:].replace("-", "_")
            on = [[flag], [_underscored(flag)], [f"{flag}=True"], [flag, "True"]]
            off = [[f"{flag}=False"], [flag, "False"], ["--no" + key]]
            if short:
                on += [[short], [short, "True"], [f"{short}=True"]]
                off += [[short, "False"], [f"{short}=False"]]
            for form in on:
                yield base + form, function, given | extra_given | {key: True}
            for form in off:
                yield base + form, function, given | extra_given | {key: False}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        config_home = tempfile.TemporaryDirectory()
        self.addCleanup(config_home.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home.name})
        environment.start()
        self.addCleanup(environment.stop)

    def call(self, argv, function):
        """Run argv; return the arguments `function` got, bound with its defaults."""
        original = getattr(cli_main, function)
        signature = inspect.signature(original)
        output = io.StringIO()
        with (
            patch.object(cli_main, function) as stand_in,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            stand_in.return_value = None
            cli_main.main(list(argv))
        self.assertEqual(stand_in.call_count, 1, output.getvalue())
        bound = signature.bind(*stand_in.call_args.args, **stand_in.call_args.kwargs)
        bound.apply_defaults()
        return {
            name: list(value) if isinstance(value, tuple) else value
            for name, value in bound.arguments.items()
        }

    def defaults(self, function):
        signature = inspect.signature(getattr(cli_main, function))
        values = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind is parameter.VAR_POSITIONAL:
                values[name] = []
            elif parameter.default is not parameter.empty:
                value = parameter.default
                values[name] = list(value) if isinstance(value, tuple) else value
        return values

    def exit_code(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                cli_main.main(list(argv))
            except SystemExit as done:
                return done.code, output.getvalue()
        return 0, output.getvalue()


class OldSurfaceTest(CliTestCase):
    def test_every_old_spelling_gives_the_same_call(self):
        count = 0
        for argv, function, given in spellings():
            with self.subTest(argv=" ".join(argv)):
                self.assertEqual(
                    self.call(argv, function), self.defaults(function) | given
                )
            count += 1
        self.assertGreater(count, 300)

    def test_variadic_inputs_and_the_shell_command(self):
        self.assertEqual(
            self.call(["flake", "update", "mods", "rex", "--build"], "_flake_update"),
            {"inputs": ["mods", "rex"], "build": True, "image_name": IMAGE_NAME},
        )
        shell = self.call(["sandbox", "shell", "dev", "--", "ls", "-la"], "_shell")
        self.assertEqual((shell["name"], shell["command"]), ("dev", ["ls", "-la"]))
        shell = self.call(["sandbox", "shell", "--", "pwd"], "_shell")
        self.assertEqual((shell["name"], shell["command"]), ("", ["pwd"]))

    def test_resume_with_a_command(self):
        for argv in (
            ["sandbox", "exec", "dev", "x", "--resume=./x -c"],
            ["sandbox", "exec", "dev", "x", "--resume-with=./x -c"],
            ["sandbox", "exec", "dev", "x", "--resume-with", "./x -c"],
            ["sandbox", "exec", "dev", "x", "-r=./x -c"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.call(argv, "_execute")["resume"], "./x -c")

    def test_none_is_not_given_for_options_the_config_file_sets(self):
        up = self.call(
            [
                "sandbox",
                "up",
                "--workdir=None",
                "--image-name",
                "None",
                "--timeout=None",
                "-m=None",
            ],
            "_create_sandbox",
        )
        self.assertEqual(up, self.defaults("_create_sandbox"))

    def test_none_is_not_given_for_every_flag_that_takes_a_value(self):
        up = self.call(
            [
                "sandbox",
                "up",
                "--name=None",
                "--hostname",
                "None",
                "--repo=None",
                "--tailscale=None",
                "--no-snapshot=None",
                "--ephemeral=None",
            ],
            "_create_sandbox",
        )
        self.assertEqual(up, self.defaults("_create_sandbox"))
        shell = self.call(["sandbox", "shell", "--name=None", "-e=None"], "_shell")
        self.assertEqual((shell["name"], shell["engine"]), ("", "tailscale"))
        stop = self.call(
            ["sandbox", "ssh", "stop", "dev", "--engine=None"], "_ssh_stop"
        )
        self.assertEqual(stop["engine"], "tailscale")
        build = self.call(
            ["image", "build", "-i=None", "--force-build=None"], "_build_image"
        )
        self.assertEqual(build, self.defaults("_build_image"))
        code, output = self.exit_code("sandbox", "down", "--sandbox-id=None")
        self.assertEqual(code, 2)
        self.assertIn("Missing argument 'SANDBOX_ID'", output)

    def test_empty_values_and_old_negations(self):
        up = self.call(
            [
                "sandbox",
                "up",
                "--secret-name=",
                "--workdir=",
                "--notailscale",
                "--nono_snapshot",
            ],
            "_create_sandbox",
        )
        self.assertEqual(
            (up["secret_name"], up["workdir"], up["tailscale"], up["no_snapshot"]),
            ("", "", False, False),
        )

    def test_help_for_every_command_and_group(self):
        for path, _, _, values, _ in SURFACE:
            command = path.split()
            forms = [["--help"]]
            if path != "sandbox shell":  # there, what follows -- runs in the sandbox
                forms.append(["--", "--help"])
            if not any(short == "-h" for _, short, _, _ in values):
                forms.append(["-h"])
            for form in forms:
                with self.subTest(argv=command + form):
                    code, output = self.exit_code(*command, *form)
                    self.assertEqual(code, 0)
                    self.assertIn(f"Usage: agentix {path}", output)
        for group in GROUPS:
            for form in ([], ["--help"], ["-h"]):
                with self.subTest(argv=[group, *form]):
                    code, output = self.exit_code(*group.split(), *form)
                    self.assertEqual(code, 0)
                    self.assertIn("Commands:", output)

    def test_config_backed_options_show_their_defaults_in_help(self):
        _, output = self.exit_code("sandbox", "up", "--help")
        output = " ".join(output.split())
        self.assertIn("--tailscale / --no-tailscale", output)
        self.assertIn("--no-snapshot / --snapshot", output)
        self.assertIn("[default: 3600, or timeout in [sandbox.up]", output)
        self.assertNotIn("None", output)


class RefusedTest(CliTestCase):
    """What Fire ran anyway, or read oddly, now stops with exit 2 before any call."""

    def refused(self, *argv, says=""):
        with patch.object(cli_main, "_backend") as backend:
            code, output = self.exit_code(*argv)
        self.assertEqual(code, 2, output)
        self.assertIn(says, output)
        backend.assert_not_called()

    def test_mistyped_flags_and_commands_are_refused_with_a_suggestion(self):
        self.refused("sandbox", "up", "--nmae=dev", says="Did you mean")
        self.refused("image", "build", "--force", says="No such option")
        self.refused("sandbox", "upp", says="Did you mean 'up'")
        self.refused("sandbox", "shell", "dev", "--engin=openssh", says="--engine")
        for path, function, _, _, _ in SURFACE:
            extra = REQUIRED.get(function, ([], {}))[0]
            with self.subTest(command=path):
                self.refused(*path.split(), *extra, "--bogus", says="No such option")

    def test_values_of_the_wrong_type_are_refused(self):
        self.refused("sandbox", "up", "--timeout=abc", says="not a valid integer")
        self.refused("sandbox", "up", "--cpu=lots", says="not a valid float")
        self.refused("sandbox", "up", "--tailscale=maybe", says="True or False")
        self.refused("sandbox", "up", "--repo", says="requires an argument")

    def test_bad_boolean_words_show_the_usage(self):
        with patch.object(cli_main, "_backend") as backend:
            code, output = self.exit_code("sandbox", "up", "--tailscale=maybe")
        self.assertEqual(code, 2)
        self.assertIn("Usage: agentix sandbox up [OPTIONS]", output)
        self.assertIn("Try 'agentix sandbox up --help' for help.", output)
        backend.assert_not_called()

    def test_flags_are_not_filled_by_position(self):
        self.refused("sandbox", "up", "img", "3", says="unexpected extra argument")
        self.refused(
            "sandbox", "restore", "dev", "im-1", says="unexpected extra argument"
        )
        self.refused("sandbox", "prune", "14d", says="unexpected extra argument")

    def test_switches_that_took_no_value_still_take_none(self):
        for flag in ("--yes=no", "--yes=0", "--keep-latest=false"):
            with self.subTest(flag=flag):
                self.refused(
                    "sandbox", "prune", "--older-than=14d", flag, says="take a value"
                )
        self.refused("sandbox", "runs", "--clean=yes", says="take a value")
        self.refused("sandbox", "sync", "--pull=1", says="take a value")
        self.refused(
            "sandbox", "exec", "dev", "true", "--detach=1", says="take a value"
        )

    def test_missing_positionals_are_refused(self):
        self.refused("sandbox", "down", says="Missing argument 'SANDBOX_ID'")
        self.refused("sandbox", "exec", "dev", says="Missing argument 'COMMAND'")


class ChangedTest(CliTestCase):
    """Spellings Fire got wrong that now do what they say."""

    def test_no_tailscale_and_lowercase_booleans_work(self):
        for argv, value in (
            (["--no-tailscale"], False),
            (["--tailscale=false"], False),
            (["--tailscale=true"], True),
            (["--tailscale=1"], True),
        ):
            with self.subTest(argv=argv):
                up = self.call(["sandbox", "up", *argv], "_create_sandbox")
                self.assertIs(up["tailscale"], value)
        up = self.call(["sandbox", "up", "--snapshot"], "_create_sandbox")
        self.assertIs(up["no_snapshot"], False)
        purge = self.call(["sandbox", "purge", "dev", "--terminate=false"], "_purge")
        self.assertIs(purge["terminate"], False)

    def test_resume_before_the_command_is_a_flag(self):
        run = self.call(["sandbox", "exec", "dev", "--resume", "claude"], "_execute")
        self.assertEqual((run["command"], run["resume"]), ("claude", True))

    def test_numbers_stay_text(self):
        up = self.call(
            ["sandbox", "up", "--name=7", "--image-name=7"], "_create_sandbox"
        )
        self.assertEqual((up["name"], up["image_name"]), ("7", "7"))


class PrintResultTest(unittest.TestCase):
    """Results print as python-fire printed them."""

    def printed(self, value):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            print_result(value)
        return output.getvalue()

    def test_values(self):
        self.assertEqual(self.printed(None), "")
        self.assertEqual(self.printed("a\nb"), "a\nb\n")
        self.assertEqual(self.printed(3), "3\n")
        self.assertEqual(self.printed(True), "True\n")

    def test_a_dict_prints_aligned_key_value_lines(self):
        self.assertEqual(
            self.printed({"sandbox_id": "sb-1", "name": "dev", "ok": True, "_x": 1}),
            "sandbox_id: sb-1\nname:       dev\nok:         true\n",
        )
        self.assertEqual(
            self.printed({"tags": ["a", "é"], "n": None}),
            'tags: ["a", "é"]\nn:    null\n',
        )
        self.assertEqual(self.printed({}), "{}\n")

    def test_a_list_prints_one_json_line_per_item(self):
        self.assertEqual(
            self.printed([{"a": 1}, "two\nlines", 3]),
            '{"a": 1}\ntwo lines\n3\n',
        )


if __name__ == "__main__":
    unittest.main()
