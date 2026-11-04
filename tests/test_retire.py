import importlib.util, os, pathlib, tempfile, unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "retire", pathlib.Path(__file__).parent.parent / "scripts" / "retire.py")
retire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire)

class RetirementTests(unittest.TestCase):
    def test_shared_links_release_space_only_when_the_last_copy_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            first, second, external = root / "first", root / "second", root / "external"
            for path in (first, second, external):
                path.mkdir()
            file = first / "dependency"
            file.write_bytes(b"dependency" * 4096)
            os.link(str(file), str(second / "dependency"))
            os.link(str(file), str(external / "dependency"))
            (first / "symbolic").symlink_to(external / "dependency")
            allocated = file.stat().st_blocks * 512
            usage = retire.dependency_file_usage([str(first), str(second)])
            self.assertEqual(usage, dict(reclaimableFileBytes=0, sharedFileBytes=allocated))
            usage = retire.dependency_file_usage([str(first), str(second), str(external)])
            self.assertEqual(usage, dict(reclaimableFileBytes=allocated, sharedFileBytes=0))

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
                    patch.object(retire, "RETIRED_STORES", ()), \
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

    def test_cache_accepts_only_sha512_shards_at_the_inventoried_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            shard = root / "ab"
            shard.mkdir()
            for suffix in ("", "-exec", "-index.json"):
                (shard / ("c" * 126 + suffix)).write_text("cached")
            with patch.object(retire, "RETIRED_STORES", (str(root),)):
                self.assertEqual(retire.retired_store(str(root)), [str(root)])
                with self.assertRaisesRegex(RuntimeError, "canonical inventoried"):
                    retire.retired_store(str(shard))
                (shard / "user-data.json").write_text("preserve")
                with self.assertRaisesRegex(RuntimeError, "unexpected content"):
                    retire.retired_store(str(root))

    def test_cache_symlinks_and_nested_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            shard = root / "ab"
            shard.mkdir()
            candidate = shard / ("c" * 126)
            candidate.symlink_to(root, target_is_directory=True)
            with patch.object(retire, "RETIRED_STORES", (str(root),)):
                with self.assertRaisesRegex(RuntimeError, "unexpected content"):
                    retire.retired_store(str(root))
                candidate.unlink()
                candidate.mkdir()
                with self.assertRaisesRegex(RuntimeError, "unexpected content"):
                    retire.retired_store(str(root))

    def test_active_package_manager_is_detected_without_a_store_path(self):
        self.assertTrue(retire.package_manager_active("node\0/usr/lib/pnpm/bin/pnpm.cjs\0install\0"))
        self.assertTrue(retire.package_manager_active("/usr/local/bin/pnpm\0store\0prune\0"))
        self.assertFalse(retire.package_manager_active("node\0/opt/app/server.js\0"))
        self.assertFalse(retire.package_manager_active("bash\0-c\0echo pnpm\0"))

    def test_new_cache_reference_aborts_dependency_removal_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            old, cache = root / "old", root / "cache"
            old.mkdir()
            (old / "package.json").write_text("{}")
            (old / "node_modules").mkdir()
            cache.mkdir()
            empty = {str(old): [], str(cache): []}
            active = {str(old): [], str(cache): ["active pnpm process"]}
            with patch.object(retire, "RETIRED_BUILDS", (str(old),)), \
                    patch.object(retire, "RETIRED_STORES", (str(cache),)), \
                    patch.object(retire.pathlib.Path, "glob", return_value=[]), \
                    patch.object(retire, "disk_sizes", create=True, return_value={}), \
                    patch.object(retire, "disk_command", create=True, return_value={}), \
                    patch.object(retire, "build_references", side_effect=[empty, active]), \
                    patch.object(retire.subprocess, "check_output", return_value="4096\tcache"), \
                    patch.object(retire.shutil, "rmtree") as remove, patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "references changed"):
                    retire.retire_inactive_builds(str(root))
            remove.assert_not_called()
