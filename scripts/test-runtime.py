#!/usr/bin/env python3
"""Exercise the pinned image with dummy secrets; no Kubernetes access required."""
import json
import os
import subprocess
import time
import uuid

import yaml

from validate import ARTIFACTS, ROOT, release, require


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main():
    os.chdir(ROOT)
    _, image = release()
    # Pull the tag too: an existing but mismatched tag/digest must block a release.
    tag = image.split("@")[0]
    docker("pull", tag)
    info = json.loads(docker("image", "inspect", tag, capture_output=True).stdout)[0]
    require(tag.rsplit(":", 1)[0] + "@" + image.split("@")[1] in info["RepoDigests"],
            "Published image tag does not match pinned digest")
    docker("pull", image)
    require(info["Config"]["Entrypoint"] == ["tini", "-s", "--", "node", "/app/docker-entrypoint.mjs"],
            "Official startup entrypoint changed; review migration contract")
    require(info["Config"]["User"] in ("node", "1000", "1000:1000"), "Image no longer defaults to non-root")
    docs = list(yaml.safe_load_all((ARTIFACTS / "runtime.yaml").read_text()))
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    gateway, = pod["containers"]
    spec = ARTIFACTS / "runtime-test.json"
    spec.write_text(json.dumps({
        "version": release()[0],
        "entrypoint": info["Config"]["Entrypoint"],
        "args": gateway["args"],
        "startupBudgetSeconds": gateway["startupProbe"]["failureThreshold"]
        * gateway["startupProbe"]["periodSeconds"],
        "probes": {name: gateway[name]["exec"]["command"]
                   for name in ("startupProbe", "readinessProbe", "livenessProbe")}
    }))
    name = "openclaw-validation-" + uuid.uuid4().hex[:12]
    env = {
        "OPENCLAW_GATEWAY_TOKEN": "dummy-gateway-token-for-ci-only-not-a-secret",
        "OPENROUTER_API_KEY": "dummy-openrouter-key-not-a-secret",
        "SLACK_APP_TOKEN": "xapp-dummy-not-a-secret",
        "SLACK_BOT_TOKEN": "xoxb-dummy-not-a-secret",
    }
    for item in pod["initContainers"][0]["env"]:
        if "value" in item:
            env[item["name"]] = item["value"]
    args = ["run", "--detach", "--name", name, "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--memory=2g", "--cpus=1",
            "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
            "--tmpfs=/home/node/.openclaw:rw,nosuid,nodev,uid=1000,gid=1000,mode=0700",
            "--mount", f"type=bind,src={ROOT / 'runtime'},dst=/bootstrap,readonly",
            "--mount", f"type=bind,src={ROOT / 'runtime/config'},dst=/config,readonly",
            "--mount", f"type=bind,src={ROOT / 'scripts'},dst=/tests,readonly",
            "--mount", f"type=bind,src={spec},dst=/test-spec.json,readonly"]
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    try:
        docker(*args, image, "node", "/tests/runtime-check.mjs", capture_output=True)
        deadline = time.monotonic() + 900
        disconnected = False
        while time.monotonic() < deadline:
            logs = docker("logs", name, capture_output=True).stdout
            if not disconnected and "BOOTSTRAP_READY_FOR_OFFLINE" in logs:
                docker("network", "disconnect", "bridge", name)
                docker("exec", name, "node", "-e",
                       "require('fs').writeFileSync('/tmp/offline-ready', '')")
                disconnected = True
            state = json.loads(docker("inspect", name, capture_output=True).stdout)[0]["State"]
            if not state["Running"]:
                require(state["ExitCode"] == 0 and disconnected and "RUNTIME_TEST_PASS" in logs,
                        "Pinned runtime smoke test failed; see .artifacts/runtime-test.log")
                print("PASS: version, plugin runtime, repeat init, persistence, probes, token auth, and entrypoint")
                return
            time.sleep(2)
        raise TimeoutError("Pinned runtime smoke test timed out")
    finally:
        log = subprocess.run(["docker", "logs", name], text=True, capture_output=True)
        (ARTIFACTS / "runtime-test.log").write_text(log.stdout + log.stderr)
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


if __name__ == "__main__":
    main()
