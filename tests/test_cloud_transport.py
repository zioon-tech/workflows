import importlib.util
import io
import http.server
import pathlib
import threading
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

    def test_streamed_upload_sends_content_length_and_small_blocks_over_http(self):
        payload = b"qualified archive" * 10000
        received = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                received["length"] = self.headers["Content-Length"]
                received["transfer"] = self.headers.get("Transfer-Encoding")
                received["body"] = self.rfile.read(int(received["length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"stored")
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        class Stream(io.BytesIO):
            sizes = []
            def read(self, size=-1):
                self.sizes.append(size)
                return super().read(size)
        stream = Stream(payload)
        original_request = cloud.request
        def local_request(url, method, data, headers, timeout):
            expiry = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["Expires"][0])
            self.assertEqual(timeout, 60)
            self.assertGreater(expiry - cloud.time.time(), data.budget + 2 * timeout)
            return original_request(f"http://127.0.0.1:{server.server_port}/",
                                    method, data, headers, timeout)
        with patch.object(cloud, "request", side_effect=local_request):
            self.assertEqual(cloud.oss(
                "test-bucket", "release.tgz", self.credentials, "PUT", stream), b"stored")
        self.assertEqual(received["body"], payload)
        self.assertEqual(received["length"], str(len(payload)))
        self.assertIsNone(received["transfer"])
        self.assertGreater(len(stream.sizes), 1)
        self.assertTrue(all(0 < size <= 64 * 1024 for size in stream.sizes))

    def test_stream_budget_exhaustion_stops_before_another_read(self):
        stream = io.BytesIO(b"archive")
        with patch.object(cloud.time, "monotonic", return_value=0):
            body = cloud.UploadBody(stream)
        with patch.object(cloud.time, "monotonic", return_value=601):
            with self.assertRaisesRegex(TimeoutError, "transfer budget"):
                body.read(8192)
        self.assertEqual(stream.tell(), 0)

    def test_truncated_stream_fails_instead_of_reporting_complete(self):
        stream = io.BytesIO(b"archive")
        body = cloud.UploadBody(stream)
        stream.truncate(3)
        self.assertEqual(body.read(8192), b"arc")
        with self.assertRaisesRegex(RuntimeError, "size changed"):
            body.read(8192)

    def test_rpc_timeout_never_repeats_a_mutating_cloud_request(self):
        with patch.object(cloud.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError(TimeoutError())) as open_url:
            with self.assertRaises(urllib.error.URLError):
                cloud.rpc("ecs.example.test", "2014-05-26", "RunCommand", {})
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.kwargs["timeout"], 60)
