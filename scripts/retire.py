"""Retire dependencies from explicitly inventoried, unreferenced old builds."""
import contextlib, fcntl, json, os, pathlib, re, shutil, stat, subprocess, time

# Preserve source, data, archives and static output, even in temporary staging.
RETIRED_BUILDS = (
    "/opt/agent-device-cloud/releases/91e2217",
    "/opt/agent-device-cloud/releases/6fc4315",
    "/opt/agent-device-cloud/releases/f8e71eb",
    "/opt/agent-device-cloud/releases/247040a",
    "/opt/agent-device-cloud/releases/d3d84de",
    "/opt/agent-device-cloud/releases/c9e2322",
    "/opt/agent-device-cloud/releases/343d469",
    "/opt/agent-device-cloud/releases/console-refinement-20261101",
    "/opt/agent-device-cloud/releases/root-sharing-20261101",
    "/opt/agent-device-cloud/releases/collaboration-20261031",
    # Inventoried ADC releases; live and immediate rollback references are
    # rechecked under component locks before any dependency tree is removed.
    "/opt/agent-device-cloud/releases/000b0a19808d4a3e92ddd7bffa812f256989f04c-41770857505",
    "/opt/agent-device-cloud/releases/dc4be47385d9daf2ed816b22a0ae47f4f973bd58-41829701455",
    "/opt/agent-device-cloud/releases/510724f2bd1560741ae33a76ab28801238a8ea77-41849418067",
    # This inventoried pilot is the current rollback. Reference checks retain it
    # until a successful service switch makes the previous full release rollback.
    "/opt/agent-device-cloud/releases/510724f2-gui-fast-path-20261106",
    "/opt/agent-device-cloud/releases/60841571d2681090a68ea6b51b2b0719dcb8da62-41495697498",
    "/opt/agent-device-cloud/releases/87489f74762bae0456f412774bf649eb4ddeb8ec-41686300466",
    "/tmp/zioon-plugin-release-vrruwpi8",
)
RETIRED_STORES = (
    "/root/.local/share/pnpm/store/v3/files",
    "/var/lib/agent-device-cloud/.local/share/pnpm/store/v3/files",
)

def inside(path, base):
    return path == base or path.startswith(base + os.sep)

def dependency_file_usage(paths):
    """Estimate released blocks without counting hardlinks retained elsewhere."""
    inodes = {}
    for path in paths:
        for directory, folders, filenames in os.walk(path):
            for filename in filenames:
                info = (pathlib.Path(directory) / filename).lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                key = (info.st_dev, info.st_ino)
                entry = inodes.setdefault(key, dict(links=info.st_nlink, removed=0,
                                                    bytes=info.st_blocks * 512))
                entry["removed"] += 1
    return dict(
        reclaimableFileBytes=sum(entry["bytes"] for entry in inodes.values()
                                 if entry["removed"] == entry["links"]),
        sharedFileBytes=sum(entry["bytes"] for entry in inodes.values()
                           if entry["removed"] < entry["links"]),
    )

def retired_dependencies(build):
    base = pathlib.Path(build)
    if base.is_symlink() or str(base.resolve()) != str(base):
        raise RuntimeError("Inventoried build path is a symlink")
    if not base.is_dir():
        return []
    paths = []
    for root, directories, files in os.walk(str(base)):
        if "node_modules" in directories:
            candidate = pathlib.Path(root) / "node_modules"
            if candidate.is_symlink():
                raise RuntimeError("Inventoried dependencies are a symlink")
            # A dependency tree must belong to a package, never an arbitrary data folder.
            if not (pathlib.Path(root) / "package.json").is_file():
                raise RuntimeError("Dependency tree has no owning package manifest")
            paths.append(str(candidate))
            directories.remove("node_modules")
    return paths

def retired_store(path):
    """Accept only the inventoried pnpm v3 SHA-512 cache, never arbitrary data."""
    base = pathlib.Path(path)
    if path not in RETIRED_STORES or base.is_symlink() or str(base.resolve()) != path:
        raise RuntimeError("Package cache is not the canonical inventoried path")
    if not base.exists():
        return []
    if not base.is_dir():
        raise RuntimeError("Package cache is not a directory")
    for shard in base.iterdir():
        if shard.is_symlink() or not shard.is_dir() or not re.fullmatch("[0-9a-f]{2}", shard.name):
            raise RuntimeError("Package cache has an unexpected shard")
        for file in shard.iterdir():
            if not stat.S_ISREG(file.lstat().st_mode) or not re.fullmatch(
                    r"[0-9a-f]{126}(?:-exec|-index\.json)?", file.name):
                raise RuntimeError("Package cache has unexpected content")
    return [path]

def package_manager_active(command):
    return any(pathlib.Path(token).name in ("pnpm", "pnpm.cjs", "pnpm.js")
               for token in command.split("\0") if token)

def build_references(builds):
    references = {base: [] for base in builds}
    def add(path, target):
        for base in builds:
            if inside(target, base):
                references[base].append(path)
    search = [path for path in ["/opt", "/srv", "/etc/systemd/system", "/etc/caddy",
                               "/var/lib/zion-next", "/home", "/root/.local/share/pnpm"]
              if os.path.isdir(path)]
    links = subprocess.check_output(
        ["find", *search, "-type", "l", "-print0"], timeout=90)
    for raw in links.split(b"\0"):
        if raw:
            path = os.fsdecode(raw)
            target = os.path.realpath(path)
            # Internal dependency links don't make a retired build live.
            for base in builds:
                if inside(target, base) and not inside(path, base):
                    references[base].append("symlink: " + path)
    units = subprocess.check_output(
        ["systemctl", "show", "*.service", "-p", "ExecStart", "-p", "WorkingDirectory",
         "-p", "EnvironmentFiles", "-p", "RootDirectory"],
        universal_newlines=True, timeout=30)
    for base in builds:
        if base in units:
            references[base].append("systemd configuration")
    for directory in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            add("process cwd " + directory.name, os.path.realpath(str(directory / "cwd")))
            add("process exe " + directory.name, os.path.realpath(str(directory / "exe")))
            for descriptor in (directory / "fd").iterdir():
                add("process fd " + directory.name, os.path.realpath(str(descriptor)))
            for filename in ["maps", "cmdline", "mountinfo"]:
                text = (directory / filename).read_bytes().decode("utf-8", "replace")
                if filename == "cmdline" and package_manager_active(text):
                    for base in builds:
                        if base in RETIRED_STORES:
                            references[base].append("active pnpm process " + directory.name)
                for base in builds:
                    if base in text:
                        references[base].append("process " + filename + " " + directory.name)
        except FileNotFoundError:
            pass  # A process exited during inspection.
        except PermissionError:
            raise RuntimeError("Cannot inspect running process references")
    for parent in [pathlib.Path("/opt"), pathlib.Path("/srv")]:
        for journal in parent.glob("*/deployments.jsonl"):
            rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
            if rows:
                # Retain the latest deployment and its immediate rollback source.
                for key in ["release", "previous"]:
                    if rows[-1].get(key):
                        add("rollback journal: " + str(journal),
                            os.path.realpath(rows[-1][key]))
    return references

def retire_inactive_builds(root):
    with contextlib.ExitStack() as locks:
        # Exclude concurrent component switches while references are inspected.
        lock_paths = {pathlib.Path(root) / ".deploy.lock"}
        for parent in [pathlib.Path("/opt"), pathlib.Path("/srv")]:
            lock_paths.update(parent.glob("*/.deploy.lock"))
        for path in sorted(lock_paths):
            stream = locks.enter_context(path.open("a"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = shutil.disk_usage(str(root)).free
        whole_disk = disk_sizes("/", 2)
        # Bound the second pass too; timeout output is kept for incomplete diagnostics.
        major = disk_command(["du", "-x", "-s", "-B1", "/usr", "/opt", "/tmp",
                              "/root", "/srv", "/home", "/var"])
        candidates = RETIRED_BUILDS + RETIRED_STORES
        references = build_references(candidates)
        plan, caches, retained = [], [], []
        for base in candidates:
            if references[base]:
                retained.append(dict(path=base, references=references[base][:20]))
                continue
            try:
                dependencies = retired_store(base) if base in RETIRED_STORES else retired_dependencies(base)
            except RuntimeError as error:
                retained.append(dict(path=base, references=["validation: " + str(error)]))
                continue
            for path in dependencies:
                size = subprocess.check_output(["du", "-s", "-B1", path],
                                               universal_newlines=True, timeout=60)
                destination = caches if base in RETIRED_STORES else plan
                destination.append(dict(path=path, allocatedBytes=int(size.split()[0])))
        # Hardlinked pnpm files may remain in other copies or its content-addressed store.
        # Count the links being removed together, rather than equating du with free space.
        removals = plan + caches
        file_usage = dependency_file_usage([item["path"] for item in removals])
        print(json.dumps(dict(status="retirement_plan", freeBytes=before, wholeDisk=whole_disk,
                              majorDirectories=major, dependencies=plan, packageCaches=caches,
                              retained=retained,
                              **file_usage)), flush=True)
        # Refresh all live references immediately before removing dependencies.
        refreshed = build_references(candidates)
        if any(refreshed[base] for base in candidates
               if any(inside(item["path"], base) for item in removals)):
            raise RuntimeError("Retirement references changed during inspection")
        for item in caches:
            retired_store(item["path"])
        for item in removals:
            if pathlib.Path(item["path"]).is_symlink():
                raise RuntimeError("Dependency path changed to a symlink")
        for item in removals:
            shutil.rmtree(item["path"])
        report = dict(status="inactive_dependencies_retired", freeBeforeBytes=before,
                      freeAfterBytes=shutil.disk_usage(str(root)).free,
                      dependencies=plan, packageCaches=caches, retained=retained,
                      sourceAndDataPreserved=True,
                      timestamp=int(time.time()))
        journal = pathlib.Path(root) / "dependency-retirements.jsonl"
        with journal.open("a") as stream:
            stream.write(json.dumps(report) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(report), flush=True)
