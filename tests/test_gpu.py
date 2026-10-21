import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'collector', Path(__file__).resolve().parents[1] / 'bin/sysmon-collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class GpuTests(unittest.TestCase):
    def setUp(self):
        self.gpu = collector.GpuSampler()
        self.gpu._smi = '/usr/bin/nvidia-smi'
        self.gpu._dev = '/gpu'

    def processes(self, text, code=0):
        with patch.object(collector.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], code, text)), \
                patch.object(collector, 'read_text', return_value=None):
            return self.gpu._processes()

    def test_graphics_compute_and_unavailable_metrics(self):
        rows = self.processes('''# gpu pid type sm mem enc dec jpg ofa fb ccpm command
# Idx # C/G % % % % % % MB MB name
0 42 G - - - - - - 647 0 chrome --type=g
0 43 C 80 2 - - - - 1024 0 python3
0 44 G 0 0 - - - - - - Xorg
''')
        self.assertEqual([r['pid'] for r in rows], [43, 42, 44])
        self.assertEqual(rows[0]['gpu'], 80)
        self.assertEqual(rows[1]['vram'], 647 * 1024 ** 2)
        self.assertIsNone(rows[1]['gpu'])
        self.assertIsNone(rows[2]['vram'])

    def test_older_driver_columns_and_no_processes(self):
        rows = self.processes('# gpu pid type sm mem enc dec fb command\n'
                              '0 42 G 10 0 - - 32 Xorg\n')
        self.assertEqual(rows[0]['vram'], 32 * 1024 ** 2)
        self.assertEqual(self.processes('# gpu pid type sm fb command\n'
                                        '0 - - - - -\n'), [])

    def test_unavailable_is_different_from_empty(self):
        self.assertIsNone(self.processes('Not supported', 1))
        self.assertIsNone(self.processes('Not supported'))
        with patch.object(collector.subprocess, 'run', side_effect=
                          subprocess.TimeoutExpired('nvidia-smi', 4)):
            self.assertIsNone(self.gpu._processes())

    def test_sleeping_gpu_is_never_queried(self):
        with patch.object(collector, 'read_text', return_value='suspended\n'), \
                patch.object(collector.subprocess, 'run') as run:
            self.assertEqual(self.gpu.sample()['state'], 'suspended')
            run.assert_not_called()

    def test_zero_vram_is_zero_percent(self):
        with patch.object(collector, 'read_text', return_value='active'), \
                patch.object(collector.subprocess, 'run', return_value=
                             subprocess.CompletedProcess([], 0, 'NVIDIA GPU, 0, 0, 1024, 45, 8')), \
                patch.object(self.gpu, '_processes', return_value=[]):
            self.assertEqual(self.gpu.sample()['mem_pct'], 0)


if __name__ == '__main__':
    unittest.main()
