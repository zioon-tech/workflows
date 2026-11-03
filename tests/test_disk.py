import collections, importlib.util, pathlib, unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "disk", pathlib.Path(__file__).parent.parent / "scripts" / "disk.py")
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)
Usage = collections.namedtuple("Usage", "total used free")

class DiskTests(unittest.TestCase):
    def test_enough_space_does_not_run_diagnostics(self):
        with patch.object(disk.shutil, "disk_usage", return_value=Usage(2000, 1000, 1000)), \
                patch.object(disk, "disk_command") as command:
            disk.require_disk_space("/release", 1000)
        command.assert_not_called()

    def test_blocked_release_reports_exact_deficit_without_mutating_files(self):
        with patch.object(disk.shutil, "disk_usage", return_value=Usage(2000, 1900, 100)), \
                patch.object(disk, "disk_command", return_value={"output": "", "exitCode": 0}), \
                patch.object(disk.pathlib.Path, "glob", return_value=[]), \
                patch.object(disk.pathlib.Path, "is_dir", return_value=False), \
                patch("builtins.print") as output:
            with self.assertRaisesRegex(RuntimeError, "required 600 bytes, available 100"):
                disk.require_disk_space("/release", 600)
        import json
        report = json.loads(output.call_args[0][0])
        self.assertEqual(report["deficitBytes"], 500)
        self.assertEqual(report["freeBytes"], 100)

    def test_sizes_preserve_partial_output_and_rank_allocated_bytes(self):
        with patch.object(disk, "disk_command", return_value=dict(
                output="10\t/small\n400\t/large\n20\t/middle\n",
                error="unreadable directory", exitCode=1)):
            result = disk.disk_sizes("/", 2, 2)
        self.assertEqual([entry["path"] for entry in result["largest"]], ["/large", "/middle"])
        self.assertEqual(result["exitCode"], 1)
