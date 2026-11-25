import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('firewall_install', Path(__file__).with_name('install.py'))
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class GatewaySubnetTests(unittest.TestCase):
    def inspect(self, networks, subnets):
        def fake_run(command, *, output=False):
            if command[:2] == ['docker', 'inspect']:
                return json.dumps([{'NetworkSettings': {'Networks': networks}}])
            if command[:3] == ['docker', 'network', 'inspect']:
                return json.dumps([{'IPAM': {'Config': [{'Subnet': value} for value in subnets]}}])
            raise AssertionError('unexpected command')
        with patch.object(install, 'run', side_effect=fake_run):
            return install.portal_subnet()

    def test_gateway_subnet_is_restricted_to_its_private_network(self):
        self.assertEqual(self.inspect({'gateway': {'IPAddress': '172.20.0.7'}},
                                      ['172.20.0.0/24']), '172.20.0.0/24')

    def test_ambiguous_or_public_gateway_subnets_are_rejected(self):
        for networks, subnets in [
            ({'a': {'IPAddress': '172.20.0.7'}, 'b': {'IPAddress': '172.21.0.7'}}, ['172.20.0.0/24']),
            ({'a': {'IPAddress': '8.8.8.8'}}, ['8.8.8.0/24']),
            ({'a': {'IPAddress': '172.20.0.7'}}, ['172.16.0.0/12']),
            ({'a': {'IPAddress': '172.20.0.7'}}, ['172.21.0.0/24']),
        ]:
            with self.subTest(networks=networks, subnets=subnets), self.assertRaises(RuntimeError):
                self.inspect(networks, subnets)


if __name__ == '__main__':
    unittest.main()
