"""Upload with a checksum-pinned official OSS client after a small speed probe."""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import zipfile

from cloud import oss, request

# Official Linux amd64 package and checksum, independent of release credentials:
# https://help.aliyun.com/zh/oss/developer-reference/install-ossutil2
CLIENT_URL = "https://gosspublic.alicdn.com/ossutil/v2/2.3.0/ossutil-2.3.0-linux-amd64.zip"
CLIENT_SHA256 = "3ae4d9fc85a7a6e9f5654d1599766f1a3a42a3692870887b5ae9338d582ef65a"
PROBE_BYTES = 2 * 1024 * 1024
MIN_BYTES_PER_SECOND = 256 * 1024
MAX_SINGLE_PUT_BYTES = 5 * 1024 * 1024 * 1024


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


def put_file(binary, directory, bucket, key, credentials, path, timeout):
    # Environment-only STS credentials; do not write them into command arguments,
    # configuration, reports or logs. Ignore ambient OSS configuration.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OSS_")}
    environment.update(
        OSS_ACCESS_KEY_ID=credentials["AccessKeyId"],
        OSS_ACCESS_KEY_SECRET=credentials["AccessKeySecret"],
        OSS_SESSION_TOKEN=credentials["SecurityToken"],
    )
    command = [
        str(binary), "cp", str(path), "oss://" + bucket + "/" + key,
        "--force", "--no-progress", "--no-error-report", "--quiet",
        "--config-file", os.devnull, "--mode", "StsToken",
        "--region", "cn-shanghai", "--sign-version", "v1",
        "--endpoint", "https://oss-cn-shanghai.aliyuncs.com",
        # Keep this a single PutObject: no multipart lifecycle or new permissions.
        "--bigfile-threshold", str(MAX_SINGLE_PUT_BYTES),
        "--write-buffer-size", "1Mi", "--retry-times", "0",
        "--connect-timeout", "10", "--read-timeout", "30", "--loglevel", "off",
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(command, env=environment, cwd=directory,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError("Official OSS upload exceeded its transfer budget") from None
    if result.returncode:
        # Native diagnostics may contain resource URLs. Only publish an error code.
        match = re.search(r"\bCode:\s*([A-Za-z0-9_]+)", result.stdout + result.stderr)
        code = match.group(1) if match else "unknown"
        raise RuntimeError(f"Official OSS upload failed (exit={result.returncode}, code={code})")
    return time.monotonic() - started


def upload_archive(bucket, key, credentials, archive):
    size = archive.stat().st_size
    if not 0 < size < MAX_SINGLE_PUT_BYTES:
        raise RuntimeError("Archive exceeds the qualified single-PUT size limit")
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
        report("release_archive_native_upload_started", archiveBytes=size)
        elapsed = put_file(binary, directory, bucket, key, credentials, archive, 450)
        report("release_archive_native_upload_completed", archiveBytes=size,
               elapsedSeconds=round(elapsed, 3), bytesPerSecond=round(size / max(elapsed, 0.001)))
