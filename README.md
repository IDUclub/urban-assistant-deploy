# Urban Assistant Deploy

[Русская версия](README.ru.md) | English

Private GitOps repository for the Urban Assistant platform. Git is the source
of truth; GitHub Actions never receives a kubeconfig and does not connect to the
control plane. Argo CD reconciles reviewed commits from `main` into Kubernetes.

The active environment is `dev`. `environments/prod` is an intentionally
inactive skeleton for a future, separate production cluster and Argo CD
instance.

## Delivery flow

```text
push to an application's protected dev branch
  -> tests
  -> build and push dev-<full-sha>
  -> capture docker/build-push-action sha256 digest
  -> repository_dispatch(promote-dev-image)
  -> validate services.yaml allowlist and current dev HEAD
  -> verify every digest in the on-prem registry
  -> bot PR updating all release digests atomically
  -> required validation and squash auto-merge
  -> Argo CD sync, PreSync migration hook, rolling update
```

Legacy Compose workflows remain in application repositories during the gradual
cutover. Disable each one only after its Kubernetes deploy and Git revert have
been tested.

## Repository layout

```text
apps/<service>/base/                  environment-neutral workloads
environments/dev/apps/<service>/     dev config, endpoints and image digests
environments/dev/prerequisites/      Vault secrets, Redis and PVC prerequisites
environments/dev/platform/           dev platform overlays
environments/prod/                   inactive production skeleton
cluster/ and platform/               shared cluster/platform resources
operators/                           pinned Helm releases and values
argocd/bootstrap/                    one-time chart values and root Application
argocd/adoption/                     manual sync, no automated prune
argocd/root/                         steady-state auto-sync/self-heal/prune
scripts/                              render, validation and promotion helpers
ci-templates/                         thin application workflow caller example
services.yaml                         machine-readable delivery allowlist
vault-contract.yaml                   complete required Vault keys by path
```

The complete dev render is deterministic and needs no `.env` file:

```bash
./scripts/render.sh environments/dev > /tmp/urban-assistant-dev.yaml
python3 scripts/validate.py --root .
```

First-party workload images use logical names in bases. Every dev overlay maps
them to the current registry repository and an exact `sha256` digest. `latest`
is forbidden. The `${pvc.metadata...}` text in the StorageClass is an NFS CSI
template, not an unresolved environment variable.

## Secrets and configuration

Internal Kubernetes Service DNS names belong in Git because they are part of the
desired cluster topology. External runtime IP addresses and URLs belong in
Vault even when they contain no credentials. `vault-contract.yaml` is the
machine-readable inventory of every key read from Vault, including credentials,
TLS material and external endpoints. Validation requires an exact match between
the contract and every `get .Secrets` reference, rejects Vault-only variables in
ConfigMaps, dotenv payloads and literal workload environment values, and rejects
literal external endpoints in `VaultStaticSecret` templates. The repository
must never contain tokens, private keys, TLS key material, `Secret.data` or
`Secret.stringData`.

Vault Secrets Operator reads credentials from the external Vault and
materializes Kubernetes Secrets. Committed `VaultStaticSecret` resources contain
only paths and templates. GitHub App private keys and optional registry
credentials are GitHub Secrets. Frontend build-time values cannot be read by
Argo CD or Vault Secrets Operator, so `MAPBOX_PUBLIC_TOKEN`,
`FRONTEND_KEYCLOAK_AUTH_URL` and `FRONTEND_KEYCLOAK_LOGOUT_REDIRECT` are
protected GitHub Actions secrets. Only non-endpoint Vite config is versioned in
`environments/dev/build/frontend.env`.

GitHub Secrets are available to CI but not to Argo CD's normal Kustomize render.
The image registry hostname is therefore part of the desired `image` reference
and remains committed to Git until an internal DNS name is available; only
registry credentials are secret. Other deliberate literals are internal
Kubernetes service discovery, the `VaultConnection` bootstrap address, the
Kubernetes API audience and local `0.0.0.0` bind/listener addresses. Moving the
Vault bootstrap address into Vault would create a circular dependency.

## Argo CD

The pinned bootstrap release is Helm chart `argo-cd 10.4.1`, application
`v3.5.2`. `scripts/bootstrap-argocd.sh` refuses Kubernetes older than 1.25.
The current cluster baseline is validated against Kubernetes `1.36.3`.

Argo CD's server remains `ClusterIP`; use VPN plus port-forward:

```bash
kubectl port-forward -n argocd svc/argocd-server 8080:443
```

An ApplicationSet creates one Application per service. Separate Applications
own cluster foundation, operators, Vault integration, monitoring, Kafka,
Gateway and migration prerequisites. Operator Applications are generated from
`operators/releases.yaml` and use upstream charts plus values from this Git
repository via multiple sources.

Urban API and PZZ migrations are `PreSync` hooks with
`BeforeHookCreation,HookSucceeded`, bounded retries and deadlines. A failed hook
blocks the Deployment sync, so old pods stay active. Reverting Git restores an
old image digest but never attempts a database downgrade; migrations must remain
backward-compatible and idempotent.

See [docs/GITOPS-RUNBOOK.md](docs/GITOPS-RUNBOOK.md) for bootstrap and adoption.

## GitHub identities and runners

Use two separate identities:

- `deploy-bot` GitHub App: contents/PR write only in this repository and
  repository-dispatch access from allowlisted application repositories;
- `git-reader` GitHub App: contents read-only in this repository and allowlisted
  application repositories, used for stale-source checks and by Argo CD.

The deploy repository stores `SOURCE_READER_APP_ID` and
`SOURCE_READER_PRIVATE_KEY` as protected Actions secrets. Argo CD uses a
separate private key generated for the same read-only App.

The registry build runner uses the `13_runner` label. It must have registry
connectivity but no kubeconfig,
Vault token or SSH key for the control plane. Prefer an ephemeral runner;
otherwise use a dedicated VM with cleanup between jobs.

Application repositories call
`.github/workflows/reusable-application-release.yaml` from a separate workflow
triggered by pushes to `dev`. The reusable workflow reads its build matrix from
`services.yaml`, runs tests first, publishes immutable images and dispatches one
atomic event. `ci-templates/application-caller.yaml` is the starting caller.

## Required branch checks

Protect `main`, require pull requests and require the `Validate desired state`
job. Enable squash auto-merge for the deploy bot. The validation workflow runs:

- every app overlay, prerequisite, platform overlay and the full dev render;
- exact digest, no `latest`, no unresolved substitutions and catalog checks;
- duplicate owner, NodePort and unexpected shared Vault-path checks;
- rejection of committed Secret payloads;
- kubeconform against Kubernetes 1.36.3 and pinned core/CRD schema revisions;
- yamllint, shellcheck, actionlint, updater unit tests and gitleaks.

The promotion workflow additionally checks the source SHA is still the `dev`
HEAD and verifies each digest through the registry's read-only manifest API.

## Stable interfaces

Resource names, namespaces, selectors, NodePorts and PVC names from the existing
deployment are intentionally preserved for non-destructive Argo CD adoption.
The established NodePort groups remain:

| Range | Purpose |
|---|---|
| `31000-31049` | application API |
| `31050-31099` | MCP |
| `31100-31199` | UI/admin |
| `31200-31299` | observability |
| `31300-31399` | Gateway/edge |

Current fixed ports are enforced by the rendered-manifest uniqueness check.

## Production policy

Production will use its own cluster, Argo CD, Vault paths, capacity and approval
rules. Backend artifacts are promoted by digest from dev without rebuilding,
through a reviewed manual PR. The frontend is rebuilt for production because
its public configuration is embedded at build time. No production Application
exists yet.
