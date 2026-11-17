#!/usr/bin/env python3
"""Fail closed on upstream workload, security, and configuration drift."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts"
IMAGE_NAME = "ghcr.io/openclaw/openclaw"
CHAT = "openrouter/anthropic/claude-sonnet-4.6"
UTILITY = "openrouter/google/gemini-2.5-flash"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, **kwargs):
    try:
        return subprocess.run(args, check=True, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr)
        raise


def tools():
    return dict(line.split("=", 1) for line in (ROOT / ".github/tools.env").read_text().splitlines()
                if line and not line.startswith("#"))


def release():
    k = yaml.safe_load((ROOT / "runtime/kustomization.yaml").read_text())
    require(len(k["resources"]) == 1, "Only the upstream base may supply workloads")
    resource = k["resources"][0]
    require(resource.startswith("github.com/openclaw/openclaw//scripts/k8s/manifests?ref=v"),
            "Unexpected upstream base")
    version = parse_qs(urlsplit("https://" + resource).query)["ref"][0].removeprefix("v")
    require(re.fullmatch(r"\d+\.\d+\.\d+(?:-\d+)?", version), "Base must be a release tag")
    image, = k["images"]
    require(image["name"] == IMAGE_NAME and "digest" not in image, "Unexpected image override")
    tag = image["newTag"]
    require(re.fullmatch(re.escape(version) + r"-slim@sha256:[0-9a-f]{64}", tag),
            "Base and image must match, with a version and digest")
    plugin = json.loads((ROOT / "runtime/plugin.json").read_text())
    require(plugin == {"name": "@openclaw/slack", "version": version},
            "Slack plugin and upstream release must match")
    return version, f"{IMAGE_NAME}:{tag}"


def check_bootstrap(documents):
    require(len(documents) == 1, "Bootstrap must render only the runtime Application")
    app = documents[0]
    require((app["apiVersion"], app["kind"], app["metadata"]["name"],
             app["metadata"]["namespace"]) ==
            ("argoproj.io/v1alpha1", "Application", "openclaw-runtime", "infra-argocd"),
            "Unexpected bootstrap resource")
    require(not app["metadata"].get("finalizers"), "Cascading deletion is forbidden")
    spec = app["spec"]
    require(spec["source"] == {
        "repoURL": "https://github.com/klodnicki-cluster/openclaw.git",
        "targetRevision": "HEAD", "path": "runtime"}, "Unexpected runtime source")
    require(spec["destination"] == {
        "server": "https://kubernetes.default.svc", "namespace": "openclaw"},
        "Unexpected runtime destination")
    require(spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True},
            "Automated reconciliation required")
    require("CreateNamespace=true" in spec["syncPolicy"]["syncOptions"], "Namespace creation required")


def check_runtime(documents):
    _, image = release()
    expected = {("Deployment", "openclaw"), ("Service", "openclaw"),
                ("PersistentVolumeClaim", "openclaw-home-pvc")}
    objects = {}
    configmaps = {}
    for obj in documents:
        kind, meta = obj["kind"], obj["metadata"]
        require(meta.get("namespace") == "openclaw", "Unexpected namespace")
        require(not meta.get("finalizers"), "Unexpected resource finalizer")
        require("ownerReferences" not in meta, "Unexpected resource ownership")
        key = kind, meta["name"]
        require(key not in objects, "Duplicate resource")
        objects[key] = obj
        if kind == "ConfigMap":
            require(re.fullmatch(r"openclaw-(?:config-git|bootstrap)-[a-z0-9]{10}", meta["name"]),
                    "Only hashed configuration ConfigMaps are permitted")
            require(not obj.get("binaryData"), "Unexpected binary configuration")
            configmaps[meta["name"].rsplit("-", 1)[0]] = obj
        else:
            require(key in expected, f"Unexpected resource: {key}; Secrets/public endpoints forbidden")
    require(len(objects) == 5 and set(configmaps) == {"openclaw-config-git", "openclaw-bootstrap"},
            "Unexpected runtime inventory")
    require(expected <= objects.keys(), "Missing upstream resources")
    svc = objects["Service", "openclaw"]["spec"]
    require(svc.get("type", "ClusterIP") == "ClusterIP", "Public services forbidden")
    require(not any(svc.get(k) for k in ("externalIPs", "externalName", "loadBalancerIP",
                                        "loadBalancerClass")), "External service routing forbidden")
    require(svc["ports"] == [{"name": "gateway", "port": 18789, "targetPort": 18789,
                             "protocol": "TCP"}], "Unexpected service ports")
    pvc = objects["PersistentVolumeClaim", "openclaw-home-pvc"]
    require(pvc["spec"] == {"accessModes": ["ReadWriteOnce"], "storageClassName": "hot-retain",
                            "resources": {"requests": {"storage": "10Gi"}}}, "Unexpected PVC settings")
    annotations = pvc["metadata"]["annotations"]
    require(annotations.get("btrfs.csi.local/cow") == "false"
            and annotations.get("btrfs.csi.local/compression") == "none", "PVC tuning required")
    require(set(annotations.get("argocd.argoproj.io/sync-options", "").split(",")) ==
            {"Prune=false", "Delete=false"}, "PVC deletion/prune protection required")
    deploy = objects["Deployment", "openclaw"]["spec"]
    require(deploy["replicas"] == 1 and deploy["strategy"] == {"type": "Recreate"},
            "State must have exclusive access during activation")
    pod = deploy["template"]["spec"]
    require(pod["automountServiceAccountToken"] is False, "Service-account mounting forbidden")
    require(not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace")),
            "Host access forbidden")
    require(not pod.get("serviceAccountName"), "Custom cluster identity forbidden")
    require(pod["securityContext"].get("fsGroup") == 1000
            and pod["securityContext"].get("seccompProfile") == {"type": "RuntimeDefault"},
            "Pod security changed")
    require(len(pod["containers"]) == 1 and len(pod["initContainers"]) == 1,
            "Sidecars/extra init containers require review")
    volumes = {v["name"]: v for v in pod["volumes"]}
    require(set(volumes) == {"openclaw-home", "config", "bootstrap", "tmp-volume"},
            "Unexpected volume")
    require(volumes["openclaw-home"].get("persistentVolumeClaim") ==
            {"claimName": "openclaw-home-pvc"}, "State PVC missing")
    require(volumes["tmp-volume"] == {"name": "tmp-volume", "emptyDir": {}}, "Unexpected temp volume")
    for name in ("config", "bootstrap"):
        key = "openclaw-config-git" if name == "config" else "openclaw-bootstrap"
        require(volumes[name].get("configMap") ==
                {"name": configmaps[key]["metadata"]["name"]}, "Config rollout reference missing")
    for container in pod["initContainers"] + pod["containers"]:
        require(container["image"] == image, "Every runtime image must share the pin")
        require(container["resources"] == {
            "requests": {"cpu": "250m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "2Gi"}}, "Resource budget changed")
        require(container["securityContext"] == {
            "runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}, "Container privilege escalation")
        require(not container.get("envFrom"), "Bulk environment injection forbidden")
        for env in container["env"]:
            if "valueFrom" in env:
                ref = env["valueFrom"].get("secretKeyRef", {})
                require(ref.get("name") == "openclaw-secrets" and ref.get("key") == env["name"]
                        and not ref.get("optional", False), "Unexpected/optional credential")
        secret_keys = {e["name"] for e in container["env"] if "valueFrom" in e}
        require(secret_keys == {"OPENCLAW_GATEWAY_TOKEN", "OPENROUTER_API_KEY",
                                "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"}, "Missing/extra credentials")
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        require(set(mounts) <= volumes.keys(), "Unknown mount")
        require(mounts["openclaw-home"]["mountPath"] == "/home/node/.openclaw",
                "State mount changed")
        require(not any(m.get("subPath") or m.get("mountPropagation") for m in mounts.values()),
                "Unexpected mount semantics")
        require(not any(p.get("hostPort") for p in container.get("ports", [])), "Host port forbidden")
    init, = pod["initContainers"]
    require(init["command"] == ["node", "/bootstrap/bootstrap.mjs"], "Bootstrap script bypassed")
    require({m["name"] for m in init["volumeMounts"]} == set(volumes), "Init mounts changed")
    gateway, = pod["containers"]
    require(not gateway.get("command"), "Official image entrypoint must run migrations")
    require(gateway["args"] == ["node", "openclaw.mjs", "gateway", "run"], "Gateway command changed")
    for name in ("startupProbe", "readinessProbe", "livenessProbe"):
        probe = gateway[name]
        command = probe.get("exec", {}).get("command", [])
        endpoint = "/healthz" if name == "livenessProbe" else "/startupz"
        require(command[:2] == ["node", "-e"] and len(command) == 3,
                "Probes must validate JSON, not status-only HTTP checks")
        code = command[2]
        require(endpoint in code and "JSON.parse" in code and "=== 200" in code
                and ".ok === true" in code, "Probe contract changed")
        if name != "livenessProbe":
            require(".status === 'started'" in code, "Startup admission contract changed")
    config = json.loads(configmaps["openclaw-config-git"]["data"]["openclaw.json"])
    require(config == json.loads((ROOT / "runtime/config/openclaw.json").read_text()),
            "Rendered config diverges from Git")
    require(config["gateway"] == {
        "mode": "local", "bind": "loopback", "port": 18789, "auth": {"mode": "token"},
        "controlUi": {"enabled": True}}, "Loopback/token authentication required")
    defaults = config["agents"]["defaults"]
    require(defaults["model"] == CHAT and defaults["utilityModel"] == UTILITY,
            "Models must remain operator-selected")
    slack = config["channels"]["slack"]
    require(slack["mode"] == "socket" and slack["dmPolicy"] == "open"
            and slack["allowFrom"] == ["*"] and slack["groupPolicy"] == "open",
            "Workspace Slack access changed")
    require(slack["allowBots"] is False and slack["joinIntro"] is False
            and slack["requireMentionInBotThreads"] is True
            and slack["implicitMentions"] == {"replyToBot": False, "threadParticipation": False}
            and slack["channels"] == {"*": {"requireMention": True, "allowBots": False}},
            "Strict mention/bot gating required")
    require(config["session"]["dmScope"] == "per-channel-peer", "DM isolation required")
    require(config["cron"] == {"enabled": True, "triggers": {"enabled": False}},
            "Agent scheduling only; host scripts/triggers forbidden")
    require(config["plugins"]["slots"]["memory"] == "none"
            and config["plugins"]["entries"]["memory-core"] == {
                "enabled": False, "config": {"dreaming": {"enabled": False}}},
            "Automatic memory jobs must remain disabled")
    policy = config["tools"]["gatewayPolicy"]
    require(config["tools"]["allow"] == ["message", "automations"]
            and policy["allow"] == ["message", "automations"]
            and policy["automations"] == {"scope": "caller", "allowCommandJobs": False},
            "Messaging/caller scheduling ceiling changed")
    require(set(policy["actions"]) == {"message", "automations"}
            and set(policy["actions"]["message"]) == {"send", "read", "edit", "delete", "react", "reactions"}
            and set(policy["actions"]["automations"]) == {
                "status", "list", "get", "add", "update", "remove", "run", "runs",
                "next_check", "scratch_get", "scratch_set", "record_result"},
            "Tool action ceiling changed")
    require(config["tools"]["elevated"] == {"enabled": False}
            and config["tools"]["exec"] == {"security": "deny"}
            and config["browser"] == {"enabled": False}
            and all(value is False for value in config["commands"].values()),
            "Administrative/host tools forbidden")
    require(configmaps["openclaw-bootstrap"]["data"]["bootstrap.mjs"] ==
            (ROOT / "runtime/bootstrap.mjs").read_text(), "Bootstrap code drift")
    require(json.loads(configmaps["openclaw-bootstrap"]["data"]["plugin.json"]) ==
            json.loads((ROOT / "runtime/plugin.json").read_text()), "Plugin release drift")
    return pod


def check_rollout(documents):
    original = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp) / "runtime"
        shutil.copytree(ROOT / "runtime", overlay)
        # Reuse the real rendered inventory for hash tests; do not clone upstream four more times.
        base = copy.deepcopy([d for d in documents if d["kind"] != "ConfigMap"])
        names = {d["metadata"]["name"]: d["metadata"]["name"].rsplit("-", 1)[0]
                 for d in documents if d["kind"] == "ConfigMap"}
        for obj in base:
            obj["metadata"].pop("namespace", None)
            if obj["kind"] == "Deployment":
                for volume in obj["spec"]["template"]["spec"]["volumes"]:
                    if "configMap" in volume:
                        volume["configMap"]["name"] = names[volume["configMap"]["name"]]
        (overlay / "base.yaml").write_text(yaml.safe_dump_all(base))
        kpath = overlay / "kustomization.yaml"
        k = yaml.safe_load(kpath.read_text())
        k["resources"] = ["base.yaml"]
        k["patches"] = [p for p in k["patches"] if p["path"] != "configmap.patch.yaml"]
        kpath.write_text(yaml.safe_dump(k))
        for file in ("config/openclaw.json", "config/AGENTS.md", "plugin.json", "bootstrap.mjs"):
            target = overlay / file
            previous = target.read_text()
            target.write_text(previous + "\n")
            changed = run("kustomize", "build", str(overlay), capture_output=True).stdout
            pod = next(d for d in yaml.safe_load_all(changed) if d["kind"] == "Deployment")["spec"]["template"]
            require(pod != original, f"{file} must change the pod template")
            target.write_text(previous)


def check_rejections(documents):
    """Exercise the policy guard with representative hostile upstream changes."""
    mutations = [
        lambda docs: docs.append({"apiVersion": "v1", "kind": "Secret",
                                  "metadata": {"name": "forbidden", "namespace": "openclaw"}}),
        lambda docs: next(d for d in docs if d["kind"] == "Service")["spec"].update(type="LoadBalancer"),
        lambda docs: next(d for d in docs if d["kind"] == "Deployment")["spec"].update(replicas=2),
        lambda docs: next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        ["containers"][0]["securityContext"].update(privileged=True),
        lambda docs: next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
        ["metadata"]["annotations"].pop("argocd.argoproj.io/sync-options"),
    ]
    for mutate in mutations:
        changed = copy.deepcopy(documents)
        mutate(changed)
        try:
            check_runtime(changed)
        except ValueError:
            continue
        raise ValueError("Policy guard accepted an unsafe mutation")


def main():
    os.chdir(ROOT)
    os.environ["PATH"] = f"{ARTIFACTS / 'bin'}:{os.environ['PATH']}"
    ARTIFACTS.mkdir(exist_ok=True)
    release()
    run("helm", "lint", "argocd", "--strict")
    bootstrap = run("helm", "template", "openclaw", "argocd", capture_output=True).stdout
    runtime = run("kustomize", "build", "runtime", capture_output=True).stdout
    (ARTIFACTS / "bootstrap.yaml").write_text(bootstrap)
    (ARTIFACTS / "runtime.yaml").write_text(runtime)
    bootstrap_docs = list(yaml.safe_load_all(bootstrap))
    runtime_docs = list(yaml.safe_load_all(runtime))
    check_bootstrap(bootstrap_docs)
    check_runtime(runtime_docs)
    check_rejections(runtime_docs)
    crd = yaml.safe_load((ARTIFACTS / "schemas/application-crd.yaml").read_text())
    version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
    schema_dir = ARTIFACTS / "schemas/argoproj.io"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / "application_v1alpha1.json").write_text(
        json.dumps(version["schema"]["openAPIV3Schema"]))
    run("kubeconform", "-strict", "-summary", "-kubernetes-version", tools()["KUBERNETES_VERSION"],
        "-schema-location", "default", "-schema-location",
        str(ARTIFACTS / "schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),
        str(ARTIFACTS / "bootstrap.yaml"), str(ARTIFACTS / "runtime.yaml"))
    check_rollout(runtime_docs)
    print("PASS: rendered inventory, schemas, security, compatibility, negative guards, and rollout hashes")


if __name__ == "__main__":
    main()
