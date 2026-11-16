import contextlib
import hashlib
import io
import json
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
        with contextlib.redirect_stdout(io.StringIO()), patch.object(upload, "qualify_lifecycle"):
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

    def test_actual_client_json_footer_is_accepted(self):
        credentials = {"AccessKeyId": "id", "AccessKeySecret": "secret", "SecurityToken": "token"}
        result = subprocess.CompletedProcess([], 0, '{"UploadId":"test"}\n\n0.1(s) elapsed\n', "")
        with patch.object(upload.subprocess, "run", return_value=result):
            self.assertEqual(upload.client_api(
                "client", self.directory.name, "bucket", "key", credentials,
                "initiate-multipart-upload"), {"UploadId": "test"})

    def test_missing_list_permission_aborts_empty_upload(self):
        def api(*args, **kwargs):
            operation = args[5]
            if operation == "initiate-multipart-upload":
                return {"UploadId": "test"}
            if operation == "list-parts":
                raise RuntimeError("code=AccessDenied")
            return {}
        with patch.object(upload, "client_api", side_effect=api) as calls:
            with self.assertRaisesRegex(RuntimeError, "AccessDenied"):
                upload.qualify_lifecycle("client", self.directory.name, "bucket", "key", {})
        self.assertEqual([call.args[5] for call in calls.call_args_list], [
            "initiate-multipart-upload", "list-parts", "abort-multipart-upload"])

    def test_multipart_is_completed_with_verified_sorted_parts(self):
        completed = []
        def api(*args, **kwargs):
            operation = args[5]
            if operation == "initiate-multipart-upload":
                return {"UploadId": "test"}
            options = args[6]
            if operation == "upload-part":
                path = pathlib.Path(options[options.index("--body") + 1][7:])
                return {"Header": {"Etag": [
                    '"' + hashlib.md5(path.read_bytes()).hexdigest().upper() + '"']}}
            if operation == "complete-multipart-upload":
                completed.extend(json.loads(pathlib.Path(options[-1][7:]).read_text())["Part"])
                return {"ETag": "confirmed"}
            raise AssertionError("Unexpected abort after confirmed completion")
        with patch.object(upload, "client_api", side_effect=api):
            upload.put_file("client", self.directory.name, "bucket", "key", {}, self.archive, 40)
        self.assertEqual([part["PartNumber"] for part in completed], list(range(1, 18)))

    def test_corrupt_part_aborts_and_never_completes(self):
        def api(*args, **kwargs):
            if args[5] == "initiate-multipart-upload":
                return {"UploadId": "test"}
            return {"ETag": "invalid"}
        with patch.object(upload, "client_api", side_effect=api) as calls:
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                upload.put_file("client", self.directory.name, "bucket", "key", {}, self.archive, 40)
        operations = [call.args[5] for call in calls.call_args_list]
        self.assertNotIn("complete-multipart-upload", operations)
        self.assertEqual(operations[-1], "abort-multipart-upload")

    def test_lifecycle_failure_blocks_probe_and_full_archive(self):
        with patch.object(upload, "install_client", return_value="client"), \
                patch.object(upload, "qualify_lifecycle", side_effect=RuntimeError("AccessDenied")), \
                patch.object(upload, "put_file") as put:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "AccessDenied"):
                upload.upload_archive("bucket", "key", {}, self.archive)
        put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
