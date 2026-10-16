import copy
import http.server
import json
import ntpath
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
from unittest import mock

import auto_health as h
import install_windows as install


class InstallMetadataTest(unittest.TestCase):
    def fixture(self):
        profile = {'current': 'new-pc-profile', 'items': [
            {'id': 'other-profile', 'override': ['unrelated']},
            {'id': 'new-pc-profile', 'override': ['custom-routing'], 'url': 'redacted'}]}
        overrides = {'items': [{'id': 'custom-routing', 'type': 'local', 'ext': 'js', 'global': False}]}
        runtime = {'proxies': [{'name': 'Test node'}], 'proxy-groups': [{'name': 'Auto', 'type': 'url-test'}]}
        return profile, overrides, runtime

    def test_fresh_machine_binds_its_profile_and_starts_empty(self):
        p, o, r = self.fixture()
        original = copy.deepcopy(p)
        profile, overrides, policy, state, blocked = install.prepare_metadata(p, o, r)
        self.assertEqual(policy['profile_id'], 'new-pc-profile')
        self.assertEqual(profile['items'][0], original['items'][0])
        self.assertEqual(profile['items'][1]['override'], ['custom-routing', h.OVERRIDE_ID])
        self.assertEqual(state['nodes'], {})
        self.assertEqual(blocked, set())
        self.assertEqual(p, original)
        self.assertEqual(overrides['items'][0], o['items'][0])

    def test_reinstall_retains_policy_state_and_single_binding(self):
        p, o, r = self.fixture()
        p['items'][1]['override'].append(h.OVERRIDE_ID)
        old = {'profile_id': p['current'], 'timeout_ms': 4500}
        state = {'profile_id': p['current'], 'nodes': {'Test node': {'quarantined': True, 'recovery_streak': 2}}}
        p2, _, policy, state2, blocked = install.prepare_metadata(p, o, r, old, state)
        self.assertEqual(p2['items'][1]['override'].count(h.OVERRIDE_ID), 1)
        self.assertEqual(policy['timeout_ms'], 4500)
        self.assertEqual(state, state2)
        self.assertEqual(blocked, {'Test node'})

    def test_foreign_profile_state_and_policy_are_refused(self):
        p, o, r = self.fixture()
        with self.assertRaises(RuntimeError):
            install.prepare_metadata(p, o, r, {'profile_id': 'old-computer'})
        with self.assertRaises(RuntimeError):
            install.prepare_metadata(p, o, r, old_state={'profile_id': 'old-computer'})

    def test_provider_only_or_manual_only_profiles_are_refused(self):
        p, o, r = self.fixture()
        with self.assertRaises(RuntimeError):
            install.prepare_metadata(p, o, dict(r, proxies=[]))
        with self.assertRaises(RuntimeError):
            install.prepare_metadata(p, o, dict(r, **{'proxy-groups': [{'type': 'select'}]}))

    def test_task_failure_restores_exact_bytes_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            original = root / 'profile.yaml'
            created = root / 'new.json'
            old_bytes = b'\xef\xbb\xbfhello: world\r\n'
            original.write_bytes(old_bytes)
            calls = []
            def task(action, name, **kwargs):
                calls.append(action)
                if action == 'Register':
                    raise RuntimeError('registration denied')
                return ''
            with mock.patch.object(install, 'run_task', side_effect=task):
                with self.assertRaisesRegex(RuntimeError, 'rolled back'):
                    install.transact({original: 'changed', created: '{}'}, 'fixture', {}, root / 'backups')
            self.assertEqual(original.read_bytes(), old_bytes)
            self.assertFalse(created.exists())
            self.assertEqual(calls, ['Export', 'Register', 'Remove'])


class WindowsControllerTest(unittest.TestCase):
    def test_matches_only_its_own_data_directory(self):
        args = [r'C:\Apps\Clash Party\mihomo.exe', '-d', r'C:\Users\A\AppData\Roaming\mihomo-party\work',
                '-ext-ctl-pipe', r'\\.\pipe\MihomoParty\mihomo-user-Console-123']
        self.assertEqual(h.controller_from_args(args, r'c:\users\a\AppData\Roaming\mihomo-party\work', ntpath.normcase), args[-1])
        self.assertIsNone(h.controller_from_args(args, r'C:\Users\B\AppData\Roaming\mihomo-party\work', ntpath.normcase))
        self.assertIsNone(h.controller_from_args(['-d'], 'missing'))

    def test_process_lock_excludes_competing_cycle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(h, 'HOME_DIR', Path(d)):
            with h.process_lock() as first:
                self.assertTrue(first)
                with h.process_lock() as second:
                    self.assertFalse(second)
            with h.process_lock() as third:
                self.assertTrue(third)


@unittest.skipIf(h.IS_WINDOWS, 'UNIX fixture tests the same Node socketPath HTTP client on macOS/Linux')
class NodeIpcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='ah-', dir='/tmp')
        cls.path = str(Path(cls.tmp.name) / 'core.sock')
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == '/timeout':
                    time.sleep(0.5)
                    return
                if self.path == '/missing':
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                payload = json.dumps({'version': 'fixture', 'name': '节点'}, ensure_ascii=False).encode()
                try:
                    for chunk in [payload[:9], payload[9:]]:
                        self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                    self.wfile.write(b'0\r\n\r\n')
                except BrokenPipeError:
                    pass
            def do_PUT(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                result = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(result)))
                self.end_headers()
                self.wfile.write(result)
        cls.server = socketserver.ThreadingUnixStreamServer(cls.path, Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def test_chunked_utf8_response(self):
        self.assertEqual(h.node_ipc_api(self.path, '/version')['name'], '节点')

    def test_request_body_unicode_and_path(self):
        body = {'path': 'C:\\Users\\用户\\config.yaml'}
        self.assertEqual(h.node_ipc_api(self.path, '/configs', 'PUT', body), body)

    def test_http_error_propagates(self):
        with self.assertRaisesRegex(RuntimeError, '404'):
            h.node_ipc_api(self.path, '/missing')

    def test_timeout_is_bounded(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'timeout'):
            h.node_ipc_api(self.path, '/timeout', timeout=0.1)
        self.assertLess(time.monotonic() - start, 2)


if __name__ == '__main__':
    unittest.main()
