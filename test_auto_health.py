import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import unittest
import auto_health as h

P = h.DEFAULTS

def history(results, start=100):
    return [{'time': dt.datetime.fromtimestamp(start + i, dt.timezone.utc).isoformat(),
             'delay': 120 if ok else 0} for i, ok in enumerate(results)]

class HealthRulesTest(unittest.TestCase):
    def test_yaml_preserves_ipv6_and_colon_literals(self):
        text = "lan-allowed-ips:\n  - 0.0.0.0/0\n  - ::/0\nlabel: :literal\n"
        result = h.yaml_load(text)
        self.assertEqual(result['lan-allowed-ips'][1], '::/0')
        self.assertEqual(result['label'], ':literal')
        self.assertEqual(h.yaml_load(h.yaml_dump(result)), result)

    def test_three_consecutive_failures_isolate(self):
        entry = {}
        self.assertTrue(h.observe(entry, history([False] * 3), P, 120))
        self.assertTrue(entry['quarantined'])

    def test_mixed_failures_need_enough_samples(self):
        entry = {}
        self.assertFalse(h.observe(entry, history([False, True, False, True, False, True, True]), P, 120))
        self.assertTrue(h.observe(entry, history([True], 107), P, 121))

    def test_duplicate_samples_and_old_history_ignored(self):
        entry = {}
        samples = history([True, False, True])
        h.observe(entry, samples, P, 120)
        h.observe(entry, samples, P, 121)
        self.assertEqual(len(entry['samples']), 3)
        entry.update(quarantined=True, recovery_streak=2)
        self.assertTrue(h.recover(entry, True, P, 200))
        self.assertFalse(h.observe(entry, history([False] * 10), P, 201))
        self.assertEqual(entry['samples'], [])

    def test_recovery_requires_three_consecutive_successes(self):
        entry = {'quarantined': True}
        self.assertFalse(h.recover(entry, True, P, 100))
        self.assertFalse(h.recover(entry, False, P, 220))
        self.assertEqual(entry['recovery_streak'], 0)
        self.assertFalse(h.recover(entry, True, P, 340))
        self.assertFalse(h.recover(entry, True, P, 460))
        self.assertTrue(h.recover(entry, True, P, 580))
        self.assertFalse(entry['quarantined'])
        self.assertEqual(entry['next_probe'], 700)

    def test_recovered_node_is_quarantined_if_it_fails_again(self):
        entry = {'quarantined': True, 'recovery_streak': 2}
        self.assertTrue(h.recover(entry, True, P, 200))
        self.assertFalse(entry['quarantined'])
        self.assertTrue(h.observe(entry, history([False, False, False], 201), P, 205))
        self.assertTrue(entry['quarantined'])
        self.assertEqual(entry['recovery_streak'], 0)

    def test_previously_healthy_new_node_has_no_permanent_allowance(self):
        entry = {}
        self.assertFalse(h.observe(entry, history([True] * 8), P, 120))
        self.assertTrue(h.observe(entry, history([False] * 3, 121), P, 125))
        self.assertTrue(entry['quarantined'])

    def test_generated_snapshot_is_a_current_blocklist(self):
        code = h.render_override(['currently-failing-node'], P)
        self.assertIn('AUTO_HEALTH_CURRENT_QUARANTINE', code)
        self.assertIn('generatedAt', code)
        self.assertIn('禁止进入自动组', code)
        config = self.fixture()
        config['proxies'].append({'name': 'currently-failing-node', 'type': 'ss'})
        isolated = h.transform(config, ['currently-failing-node'], P)
        self.assertIn('currently-failing-node', isolated['proxy-groups'][0]['exclude-filter'])
        recovered = h.transform(isolated, [], P)
        self.assertNotIn('currently-failing-node', recovered['proxy-groups'][0]['exclude-filter'])

    def fixture(self):
        return {'proxies': [{'name': 'node (v6) | [A]+', 'type': 'ss'}, {'name': '日本'}],
                'proxy-groups': [
                    {'name': '自动选择', 'type': 'url-test', 'include-all': True,
                     'tolerance': 0, 'lazy': True, 'exclude-filter': '(?i)IPLC|住宅'},
                    {'name': '手选', 'type': 'select', 'include-all': True}],
                'rules': ['MATCH,自动选择'], 'dns': {'enable': True}}

    def test_update_reapplies_rules_without_changing_manual_routes(self):
        original = self.fixture()
        updated = h.transform(original, ['node (v6) | [A]+'], P)
        self.assertEqual(updated['proxy-groups'][0]['tolerance'], 80)
        self.assertFalse(updated['proxy-groups'][0]['lazy'])
        self.assertEqual(updated['proxy-groups'][0]['empty-fallback'], 'REJECT')
        self.assertEqual(updated['rules'], original['rules'])
        self.assertEqual(updated['dns'], original['dns'])
        self.assertEqual(updated['proxies'], original['proxies'])
        self.assertEqual(updated['proxy-groups'][1], original['proxy-groups'][1])
        self.assertEqual(updated, h.transform(updated, ['node (v6) | [A]+'], P))

    def test_recovery_removes_only_managed_exclusion(self):
        original = self.fixture()
        isolated = h.transform(original, ['node (v6) | [A]+'], P)
        recovered = h.transform(isolated, [], P)
        self.assertEqual(recovered['proxy-groups'][0]['exclude-filter'], '(?i)IPLC|住宅')

    def test_no_base_filter_does_not_match_everything(self):
        config = self.fixture()
        del config['proxy-groups'][0]['exclude-filter']
        changed = h.transform(config, ['日本'], P)
        self.assertTrue(changed['proxy-groups'][0]['exclude-filter'].startswith('a^|'))
        self.assertNotIn('exclude-filter', h.transform(changed, [], P)['proxy-groups'][0])

    def test_javascript_override_matches_python_and_is_idempotent(self):
        cfg = self.fixture()
        code = h.render_override(['node (v6) | [A]+', 'missing-node'], P)
        runner = "const vm=require('node:vm');const x=JSON.parse(require('node:fs').readFileSync(0,'utf8'));const c=vm.createContext({});vm.runInContext(x.code,c);c.cfg=x.config;let once=vm.runInContext('main(cfg)',c);let twice=vm.runInContext('main(cfg)',c);process.stdout.write(JSON.stringify({once,twice}));"
        result = json.loads(subprocess.check_output([h.node_binary(), '-e', runner],
             input=json.dumps({'code': code, 'config': cfg}), text=True))
        expected = h.transform(cfg, ['node (v6) | [A]+', 'missing-node'], P)
        self.assertEqual(result['once'], expected)
        self.assertEqual(result['twice'], expected)

    def test_group_patch_preserves_all_other_sections(self):
        cfg = self.fixture()
        text = h.yaml_dump(cfg)
        updated = h.transform(cfg, ['日本'], P)
        changed = h.patch_group_block(text, updated['proxy-groups'])
        self.assertEqual(h.yaml_load(changed), updated)

if __name__ == '__main__':
    unittest.main()
