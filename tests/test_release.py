import hashlib, importlib.util, io, json, os, pathlib, tarfile, tempfile, unittest
from unittest.mock import patch

def module(name):
    path = pathlib.Path(__file__).parent.parent / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

deploy = module("deploy")
pack = module("pack")

class ReleaseTests(unittest.TestCase):
    def archive(self, root, members):
        path = root / "release.tgz"
        with tarfile.open(path, "w:gz") as archive:
            for name, target in members:
                info = tarfile.TarInfo(name)
                if target is None:
                    info.size = 2
                    archive.addfile(info, io.BytesIO(b"ok"))
                else:
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    archive.addfile(info)
        return path, dict(entries=len(members), expandedBytes=sum(2 for _, t in members if t is None))

    def test_escape_and_symlink_write_are_rejected_before_extraction(self):
        for members in [[("../outside", None)], [("link", "../../escape")],
                        [("link", "inside"), ("link/payload", None)]]:
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                path, manifest = self.archive(root, members)
                destination = root / "stage"
                destination.mkdir()
                with self.assertRaises(ValueError):
                    deploy.extract(path, destination, manifest)
                self.assertEqual(list(destination.iterdir()), [])

    def test_failed_health_restores_previous_pointer_and_records_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path, manifest = self.archive(root, [("entry", None)])
            manifest.update(archiveSha256=deploy.digest(path), platform="linux-x64",
                            commit="a"*40, ciRun="10", component="site")
            previous = root / "previous"
            previous.mkdir()
            current = root / "current"
            current.symlink_to(previous)
            config = dict(root=str(root), current=str(current), health=["http://example.test"],
                          required=["entry"])
            with patch.object(deploy.os, "uname") as uname, patch.object(
                    deploy, "ready", side_effect=[RuntimeError("unhealthy"), None]):
                uname.return_value.machine = "x86_64"
                with self.assertRaises(RuntimeError):
                    deploy.deploy(path, manifest, config)
            self.assertEqual(current.resolve(), previous.resolve())
            statuses = [json.loads(line)["status"] for line in (root/"deployments.jsonl").read_text().splitlines()]
            self.assertEqual(statuses, ["prepared", "switching", "rolled_back"])

    def test_package_with_dependency_hardlinks_validates_and_extracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            stage, output, destination = root/"stage", root/"out", root/"extracted"
            stage.mkdir()
            (stage/"dependency").write_text("runtime dependency")
            os.link(str(stage/"dependency"), str(stage/"alias"))
            (stage/"symbolic").symlink_to("dependency")
            pack.package(stage, output, "memory", "service")
            manifest = json.loads((output/"manifest.json").read_text())
            deploy.extract(output/"release.tgz", destination, manifest, validate_only=True)
            self.assertFalse(destination.exists())
            destination.mkdir()
            deploy.extract(output/"release.tgz", destination, manifest)
            self.assertEqual((destination/"dependency").read_text(), "runtime dependency")
            self.assertEqual((destination/"alias").read_text(), "runtime dependency")
            self.assertTrue((destination/"symbolic").is_symlink())

    def test_hardlinks_cannot_target_outside_or_symlinks(self):
        for target in ["../../outside", "symbolic"]:
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                archive = root/"release.tgz"
                with tarfile.open(str(archive), "w:gz") as stream:
                    symbolic = tarfile.TarInfo("symbolic")
                    symbolic.type, symbolic.linkname = tarfile.SYMTYPE, "internal"
                    stream.addfile(symbolic)
                    link = tarfile.TarInfo("alias")
                    link.type, link.linkname = tarfile.LNKTYPE, target
                    stream.addfile(link)
                with self.assertRaises(ValueError):
                    deploy.extract(archive, root/"stage", dict(entries=2, expandedBytes=0))

if __name__ == "__main__":
    unittest.main()
