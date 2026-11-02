import hashlib, importlib.util, io, json, pathlib, tarfile, tempfile, unittest
from unittest.mock import patch

def module(name):
    path = pathlib.Path(__file__).parent.parent / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

deploy = module("deploy")

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

if __name__ == "__main__":
    unittest.main()
