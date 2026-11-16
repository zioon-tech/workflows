"""Bounded parallel multipart uploads through a checksum-pinned official client."""
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time
import zipfile

from cloud import oss, request

# Official Linux amd64 package and checksum, independent of release credentials:
# https://help.aliyun.com/zh/oss/developer-reference/install-ossutil2
CLIENT_URL = "https://gosspublic.alicdn.com/ossutil/v2/2.3.0/ossutil-2.3.0-linux-amd64.zip"
CLIENT_SHA256 = "3ae4d9fc85a7a6e9f5654d1599766f1a3a42a3692870887b5ae9338d582ef65a"
PROBE_BYTES = 2 * 1024 * 1024
MIN_BYTES_PER_SECOND = 256 * 1024
PART_BYTES = 128 * 1024
PARALLEL = 32
MAX_ARCHIVE_BYTES = PART_BYTES * 10000


def report(status, **values):
    print(json.dumps(dict(status=status, **values)), flush=True)


def install_client(directory):
    payload = request(CLIENT_URL, timeout=30)
    if hashlib.sha256(payload).hexdigest() != CLIENT_SHA256:
        raise RuntimeError("Official OSS client package checksum mismatch")
    package = directory / "client.zip"
    package.write_bytes(payload)
    binary = directory / "ossutil"
    with zipfile.ZipFile(package) as archive:
        binary.write_bytes(archive.read("ossutil-2.3.0-linux-amd64/ossutil"))
    binary.chmod(0o700)
    return binary


def client_api(binary, directory, bucket, key, credentials, operation, options=(), timeout=30):
    # Environment-only STS credentials; do not write them into command arguments,
    # configuration, reports or logs. Ignore ambient OSS configuration.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OSS_")}
    environment.update(
        OSS_ACCESS_KEY_ID=credentials["AccessKeyId"],
        OSS_ACCESS_KEY_SECRET=credentials["AccessKeySecret"],
        OSS_SESSION_TOKEN=credentials["SecurityToken"],
    )
    command = [
        str(binary), "api", operation, "--bucket", bucket, "--key", key,
        *options, "--output-format", "json",
        "--config-file", os.devnull, "--mode", "StsToken",
        "--region", "cn-shanghai", "--sign-version", "v1",
        "--endpoint", "https://oss-cn-shanghai.aliyuncs.com",
        "--retry-times", "0",
        "--connect-timeout", "10", "--read-timeout", "30", "--loglevel", "off",
    ]
    try:
        result = subprocess.run(command, env=environment, cwd=directory,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("Official OSS upload exceeded its transfer budget") from None
    if result.returncode:
        # Native diagnostics may contain resource URLs. Only publish an error code.
        match = re.search(r"\b(?:Code|ErrorCode):\s*([A-Za-z0-9_]+)", result.stdout + result.stderr)
        code = match.group(1) if match else "unknown"
        raise RuntimeError(f"Official OSS upload failed (exit={result.returncode}, code={code})")
    # ossutil adds an elapsed-time footer after its JSON result.
    try:
        value, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
    except ValueError:
        raise RuntimeError("Official OSS client returned an invalid API response") from None
    if not isinstance(value, dict):
        raise RuntimeError("Official OSS client returned an invalid API result")
    return value


def abort_upload(binary, directory, bucket, key, credentials, upload_id):
    try:
        client_api(binary, directory, bucket, key, credentials,
                   "abort-multipart-upload", ("--upload-id", upload_id))
    except RuntimeError as error:
        # A completion may have succeeded even if its response was lost.
        if "code=NoSuchUpload" not in str(error):
            raise


def qualify_lifecycle(binary, directory, bucket, key, credentials):
    result = client_api(binary, directory, bucket, key, credentials, "initiate-multipart-upload")
    upload_id = result["UploadId"]
    try:
        client_api(binary, directory, bucket, key, credentials,
                   "list-parts", ("--upload-id", upload_id))
    finally:
        abort_upload(binary, directory, bucket, key, credentials, upload_id)
    report("release_upload_multipart_lifecycle_verified")


def put_file(binary, directory, bucket, key, credentials, path, timeout):
    started = time.monotonic()
    deadline = started + timeout
    result = client_api(binary, directory, bucket, key, credentials, "initiate-multipart-upload")
    upload_id = result["UploadId"]
    complete = False
    try:
        parts = []
        with pathlib.Path(path).open("rb") as stream:
            for number in range(1, 10001):
                content = stream.read(PART_BYTES)
                if not content:
                    break
                part = pathlib.Path(directory) / f"part-{number}.bin"
                part.write_bytes(content)
                parts.append((number, part, hashlib.md5(content).hexdigest().upper()))
            if stream.read(1):
                raise RuntimeError("Archive exceeds the multipart part limit")
        if not parts:
            raise RuntimeError("Cannot multipart upload an empty archive")
        cancelled = threading.Event()

        def transfer(item):
            number, part, expected = item
            remaining = deadline - time.monotonic()
            if cancelled.is_set() or remaining <= 0:
                raise TimeoutError("Official OSS upload exceeded its transfer budget")
            response = client_api(binary, directory, bucket, key, credentials, "upload-part", (
                "--upload-id", upload_id, "--part-number", str(number),
                "--body", "file://" + str(part)), timeout=min(30, remaining))
            # UploadPart is a header-only response in ossutil 2.3.0.
            etags = response.get("Header", {}).get("Etag", [])
            etag = etags[0] if len(etags) == 1 else None
            if not isinstance(etag, str) or etag.strip('"').upper() != expected:
                raise RuntimeError("Uploaded part integrity mismatch")
            return {"PartNumber": number, "ETag": etag}

        # On failure, stop pending work and wait for in-flight subprocesses before
        # aborting; no writer may remain active after cleanup.
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL) as executor:
            futures = [executor.submit(transfer, part) for part in parts]
            results = []
            try:
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())
            except BaseException:
                cancelled.set()
                for future in futures:
                    future.cancel()
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Official OSS upload exceeded its transfer budget")
        completion = pathlib.Path(directory) / "complete.json"
        completion.write_text(json.dumps({"Part": sorted(results, key=lambda p: p["PartNumber"])}))
        response = client_api(binary, directory, bucket, key, credentials,
                             "complete-multipart-upload", (
                                 "--upload-id", upload_id, "--complete-multipart-upload",
                                 "file://" + str(completion)), timeout=min(30, remaining))
        if not response.get("ETag"):
            raise RuntimeError("Multipart completion was not confirmed")
        complete = True
        return time.monotonic() - started
    finally:
        if not complete:
            abort_upload(binary, directory, bucket, key, credentials, upload_id)


def upload_archive(bucket, key, credentials, archive):
    size = archive.stat().st_size
    if not 0 < size <= MAX_ARCHIVE_BYTES:
        raise RuntimeError("Archive exceeds the qualified multipart size limit")
    with tempfile.TemporaryDirectory(prefix="zioon-oss-") as temporary:
        directory = pathlib.Path(temporary)
        binary = install_client(directory)
        report("release_upload_client_verified", clientVersion="2.3.0")
        with archive.open("rb") as stream:
            probe_bytes = stream.read(PROBE_BYTES)
        probe = directory / "probe.bin"
        probe.write_bytes(probe_bytes)
        # Per-workflow key prevents cleanup from colliding with another probe.
        probe_key = key + ".probe-" + os.environ["GITHUB_RUN_ID"]
        qualify_lifecycle(binary, directory, bucket, probe_key, credentials)
        report("release_upload_probe_started", probeBytes=len(probe_bytes))
        try:
            elapsed = put_file(binary, directory, bucket, probe_key, credentials, probe, 40)
            stored = oss(bucket, probe_key, credentials)
            if stored != probe_bytes:
                raise RuntimeError("Upload probe integrity mismatch")
            rate = len(probe_bytes) / max(elapsed, 0.001)
            report("release_upload_probe_verified", probeBytes=len(probe_bytes),
                   elapsedSeconds=round(elapsed, 3), bytesPerSecond=round(rate))
            if rate < MIN_BYTES_PER_SECOND:
                raise RuntimeError("Upload probe throughput is insufficient; full archive was not sent")
        finally:
            # Existing PutObject permission is sufficient to clear probe content.
            oss(bucket, probe_key, credentials, "PUT", b"")
        report("release_archive_native_upload_started", archiveBytes=size,
               parallel=PARALLEL, partBytes=PART_BYTES)
        elapsed = put_file(binary, directory, bucket, key, credentials, archive, 450)
        report("release_archive_native_upload_completed", archiveBytes=size,
               elapsedSeconds=round(elapsed, 3), bytesPerSecond=round(size / max(elapsed, 0.001)))
