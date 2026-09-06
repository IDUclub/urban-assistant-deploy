# GitOps bootstrap and adoption runbook

This runbook covers attaching Urban Assistant to an existing Argo CD instance
and adopting its resources. Argo CD itself and cluster-wide controllers are
managed separately by the cluster owner. Run Kubernetes commands only from the
control plane with the intended kubeconfig. Stop on every failed check.

## 1. Publish and protect the repository

Create the private repository `IDUclub/urban-assistant-deploy`, then from this
directory:

```bash
git remote add origin git@github.com:IDUclub/urban-assistant-deploy.git
git push -u origin main
```

Protect `main`: require a pull request, require the `Validate desired state`
check, dismiss stale approvals as appropriate, block force pushes and enable
auto-merge. Create the `automated` and `environment/dev` labels. Ensure the
visible `@IDUclub/platform` team exists with write access, or replace it with
the actual platform team before enabling required CODEOWNERS review.

Create a GitHub App named `deploy-bot`, grant Contents and Pull requests
read/write, and install it only on the deploy repository. Store
`DEPLOY_BOT_APP_ID` and `DEPLOY_BOT_PRIVATE_KEY` as protected repository or
organization secrets available to the deploy and allowlisted application
repositories. The App private key is never copied to the runner filesystem
outside a job.

Create a second GitHub App named `git-reader` with Contents read-only and install
it on the deploy and allowlisted application repositories. Store
`SOURCE_READER_APP_ID` and `SOURCE_READER_PRIVATE_KEY` in the deploy repository
for stale-source checks. Generate a separate private key for Argo CD; do not
reuse the deploy-bot write credential.

## 2. Prepare the registry runner

Register an existing Linux runner with this label:

```text
47_runner
```

It needs Docker Buildx, `bash`, `curl`, `jq`, Python 3.12 and network
access to the current registry. It must not contain a kubeconfig, Vault token,
control-plane SSH key or cluster-admin credential. Restrict it to protected
`dev` push workflows in trusted repositories. Create a `dev-build` GitHub
Environment restricted to the protected `dev` branch and keep registry secrets
there; restrict the runner group to the allowlisted application repositories.

## 3. Add application workflows

In each application repository copy `ci-templates/application-caller.yaml` to
`.github/workflows/kubernetes-release.yaml`, set the service key and its actual
test script, and keep the existing Compose workflow during adoption. The test
script must install its dependencies and run the required suite. The template
fails closed until this placeholder is replaced. The workflow must trigger on a
successful push to protected branch `dev`.

Every backend repository must have the same protected release branch named
`dev`. Create it from that repository's current integration branch before
installing the caller. Do not point the Kubernetes release workflow at `main`,
`master` or `develop`; both the reusable workflow and the promotion workflow
reject those refs.

Before enabling it, verify Dockerfile and target values in `services.yaml`.
`idu_api` uses separate release contracts for `urban-api` (API plus migrator)
and `urban-mcp`; do not combine their dispatch payloads. Multi-image contracts
such as Urban API and gMART are accepted only when every declared image is
present.

For frontend, add `MAPBOX_PUBLIC_TOKEN`, `FRONTEND_KEYCLOAK_AUTH_URL` and
`FRONTEND_KEYCLOAK_LOGOUT_REDIRECT` as protected Actions secrets. The reusable
workflow checks out the exact deploy-config revision and generates `service.env`
immediately before the environment-specific build.

## 4. Populate the Vault contract

Before syncing any `VaultStaticSecret`, populate every key listed in
`vault-contract.yaml` under its declared `urban-assistant-kv` path. The contract
is the complete inventory of credentials, TLS data, external endpoints and all
other values read by templates. It contains names only; never copy real values
into this repository.

Internal Kubernetes Service DNS names remain in Git and are not part of this
contract.

Verify each path directly in Vault, then confirm that no endpoint value appears
in the rendered manifests:

```bash
vault kv get urban-assistant-kv/dev/urban-api
python3 scripts/validate.py --root .
```

## 5. Validate locally before touching the cluster

```bash
python3 -m pip install PyYAML==6.0.3
python3 scripts/validate.py --root .
python3 -m unittest discover -s tests -v
./scripts/render.sh environments/dev > /tmp/urban-assistant-dev.yaml
```

If `kubeconform` is installed, `scripts/validate.sh` also checks Kubernetes
1.36.3 and the pinned CRD schema snapshots.

## 6. Verify cluster prerequisites

On the control plane:

```bash
export KUBECONFIG=<cluster-admin-kubeconfig>
kubectl config current-context
kubectl version -o json | jq '.serverVersion.gitVersion'
helm status argocd -n argocd
kubectl get pods -n argocd
kubectl get svc argocd-server -n argocd
kubectl get deployment metrics-server -n kube-system
kubectl get deployment vault-secrets-operator-controller-manager \
  -n vault-secrets-operator
kubectl get deployment envoy-gateway -n envoy-gateway-system
```

Do not continue until Argo CD, Metrics Server, NFS CSI, Vault Secrets Operator
and Envoy Gateway are healthy. Their versions and external access are cluster
infrastructure concerns, not desired state in this repository.

Add the read-only deploy-repository credential with Argo CD's native GitHub App
authentication. Keep the private key file in a temporary protected location
and remove the local copy after the connection is verified:

```bash
argocd repo add https://github.com/IDUclub/urban-assistant-deploy.git \
  --github-app-id <GIT_READER_APP_ID> \
  --github-app-installation-id <INSTALLATION_ID> \
  --github-app-private-key-path <PATH_TO_PRIVATE_KEY>
argocd repo get https://github.com/IDUclub/urban-assistant-deploy.git
```

## 7. Start in adoption mode

The cluster-owned root Application initially points to `argocd/adoption`, which
removes automated sync and prune from every generated Application. Apply its
manifest from the cluster infrastructure bundle:

```bash
kubectl apply --server-side --force-conflicts \
  -f <cluster-infra-dir>/argocd/urban-assistant-root-application.yaml
argocd app diff urban-assistant-bootstrap
argocd app sync urban-assistant-bootstrap
argocd app list
```

Adopt in this order, always reviewing `argocd app diff` before `argocd app sync`
and never adding `--prune`:

1. Urban Assistant-scoped component Applications and `dev-cluster-foundation`;
2. `dev-vault-integration`, then confirm `VaultAuth` and generated Secrets;
3. monitoring, Kafka and Gateway Applications;
4. `dev-urban-api-prerequisites` and `dev-pzz-compare-prerequisites`;
5. application Applications one at a time.

Cluster-wide controllers and administration tools, including Headlamp, remain
outside this repository. Their resources must not be added to an Urban
Assistant Application merely to make them visible in Argo CD.

For Urban API and PZZ, first inspect the existing migration Jobs and database
backup state. Do not sync their Applications until a repeated forward migration
is confirmed safe:

```bash
kubectl get jobs -n urban-assistant-dev urban-migrate pzz-migrate
kubectl get secret,pvc -n urban-assistant-dev
```

Compare resource names, selectors, Services, NodePorts, PVCs and images. A
deletion in Git must not be pruned during this phase.

## 8. Enable steady state

After every Application has passed deploy and rollback acceptance, change the
cluster-owned root Application from `path: argocd/adoption` to
`path: argocd/root`, apply it once and sync the root Application:

```bash
kubectl apply --server-side --force-conflicts \
  -f <cluster-infra-dir>/argocd/urban-assistant-root-application.yaml
argocd app sync urban-assistant-bootstrap
argocd app list
```

Steady state enables `automated`, `selfHeal`, `prune` and `allowEmpty: false`.
Only then disable the corresponding legacy Compose job. After all services have
migrated, stop copying tar archives to the control plane.

## 9. Acceptance per service

- Push a harmless change to `dev`; observe immutable tag, digest, bot PR,
  required checks, auto-merge and only that Argo rollout.
- For multi-image releases, confirm one PR contains every digest.
- Dispatch an older SHA and confirm rejection before a PR is created.
- Force a migration failure in a safe test revision and confirm old pods remain.
- Revert the deploy commit and confirm the previous image digest returns; do not
  expect a database downgrade.
- Change a safe live annotation and confirm self-heal restores Git state.
- Confirm application workflows and runner files contain no kubeconfig or
  cluster-admin credentials.

## 10. Production

Do not point the dev Argo CD instance at production. Build a separate cluster
and Argo CD instance, then add reviewed overlays under `environments/prod`.
Promote backend digests without rebuild through a manual PR and environment
approval. Build the frontend separately with production public configuration.
