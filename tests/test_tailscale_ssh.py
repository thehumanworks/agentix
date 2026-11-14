import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentix.ssh import _SCRIPTS


@unittest.skipUnless(shutil.which("jq"), "jq is required for the sandbox script tests")
class TailscaleScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / "socket"))
        self.addCleanup(self.sock.close)
        self.script = self.root / "ssh.sh"
        self.script.write_text(
            (_SCRIPTS / "tailscale-ssh.sh")
            .read_text()
            .replace(
                "/run/agentix-tailscale/tailscaled.sock", str(self.root / "socket")
            )
        )
        stub = """import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
state = json.loads((root / 'state').read_text())
args = sys.argv[1:]
if Path(sys.argv[0]).name == 'ts':
    state['BackendState'] = 'Running'
    state['enabled'] = True
    with (root / 'mutations').open('a') as f: f.write('up\\n')
elif args[1:] == ['status', '--json']:
    print(json.dumps(state))
elif args[1:] == ['debug', 'prefs']:
    print(json.dumps({'RunSSH': state['enabled']}))
elif args[1:] == ['set', '--ssh=false']:
    state['enabled'] = False
    with (root / 'mutations').open('a') as f: f.write('disable\\n')
elif args[1:] == ['set', '--ssh=true']:
    state['enabled'] = True
    with (root / 'mutations').open('a') as f: f.write('enable\\n')
else:
    raise SystemExit('unexpected arguments')
(root / 'state').write_text(json.dumps(state))
"""
        for name in ("tailscale", "ts"):
            path = self.root / name
            path.write_text(f"#!{sys.executable}\n" + stub)
            path.chmod(0o755)
        self.state = {
            "BackendState": "Running",
            "enabled": True,
            "Self": {"DNSName": "existing.test.ts.net."},
            "TailscaleIPs": ["fd7a::1", "100.64.0.2"],
        }
        (self.root / "state").write_text(json.dumps(self.state))

    def run_action(self, action):
        proc = subprocess.run(
            ["bash", str(self.script), action, "ignored-on-repeat"],
            env={
                **os.environ,
                "TEST_ROOT": str(self.root),
                "PATH": f"{self.root}:{os.environ['PATH']}",
            },
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_running_start_only_reads_addresses(self):
        result = self.run_action("start")
        self.assertEqual(
            result,
            {
                "status": "running",
                "magicdns": "existing.test.ts.net",
                "ipv4": "100.64.0.2",
            },
        )
        self.assertFalse((self.root / "mutations").exists())

    def test_stop_is_idempotent_and_restart_preserves_network_identity(self):
        self.assertEqual(self.run_action("stop")["status"], "stopped")
        self.run_action("stop")
        state = json.loads((self.root / "state").read_text())
        self.assertEqual(state["BackendState"], "Running")
        self.assertEqual(state["Self"], self.state["Self"])
        self.assertEqual(self.run_action("start")["status"], "running")
        self.assertEqual((self.root / "mutations").read_text(), "disable\nenable\n")

    def test_unstarted_status_and_stop_are_read_only(self):
        (self.root / "socket").unlink()
        for action in ("status", "stop"):
            self.assertEqual(
                self.run_action(action),
                {"status": "stopped", "magicdns": "", "ipv4": ""},
            )
        self.assertFalse((self.root / "mutations").exists())

    def test_disconnected_network_is_not_reported_as_running_ssh(self):
        self.state["BackendState"] = "Stopped"
        (self.root / "state").write_text(json.dumps(self.state))
        self.assertEqual(self.run_action("status")["status"], "stopped")
        self.assertEqual(self.run_action("start")["status"], "running")


if __name__ == "__main__":
    unittest.main()
