"""Deploy one green CI component from its own repository using GitHub OIDC."""
import base64, json, os, pathlib, shutil, subprocess, sys, tempfile, time
from cloud import oss, request, rpc, python_command
from artifact import stage_artifact

component, run_id = sys.argv[1:3]
repository = os.environ["GITHUB_REPOSITORY"]
if os.environ["GITHUB_REF"] != "refs/heads/main" or not run_id.isdecimal():
    raise RuntimeError("Production releases must be dispatched from main with a numeric CI run ID")
config = json.loads(os.environ["DEPLOY_CONFIG"])
target = config["components"][component]
headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"}
api = "https://api.github.com/repos/" + repository
run = json.loads(request(api + "/actions/runs/" + run_id, headers=headers))
workflow = json.loads(request(api + "/actions/workflows/" + str(run["workflow_id"]), headers=headers))
if (run["status"] != "completed" or run["conclusion"] != "success" or
        run["head_branch"] != "main" or run["event"] not in ("push", "workflow_dispatch") or
        workflow["path"] not in target["ciWorkflows"]):
    raise RuntimeError("Selected CI run is not a successful main-branch product build")
sha = run["head_sha"]
artifact_name = repository.split("/")[1] + "-" + component + "-" + sha
with tempfile.TemporaryDirectory() as directory:
    subprocess.check_call(["gh", "run", "download", run_id, "--repo", repository,
                           "--name", artifact_name, "--dir", directory])
    folder = pathlib.Path(directory)
    if sorted(p.name for p in folder.iterdir()) != ["manifest.json", "release.tgz"]:
        raise RuntimeError("Unexpected artifact contents")
    manifest = json.loads((folder / "manifest.json").read_text())
    if (manifest["repository"] != repository or manifest["commit"] != sha or
            str(manifest["ciRun"]) != run_id or manifest["component"] != component):
        raise RuntimeError("CI artifact provenance mismatch")
    from deploy import digest
    archive = folder / "release.tgz"
    if digest(archive) != manifest["archiveSha256"] or archive.stat().st_size != manifest["archiveBytes"]:
        raise RuntimeError("CI artifact integrity mismatch")
    # Request short-lived credentials only after the artifact passes validation.
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.aliyuncs.com"
    oidc = json.loads(request(oidc_url, headers={
        "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}))["value"]
    credentials = rpc("sts.cn-shanghai.aliyuncs.com", "2015-04-01", "AssumeRoleWithOIDC", dict(
        RoleArn=os.environ["DEPLOY_ROLE_ARN"], OIDCProviderArn=os.environ["DEPLOY_OIDC_PROVIDER_ARN"],
        OIDCToken=oidc, RoleSessionName="github-" + os.environ["GITHUB_RUN_ID"], DurationSeconds=3600))["Credentials"]
    prefix = "releases/" + repository + "/" + sha + "/" + run_id + "/" + component
    bucket = os.environ["DEPLOY_BUCKET"]
    artifact_status = stage_artifact(bucket, prefix, credentials, manifest, archive)
    print(json.dumps(dict(status="release_artifact_" + artifact_status,
                          component=component, commit=sha, ciRun=run_id)), flush=True)
    script_dir = pathlib.Path(__file__).parent
    remote = ((script_dir / "cloud.py").read_text() + "\n" +
              (script_dir / "disk.py").read_text() + "\n" +
              (script_dir / "retire.py").read_text() + "\n" +
              (script_dir / "deploy.py").read_text())
    remote += "\nsettings = " + repr(dict(bucket=bucket, key=prefix, manifest=manifest,
                                           target=target, checkOnly=os.environ.get("CHECK_ONLY") == "true",
                                           retire=os.environ.get("RETIRE_INACTIVE_BUILDS") == "true")) + "\n"
    remote += """
credentials = instance_credentials()
expected = settings["manifest"]
root = pathlib.Path(settings["target"]["root"])
root.mkdir(parents=True, exist_ok=True)
if settings["retire"]:
    retire_inactive_builds(root)
needed = expected["expandedBytes"] + expected["archiveBytes"] + 512*1024*1024
require_disk_space(root, needed)
downloaded = json.loads(oss(settings["bucket"], settings["key"] + "/manifest.json", credentials))
if downloaded != expected:
    raise RuntimeError("Stored manifest mismatch")
incoming = root / (".incoming-" + expected["commit"] + "-" + str(expected["ciRun"]))
if incoming.exists():
    raise RuntimeError("Prior incoming archive requires reconciliation")
try:
    incoming.write_bytes(oss(settings["bucket"], settings["key"] + "/release.tgz", credentials))
    deploy(incoming, expected, settings["target"], settings["checkOnly"])
finally:
    if incoming.exists():
        incoming.unlink()
"""
    command = python_command(remote)
    response = rpc("ecs.cn-shanghai.aliyuncs.com", "2014-05-26", "RunCommand", dict(
        RegionId="cn-shanghai", **{"InstanceId.1": os.environ["DEPLOY_INSTANCE_ID"]},
        CommandContent=base64.b64encode(command.encode()).decode(), ContentEncoding="Base64",
        Type="RunShellScript", Name="zioon-" + component + "-" + run_id,
        Timeout=600, KeepCommand="false"), credentials)
    invocation = response["InvokeId"]
    print(json.dumps(dict(invocationId=invocation, repository=repository,
                          component=component, commit=sha, ciRun=run_id)))
    # Once dispatch returns an ID, never dispatch again on a polling error.
    for attempt in range(200):
        result = rpc("ecs.cn-shanghai.aliyuncs.com", "2014-05-26", "DescribeInvocationResults", dict(
            RegionId="cn-shanghai", InvokeId=invocation, InstanceId=os.environ["DEPLOY_INSTANCE_ID"],
            ContentEncoding="PlainText"), credentials)
        results = result.get("Invocation", {}).get("InvocationResults", {}).get("InvocationResult", [])
        if results and results[0]["InvocationStatus"] in ("Success", "Failed", "Stopped", "PartialFailed", "Finished"):
            outcome = results[0]
            print(outcome.get("Output", ""))
            if outcome["InvocationStatus"] != "Success" or int(outcome["ExitCode"]) != 0:
                raise RuntimeError("Deployment failed; inspect its durable journal and invocation " + invocation)
            break
        time.sleep(3)
    else:
        raise RuntimeError("Deployment outcome unknown; reconcile invocation " + invocation + " before retrying")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write("Released `{}` / `{}` from CI [{}]({}/actions/runs/{}), commit `{}`.\n\nECS invocation: `{}`.\n".format(
                repository, component, run_id, "https://github.com/" + repository, run_id, sha, invocation))
