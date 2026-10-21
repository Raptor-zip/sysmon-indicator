import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'collector', Path(__file__).resolve().parents[1] / 'bin/sysmon-collect.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class ProcessPathTests(unittest.TestCase):
    def test_live_process_in_unicode_directory(self):
        with tempfile.TemporaryDirectory(prefix='作業 スペース-') as directory:
            proc = subprocess.Popen([sys.executable, '-c',
                                     'import time; time.sleep(60)'], cwd=directory)
            try:
                self.assertEqual(collector.read_proc_cwd(proc.pid), directory)
            finally:
                proc.terminate()
                proc.wait()
            self.assertIsNone(collector.read_proc_cwd(proc.pid))

    def test_deleted_directory(self):
        directory = tempfile.mkdtemp()
        proc = subprocess.Popen([sys.executable, '-c',
                                 'import time; time.sleep(60)'], cwd=directory)
        try:
            os.rmdir(directory)
            self.assertIsNone(collector.read_proc_cwd(proc.pid))
        finally:
            proc.terminate()
            proc.wait()
            if os.path.exists(directory):
                os.rmdir(directory)

    def test_permission_denied(self):
        with patch.object(collector.os, 'readlink', side_effect=PermissionError):
            self.assertIsNone(collector.read_proc_cwd(123))

    def test_cpu_and_memory_paths_only_read_for_unique_top_processes(self):
        with patch.object(collector, 'read_proc_cwd', return_value='/workspace') as read:
            result = collector.ProcSampler().sample(1)
        rows = result['by_cpu'] + result['by_mem']
        self.assertTrue(rows)
        self.assertTrue(all(row['cwd'] == '/workspace' for row in rows))
        self.assertEqual(read.call_count, len({row['pid'] for row in rows}))

    def test_gpu_paths_only_read_for_top_processes(self):
        gpu = collector.GpuSampler()
        gpu._smi = 'nvidia-smi'
        output = '# gpu pid sm fb command\n' + '\n'.join(
            '0 %d %d 10 python3' % (100 + n, n) for n in range(10))
        with patch.object(collector.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, output)), \
                patch.object(collector, 'read_text', return_value=None), \
                patch.object(collector, 'read_proc_cwd', return_value='/workspace') as read:
            rows = gpu._processes()
        self.assertEqual(len(rows), collector.TOP_N)
        self.assertEqual(read.call_count, collector.TOP_N)
        self.assertTrue(all(row['cwd'] == '/workspace' for row in rows))


if __name__ == '__main__':
    unittest.main()
