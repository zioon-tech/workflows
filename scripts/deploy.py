"""Atomic component deployment; compatible with ECS Python 3.6."""
import fcntl, hashlib, json, os, pathlib, pwd, shutil, subprocess, tarfile, time, urllib.request

def digest(path):
    value = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def extract(archive, destination, manifest):
    """Validate the entire archive before writing; links may only stay inside it."""
    root = pathlib.Path(destination).resolve()
    with tarfile.open(str(archive), "r:gz") as tar:
        members = tar.getmembers()
        if len(members) != manifest["entries"]:
            raise ValueError("Release entry count mismatch")
        if sum(m.size for m in members if m.isfile()) != manifest["expandedBytes"]:
            raise ValueError("Release expanded size mismatch")
        names, links = set(), []
        for member in members:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.name in names:
                raise ValueError("Invalid archive path")
            names.add(member.name)
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsupported archive member")
            if member.issym():
                target = pathlib.Path(os.path.normpath(str(root / member.name / ".." / member.linkname)))
                if os.path.isabs(member.linkname) or not str(target).startswith(str(root) + os.sep):
                    raise ValueError("Archive link escapes release")
                links.append(member.name)
        for name in names:
            if any(name.startswith(link + "/") for link in links):
                raise ValueError("Archive writes through a symlink")
        for member in members:
            member.mode &= 0o755
            member.uid = member.gid = 0
            member.uname = member.gname = ""
        tar.extractall(str(root), members=members)

def ready(urls, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for url in urls:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status != 200:
                        raise RuntimeError("Unhealthy response")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("HTTP readiness timed out")

def switch(pointer, target):
    pointer = pathlib.Path(pointer)
    temporary = pointer.with_name(pointer.name + ".next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(str(temporary), str(pointer))

def deploy(archive, manifest, config, check_only=False):
    root = pathlib.Path(config["root"])
    pointer = pathlib.Path(config["current"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if digest(archive) != manifest["archiveSha256"]:
            raise ValueError("Archive checksum mismatch")
        if manifest["platform"] != "linux-x64" or os.uname().machine not in ("x86_64", "amd64"):
            raise ValueError("Release platform mismatch")
        needed = manifest["expandedBytes"] + 512 * 1024 * 1024
        if shutil.disk_usage(str(root)).free < needed:
            raise RuntimeError("Insufficient disk space for release plus 512 MiB reserve")
        if pointer.exists() and not pointer.is_symlink():
            raise RuntimeError("Current pointer is a real directory; migrate it explicitly first")
        if check_only:
            print(json.dumps(dict(status="preflight_passed", component=manifest["component"])))
            return
        release_id = manifest["commit"] + "-" + str(manifest["ciRun"])
        release = root / "releases" / release_id
        release.parent.mkdir(parents=True, exist_ok=True)
        journal = root / "deployments.jsonl"
        previous = os.readlink(str(pointer)) if pointer.is_symlink() else None
        operation = dict(component=manifest["component"], commit=manifest["commit"],
                         ciRun=manifest["ciRun"], sha256=manifest["archiveSha256"],
                         previous=previous, release=str(release), timestamp=int(time.time()))
        def record(status):
            with journal.open("a") as stream:
                stream.write(json.dumps(dict(operation, status=status)) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        def restart():
            if config.get("service"):
                subprocess.check_call(["systemctl", "restart", config["service"]])
        if release.exists():
            deployed_manifest = release / ".release-manifest.json"
            if not deployed_manifest.exists() or json.loads(deployed_manifest.read_text()) != manifest:
                raise RuntimeError("Release ID already exists with a different manifest")
            if pointer.is_symlink() and pointer.resolve() == release.resolve():
                ready(config["health"])
                record("already_deployed")
                return
        else:
            stage = root / (".staging-" + release_id)
            if stage.exists():
                raise RuntimeError("Incomplete staging directory requires reconciliation")
            stage.mkdir()
            try:
                extract(archive, stage, manifest)
                for name in config.get("required", []):
                    if not (stage / name).exists():
                        raise ValueError("Required release file missing: " + name)
                (stage / ".release-manifest.json").write_text(json.dumps(manifest))
                # Preserve root ownership: running services cannot alter a published release.
                os.rename(str(stage), str(release))
            except BaseException:
                shutil.rmtree(str(stage))
                raise
        record("prepared")
        record("switching")
        switch(pointer, str(release))
        try:
            restart()
            ready(config["health"])
            record("deployed")
        except BaseException:
            if previous is None:
                pointer.unlink()
                record("failed_no_previous")
                raise
            switch(pointer, previous)
            try:
                restart()
                ready(config["health"])
                record("rolled_back")
            except BaseException:
                record("rollback_failed")
                raise
            raise
