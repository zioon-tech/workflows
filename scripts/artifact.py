"""Stage immutable CI objects, reusing only a complete checksum-verified upload."""
import hashlib
import json
from cloud import CloudHTTPError, oss


def stage_artifact(bucket, prefix, credentials, manifest, archive):
    try:
        stored_manifest = json.loads(oss(bucket, prefix + "/manifest.json", credentials))
    except CloudHTTPError as error:
        if error.status != 404:
            raise
        stored_manifest = None
    else:
        if stored_manifest != manifest:
            raise RuntimeError("Existing release manifest differs from the qualified CI artifact")
    if stored_manifest is not None:
        try:
            stored_archive = oss(bucket, prefix + "/release.tgz", credentials)
        except CloudHTTPError as error:
            if error.status != 404:
                raise
        else:
            if (len(stored_archive) != manifest["archiveBytes"] or
                    hashlib.sha256(stored_archive).hexdigest() != manifest["archiveSha256"]):
                raise RuntimeError("Existing release archive differs from the qualified CI artifact")
            return "reused"
    oss(bucket, prefix + "/release.tgz", credentials, "PUT", archive.read_bytes())
    oss(bucket, prefix + "/manifest.json", credentials, "PUT",
        json.dumps(manifest).encode())
    return "uploaded"
