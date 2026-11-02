"""Package a prepared component. CI installs/builds dependencies before this step."""
import argparse, hashlib, json, os, pathlib, subprocess, tarfile

def package(stage, output, product, component):
    stage, output = pathlib.Path(stage).resolve(), pathlib.Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sha = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], universal_newlines=True).strip()
    run = os.environ.get("GITHUB_RUN_ID", "local")
    files = sorted(stage.rglob("*"))
    for path in files:
        if path.is_symlink() and not str(path.resolve()).startswith(str(stage) + os.sep):
            raise ValueError("Release symlink escapes staging directory: " + str(path))
        if not (path.is_file() or path.is_dir() or path.is_symlink()):
            raise ValueError("Unsupported release entry")
    archive = output / "release.tgz"
    with tarfile.open(str(archive), "w:gz") as tar:
        for path in files:
            tar.add(str(path), arcname=path.relative_to(stage).as_posix(), recursive=False)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    manifest = dict(schema=1, product=product, component=component, commit=sha,
                    ciRun=run, repository="zioon-tech/" + product,
                    archiveSha256=digest.hexdigest(), archiveBytes=archive.stat().st_size,
                    expandedBytes=sum(p.stat().st_size for p in files if p.is_file() and not p.is_symlink()),
                    entries=len(files), platform="linux-x64")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("stage", "output", "product", "component"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    package(args.stage, args.output, args.product, args.component)
