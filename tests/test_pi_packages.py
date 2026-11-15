"""The pi packages that pi.nix installs, and how activation lays them out.

scripts/pi-packages.sh runs here under bash, as writeShellApplication runs it
when Home Manager activates.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "nix/home-manager/modules"
PI_PACKAGES = MODULES / "scripts/pi-packages.sh"


@unittest.skipUnless(shutil.which("jq"), "the script needs jq")
class PiLayoutTests(unittest.TestCase):
    """How activation lays out the packages in pi's npm directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "home/.pi/agent/npm"
        self.modules = self.root / "node_modules"

    def store(self, name: str, version: str) -> Path:
        store = self.tmp / "store" / name
        for package in ("pi-open-tui", "@nothumanwork/pi-tps", ".bin"):
            (store / "node_modules" / package).mkdir(parents=True)
        (store / "node_modules/pi-open-tui/package.json").write_text(version)
        deps = {"pi-open-tui": version, "@nothumanwork/pi-tps": version}
        (store / "package.json").write_text(json.dumps({"dependencies": deps}))
        (store / "package-lock.json").write_text(f"lock {version}")
        return store

    def lay_out(self, store: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-euo", "pipefail", str(PI_PACKAGES), str(self.root), str(store)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_links_each_package_into_the_store(self):
        store = self.store("a", "1")
        self.lay_out(store)
        for package in ("pi-open-tui", "@nothumanwork/pi-tps", ".bin"):
            with self.subTest(package=package):
                entry = self.modules / package
                self.assertTrue(entry.is_symlink())
                self.assertEqual(entry.resolve(), (store / "node_modules" / package))
        self.assertFalse((self.modules / "@nothumanwork").is_symlink())
        self.assertEqual((self.root / "package-lock.json").read_text(), "lock 1")
        self.assertEqual((self.root / ".gitignore").read_text(), "*\n!.gitignore\n")

    def test_a_new_store_keeps_what_the_user_installed(self):
        self.lay_out(self.store("a", "1"))
        # `pi install npm:extra`, and `pi update` of pi-open-tui.
        (self.modules / "extra").mkdir()
        (self.modules / "pi-open-tui").unlink()
        (self.modules / "pi-open-tui").mkdir()
        package = json.loads((self.root / "package.json").read_text())
        package["dependencies"]["extra"] = "^2.0.0"
        (self.root / "package.json").write_text(json.dumps(package))
        (self.root / "package-lock.json").write_text("lock by npm")

        new = self.store("b", "2")
        self.lay_out(new)
        self.assertTrue((self.modules / "extra").is_dir())
        self.assertFalse((self.modules / "pi-open-tui").is_symlink())
        tps = self.modules / "@nothumanwork/pi-tps"
        self.assertEqual(tps.resolve(), new / "node_modules/@nothumanwork/pi-tps")
        self.assertEqual(
            json.loads((self.root / "package.json").read_text())["dependencies"],
            {"pi-open-tui": "2", "@nothumanwork/pi-tps": "2", "extra": "^2.0.0"},
        )
        self.assertEqual((self.root / "package-lock.json").read_text(), "lock by npm")

    def test_a_new_store_replaces_its_own_layout(self):
        self.lay_out(self.store("a", "1"))
        new = self.store("b", "2")
        self.lay_out(new)
        self.assertEqual(
            (self.modules / "pi-open-tui").resolve(), new / "node_modules/pi-open-tui"
        )
        self.assertEqual((self.root / "package-lock.json").read_text(), "lock 2")

    def test_the_same_store_again_changes_nothing(self):
        store = self.store("a", "1")
        self.lay_out(store)
        (self.modules / "pi-open-tui").unlink()
        self.lay_out(store)
        self.assertFalse((self.modules / "pi-open-tui").exists())

    def test_an_invalid_package_json_is_written_again(self):
        store = self.store("a", "1")
        for content in ("not json", "[1]", '{"dependencies": [1]}', ""):
            with self.subTest(content=content):
                self.root.mkdir(parents=True, exist_ok=True)
                (self.root / "package.json").write_text(content)
                (self.root / ".agentix-store").unlink(missing_ok=True)
                result = self.lay_out(store)
                self.assertIn("not a valid package.json", result.stderr)
                self.assertEqual(
                    (self.root / "package.json").read_text(),
                    (store / "package.json").read_text(),
                )
                self.assertTrue((self.modules / "pi-open-tui").is_symlink())


class PiPackageTests(unittest.TestCase):
    """The settings list the packages that the image installs from the lock."""

    def test_settings_lock_and_package_json_agree(self):
        listed = re.findall(r'"npm:([^"]+)"', (MODULES / "pi.nix").read_text())
        package = json.loads((MODULES / "pi-packages/package.json").read_text())
        lock = json.loads((MODULES / "pi-packages/package-lock.json").read_text())
        self.assertEqual(len(listed), 6)
        self.assertEqual(sorted(listed), sorted(package["dependencies"]))
        self.assertEqual(lock["packages"][""]["dependencies"], package["dependencies"])
        # Exact versions, so the lock decides what the image holds.
        for name, version in package["dependencies"].items():
            with self.subTest(name=name):
                self.assertRegex(version, r"^\d+\.\d+\.\d+$")
                self.assertEqual(
                    lock["packages"][f"node_modules/{name}"]["version"], version
                )

    def test_every_locked_package_comes_from_the_registry_with_a_hash(self):
        lock = json.loads((MODULES / "pi-packages/package-lock.json").read_text())
        for name, entry in lock["packages"].items():
            if not name:
                continue
            with self.subTest(name=name):
                self.assertTrue(
                    entry["resolved"].startswith("https://registry.npmjs.org/")
                )
                self.assertTrue(entry["integrity"].startswith("sha512-"))


if __name__ == "__main__":
    unittest.main()
