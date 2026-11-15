"""scripts/check_env.py: committed env files hold no plaintext values.

The parsing tests run anywhere. The others run the real dotenvx, from PATH or
$DOTENVX, and are skipped without it; the flake's `env-check` check runs them
with the pinned dotenvx.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_env.py"

spec = importlib.util.spec_from_file_location("check_env", SCRIPT)
check_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_env)

DOTENVX = os.environ.get("DOTENVX") or shutil.which("dotenvx")

ENVFILE = textwrap.dedent(
    """\
    # comment
    strict true
    env "TOKEN", redacted: true, encrypted: true
    env "OTHER"
    env "OPTIONAL", optional: true
    """
)
# Placeholders, not secrets: the check must refuse them as plaintext.
PLAIN = "not-a-real-secret"


class ParsingTests(unittest.TestCase):
    def test_env_files_are_the_ones_dotenvx_protects(self):
        for path in [
            ".env",
            ".env.local",
            ".env.example",
            ".env.keys",
            "app/.env.production",
            "prod.env",
            ".flaskenv",
            ".dev.vars",
            ".env.d/extra",
        ]:
            self.assertTrue(check_env.is_env_file(path), path)
        for path in [".envrc", "env.py", "Envfile", "docs/env.md", "environment"]:
            self.assertFalse(check_env.is_env_file(path), path)

    def test_templates(self):
        self.assertTrue(check_env.is_template(".env.example"))
        self.assertTrue(check_env.is_template("app/.env.prod.sample"))
        self.assertFalse(check_env.is_template(".env"))

    def test_keys_with_quotes_comments_export_and_multiline_values(self):
        content = textwrap.dedent(
            """\
            # A=commented out
            export B=op://v/i/f
            C='single' # trailing
            D="double
            E=inside the quoted value
            "
            F=`backtick`
              G = spaced
            H: colon
            I=
            B=again
            """
        )
        self.assertEqual(
            check_env.env_keys(content), ["B", "C", "D", "F", "G", "H", "I"]
        )

    def test_crlf(self):
        self.assertEqual(check_env.env_keys("A=1\r\nB=2\r\n"), ["A", "B"])

    def test_declared_keys_in_any_block(self):
        envfile = ENVFILE + 'file ".env.production" do\n  env "PROD_ONLY"\nend\n'
        self.assertEqual(
            check_env.declared_keys(envfile),
            {"TOKEN", "OTHER", "OPTIONAL", "PROD_ONLY"},
        )

    def test_public_key_needs_no_declaration(self):
        self.assertEqual(
            check_env.undeclared(
                ["TOKEN", "DOTENV_PUBLIC_KEY", "DOTENV_PUBLIC_KEY_CI", "EXTRA"],
                {"TOKEN"},
            ),
            ["EXTRA"],
        )

    def test_no_dotenvx(self):
        with mock.patch.dict(os.environ, {"DOTENVX": ""}):
            with mock.patch.object(check_env.shutil, "which", return_value=None):
                self.assertEqual(check_env.main([]), 2)


@unittest.skipUnless(DOTENVX, "dotenvx is not installed")
class DotenvxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One real ciphertext, made offline with a throwaway key pair that is
        # deleted with the directory.
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(f"TOKEN={PLAIN}\n")
            subprocess.run(
                [DOTENVX, "encrypt", "-f", str(env_file)],
                cwd=directory,
                env=check_env.dotenvx_env(),
                check=True,
                capture_output=True,
            )
            lines = env_file.read_text().splitlines()
        cls.ciphertext = next(
            line.split("=", 1)[1] for line in lines if line.startswith("TOKEN=")
        )
        cls.public_key = next(
            line for line in lines if line.startswith("DOTENV_PUBLIC_KEY")
        )
        assert cls.ciphertext.strip("\"'").startswith("encrypted:")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.git("init", "-q")
        (self.repo / "Envfile").write_text(ENVFILE)
        (self.repo / ".gitignore").write_text(".env.*\n!.env.example\n")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))

    def check(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            env={**os.environ, "DOTENVX": DOTENVX},
            capture_output=True,
            text=True,
        )
        # Whatever happens, no plaintext value reaches the output.
        self.assertNotIn(PLAIN, result.stdout + result.stderr)
        return result

    def assertPasses(self, *args):
        result = self.check(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assertFails(self, *args):
        result = self.check(*args)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        return result

    def test_op_references(self):
        self.write(".env", "TOKEN=op://agentix/modal/token\nOTHER=op://v/i/f\n")
        self.assertPasses()

    def test_encrypted_values(self):
        self.write(
            ".env",
            f"{self.public_key}\nTOKEN={self.ciphertext}\nOTHER=op://v/i/f\n",
        )
        self.assertPasses()

    def test_plaintext_fails(self):
        self.write(".env", f"TOKEN={PLAIN}\nOTHER=op://v/i/f\n")
        result = self.assertFails()
        self.assertIn("TOKEN", result.stderr)

    def test_plaintext_in_an_undeclared_key_fails(self):
        self.write(".env", f"TOKEN=op://v/i/f\nOTHER=op://v/i/f\nEXTRA={PLAIN}\n")
        result = self.assertFails()
        self.assertIn("EXTRA", result.stderr)

    def test_empty_value_is_not_plaintext_but_a_required_key_needs_one(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\nOPTIONAL=\n")
        self.assertPasses()
        self.write(".env", "TOKEN=\nOTHER=op://v/i/f\n")
        result = self.assertFails()
        self.assertIn("TOKEN", result.stdout + result.stderr)

    def test_quoting_variants(self):
        self.write(
            ".env",
            """\
            export TOKEN='op://agentix/modal/token'
            OTHER="op://v/i/f" # a comment
            """,
        )
        self.assertPasses()
        for line in [
            f'OTHER="{PLAIN}"',
            f"OTHER='{PLAIN}'",
            f"export OTHER={PLAIN}",
            f"OTHER={PLAIN} # op://looks/like/a/reference",
            'OTHER=" op://leading/space/makes/it/plaintext"',
        ]:
            with self.subTest(line=line):
                self.write(".env", f"TOKEN=op://v/i/f\n{line}\n")
                self.assertFails()

    def test_duplicate_key_with_a_plaintext_value_fails(self):
        self.write(".env", f"TOKEN=op://v/i/f\nOTHER=op://v/i/f\nTOKEN={PLAIN}\n")
        self.assertFails()

    def test_comments_are_not_values(self):
        self.write(
            ".env",
            f"""\
            # TOKEN={PLAIN}
            TOKEN=op://v/i/f
            OTHER=op://v/i/f
            """,
        )
        self.assertPasses()

    def test_key_missing_from_the_file(self):
        self.write(".env", "TOKEN=op://v/i/f\n")
        result = self.assertFails()
        self.assertIn("OTHER", result.stdout + result.stderr)

    def test_key_not_in_the_spec(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\nEXTRA=op://v/i/f\n")
        result = self.assertFails()
        self.assertIn("EXTRA is not declared in Envfile", result.stderr)

    def test_no_shell_value_stands_in_for_a_missing_key(self):
        self.write(".env", "TOKEN=op://v/i/f\n")
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.repo,
            env={**os.environ, "DOTENVX": DOTENVX, "OTHER": "op://v/i/f"},
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)

    def test_example_files_need_no_values_but_hold_no_plaintext(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.write(".env.example", "TOKEN=\nOTHER=op://v/i/f\nNEW_KEY=\n")
        self.assertPasses()
        self.write(".env.example", f"TOKEN={PLAIN}\n")
        self.assertFails()

    def test_private_key_files_fail(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.write(".env.keys", "DOTENV_PRIVATE_KEY=\n")
        self.assertFails(".env.keys")

    def test_ignored_files_are_left_alone_and_untracked_ones_checked(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.write(".env.local", f"TOKEN={PLAIN}\n")
        self.assertPasses()
        self.write("prod.env", f"TOKEN={PLAIN}\n")
        self.assertFails()

    def test_staged_checks_the_index_not_the_working_tree(self):
        self.write(".env", f"TOKEN={PLAIN}\nOTHER=op://v/i/f\n")
        self.git("add", ".env")
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.assertFails("--staged")
        self.git("add", ".env")
        self.assertPasses("--staged")

    def test_staged_envfile_rechecks_every_env_file(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.git("add", ".env")
        self.git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "env",
        )
        self.write("Envfile", ENVFILE + 'env "MORE"\n')
        self.git("add", "Envfile")
        result = self.assertFails("--staged")
        self.assertIn("MORE", result.stdout + result.stderr)

    def test_named_files(self):
        self.write("sub/.env", f"TOKEN={PLAIN}\n")
        self.assertFails("sub/.env")

    def test_missing_envfile(self):
        (self.repo / "Envfile").unlink()
        self.write(".env", "TOKEN=op://v/i/f\n")
        self.assertIn("Envfile is missing", self.assertFails().stderr)


if __name__ == "__main__":
    unittest.main()
