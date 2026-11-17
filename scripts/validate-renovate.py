#!/usr/bin/env python3
"""Run Renovate itself and assert that no dependency declaration is invisible."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from validate import ARTIFACTS, ROOT, require


def main():
    os.chdir(ROOT)
    subprocess.run(["renovate-config-validator", "--strict", "renovate.json"], check=True)
    with tempfile.TemporaryDirectory(prefix="openclaw-renovate-") as tmp:
        fixture = Path(tmp) / "repo"
        shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns(
            ".git", ".artifacts", ".agents", ".codex", ".aws", "node_modules", "__pycache__", ".venv"))
        subprocess.run(["git", "init", "--initial-branch=main", str(fixture)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(fixture), "add", "."], check=True)
        subprocess.run(["git", "-C", str(fixture), "-c", "user.name=Validation",
                        "-c", "user.email=validation@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-m", "Extraction fixture"], check=True, capture_output=True)
        env = {**os.environ, "LOG_LEVEL": "trace", "LOG_FORMAT": "json",
               "RENOVATE_BASE_DIR": str(Path(tmp) / "renovate"),
               "RENOVATE_CACHE_DIR": str(Path(tmp) / "cache")}
        result = subprocess.run(["renovate", "--platform=local", "--dry-run=extract",
                                 "--onboarding=false", "--require-config=required"],
                                cwd=fixture, env=env, text=True, capture_output=True)
        ARTIFACTS.mkdir(exist_ok=True)
        output = result.stdout + result.stderr
        (ARTIFACTS / "renovate-extraction.jsonl").write_text(output)
        require(result.returncode == 0, "Renovate extraction failed; see .artifacts/renovate-extraction.jsonl")
        records = []
        for line in output.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        require(not any(r.get("level", 0) >= 50 for r in records), "Renovate logged an error")
        snapshots = [r["config"] for r in records if r.get("msg") == "packageFiles"]
        require(snapshots, "Renovate did not emit dependency extraction results")
        files = snapshots[-1]
        dependencies = [(manager, file["packageFile"], dep)
                        for manager, entries in files.items()
                        for file in entries for dep in file["deps"]]
        names = {d.get("depName") for _, _, d in dependencies}
        expected = {"openclaw/openclaw", "ghcr.io/openclaw/openclaw", "@openclaw/slack",
                    "actions/checkout", "actions/setup-node", "PyYAML", "renovate",
                    "helm/helm", "kubernetes-sigs/kustomize", "yannh/kubeconform",
                    "argoproj/argo-cd", "kubernetes/kubernetes", "node"}
        require(expected <= names, f"Untracked dependencies: {expected - names}")
        for manager, file, dep in dependencies:
            require(not dep.get("skipReason"), f"Skipped dependency in {manager}:{file}: {dep}")
        image = next(d for _, _, d in dependencies if d.get("depName") == "ghcr.io/openclaw/openclaw")
        require(image.get("currentDigest", "").startswith("sha256:"), "Gateway digest not extracted")
        for name in ("actions/checkout", "actions/setup-node"):
            action = next(d for _, _, d in dependencies if d.get("depName") == name)
            require(len(action.get("currentDigest", "")) == 40, f"{name} SHA not extracted")
        require(next(d for _, _, d in dependencies if d.get("depName") == "renovate")
                ["currentValue"] == json.loads((ROOT / ".github/validation/package.json").read_text())
                ["dependencies"]["renovate"], "Renovate self-pin untracked")
        require(not any("openrouter/" in (d.get("depName") or "") for _, _, d in dependencies),
                "Model IDs must remain operator-selected")
        print(f"PASS: Renovate configuration and {len(dependencies)} extracted dependencies")


if __name__ == "__main__":
    main()
