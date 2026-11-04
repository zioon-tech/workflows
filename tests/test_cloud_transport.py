import importlib.util
import pathlib
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "cloud", pathlib.Path(__file__).parent.parent / "scripts" / "cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CloudTransportTests(unittest.TestCase):
    credentials = {
        "AccessKeyId": "test-id",
        "AccessKeySecret": "test-secret",
        "SecurityToken": "test-token",
    }

    def exchange(self, method, payload=None):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"stored"
        with patch.object(cloud.time, "time", return_value=1000), patch.object(
                cloud.urllib.request, "urlopen", return_value=response) as open_url:
            result = cloud.oss("test-bucket", "release.tgz", self.credentials, method, payload)
        self.assertEqual(result, b"stored")
        open_url.assert_called_once()
        request = open_url.call_args.args[0]
        return request, open_url.call_args.kwargs["timeout"]

    def test_archive_upload_signature_outlives_the_full_transfer_budget(self):
        payload = b"qualified archive"
        request, timeout = self.exchange("PUT", payload)
        expiry = int(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["Expires"][0])
        self.assertEqual(request.data, payload)
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(timeout, 600)
        self.assertGreater(expiry - 1000, timeout)

    def test_download_keeps_the_existing_read_deadline_and_signature_lifetime(self):
        request, timeout = self.exchange("GET")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(timeout, 60)
        self.assertEqual(query["Expires"], ["1600"])

    def test_rpc_timeout_never_repeats_a_mutating_cloud_request(self):
        with patch.object(cloud.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError(TimeoutError())) as open_url:
            with self.assertRaises(urllib.error.URLError):
                cloud.rpc("ecs.example.test", "2014-05-26", "RunCommand", {})
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.kwargs["timeout"], 60)
