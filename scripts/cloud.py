"""Small dependency-free Alibaba RPC/OSS client. Credentials never reach stdout."""
import base64, datetime, hashlib, hmac, json, time, urllib.error, urllib.parse, urllib.request, uuid, zlib

class CloudHTTPError(RuntimeError):
    def __init__(self, status, code, request_id):
        self.status = status
        super().__init__("HTTP request failed with status {} ({}; request {})".format(
            status, code, request_id))

def python_command(source):
    """Transmit the fixed, reviewed program within ECS's command-content limit."""
    payload = base64.b64encode(zlib.compress(source.encode(), 9)).decode()
    command = ("python3 - <<'ZIOON_RELEASE_PY'\nimport base64, zlib\n"
               "exec(compile(zlib.decompress(base64.b64decode(" + repr(payload) +
               ")), '<zioon-release>', 'exec'))\nZIOON_RELEASE_PY\n")
    if len(base64.b64encode(command.encode())) > 16 * 1024:
        raise RuntimeError("Compressed release command exceeds ECS content limit")
    return command

def request(url, method="GET", data=None, headers=None, timeout=60):
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=data, headers=headers or {}, method=method), timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        code, request_id = "unknown", "unknown"
        try:
            body = json.loads(error.read())
            code, request_id = body.get("Code", "unknown"), body.get("RequestId", "unknown")
        except Exception:
            pass
        raise CloudHTTPError(error.code, code, request_id) from None

class UploadBody:
    """Bound file reads so HTTPS sends small blocks, with no whole-archive copy."""
    budget = 600

    def __init__(self, stream):
        self.stream = stream
        self.size = stream.seek(0, 2)
        stream.seek(0)
        self.started = time.monotonic()
        self.reported = self.started
        self.bytes_read = 0
        self.progress()

    def progress(self):
        print(json.dumps(dict(status="release_archive_upload_progress",
                              bytesRead=self.bytes_read, archiveBytes=self.size,
                              elapsedSeconds=round(time.monotonic() - self.started, 3))),
              flush=True)

    def read(self, size=-1):
        if time.monotonic() - self.started >= self.budget:
            raise TimeoutError("Archive upload exceeded its transfer budget")
        block = self.stream.read(min(size, 64 * 1024) if size >= 0 else 64 * 1024)
        self.bytes_read += len(block)
        if self.bytes_read > self.size or (not block and self.bytes_read != self.size):
            raise RuntimeError("Archive size changed during upload")
        now = time.monotonic()
        if now - self.reported >= 5 or not block:
            self.progress()
            self.reported = now
        return block

def rpc(endpoint, version, action, parameters, credentials=None):
    query = dict(parameters, Action=action, Version=version, Format="JSON",
                 SignatureNonce=str(uuid.uuid4()),
                 Timestamp=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    if credentials:
        query.update(AccessKeyId=credentials["AccessKeyId"], SignatureMethod="HMAC-SHA1",
                     SignatureVersion="1.0")
        if credentials.get("SecurityToken"):
            query["SecurityToken"] = credentials["SecurityToken"]
        quote = lambda s: urllib.parse.quote(str(s), safe="~")
        canonical = "&".join(quote(k) + "=" + quote(query[k]) for k in sorted(query))
        text = "POST&%2F&" + quote(canonical)
        query["Signature"] = base64.b64encode(hmac.new(
            (credentials["AccessKeySecret"] + "&").encode(), text.encode(), hashlib.sha1).digest()).decode()
    result = json.loads(request("https://" + endpoint + "/", "POST",
                               urllib.parse.urlencode(query).encode(),
                               {"Content-Type": "application/x-www-form-urlencoded"}))
    if "Code" in result:
        raise RuntimeError("Cloud RPC failed: " + result["Code"])
    return result

def instance_credentials():
    # IMDSv2 token and RAM role credentials stay in memory.
    base = "http://100.100.100.200"
    token = request(base + "/latest/api/token", "PUT",
                    headers={"X-aliyun-ecs-metadata-token-ttl-seconds": "21600"}).decode()
    headers = {"X-aliyun-ecs-metadata-token": token}
    role = request(base + "/latest/meta-data/ram/security-credentials/", headers=headers).decode().strip()
    return json.loads(request(base + "/latest/meta-data/ram/security-credentials/" + role, headers=headers))

def oss(bucket, key, credentials, method="GET", data=None):
    streamed = method == "PUT" and hasattr(data, "read")
    body = UploadBody(data) if streamed else data
    # Streaming writes have a 60s I/O timeout and a bounded 600s read budget.
    # Leave time for the last write and response after the final body read.
    timeout = 60 if streamed or method != "PUT" else 600
    expiry = str(int(time.time()) + (900 if streamed else max(600, timeout + 60)))
    canonical = "/" + bucket + "/" + key + "?security-token=" + credentials["SecurityToken"]
    text = method + "\n\n\n" + expiry + "\n" + canonical
    signature = base64.b64encode(hmac.new(credentials["AccessKeySecret"].encode(),
                                          text.encode(), hashlib.sha1).digest()).decode()
    query = urllib.parse.urlencode(dict(OSSAccessKeyId=credentials["AccessKeyId"],
                                       Expires=expiry, Signature=signature,
                                       **{"security-token": credentials["SecurityToken"]}))
    headers = {"Content-Type": ""} if method == "PUT" else {}
    if streamed:
        headers["Content-Length"] = str(body.size)
    return request("https://" + bucket + ".oss-cn-shanghai.aliyuncs.com/" +
                   urllib.parse.quote(key, safe="/") + "?" + query, method, body,
                   headers, timeout=timeout)
