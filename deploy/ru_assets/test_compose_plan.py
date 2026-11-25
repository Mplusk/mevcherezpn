import os
from pathlib import Path
import tempfile
import unittest

from compose_plan import compose_args, recreate_panel_command, validate_command


class ComposePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / 'release'
        self.override = self.release / 'deploy/ru_assets/compose.ru-assets.yaml'
        self.override.parent.mkdir(parents=True)
        self.override.write_text('services: {}')
        (self.release / 'compose.panel.yaml').write_text('services: {}')
        self.env = self.root / '.env'
        self.env.write_text('secret=dummy')

    def tearDown(self):
        self.temp.cleanup()

    def test_each_command_includes_the_extension(self):
        for command in (validate_command(self.release, self.env), recreate_panel_command(self.release, self.env)):
            self.assertIn(str(self.override), command)
            self.assertEqual(command.count('-f'), 2)
            self.assertNotIn('secret=dummy', ' '.join(command))

    def test_missing_extension_refuses_to_build_command(self):
        self.override.unlink()
        with self.assertRaises(ValueError):
            compose_args(self.release, self.env)

    def test_rejects_relative_inputs(self):
        with self.assertRaises(ValueError):
            compose_args(Path('release'), self.env)


if __name__ == '__main__':
    unittest.main()
