"""Small dependency-free Alibaba RPC/OSS client. Credentials never reach stdout."""
import base64, datetime, hashlib, hmac, json, time, urllib.error, urllib.parse, urllib.request, uuid, zlib

def python_command(source):
    """Transmit the fixed, reviewed program within ECS's command-content limit."""
    payload = base64.b64encode(zlib.compress(source.encode(), 9)).decode()
    command = ("python3 - <<'ZIOON_RELEASE_PY'\nimport base64, zlib\n"
               "exec(compile(zlib.decompress(base64.b64decode(" + repr(payload) +
               ")), '<zioon-release>', 'exec'))\nZIOON_RELEASE_PY\n")
    if len(base64.b64encode(command.encode())) > 16 * 1024:
        raise RuntimeError("Compressed release command exceeds ECS content limit")
    return command

def request(url, method="GET", data=None, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=data, headers=headers or {}, method=method), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        code, request_id = "unknown", "unknown"
        try:
            body = json.loads(error.read())
            code, request_id = body.get("Code", "unknown"), body.get("RequestId", "unknown")
        except Exception:
            pass
        raise RuntimeError("HTTP request failed with status {} ({}; request {})".format(
            error.code, code, request_id)) from None

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
    expiry = str(int(time.time()) + 600)
    canonical = "/" + bucket + "/" + key + "?security-token=" + credentials["SecurityToken"]
    text = method + "\n\n\n" + expiry + "\n" + canonical
    signature = base64.b64encode(hmac.new(credentials["AccessKeySecret"].encode(),
                                          text.encode(), hashlib.sha1).digest()).decode()
    query = urllib.parse.urlencode(dict(OSSAccessKeyId=credentials["AccessKeyId"],
                                       Expires=expiry, Signature=signature,
                                       **{"security-token": credentials["SecurityToken"]}))
    return request("https://" + bucket + ".oss-cn-shanghai.aliyuncs.com/" +
                   urllib.parse.quote(key, safe="/") + "?" + query, method, data,
                   {"Content-Type": ""} if method == "PUT" else {})
