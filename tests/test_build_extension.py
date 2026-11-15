import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'extension/sysmon@raptor-zip.github.io'

spec = importlib.util.spec_from_file_location(
    'build_extension', ROOT / 'bin/build-extension.py')
build_extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_extension)


class BuildExtensionTests(unittest.TestCase):
    def build(self, version):
        directory = Path(tempfile.mkdtemp())
        build_extension.build(SOURCE, directory, version)
        script = (directory / 'extension.js').read_text()
        metadata = json.loads((directory / 'metadata.json').read_text())
        self.assertTrue((directory / 'stylesheet.css').exists())
        return script, metadata

    def test_legacy_build_is_source_copy(self):
        script, metadata = self.build(42)
        self.assertEqual(script, (SOURCE / 'extension.js').read_text())
        self.assertEqual(metadata['shell-version'], ['42'])

    def test_esm_build(self):
        script, metadata = self.build(46)
        self.assertEqual(metadata['shell-version'], ['45', '46'])
        self.assertIn('export default class SysmonExtension extends Extension', script)
        self.assertNotIn('imports.ui', script)
        self.assertNotIn('imports.misc', script)
        self.assertNotIn('function init(', script)
        self.assertIn('const SysmonIndicator = GObject.registerClass', script)

    def test_unsupported_version(self):
        with self.assertRaises(ValueError):
            build_extension.build(SOURCE, Path(tempfile.mkdtemp()), 44)


if __name__ == '__main__':
    unittest.main()
