"""Stage immutable CI objects, reusing only a complete checksum-verified upload."""
import hashlib
import json
from cloud import CloudHTTPError, oss


def stage_artifact(bucket, prefix, credentials, manifest, archive, reuse_upload=False):
    # The release caller has already verified the selected CI artifact. The
    # instance must still compare the stored manifest and verify archive SHA-256
    # before preparing or switching anything. No GitHub-side object GET is
    # needed when explicitly reusing a preflight upload.
    if reuse_upload:
        return "reuse_requested"
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
    with archive.open("rb") as stream:
        oss(bucket, prefix + "/release.tgz", credentials, "PUT", stream)
    oss(bucket, prefix + "/manifest.json", credentials, "PUT",
        json.dumps(manifest).encode())
    return "uploaded"
