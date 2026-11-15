"""scripts/check_env.py: committed env files hold no plaintext values.

The parsing tests run anywhere. The others run the real dotenvx, from PATH or
$DOTENVX, and are skipped without it; the flake's `env-check` check runs them
with the pinned dotenvx.
"""

import ast
import base64
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
# Well-formed stand-ins: a compressed public key, and ciphertext whose bytes
# start with an uncompressed ephemeral key.
FAKE_PUBLIC_KEY = "02" + "ab" * 32
FAKE_CIPHERTEXT = "encrypted:" + base64.b64encode(b"\x04" + b"\x01" * 120).decode()


def problems(content):
    return check_env.parse(textwrap.dedent(content))[1]


class ParsingTests(unittest.TestCase):
    def test_runs_on_python_3_9(self):
        # A Mac's python3 is 3.9; ruff formats for 3.14.
        ast.parse(SCRIPT.read_text(), feature_version=(3, 9))

    def test_env_files(self):
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
            "x/.env/secrets",
            ".ENV",
            "app/.Env.Local",
            "PROD.ENV",
        ]:
            self.assertTrue(check_env.is_env_file(path), path)
        for path in [".envrc", "env.py", "Envfile", "docs/env.md", "environment"]:
            self.assertFalse(check_env.is_env_file(path), path)

    def test_envfiles(self):
        for path in ["Envfile", "sub/Envfile", "sub/envfile", "Dotenvxfile"]:
            self.assertTrue(check_env.is_envfile(path), path)
        self.assertFalse(check_env.is_envfile("Envfile.md"))

    def test_templates(self):
        self.assertTrue(check_env.is_template(".env.example"))
        self.assertTrue(check_env.is_template("app/.env.prod.sample"))
        self.assertFalse(check_env.is_template(".env"))

    def test_allowed_values(self):
        content = f"""\
            # a comment
            #/---[DOTENV_PUBLIC_KEY]---/
            DOTENV_PUBLIC_KEY="{FAKE_PUBLIC_KEY}"
            export A=op://vault/item/field
            B='op://vault/item/section/field' # trailing comment
            C="{FAKE_CIPHERTEXT}"
            D=`op://v/i/f`
            E=
            F=""
            # G=op://commented/out/reference

            """
        self.assertEqual(problems(content), [])
        self.assertEqual(
            check_env.parse(textwrap.dedent(content))[0],
            ["DOTENV_PUBLIC_KEY", "A", "B", "C", "D", "E", "F"],
        )

    def test_crlf_cr_and_bom(self):
        content = "﻿A=op://v/i/f\r\nB=op://v/i/f\rC=\n"
        self.assertEqual(check_env.parse(content), (["A", "B", "C"], []))

    def test_plaintext_values(self):
        for line in [
            f"A={PLAIN}",
            f'A="{PLAIN}"',
            f"A='{PLAIN}'",
            f"A=`{PLAIN}`",
            f"export A={PLAIN}",
            f"A={PLAIN} # op://looks/like/a/reference",
            "A= op://leading/space",
            'A=" op://leading/space"',
        ]:
            with self.subTest(line=line):
                self.assertTrue(problems(line), line)

    def test_lines_that_are_not_assignments(self):
        # HIGH-1: dotenv parsers skip these, so they need a rule of their own.
        for content in [
            PLAIN,
            f"MY KEY={PLAIN}",
            f"A = {PLAIN}",
            f"A: {PLAIN}",
            f'A="op://a/b/c\n{PLAIN}"',
            f'A="{PLAIN}',
            f"1A={PLAIN}",
            "A.B=op://v/i/f",
        ]:
            with self.subTest(content=content):
                found = problems(content)
                self.assertTrue(found, content)
                self.assertTrue(all(PLAIN not in p for p in found))

    def test_commented_out_assignments(self):
        for line in [
            f"# TOKEN={PLAIN}",
            f"#TOKEN={PLAIN}",
            f"#   export TOKEN = {PLAIN}",
            f'# TOKEN="{PLAIN}"',
        ]:
            with self.subTest(line=line):
                self.assertTrue(problems(line + "\nTOKEN=op://v/i/f\n"))
        self.assertEqual(problems("# TOKEN=op://v/i/f\n# not an assignment\n"), [])

    def test_prefixes_do_not_make_plaintext_safe(self):
        # MEDIUM-2.
        for line in [
            f'A="op://x {PLAIN}"',
            f'A="op://a/b/c\\n{PLAIN}"',
            "A=op://only/two",
            "A=op://a/b/c/d/e",
            "A=op://a//c",
            f"A=encrypted:{PLAIN}",
            "A=encrypted:" + base64.b64encode(b"\x01" * 121).decode(),
            "A=encrypted:" + "A" * 119,
            f"DOTENV_PUBLIC_KEY={PLAIN}",
            f"DOTENV_PUBLIC_KEY_PRODUCTION={PLAIN}",
            "DOTENV_PUBLIC_KEY=04" + "ab" * 32,
            "DOTENV_PUBLIC_KEY=",
            "DOTENV_PRIVATE_KEY=" + "ab" * 32,
        ]:
            with self.subTest(line=line):
                self.assertTrue(problems(line), line)

    def test_envfile_grammar(self):
        self.assertEqual(check_env.envfile_problems(ENVFILE), [])
        self.assertEqual(check_env.envfile_problems((ROOT / "Envfile").read_text()), [])
        # MEDIUM-3 and LOW-7.
        for line in [
            'env "FOO", encrypted: false',
            'env "FOO", redacted: false',
            f'env "FOO", default: "{PLAIN}"',
            'env "FOO", optional: true, encrypted: false',
            'file ".env.production" do',
            'profile "dev", files: [".env"]',
            "strict false",
            "env FOO",
            PLAIN,
        ]:
            with self.subTest(line=line):
                found = check_env.envfile_problems(ENVFILE + line + "\n")
                self.assertTrue(found, line)
                self.assertTrue(all(PLAIN not in p for p in found))

    def test_declared_keys(self):
        self.assertEqual(
            check_env.declared_keys(ENVFILE), {"TOKEN", "OTHER", "OPTIONAL"}
        )

    def test_public_key_needs_no_declaration(self):
        self.assertEqual(
            check_env.undeclared(
                ["TOKEN", "DOTENV_PUBLIC_KEY", "DOTENV_PUBLIC_KEY_CI", "EXTRA"],
                {"TOKEN"},
            ),
            ["EXTRA"],
        )

    def test_dotenvx_env_turns_off_armor_and_key_stores(self):
        # LOW-5: dotenvx 2.38 compares these with "true".
        env = check_env.dotenvx_env()
        for name in ["DOTENVX_NO_ARMOR", "DOTENVX_NO_NATIVE", "DOTENVX_NO_BITWARDEN"]:
            self.assertEqual(env[name], "true")

    def test_no_dotenvx(self):
        with mock.patch.dict(os.environ, {"DOTENVX": ""}):
            with mock.patch.object(check_env.shutil, "which", return_value=None):
                self.assertEqual(check_env.main([]), 2)

    def test_dotenvx_version(self):
        # LOW-6.
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "dotenvx"
            for version, ok in [
                ("2.38.2", True),
                ("2.32.4", True),
                ("2.32.3", False),
                ("1.51.0", False),
                ("2.39.0", False),
                ("3.0.0", False),
                ("garbage", False),
            ]:
                with self.subTest(version=version):
                    fake.write_text(f"#!/bin/sh\necho {version}\n")
                    fake.chmod(0o755)
                    problem = check_env.dotenvx_problem(str(fake))
                    self.assertEqual(problem is None, ok, problem)
                    if not ok:
                        self.assertIn(str(fake), problem)
                        with mock.patch.dict(os.environ, {"DOTENVX": str(fake)}):
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
        # --staged reads the Envfile from the index.
        self.git("add", "Envfile")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))

    def check(self, *args, env=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            env={**os.environ, "DOTENVX": DOTENVX, **(env or {})},
            capture_output=True,
            text=True,
        )
        # Whatever happens, no plaintext value reaches the output.
        self.assertNotIn(PLAIN, result.stdout + result.stderr)
        return result

    def assertPasses(self, *args, **kwargs):
        result = self.check(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assertFails(self, *args, **kwargs):
        result = self.check(*args, **kwargs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        return result

    def test_the_repository_passes(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env={**os.environ, "DOTENVX": DOTENVX},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    def test_comments(self):
        self.write(
            ".env",
            """\
            # The Modal token, from 1Password.
            # TOKEN=op://old/reference/here
            TOKEN=op://v/i/f
            OTHER=op://v/i/f
            """,
        )
        self.assertPasses()

    def test_commented_out_plaintext_fails(self):
        # HIGH-1 (a): the likeliest leak, an old value left above its reference.
        self.write(".env", f"# TOKEN={PLAIN}\nTOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.assertIn("commented-out TOKEN", self.assertFails().stderr)

    def test_lines_that_are_not_assignments_fail(self):
        # HIGH-1 (b) and (c).
        for line in [PLAIN, f"MY KEY={PLAIN}"]:
            with self.subTest(line=line):
                self.write(".env", f"TOKEN=op://v/i/f\nOTHER=op://v/i/f\n{line}\n")
                self.assertIn("line 3", self.assertFails().stderr)
                self.git("add", ".env")
                self.assertFails("--staged")

    def test_plaintext_behind_an_allowed_prefix_fails(self):
        # MEDIUM-2.
        for line in [
            f'OTHER="op://a/b/c\n{PLAIN}"',
            f'OTHER="op://x {PLAIN}"',
            f'OTHER="op://a/b/c\\n{PLAIN}"',
            f"OTHER=encrypted:{PLAIN}",
            f"DOTENV_PUBLIC_KEY={PLAIN}",
            f"DOTENV_PUBLIC_KEY_X={PLAIN}",
        ]:
            with self.subTest(line=line):
                self.write(".env", f"TOKEN=op://v/i/f\nOTHER=op://v/i/f\n{line}\n")
                self.assertFails()

    def test_a_loosened_envfile_fails(self):
        # MEDIUM-3 (a): plaintext allowed, and printed by `dotenvx check`.
        self.write(".env", f"TOKEN=op://v/i/f\nOTHER=op://v/i/f\nFOO={PLAIN}\n")
        for option in [
            "encrypted: false",
            "redacted: false",
            "encrypted: false, redacted: false",
        ]:
            with self.subTest(option=option):
                self.write("Envfile", ENVFILE + f'env "FOO", {option}\n')
                self.assertIn("is not allowed", self.assertFails().stderr)

    def test_a_nested_envfile_fails(self):
        # MEDIUM-3 (b): dotenvx reads the Envfile beside the file first.
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.write(
            "sub/Envfile",
            'env "TOKEN", encrypted: false\nenv "OTHER", encrypted: false\n',
        )
        self.write("sub/.env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.assertIn("only the root Envfile", self.assertFails().stderr)
        self.write("sub/.env", f"TOKEN={PLAIN}\nOTHER={PLAIN}\n")
        self.git("add", "-A")
        self.assertFails("--staged")

    def test_a_default_value_in_the_envfile_fails(self):
        # LOW-7.
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.write(
            "Envfile", ENVFILE + f'env "FOO", optional: true, default: "{PLAIN}"\n'
        )
        self.assertFails()

    def test_more_env_file_names(self):
        # LOW-7: a directory named .env, and case variants.
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        for name in ["x/.env/secrets", ".ENV", "PROD.Env"]:
            with self.subTest(name=name):
                self.write(name, f"TOKEN={PLAIN}\n")
                self.assertFails()
                (self.repo / name).unlink()

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
        self.assertFails(env={"OTHER": "op://v/i/f"})

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

    def test_staged_uses_the_staged_envfile(self):
        # LOW-4: an unstaged Envfile edit does not change the verdict.
        self.git("add", "Envfile")
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\nFOO=op://v/i/f\n")
        self.git("add", ".env")
        self.write("Envfile", ENVFILE + 'env "FOO"\n')
        self.assertIn("FOO is not declared", self.assertFails("--staged").stderr)
        self.git("add", "Envfile")
        self.assertPasses("--staged")

    def test_staged_envfile_rechecks_every_env_file(self):
        self.write(".env", "TOKEN=op://v/i/f\nOTHER=op://v/i/f\n")
        self.git("add", ".env", "Envfile")
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
