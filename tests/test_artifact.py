import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("cloud", SCRIPTS / "cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)
with patch.dict(sys.modules, cloud=cloud):
    spec = importlib.util.spec_from_file_location("artifact", SCRIPTS / "artifact.py")
    artifact = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifact)


class ArtifactTests(unittest.TestCase):
    payload = b"qualified archive"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = pathlib.Path(self.directory.name) / "release.tgz"
        self.archive.write_bytes(self.payload)
        self.manifest = {
            "repository": "zioon-tech/adc", "component": "service",
            "commit": "a" * 40, "ciRun": "42", "archiveBytes": len(self.payload),
            "archiveSha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def stage(self):
        return artifact.stage_artifact(
            "bucket", "immutable-release", {}, self.manifest, self.archive)

    def test_complete_verified_preflight_upload_is_reused_without_writes(self):
        with patch.object(artifact, "oss",
                          side_effect=[json.dumps(self.manifest).encode(), self.payload]) as oss:
            self.assertEqual(self.stage(), "reused")
        self.assertEqual(oss.call_count, 2)
        self.assertTrue(all(len(call.args) == 3 for call in oss.call_args_list))

    def test_corrupt_or_truncated_archive_is_rejected_without_overwriting(self):
        for payload in (b"wrong archive!!!!", self.payload[:-1]):
            with patch.object(artifact, "oss",
                              side_effect=[json.dumps(self.manifest).encode(), payload]) as oss:
                with self.assertRaisesRegex(RuntimeError, "archive differs"):
                    self.stage()
            self.assertEqual(oss.call_count, 2)

    def test_manifest_provenance_mismatch_is_rejected(self):
        wrong = dict(self.manifest, ciRun="43")
        with patch.object(artifact, "oss",
                          side_effect=[json.dumps(wrong).encode(), self.payload]) as oss:
            with self.assertRaisesRegex(RuntimeError, "manifest differs"):
                self.stage()
        oss.assert_called_once()

    def test_missing_manifest_uploads_archive_before_publishing_manifest(self):
        with patch.object(artifact, "oss", side_effect=[
                cloud.CloudHTTPError(404, "NoSuchKey", "test"), b"", b""]) as oss:
            self.assertEqual(self.stage(), "uploaded")
        self.assertEqual(oss.call_count, 3)
        self.assertTrue(oss.call_args_list[1].args[1].endswith("/release.tgz"))
        self.assertEqual(oss.call_args_list[1].args[3:], ("PUT", self.payload))
        self.assertTrue(oss.call_args_list[2].args[1].endswith("/manifest.json"))
        self.assertEqual(json.loads(oss.call_args_list[2].args[4]), self.manifest)

    def test_missing_archive_does_not_treat_manifest_as_complete_upload(self):
        with patch.object(artifact, "oss", side_effect=[
                json.dumps(self.manifest).encode(),
                cloud.CloudHTTPError(404, "NoSuchKey", "test"), b"", b""]) as oss:
            self.assertEqual(self.stage(), "uploaded")
        self.assertEqual(oss.call_count, 4)

    def test_permission_and_network_failures_abort_without_writes_or_retries(self):
        for error in (cloud.CloudHTTPError(403, "AccessDenied", "test"), TimeoutError()):
            with patch.object(artifact, "oss", side_effect=error) as oss:
                with self.assertRaises(type(error)):
                    self.stage()
            oss.assert_called_once()
