import os
import unittest
from unittest.mock import patch

import node_controller


class NodeControllerTests(unittest.TestCase):
    def test_changed_bundle_fails_before_ssh(self):
        with patch.object(node_controller.sys, 'argv', ['node_controller.py', 'activate', 'main', 'a' * 32]), \
             patch.dict(os.environ, {'GAMELIFE_ROUTE_BUNDLE_SHA256': '0' * 64}), \
             patch.object(node_controller.subprocess, 'run') as ssh:
            with self.assertRaises(ValueError):
                node_controller.main()
            ssh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
