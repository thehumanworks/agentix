"""The config file of `sandbox up`: keys, precedence, errors, and `config show`."""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ModalTestCase

from agentix import config
from agentix.config import ConfigError
from agentix.defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT


class ConfigTestCase(ModalTestCase):
    """ModalTestCase points XDG_CONFIG_HOME at an empty directory."""

    def write_config(self, text: str | bytes) -> Path:
        file = Path(self.config_home) / "agentix" / "config.toml"
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            file.write_bytes(text)
        else:
            file.write_text(text)
        return file


class PathTest(unittest.TestCase):
    def test_default_path_is_under_dot_config(self):
        with patch.dict(os.environ, {"HOME": "/home/x"}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(config.path(), Path("/home/x/.config/agentix/config.toml"))

    def test_xdg_config_home_is_honoured(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/cfg"}):
            self.assertEqual(config.path(), Path("/cfg/agentix/config.toml"))


class LoadTest(ConfigTestCase):
    def test_missing_file_changes_nothing(self):
        self.assertEqual(config.load(), {})
        self.assertEqual(
            config.up_options(),
            {name: key.default for name, key in config.UP_KEYS.items()},
        )

    def test_every_key_is_read(self):
        self.write_config(
            "[sandbox.up]\n"
            'image_name = "scratch:1"\n'
            'secret_name = ""\n'
            "timeout = 600\n"
            "memory = 4096\n"
            "cpu = 2\n"
            'workdir = "/src"\n'
            "tailscale = true\n"
            "no_snapshot = true\n"
        )
        self.assertEqual(
            config.load(),
            {
                "image_name": "scratch:1",
                "secret_name": "",
                "timeout": 600,
                "memory": 4096,
                "cpu": 2.0,
                "workdir": "/src",
                "tailscale": True,
                "no_snapshot": True,
            },
        )

    def test_empty_file_and_empty_tables_are_fine(self):
        for text in ("", "[sandbox]\n", "[sandbox.up]\n"):
            with self.subTest(text=text):
                self.write_config(text)
                self.assertEqual(config.load(), {})

    def test_unknown_key_names_the_key_and_the_file(self):
        file = self.write_config("[sandbox.up]\ntimout = 600\n")
        with self.assertRaises(ConfigError) as caught:
            config.load()
        message = str(caught.exception)
        self.assertIn("sandbox.up.timout", message)
        self.assertIn(str(file), message)
        self.assertIn("timeout", message)  # the known keys

    def test_dashed_key_suggests_the_underscore_form(self):
        self.write_config('[sandbox.up]\nimage-name = "x"\n')
        with self.assertRaisesRegex(ConfigError, "Write it as image_name"):
            config.load()

    def test_unknown_tables_are_refused(self):
        for text, key in (
            ("timeout = 600\n", "timeout"),
            ("[image]\nname = 'x'\n", "image"),
            ("[sandbox.restore]\ntailscale = true\n", "sandbox.restore"),
        ):
            with self.subTest(text=text):
                file = self.write_config(text)
                with self.assertRaises(ConfigError) as caught:
                    config.load()
                self.assertIn(f"unknown key {key}", str(caught.exception))
                self.assertIn(str(file), str(caught.exception))

    def test_a_table_that_is_not_a_table_is_refused(self):
        self.write_config("sandbox = 1\n")
        with self.assertRaisesRegex(ConfigError, "sandbox must be a table"):
            config.load()

    def test_wrong_types_are_refused(self):
        for line, expected in (
            ('timeout = "600"', "timeout must be an integer"),
            ("timeout = true", "timeout must be an integer"),
            ("memory = 1.5", "memory must be an integer"),
            ('cpu = "2"', "cpu must be a number"),
            ("tailscale = 1", "tailscale must be a boolean"),
            ('no_snapshot = "yes"', "no_snapshot must be a boolean"),
            ("image_name = 7", "image_name must be a string"),
        ):
            with self.subTest(line=line):
                file = self.write_config(f"[sandbox.up]\n{line}\n")
                with self.assertRaises(ConfigError) as caught:
                    config.load()
                self.assertIn(expected, str(caught.exception))
                self.assertIn(str(file), str(caught.exception))

    def test_invalid_toml_is_a_clear_error(self):
        file = self.write_config("[sandbox.up\ntimeout = \n")
        with self.assertRaises(ConfigError) as caught:
            config.load()
        self.assertIn(f"{file} is not valid TOML", str(caught.exception))

    def test_invalid_utf8_is_a_clear_error(self):
        file = self.write_config(b"[sandbox.up]\nworkdir = '\xff'\n")
        with self.assertRaisesRegex(ConfigError, f"Cannot read the config file {file}"):
            config.load()

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_unreadable_file_is_a_clear_error(self):
        file = self.write_config("[sandbox.up]\n")
        file.chmod(0)
        with self.assertRaisesRegex(ConfigError, f"Cannot read the config file {file}"):
            config.load()

    def test_a_directory_in_place_of_the_file_is_a_clear_error(self):
        file = Path(self.config_home) / "agentix" / "config.toml"
        file.mkdir(parents=True)
        with self.assertRaisesRegex(ConfigError, f"Cannot read the config file {file}"):
            config.load()


class PrecedenceTest(ConfigTestCase):
    def test_flag_beats_config_beats_default(self):
        file = self.write_config("[sandbox.up]\ntimeout = 600\nmemory = 4096\n")
        resolved = config.resolve({"timeout": 60})
        self.assertEqual(resolved["timeout"], (60, "flag"))
        self.assertEqual(resolved["memory"], (4096, f"config {file}"))
        self.assertEqual(resolved["cpu"], (0, "default"))

    def test_flag_equal_to_the_built_in_default_still_wins(self):
        self.write_config(
            "[sandbox.up]\n"
            f"timeout = {SANDBOX_TIMEOUT + 1}\n"
            "tailscale = true\n"
            "no_snapshot = true\n"
            'workdir = "/src"\n'
            'secret_name = ""\n'
            "cpu = 4\n"
        )
        options = config.up_options(
            timeout=SANDBOX_TIMEOUT,
            tailscale=False,
            no_snapshot=False,
            workdir="",
            secret_name="1password",
            cpu=0,
        )
        self.assertEqual(options["timeout"], SANDBOX_TIMEOUT)
        self.assertIs(options["tailscale"], False)
        self.assertIs(options["no_snapshot"], False)
        self.assertEqual(options["workdir"], "")
        self.assertEqual(options["secret_name"], "1password")
        self.assertEqual(options["cpu"], 0)

    def test_ignored_file_value_falls_back_to_the_default_but_not_a_flag(self):
        self.write_config('[sandbox.up]\nworkdir = "/src"\nmemory = 4096\n')
        options = config.up_options(ignore_file=("workdir",))
        self.assertEqual(options["workdir"], "")
        self.assertEqual(options["memory"], 4096)
        options = config.up_options(ignore_file=("workdir",), workdir="/x")
        self.assertEqual(options["workdir"], "/x")

    def test_flag_of_the_wrong_type_is_refused(self):
        with self.assertRaisesRegex(
            ConfigError, r"--tailscale \(write True or False\)"
        ):
            config.up_options(tailscale="false")
        with self.assertRaisesRegex(ConfigError, "--timeout: timeout must be"):
            config.up_options(timeout="soon")

    def test_string_flag_that_fire_read_as_a_number_stays_a_string(self):
        self.assertEqual(config.up_options(image_name=7)["image_name"], "7")

    def test_broken_file_is_reported_even_when_flags_set_every_key(self):
        self.write_config("[sandbox.up]\nbogus = 1\n")
        with self.assertRaisesRegex(ConfigError, "bogus"):
            config.up_options(
                **{name: key.default for name, key in config.UP_KEYS.items()}
            )


class CliTest(ConfigTestCase):
    """`sandbox up` through the command line, as Fire parses it."""

    def setUp(self):
        super().setUp()
        self.backend = self._patch("agentix.__main__._backend").return_value
        self.backend.create_sandbox.return_value = {"sandbox_id": "sb-x", "name": ""}

    def up(self, *flags):
        self.run_cli("sandbox", "up", *flags)
        return self.backend.create_sandbox.call_args.kwargs

    def test_without_file_or_flags_the_built_in_defaults_apply(self):
        kwargs = self.up()
        self.assertEqual(kwargs["image_name"], IMAGE_NAME)
        self.assertEqual(kwargs["timeout"], SANDBOX_TIMEOUT)
        self.assertEqual(kwargs["memory"], SANDBOX_MEMORY_MIB)
        self.assertEqual(kwargs["workdir"], "")
        self.assertEqual(kwargs["cpu"], 0)
        self.assertIs(kwargs["tailscale"], False)
        self.assertIs(kwargs["exit_snapshot"], True)

    def test_file_sets_the_defaults(self):
        self.write_config(
            "[sandbox.up]\n"
            'image_name = "scratch:1"\n'
            "timeout = 600\n"
            "memory = 4096\n"
            "no_snapshot = true\n"
        )
        kwargs = self.up()
        self.assertEqual(kwargs["image_name"], "scratch:1")
        self.assertEqual(kwargs["timeout"], 600)
        self.assertEqual(kwargs["memory"], 4096)
        self.assertIs(kwargs["exit_snapshot"], False)

    def test_flags_override_the_file_also_at_the_built_in_default(self):
        self.write_config(
            "[sandbox.up]\n"
            "timeout = 600\n"
            "memory = 4096\n"
            'workdir = "/src"\n'
            "tailscale = true\n"
            "no_snapshot = true\n"
        )
        kwargs = self.up(
            f"--timeout={SANDBOX_TIMEOUT}",
            "--memory=2048",
            "--workdir=",
            "--notailscale",
            "--no-snapshot=False",
            "--secret-name=",
        )
        self.assertEqual(kwargs["timeout"], SANDBOX_TIMEOUT)
        self.assertEqual(kwargs["memory"], 2048)
        self.assertEqual(kwargs["workdir"], "")
        self.assertIs(kwargs["tailscale"], False)
        self.assertIs(kwargs["exit_snapshot"], True)
        self.assertEqual(kwargs["secret_name"], "")

    def test_ephemeral_still_turns_the_snapshot_off(self):
        self.write_config("[sandbox.up]\nno_snapshot = false\n")
        self.assertIs(self.up("--ephemeral")["exit_snapshot"], False)

    def test_tailscale_from_the_file_is_checked_against_the_secret(self):
        self.write_config('[sandbox.up]\ntailscale = true\nsecret_name = ""\n')
        with self.assertRaisesRegex(Exception, "secret"):
            self.run_cli("sandbox", "up")
        self.backend.create_sandbox.assert_not_called()

    def test_broken_file_stops_sandbox_up_before_anything_starts(self):
        self.write_config("[sandbox.up]\nmemroy = 1\n")
        with self.assertRaisesRegex(ConfigError, "memroy"):
            self.run_cli("sandbox", "up")
        self.backend.create_sandbox.assert_not_called()

    def test_broken_file_does_not_affect_other_commands(self):
        self.write_config("not toml [")
        self.backend.list_sandboxes.return_value = []
        self.run_cli("sandbox", "ls")

    def test_config_show_prints_values_and_sources(self):
        file = self.write_config("[sandbox.up]\ntimeout = 600\n")
        rows = {
            row["key"]: row
            for row in map(json.loads, self.run_cli("config", "show").splitlines())
        }
        self.assertEqual(set(rows), set(config.UP_KEYS))
        self.assertEqual(rows["timeout"]["value"], 600)
        self.assertEqual(rows["timeout"]["source"], f"config {file}")
        self.assertEqual(rows["memory"]["value"], SANDBOX_MEMORY_MIB)
        self.assertEqual(rows["memory"]["source"], "default")
        self.assertEqual(rows["tailscale"]["type"], "boolean")


class EndToEndTest(ConfigTestCase):
    def test_file_values_reach_modal(self):
        self.write_config("[sandbox.up]\ntimeout = 600\nmemory = 4096\ncpu = 2\n")
        self.run_cli("sandbox", "up", "--memory=1024")
        kwargs = self.create.aio.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 600)
        self.assertEqual(kwargs["memory"], 1024)
        self.assertEqual(kwargs["cpu"], 2.0)


if __name__ == "__main__":
    unittest.main()
