"""Read-only, bounded disk diagnostics for a blocked production release."""
import json, pathlib, shutil, subprocess

def disk_command(arguments):
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=60)
        return dict(output=result.stdout, error=result.stderr[:1000], exitCode=result.returncode)
    except (OSError, subprocess.TimeoutExpired) as error:
        output = getattr(error, "output", "") or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        return dict(output=output, error=type(error).__name__)

def disk_sizes(path, depth, limit=24):
    result = disk_command(["du", "-x", "-B1", "--max-depth=" + str(depth), str(path)])
    entries = []
    for line in result["output"].splitlines():
        size, separator, name = line.partition("\t")
        if separator and size.isdecimal():
            entries.append(dict(path=name, allocatedBytes=int(size)))
    return dict(error=result.get("error", ""), exitCode=result.get("exitCode"),
                largest=sorted(entries, key=lambda item: item["allocatedBytes"], reverse=True)[:limit])

def require_disk_space(root, required):
    usage = shutil.disk_usage(str(root))
    if usage.free >= required:
        return
    report = dict(status="insufficient_disk_space", root=str(root), requiredBytes=required,
                  freeBytes=usage.free, usedBytes=usage.used, totalBytes=usage.total,
                  deficitBytes=required-usage.free,
                  filesystems=disk_command(["df", "-B1", "-T"]),
                  inodes=disk_command(["df", "-i"]),
                  rootFilesystem=disk_sizes("/", 2),
                  releaseRoot=disk_sizes(root, 2))
    for name, path in [("backupDirectories", "/var/lib/zion-next/backups"),
                       ("rootCache", "/root/.cache"), ("temporaryDirectories", "/tmp"),
                       ("homeDirectories", "/home"), ("rootUserDirectories", "/root")]:
        if pathlib.Path(path).is_dir():
            report[name] = disk_sizes(path, 3 if name in ("homeDirectories", "rootUserDirectories") else 1)
    pointers = {}
    for base in ["/opt", "/srv"]:
        for path in pathlib.Path(base).glob("*/current"):
            if path.is_symlink():
                pointers[str(path)] = str(path.resolve())
    report["currentPointers"] = pointers
    print(json.dumps(report), flush=True)
    raise RuntimeError("Insufficient disk space before downloading release: "
                       "required {} bytes, available {} bytes".format(required, usage.free))
