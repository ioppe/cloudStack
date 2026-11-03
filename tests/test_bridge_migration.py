"""Check management-network migration preserves the original L3 configuration."""
import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'deploy/prepare-node150-bridge.py'
spec = importlib.util.spec_from_file_location('bridge_migration', str(path))
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class BridgeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.original = {'network': {'version': 2, 'ethernets': {'eno2np1': {'dhcp4': False, 'addresses': ['192.168.0.150/24'], 'routes': [{'to': 'default', 'via': '192.168.0.1'}], 'nameservers': {'addresses': ['192.168.0.1', '8.8.8.8']}}}}}

    def test_preserves_address_gateway_dns_and_mac_on_real_bridge(self):
        before = copy.deepcopy(self.original)
        result = migration.bridge_document(self.original)
        bridge = result['network']['bridges']['br0']
        self.assertEqual(bridge['interfaces'], ['eno2np1'])
        for key in ('addresses', 'routes', 'nameservers'):
            self.assertEqual(bridge[key], self.original['network']['ethernets']['eno2np1'][key])
            self.assertNotIn(key, result['network']['ethernets']['eno2np1'])
        self.assertEqual(bridge['macaddress'], 'ac:1f:6b:eb:a7:b1')
        self.assertEqual(self.original, before)
        self.assertNotIn('virbr0', result['network'].get('bridges', {}))

    def test_rejects_different_management_address(self):
        self.original['network']['ethernets']['eno2np1']['addresses'] = ['192.168.0.151/24']
        with self.assertRaises(AssertionError):
            migration.bridge_document(self.original)

    def test_rejects_existing_bridge(self):
        self.original['network']['bridges'] = {'br0': {'interfaces': []}}
        with self.assertRaises(AssertionError):
            migration.bridge_document(self.original)

    def test_rejects_different_gateway_or_dns(self):
        for key, value in [('routes', [{'to': 'default', 'via': '192.168.0.254'}]), ('nameservers', {'addresses': ['1.1.1.1']})]:
            changed = copy.deepcopy(self.original)
            changed['network']['ethernets']['eno2np1'][key] = value
            with self.assertRaises(AssertionError):
                migration.bridge_document(changed)


if __name__ == '__main__':
    unittest.main()
