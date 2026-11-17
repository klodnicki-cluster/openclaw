# OpenClaw

Private, Slack-connected OpenClaw for `klodnicki-cluster`. An `argocd/` Helm
bootstrap creates `Application/openclaw-runtime` in `infra-argocd`. The runtime
uses the official upstream Kustomize base, with local patches, not copied
workload manifests. Application state and the workspace share a retained 10Gi
RWO PVC in namespace `openclaw`.

Chat uses `openrouter/anthropic/claude-sonnet-4.6`; short internal utility tasks
use `openrouter/google/gemini-2.5-flash`. Scheduled jobs do **not** inherit the
utility model: explicitly choose Gemini Flash on every inexpensive job.
No jobs are created by this repository. The optional memory plugin and its
automatic nightly promotion job are disabled; persisted chat history is retained.

## Before You Start

You need Slack app-creation permission, an OpenRouter account with available
credit, this repository published on GitHub, and access to the intended cluster.
On your workstation, install Bash, `kubectl`, `jq`, `openssl`, and the GitHub CLI
(`gh`, for repository setup). Check `kubectl config current-context` carefully.
The cluster needs Argo CD in `infra-argocd`, its existing GitHub-topic
ApplicationSet, and the `hot-retain` StorageClass from `infra-btrfs-csi`.

The gateway makes outbound HTTPS/WebSocket connections to Slack and OpenRouter.
The init container also needs npm registry access on first install or upgrades.
There is no public ingress. Everyone in the selected Slack workspace can DM
the bot; invited channels require an explicit mention, including thread replies.
Other bots cannot trigger turns. DMs are isolated by sender. Group DMs are disabled.
Treat Slack members as trusted consumers of your API budget.

## 1. Create the Slack App

Open `https://api.slack.com/apps`. Select **Create New App -> From a manifest**,
select the intended workspace, and paste the contents of
[`slack/app-manifest.json`](slack/app-manifest.json) into the JSON editor. Review
the summary and create the app. Its name and bot display name are **OpenClaw**.

This manifest is derived from the pinned upstream Socket Mode example in
`docs/channels/slack/manifest-and-scopes.md`. It deliberately omits optional
Agent View, slash commands, files, pins, multi-person DMs, and user-token features.
No public callback URL or Slack signing secret is required for Socket Mode.

## 2. Review Slack Permissions and Events

In the app dashboard, confirm **Socket Mode -> Enable Socket Mode** is on.
Under **App Home**, enable the **Messages Tab** and **Allow users to send Slash
commands and messages from the messages tab** (Slack's wording may vary).
The manifest already requests writable messages. The optional Home tab is off.

The bot scopes have these purposes:

| Scope | Purpose |
| --- | --- |
| `app_mentions:read` | Receive mentions of the bot. |
| `channels:read`, `channels:history` | Identify and read joined public-channel context. |
| `groups:read`, `groups:history` | Identify and read invited private-channel context. |
| `im:read`, `im:history`, `im:write` | Read and open direct conversations. |
| `chat:write` | Send replies as the bot. |
| `reactions:read`, `reactions:write` | Read and apply message reactions. |
| `users:read` | Resolve Slack member identities for messaging. |

Under **Event Subscriptions**, the enabled bot events are `app_mention`,
`message.channels`, `message.groups`, and `message.im`. They deliver mentions
and message context; OpenClaw applies mention gating locally. The history scopes
can expose accessible channel context even when an unmentioned message does not
trigger a response. No `chat:write.public`, channel-join, administrative, or
user OAuth scopes are requested.

## 3. Generate the App Token

Go to **Basic Information -> App-Level Tokens -> Generate Token and Scopes**.
Give it a name such as `openclaw-socket`, add **`connections:write`**, and generate
it. Copy the value beginning **`xapp-`**. This is **`SLACK_APP_TOKEN`**, not the
bot token. Keep it private; do not paste it into Git, Slack, or an issue.

## 4. Install the App and Get the Bot Token

Open **OAuth & Permissions -> Install to Workspace**, review the scopes, and
authorize. Your organization may require a Slack administrator's approval
before installation or additional scopes are permitted. Copy the **Bot User
OAuth Token** beginning **`xoxb-`**. This is **`SLACK_BOT_TOKEN`**.

If you change scopes later, use **Reinstall to Workspace**, obtain administrator
approval again where required, and update the Kubernetes Secret. Changing a
manifest alone does not grant new permissions to an existing installation.
This setup uses static bot/app tokens, not rotating OAuth tokens or a user token.

## 5. Get an OpenRouter Key

Open `https://openrouter.ai/keys`, create an API key, and store it privately as
**`OPENROUTER_API_KEY`**. Give the key an appropriate budget limit. Ensure the
account has credit and access to **both** `anthropic/claude-sonnet-4.6` and
`google/gemini-2.5-flash`. Model availability, limits, and prices are controlled
by OpenRouter and its providers; check your account before starting.

## 6. Create the Kubernetes Secret

Run this **complete Bash block** on your workstation, not in a GitHub Actions log.
It prompts without echoing credentials, passes them to `kubectl` using private
temporary files instead of command-line literals, and never writes a Secret
manifest to the repository. Do not enable shell tracing (`set -x`).

The same block updates credentials later while **preserving the existing
gateway token**. A read error or missing token in an existing Secret aborts
instead of silently changing administrator credentials.

```bash
bash <<'BASH'
set -euo pipefail
set +x
umask 077
kubectl config current-context
kubectl create namespace openclaw --dry-run=client -o yaml | kubectl apply -f -
tmp="$(mktemp -d "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/openclaw-secrets.XXXXXXXX")"
trap 'unset SLACK_APP_TOKEN SLACK_BOT_TOKEN OPENROUTER_API_KEY existing; rm -rf "$tmp"' EXIT

IFS= read -r -s -p 'Slack app token (xapp-): ' SLACK_APP_TOKEN </dev/tty
printf '\n' >/dev/tty
IFS= read -r -s -p 'Slack bot token (xoxb-): ' SLACK_BOT_TOKEN </dev/tty
printf '\n' >/dev/tty
IFS= read -r -s -p 'OpenRouter API key: ' OPENROUTER_API_KEY </dev/tty
printf '\n' >/dev/tty
[[ "$SLACK_APP_TOKEN" == xapp-* && "$SLACK_BOT_TOKEN" == xoxb-* ]]
[[ -n "$OPENROUTER_API_KEY" ]]

existing="$(kubectl -n openclaw get secret openclaw-secrets --ignore-not-found -o json)"
if [[ -n "$existing" ]]; then
  printf '%s' "$existing" |
    jq -erj '.data.OPENCLAW_GATEWAY_TOKEN | select(type == "string" and length > 0) | @base64d' \
    > "$tmp/OPENCLAW_GATEWAY_TOKEN"
  test -s "$tmp/OPENCLAW_GATEWAY_TOKEN"
else
  openssl rand -hex 32 | tr -d '\n' > "$tmp/OPENCLAW_GATEWAY_TOKEN"
fi
printf '%s' "$SLACK_APP_TOKEN" > "$tmp/SLACK_APP_TOKEN"
printf '%s' "$SLACK_BOT_TOKEN" > "$tmp/SLACK_BOT_TOKEN"
printf '%s' "$OPENROUTER_API_KEY" > "$tmp/OPENROUTER_API_KEY"
kubectl -n openclaw create secret generic openclaw-secrets \
  --from-file=OPENCLAW_GATEWAY_TOKEN="$tmp/OPENCLAW_GATEWAY_TOKEN" \
  --from-file=SLACK_APP_TOKEN="$tmp/SLACK_APP_TOKEN" \
  --from-file=SLACK_BOT_TOKEN="$tmp/SLACK_BOT_TOKEN" \
  --from-file=OPENROUTER_API_KEY="$tmp/OPENROUTER_API_KEY" \
  --dry-run=client -o yaml |
  kubectl apply --server-side --field-manager=openclaw-credentials -f -
BASH
```

Keep credentials in a password manager. Kubernetes Secrets are base64-encoded,
not automatically encrypted at rest; use cluster encryption and limit Secret
RBAC. Do not commit Secrets, rendered credentials, backups, or `.env` files.
Resolve any server-side field-ownership conflict deliberately, without blindly
forcing ownership. This repository does not manage or prune this Secret.

## 7. Publish, Discover, and Synchronize

Publishing and live activation are manual actions. Commit and push this repository
to `klodnicki-cluster/openclaw` after reviewing the passing validation result.
Install Renovate and configure required checks as described below **before**
enabling checked automerging.

Make the repository discoverable by the existing ApplicationSet:

```bash
gh repo edit klodnicki-cluster/openclaw --add-topic argocd
kubectl -n infra-argocd get applications openclaw openclaw-runtime
```

The ApplicationSet creates **`Application/openclaw`**, rendering the small
`argocd/` Helm chart. That creates **`Application/openclaw-runtime`** in
`infra-argocd`, rendering `runtime/` into namespace `openclaw`. Both synchronize
automatically; runtime pruning and self-healing are enabled. If either is not
present yet, refresh the ApplicationSet in Argo CD and check repository access.
Use **Sync** first on `openclaw`, then `openclaw-runtime` in the Argo UI when
manual synchronization is needed.

Remote Kustomize bases require repository-server outbound access to GitHub.
The reference includes a 180-second Git fetch timeout for the large upstream
repository; configure Argo CD's repo-server execution and controller
reconciliation/RPC timeouts to exceed a cold build if your current defaults are
shorter. The Argo project must allow Applications in `infra-argocd` and runtime
resources in `openclaw`. Namespace creation must also be allowed.

```bash
kubectl -n infra-argocd get application openclaw-runtime \
  -o jsonpath='{.status.sync.status}{" "}{.status.health.status}{"\n"}'
kubectl -n openclaw get pods,pvc,service
kubectl -n openclaw rollout status deployment/openclaw --timeout=10m
kubectl -n openclaw logs deployment/openclaw -c init-config
kubectl -n openclaw logs deployment/openclaw -c gateway --tail=100
```

Expect one pod, a bound 10Gi PVC, a ClusterIP Service, and an authenticated
loopback listener. Initial plugin installation can take several minutes.
`Recreate` intentionally introduces downtime on upgrades so the old gateway
stops before migrations or plugin changes access the volume.

## 8. Send Your First Slack Messages

In Slack, use **Apps -> Add apps** (or search for **OpenClaw**) and open its
**Messages** tab. Send a DM such as `Hello, reply with one short sentence`.
No pairing code is needed because DMs are open to workspace members.

In a test channel, run `/invite @OpenClaw`. Send `@OpenClaw say hello`.
It should reply. Then send an ordinary, unmentioned message; it should not reply.
Thread follow-ups also need `@OpenClaw`. Invite it separately to each channel,
including private channels. The bot does not automatically join the workspace's
channels and cannot see a channel simply because someone mentions its name.

## 9. Troubleshoot Slack and Startup

| Symptom | What to check |
| --- | --- |
| `CreateContainerConfigError` / missing token | `kubectl -n openclaw describe pod <pod>`; all four Secret keys are required. Re-run step 6 without printing Secret contents. |
| No Messages tab or disabled composer | **App Home -> Messages Tab** and writable messages; reinstall the app if needed, reopen Slack, and confirm the app is installed in this workspace. |
| `missing_scope` | Compare **OAuth & Permissions** to step 2, obtain approval, reinstall, and rotate the bot token if it changes. |
| Socket Mode disconnected | Enable Socket Mode, check `xapp-` has `connections:write`, check `xoxb-` is from the same app/workspace, and allow outbound Slack HTTPS/WebSockets and DNS. |
| Channel mention has no reply | `/invite @OpenClaw`, check public/private history scopes and events, then send an explicit app mention. Bot-authored messages are intentionally ignored. |
| DM works but plain channel/thread text does not | Expected: explicit mentions are required, including replies to the bot's own threads. |
| Model/auth/billing error | Check OpenRouter key, budget, credit, and access to both configured models. Utility model success does not prove chat-model access. |
| Init fails on npm/plugin access | Inspect `init-config` logs, DNS/egress to npm, and matching release availability. A missing matching artifact must block an update. |
| Pod not ready but Slack is down | `/startupz` tests core startup, not Slack connectivity. Inspect Slack logs separately; do not replace JSON probes with status-only requests. |
| PVC Pending or pod unschedulable | Check `hot-retain`, CSI node health, capacity, and volume node affinity. Local storage cannot move between nodes automatically. |
| Argo render timeout | Check repo-server GitHub access and execution/RPC timeouts described in step 7. |

Do not post token-bearing logs or diagnostics publicly. `kubectl describe` shows
Secret references rather than values, but application logs may contain private
message context.

## 10. Operate, Schedule, and Recover

### Private Administration

Forward the pod's port, not a public ingress or the ClusterIP's network address:

```bash
kubectl -n openclaw port-forward --address 127.0.0.1 deployment/openclaw 18789:18789
```

Open `http://127.0.0.1:18789` on that workstation. In another terminal, obtain
the gateway token privately and enter it into the UI's gateway authentication
setting. Treat the following output as a password; do not screen-share it:

```bash
kubectl -n openclaw get secret openclaw-secrets \
  -o jsonpath='{.data.OPENCLAW_GATEWAY_TOKEN}' | base64 --decode
printf '\n'
```

Do not put the token in a URL, Git, or Slack. Stop port-forwarding when finished.
Loopback binding means the retained ClusterIP Service does not provide ordinary
pod-to-pod gateway access. Health endpoints are intentionally not authentication
tests; gateway administrative operations still require the token.

If the browser requests device pairing or shows **Limited access**, request admin
access in the UI, then review pending devices from the gateway. Approve only your
recognized browser's exact request ID, replacing `REQUEST_ID` below:

```bash
kubectl -n openclaw exec deployment/openclaw -c gateway -- \
  node /app/openclaw.mjs devices list
kubectl -n openclaw exec deployment/openclaw -c gateway -- \
  node /app/openclaw.mjs devices approve 'REQUEST_ID'
```

### Credentials and Git Configuration

Re-run step 6 for routine Slack/OpenRouter rotation. It retains the gateway
token. Restart afterward because environment-backed credentials do not update
inside running containers:

```bash
kubectl -n openclaw rollout restart deployment/openclaw
kubectl -n openclaw rollout status deployment/openclaw --timeout=10m
```

Argo self-healing may remove a manual rollout annotation, causing another
replacement; let reconciliation settle. To rotate the gateway token deliberately,
generate a new random value and update that key through your approved Secret
management workflow, then restart and reauthenticate administrator clients.

Edit `runtime/config/openclaw.json` and `runtime/config/AGENTS.md` in Git for
configuration changes. Generated ConfigMap name hashes change the pod template.
On every startup, the init container replaces **only** `openclaw.json` and the
workspace's `AGENTS.md`; UI configuration changes to those files are not durable.
Runtime migration bookkeeping is retained alongside Git-managed settings.
It installs or repairs the pinned Slack plugin idempotently. Plugin installation
records and capability consent live in retained state; conversations, other
workspace files, and automation definitions are not erased. The gateway keeps
the official image entrypoint, which runs startup repair/migrations before serving.

### Inexpensive Scheduled Jobs

Scheduling is enabled, but the repository creates **zero jobs**. In a Slack DM:

> Create a one-time agent-turn job for five minutes from now. Use
> `openrouter/google/gemini-2.5-flash` explicitly, with no model fallbacks, an
> isolated session, and a short timeout. Reply to this DM with "schedule test
> passed". Tell me the job ID and selected model.

Confirm the stored job really uses that model. `utilityModel` is **not** a cron
default or enforcement rule. The workspace instruction recommends Gemini, but
the model selection remains operator/user-controlled.

An administrator can also create a bounded one-shot test through the CLI
(replace `U0123456789` with your Slack member ID):

```bash
kubectl -n openclaw exec deployment/openclaw -c gateway -- \
  node /app/openclaw.mjs automations add \
  --name "Gemini smoke test" --at 5m --session isolated \
  --message 'Reply with only: schedule test passed' \
  --model openrouter/google/gemini-2.5-flash --fallbacks '' \
  --timeout-seconds 60 --announce --channel slack --to user:U0123456789
kubectl -n openclaw exec deployment/openclaw -c gateway -- \
  node /app/openclaw.mjs automations list --json
```

Read the resulting job ID and inspect `automations get <job-id>` and
`automations runs <job-id>` after execution. Remove the temporary job using
`automations remove <job-id>` if it remains. For recurring jobs, prefer daily or
weekly schedules and short prompts/timeouts; review OpenRouter usage afterward.
Jobs created through administrator CLI/UI have administrator authority: prefer
creating member-owned schedules in that member's Slack conversation.

Model turns can use only messaging and caller-scoped agent scheduling.
The deployment-level tool/action ceiling blocks filesystem, shell, browser,
cluster, node, gateway administration, and host command jobs even for configured
owners. `cron.triggers.enabled: false` also disables scripts, streams, and event
triggers. These controls do not restrict direct authorized human administration
or make shared Slack access a hostile multi-tenant security boundary.

### Backups and Retained-Volume Recovery

Back up the **entire** mounted `/home/node/.openclaw` tree, including SQLite
databases and plugin npm projects, not just the JSON config or workspace.
Use CSI snapshots only if your storage driver supports them, or an offline
file-level backup to a separate node/device. A same-node snapshot is not a
disaster-recovery backup. Encrypt backups and separately preserve the Secret
in a password manager or approved secrets system.

For a consistent offline backup or restore:

1. Disable automated sync/self-heal for `openclaw` and `openclaw-runtime` during
   maintenance; also prevent the ApplicationSet from restoring the parent policy.
   Stop the gateway by scaling the Deployment to zero and wait for pod termination.
2. Mount the PVC in an administrator-controlled maintenance pod, **not concurrently
   with the gateway**, and archive or restore the complete state tree. Preserve
   UID/GID `1000:1000`, private permissions, SQLite sidecars, and symlinks.
   Store the backup outside this repository and off the storage node.
3. Remove the maintenance pod, return the Deployment to one replica, then restore
   the ApplicationSet/Application policies. Watch rollout and validate Slack.
   Test restores periodically on an isolated copy with outbound integrations off.

The PVC has `Prune=false,Delete=false`; the runtime Application deliberately has
**no cascading-deletion finalizer**. These protections do not prevent manual PVC
or namespace deletion. `hot-retain` must use reclaim policy `Retain`.
If the PVC is accidentally deleted, leave the retained PV and underlying data
intact. Suspend reconciliation as above, identify the original PV, and have a
storage administrator clear its stale claim reference and recreate
`openclaw-home-pvc` explicitly bound to that PV (`spec.volumeName`) with the
original class, size, and RWO mode. Do not allow automatic provisioning of a new,
empty volume under that name. Restore from backup if the original node/data is lost.

This is **node-local** btrfs storage, not replicated storage or high availability.
PV node affinity ties scheduling to the storage node. A failed node can leave
the pod unavailable until it returns or an off-node backup is restored onto a
new volume. NOCOW (`btrfs.csi.local/cow: "false"`) disables data checksumming for
NOCOW files and prevents normal data compression; metadata remains checksummed.
The annotations request NOCOW and `compression: "none"` at provisioning time,
not an in-place conversion of existing populated files.

## Dependency Updates

Install the Renovate GitHub App for this repository and finish its onboarding.
Grant repository contents and pull-request read/write access and checks/status
read access (plus workflow-file update permission for Action upgrades, where
required by your installation). Enable Actions for PRs and your chosen merge
method in repository settings. Enable GitHub **Allow auto-merge**.

Create a branch ruleset/protection rule for `main` requiring a PR and the exact
**`validate`** status check, with the branch up to date before merging. Select
the check from GitHub Actions after the first successful workflow run. Do not
grant Renovate a bypass, and do not make the required workflow path-filtered.
If your rules require human approval, Renovate updates wait for that approval.

`renovate.json` extends `config:recommended`, automerges non-major updates only
with `validate`, and leaves major updates for review. A missing or failed check
must block merging. GitHub's required-check rule is essential for platform
automerge; repository files cannot configure it for you.

The built-in managers track remote Kustomize refs, the shared runtime image
tag/digest, GitHub Actions including SHA pins and Node, npm CI tooling, and Python requirements.
Narrow custom managers cover only `runtime/plugin.json` and `.github/tools.env`.
The base, image, and Slack plugin are grouped as **OpenClaw release**.
CI requires identical core/plugin versions and verifies the published image
tag resolves to the pinned digest, installs the exact npm plugin, and loads it
under that image. If one artifact has not been published or is incompatible,
leave the PR blocked; never merge a partial release to silence a failure.
Model IDs are intentionally outside dependency automation.

## Validation and Acceptance

CI's required `validate` job runs Helm lint/rendering, a real remote Kustomize
build, strict Kubernetes and Argo Application schema validation, explicit
resource/security/configuration assertions, rejection tests for unsafe changes,
ConfigMap rollout tests, and actual Renovate configuration validation/extraction.
Its pinned-container test uses **dummy credentials**, verifies repeat init and
Slack/OpenRouter runtime loading, then disconnects external networking before
launching the actual image entrypoint and checking JSON probes/token enforcement.
It does not contact your cluster or test real Slack/OpenRouter credentials.

To run locally on Linux amd64 with Python 3, Docker, Node 24, and npm:

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt
bash scripts/install-tools.sh
export PATH="$PWD/.artifacts/bin:$PWD/.github/validation/node_modules/.bin:$PATH"
npm install --prefix .github/validation --ignore-scripts --package-lock=false
npm rebuild --prefix .github/validation re2
.venv/bin/python scripts/validate.py
.venv/bin/python scripts/validate-renovate.py
.venv/bin/python scripts/test-runtime.py
```

Generated manifests and diagnostic logs stay ignored under `.artifacts/`.
Network access is needed for upstream manifests, release binaries/schemas,
container images, and npm. No live activation is part of repository validation.

After manual deployment, perform these live acceptance tests:

1. DM the bot as two different workspace users. Confirm both receive replies
   and one sender cannot recall a unique phrase from the other's DM.
2. Invite the bot into a test channel. Confirm explicit mentions reply,
   plain messages and unmentioned thread replies do not, and bot-authored
   mentions do not trigger a turn.
3. Tell the bot a unique phrase, replace the pod with
   `kubectl -n openclaw rollout restart deployment/openclaw`, wait for rollout,
   then ask for that phrase in the **same** DM. Verify conversation continuity.
4. Create the temporary Gemini Flash job described above. Inspect its explicit
   model and caller/destination, wait for delivery, inspect the run, and remove
   any remaining test job. Confirm its definition survives a pod replacement.
5. Ask for a shell command, file write, browser action, cluster action, another
   user's schedule, and host-executed automation. Confirm no such tool executes.

Publishing, required-check settings, Secret creation, storage provisioning,
Argo synchronization, and these live acceptance tests require operator action.
