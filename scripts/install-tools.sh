#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .github/tools.env
bin="${1:-$PWD/.artifacts/bin}"
mkdir -p "$bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fetch() { curl --fail --silent --show-error --location --retry 3 "$1" -o "$2"; }

fetch "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" "$tmp/helm.tgz"
fetch "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" "$tmp/helm.sha256"
printf '%s  %s\n' "$(awk '{print $1}' "$tmp/helm.sha256")" "$tmp/helm.tgz" | sha256sum --check
tar -xzf "$tmp/helm.tgz" -C "$tmp"
install "$tmp/linux-amd64/helm" "$bin/helm"

for tool in kustomize kubeconform; do
  if [[ "$tool" == kustomize ]]; then
    version="$KUSTOMIZE_VERSION"
    archive="kustomize_v${version}_linux_amd64.tar.gz"
    base="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v${version}"
    sums="checksums.txt"
  else
    version="$KUBECONFORM_VERSION"
    archive="kubeconform-linux-amd64.tar.gz"
    base="https://github.com/yannh/kubeconform/releases/download/v${version}"
    sums="CHECKSUMS"
  fi
  fetch "$base/$archive" "$tmp/$archive"
  fetch "$base/$sums" "$tmp/$sums"
  expected="$(awk -v archive="$archive" '$2 == archive || $2 == "*" archive {print $1}' "$tmp/$sums")"
  test -n "$expected"
  printf '%s  %s\n' "$expected" "$tmp/$archive" | sha256sum --check
  tar -xzf "$tmp/$archive" -C "$tmp" "$tool"
  install "$tmp/$tool" "$bin/$tool"
done
mkdir -p .artifacts/schemas
fetch "https://raw.githubusercontent.com/argoproj/argo-cd/v${ARGOCD_VERSION}/manifests/crds/application-crd.yaml" .artifacts/schemas/application-crd.yaml
printf 'Tools installed in %s\n' "$bin"
