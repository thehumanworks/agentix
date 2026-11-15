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
def log(mutation):
    with (root / 'mutations').open('a') as f: f.write(mutation + '\\n')
if Path(sys.argv[0]).name == 'ts':
    if state.get('up_fails'):
        raise SystemExit('timeout waiting for Tailscale to enter a Running state')
    if state['BackendState'] == 'NeedsLogin':
        # Enrollment: a new node, which is connected.
        state['Self']['Online'] = True
        state['TailscaleIPs'] = ['fd7a::9', '100.64.0.9']
        log('enroll')
    else:
        log('up')
    state['BackendState'] = 'Running'
    state['enabled'] = True
    if len(args) > 1:
        state['hostname'] = args[1]
        state['Self']['HostName'] = args[1]
        state['Self']['DNSName'] = args[1] + '.test.ts.net.'
elif args[1:] == ['status', '--json']:
    if state.get('pending_hostname'):
        state['pending_reads'] -= 1
        if state['pending_reads'] == 0:
            name = state.pop('pending_hostname')
            state['Self'].update(HostName=name, DNSName=name + '.test.ts.net.')
    print(json.dumps(state))
elif args[1:] == ['logout']:
    state.update(BackendState='NeedsLogin', AuthURL='', up_fails=False)
    state['Self']['Online'] = False
    log('logout')
elif args[1:] == ['debug', 'prefs']:
    print(json.dumps({'RunSSH': state['enabled'], 'Hostname': state['hostname']}))
elif args[1:] == ['set', '--ssh=false']:
    state['enabled'] = False
    log('disable')
elif args[1:3] == ['set', '--ssh=true']:
    state['enabled'] = True
    if len(args) > 3:
        assert len(args) == 4 and args[3].startswith('--hostname=')
        state['hostname'] = args[3].split('=', 1)[1]
        if state.get('pending_reads'):
            state['pending_hostname'] = state['hostname']
        else:
            state['Self']['HostName'] = state['hostname']
            state['Self']['DNSName'] = state['hostname'] + '.test.ts.net.'
    log('enable')
elif args[1] == 'set' and args[2:] and args[2].startswith('--hostname='):
    # A name that another node has gets a suffix, as Tailscale gives it.
    name = args[2].split('=', 1)[1]
    state['hostname'] = state['Self']['HostName'] = name
    label = name + '-1' if name in state.get('taken', []) else name
    state['Self']['DNSName'] = label + '.test.ts.net.'
    log('rename ' + label)
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
            "hostname": "existing",
            "Self": {
                "HostName": "existing",
                "DNSName": "existing.test.ts.net.",
                "Online": True,
            },
            "AuthURL": "",
            "TailscaleIPs": ["fd7a::1", "100.64.0.2"],
        }
        (self.root / "state").write_text(json.dumps(self.state))

    def run_action(self, action, hostname="existing", path=None):
        """Run the script, with the stubs first in PATH, or with PATH `path`."""
        proc = subprocess.run(
            [shutil.which("bash"), str(self.script), action, hostname],
            env={
                **os.environ,
                "TEST_ROOT": str(self.root),
                "PATH": path or f"{self.root}:{os.environ['PATH']}",
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

    def test_running_start_renames_existing_node_without_enrollment(self):
        result = self.run_action("start", "sb-unique")
        self.assertEqual(result["magicdns"], "sb-unique.test.ts.net")
        self.assertEqual(result["ipv4"], "100.64.0.2")
        self.assertEqual(self.run_action("start", "sb-unique"), result)
        self.assertEqual((self.root / "mutations").read_text(), "enable\n")

    def test_first_start_passes_hostname_to_helper(self):
        self.state["BackendState"] = "NeedsLogin"
        (self.root / "state").write_text(json.dumps(self.state))
        result = self.run_action("start", "sb-new")
        self.assertEqual(result["magicdns"], "sb-new.test.ts.net")
        self.assertEqual((self.root / "mutations").read_text(), "enroll\n")

    def write_state(self, **changes):
        self.state.update(changes)
        (self.root / "state").write_text(json.dumps(self.state))

    def test_restored_node_that_the_tailnet_removed_enrolls_again(self):
        # tailscaled reports Running from the snapshot's state, but the node
        # is offline and the control server asks it to log in.
        self.state["Self"]["Online"] = False
        self.write_state(AuthURL="https://login.example/a/1")
        result = self.run_action("start")
        self.assertEqual(
            result,
            {
                "status": "running",
                "magicdns": "existing.test.ts.net",
                "ipv4": "100.64.0.9",
            },
        )
        self.assertEqual((self.root / "mutations").read_text(), "logout\nenroll\n")

    def test_failed_start_that_asks_for_a_login_enrolls_again(self):
        # ts read the state before the control server answered, and its
        # `tailscale up` waited for an interactive login until it timed out.
        self.state["Self"]["Online"] = False
        self.write_state(
            BackendState="Starting",
            up_fails=True,
            AuthURL="https://login.example/a/1",
        )
        result = self.run_action("start", "dev")
        self.assertEqual(result["magicdns"], "dev.test.ts.net")
        self.assertEqual((self.root / "mutations").read_text(), "logout\nenroll\n")

    def test_other_failures_are_reported_without_logging_out(self):
        # Without a login request, a failed start is not a removed node.
        self.write_state(BackendState="NeedsLogin", up_fails=True)
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_action("start")
        self.assertIn("timeout waiting", error.exception.stderr)
        self.assertFalse((self.root / "mutations").exists())

    def test_node_that_never_connects_is_not_reported_as_running(self):
        self.script.write_text(self.script.read_text().replace("sleep 0.25", ":"))
        self.state["Self"]["Online"] = False
        self.write_state()
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_action("start")
        self.assertEqual(error.exception.stdout, "")
        self.assertIn("did not connect", error.exception.stderr)
        self.assertFalse((self.root / "mutations").exists())

    def test_rename_waits_for_control_plane_before_returning_address(self):
        self.state["pending_reads"] = 3
        (self.root / "state").write_text(json.dumps(self.state))
        result = self.run_action("start", "sb-renamed")
        self.assertEqual(result["magicdns"], "sb-renamed.test.ts.net")
        self.assertEqual((self.root / "mutations").read_text(), "enable\n")

    def test_suffixed_name_is_taken_back_once_it_is_free(self):
        # Named dev-1 while an earlier node had dev, which is gone now.
        self.state["Self"].update(HostName="dev", DNSName="dev-1.test.ts.net.")
        self.write_state(hostname="dev")
        self.assertEqual(self.run_action("start", "dev")["magicdns"], "dev.test.ts.net")
        self.assertEqual(
            (self.root / "mutations").read_text(), "rename dev-rename\nrename dev\n"
        )

    def test_suffixed_name_stays_while_another_node_has_the_name(self):
        self.state["Self"].update(HostName="dev", DNSName="dev-1.test.ts.net.")
        self.write_state(hostname="dev", taken=["dev"])
        self.assertEqual(
            self.run_action("start", "dev")["magicdns"], "dev-1.test.ts.net"
        )

    def test_other_names_are_not_renamed(self):
        self.state["Self"].update(HostName="dev", DNSName="dev-box.test.ts.net.")
        self.write_state(hostname="dev")
        self.run_action("start", "dev")
        self.assertFalse((self.root / "mutations").exists())

    def test_unconfirmed_rename_fails_instead_of_returning_stale_address(self):
        self.state["pending_reads"] = 100
        (self.root / "state").write_text(json.dumps(self.state))
        self.script.write_text(self.script.read_text().replace("sleep 0.2", ":"))
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_action("start", "sb-renamed")
        self.assertEqual(error.exception.stdout, "")
        self.assertIn("has not confirmed hostname", error.exception.stderr)

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

    def test_image_without_tailscale_is_named_as_the_cause(self):
        (self.root / "ts").unlink()
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_action("start", path=str(self.root))
        self.assertEqual(error.exception.stdout, "")
        self.assertIn("agentix.sandbox.tailscale is off", error.exception.stderr)
        self.assertFalse((self.root / "mutations").exists())

    def test_disconnected_network_is_not_reported_as_running_ssh(self):
        self.state["BackendState"] = "Stopped"
        (self.root / "state").write_text(json.dumps(self.state))
        self.assertEqual(self.run_action("status")["status"], "stopped")
        self.assertEqual(self.run_action("start")["status"], "running")


if __name__ == "__main__":
    unittest.main()
