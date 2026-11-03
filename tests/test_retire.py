import importlib.util, pathlib, tempfile, unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "retire", pathlib.Path(__file__).parent.parent / "scripts" / "retire.py")
retire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire)

class RetirementTests(unittest.TestCase):
    def test_dependencies_require_an_owning_package_and_keep_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            directory = str(root)
            dependencies = root / "node_modules"
            dependencies.mkdir()
            source = root / "source.ts"
            source.write_text("preserved")
            with self.assertRaisesRegex(RuntimeError, "owning package manifest"):
                retire.retired_dependencies(directory)
            (root / "package.json").write_text("{}")
            self.assertEqual(retire.retired_dependencies(directory), [str(dependencies)])
            self.assertEqual(source.read_text(), "preserved")

    def test_build_or_dependency_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                retire.retired_dependencies(str(link))
            (target / "package.json").write_text("{}")
            (target / "node_modules").symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                retire.retired_dependencies(str(target))

    def test_reference_matching_respects_path_boundaries(self):
        self.assertTrue(retire.inside("/old/release/src/main.ts", "/old/release"))
        self.assertFalse(retire.inside("/old/release-next/src/main.ts", "/old/release"))

    def test_new_reference_aborts_before_any_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            old = root / "old"
            old.mkdir()
            (old / "package.json").write_text("{}")
            (old / "node_modules").mkdir()
            builds = (str(old),)
            with patch.object(retire, "RETIRED_BUILDS", builds), \
                    patch.object(retire.pathlib.Path, "glob", return_value=[]), \
                    patch.object(retire, "disk_sizes", create=True, return_value={}), \
                    patch.object(retire, "disk_command", create=True, return_value={}), \
                    patch.object(retire, "build_references",
                                 side_effect=[{str(old): []}, {str(old): ["new live process"]}]), \
                    patch.object(retire.subprocess, "check_output", return_value="4096\told"), \
                    patch.object(retire.shutil, "rmtree") as remove, patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "references changed"):
                    retire.retire_inactive_builds(str(root))
            remove.assert_not_called()
            self.assertTrue((old / "node_modules").is_dir())
