"""Small dependency-free Alibaba RPC/OSS client. Credentials never reach stdout."""
import base64, datetime, hashlib, hmac, json, time, urllib.error, urllib.parse, urllib.request, uuid

def request(url, method="GET", data=None, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=data, headers=headers or {}, method=method), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError("HTTP request failed with status " + str(error.code)) from None

def rpc(endpoint, version, action, parameters, credentials=None):
    query = dict(parameters, Action=action, Version=version, Format="JSON")
    if credentials:
        query.update(AccessKeyId=credentials["AccessKeyId"], SignatureMethod="HMAC-SHA1",
                     SignatureVersion="1.0", SignatureNonce=str(uuid.uuid4()),
                     Timestamp=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
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
