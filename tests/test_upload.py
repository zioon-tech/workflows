import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
import upload


class NativeUploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = pathlib.Path(self.directory.name) / "release.tgz"
        self.payload = b"a" * upload.PROBE_BYTES + b"remaining archive"
        self.archive.write_bytes(self.payload)
        self.environment = patch.dict(os.environ, GITHUB_RUN_ID="verification")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            upload.upload_archive("test-bucket", "release.tgz", {}, self.archive)

    def test_binary_checksum_mismatch_prevents_execution(self):
        with patch.object(upload, "request", return_value=b"untrusted executable"):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                upload.install_client(pathlib.Path(self.directory.name))
        self.assertFalse((pathlib.Path(self.directory.name) / "ossutil").exists())

    def test_slow_or_corrupt_probe_prevents_full_upload_and_clears_content(self):
        for elapsed, stored, message in (
                (20, self.payload[:upload.PROBE_BYTES], "throughput"),
                (1, b"truncated", "integrity")):
            with patch.object(upload, "install_client", return_value="client"), \
                    patch.object(upload, "put_file", return_value=elapsed) as put, \
                    patch.object(upload, "oss", side_effect=[stored, b""]) as oss:
                with self.assertRaisesRegex(RuntimeError, message):
                    self.execute()
            put.assert_called_once()
            self.assertEqual(oss.call_args.args[-2:], ("PUT", b""))

    def test_verified_probe_is_followed_by_exact_archive_upload(self):
        with patch.object(upload, "install_client", return_value="client"), \
                patch.object(upload, "put_file", side_effect=[1, 2]) as put, \
                patch.object(upload, "oss", side_effect=[
                    self.payload[:upload.PROBE_BYTES], b""]) as oss:
            self.execute()
        self.assertEqual(put.call_count, 2)
        self.assertEqual(put.call_args.args[-2], self.archive)
        self.assertEqual(put.call_args.args[3], "release.tgz")
        self.assertEqual(oss.call_args.args[-2:], ("PUT", b""))

    def test_client_failure_redacts_resources_and_keeps_credentials_out_of_arguments(self):
        credentials = {"AccessKeyId": "private-id", "AccessKeySecret": "private-secret",
                       "SecurityToken": "private-token"}
        result = subprocess.CompletedProcess(
            [], 1, "", "Code: AccessDenied https://private-resource private-token")
        with patch.object(upload.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "code=AccessDenied") as error:
                upload.put_file("client", self.directory.name, "bucket", "key",
                                credentials, self.archive, 40)
        self.assertNotIn("private-resource", str(error.exception))
        self.assertNotIn("private-token", str(error.exception))
        arguments = run.call_args.args[0]
        self.assertTrue(all(value not in arguments for value in credentials.values()))
        self.assertEqual(run.call_args.kwargs["env"]["OSS_SESSION_TOKEN"], "private-token")
        self.assertEqual(arguments[arguments.index("--retry-times") + 1], "0")

    def test_client_timeout_is_not_retried(self):
        credentials = {"AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
        with patch.object(upload.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(["private-resource"], 40)) as run:
            with self.assertRaisesRegex(TimeoutError, "transfer budget"):
                upload.put_file("client", self.directory.name, "bucket", "key",
                                credentials, self.archive, 40)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
