#!/usr/bin/env python3
"""Mirror authenticated Argo statuses using immutable deploy-to-source provenance.

Execute current main's code only. Read the event SHA's manifests as data and
validate the destination against today's catalog before minting a scoped token.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import yaml


DEPLOY_REPOSITORY = "IDUclub/urban-assistant-deploy"
SHA_RE = re.compile(r"[0-9a-f]{40}")
CONTEXT_RE = re.compile(r"argocd/dev-([a-z0-9]+(?:-[a-z0-9]+)*)")
SOURCE_RE = re.compile(r"IDUclub/[A-Za-z0-9_.-]+")
STATES = {"pending", "success", "failure", "error"}
PROVENANCE_RE = re.compile(
    r"Argo (?:pending|success|failure|error); deploy=([0-9a-f]{40}); "
    r"at=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z); id=(\d+)"
)


class Rejected(ValueError):
    pass


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )


def ancestor(root, older, newer):
    if not SHA_RE.fullmatch(older) or not SHA_RE.fullmatch(newer):
        raise Rejected("invalid deployment history SHA")
    result = git(root, "merge-base", "--is-ancestor", older, newer)
    if result.returncode not in (0, 1):
        raise Rejected("deployment revision is unavailable in main history")
    return result.returncode == 0


def mapping(value, label):
    if not isinstance(value, dict):
        raise Rejected(f"{label} must be a mapping")
    return value


def historical_yaml(root, revision, path):
    result = git(root, "show", f"{revision}:{path}")
    if result.returncode:
        raise Rejected(f"missing historical {path}")
    return mapping(yaml.safe_load(result.stdout), path)


def service_from(catalog, name):
    services = catalog.get("services", [])
    matches = [s for s in services if isinstance(s, dict) and s.get("name") == name]
    if catalog.get("environment") != "dev" or len(matches) != 1:
        raise Rejected("service is not uniquely allowlisted in the dev catalog")
    service = matches[0]
    source = mapping(service.get("source"), "service source")
    repository = source.get("repository", "")
    if not isinstance(repository, str) or not SOURCE_RE.fullmatch(repository) or source.get("branch") != "dev":
        raise Rejected("source must be an allowlisted IDUclub repository with branch dev")
    if service.get("argocdPath") != f"environments/dev/apps/{name}":
        raise Rejected("unexpected service overlay path")
    return service


def resolve(root, event):
    mapping(event, "event")
    if event.get("repository", {}).get("full_name") != DEPLOY_REPOSITORY:
        raise Rejected("unexpected event repository")
    context = event.get("context", "")
    match = CONTEXT_RE.fullmatch(context) if isinstance(context, str) else None
    revision = event.get("sha", "")
    if not match or not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        raise Rejected("invalid Argo context or deploy SHA")
    if event.get("state") not in STATES or event.get("sender", {}).get("type") != "Bot":
        raise Rejected("expected a bot-created commit status")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    if not ancestor(root, revision, head):
        raise Rejected("deployment commit is not merged into main")
    name = match.group(1)
    current = service_from(mapping(yaml.safe_load((root / "services.yaml").read_text(encoding="utf-8")), "catalog"), name)
    historical = service_from(historical_yaml(root, revision, "services.yaml"), name)
    repository = historical["source"]["repository"]
    if repository != current["source"]["repository"]:
        raise Rejected("historical source repository differs from the current allowlist")
    overlay = historical_yaml(root, revision, historical["argocdPath"] + "/kustomization.yaml")
    annotations = mapping(overlay.get("commonAnnotations", {}), "release annotations")
    source_sha = annotations.get("deployment.urban-assistant/source-revision")
    if source_sha in (None, "legacy-import"):
        return None
    if not isinstance(source_sha, str) or not SHA_RE.fullmatch(source_sha):
        raise Rejected("invalid source revision")
    workflow = annotations.get("deployment.urban-assistant/source-workflow", "")
    if not isinstance(workflow, str) or not re.fullmatch(
        re.escape(f"https://github.com/{repository}/actions/runs/") + r"[0-9]+", workflow
    ):
        raise Rejected("source workflow does not match the allowlisted repository")
    return {"service": name, "context": context, "deploy_sha": revision,
            "source_repository": repository, "source_sha": source_sha}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, deploy_token, source_token):
        self.tokens = {"deploy": deploy_token, "source": source_token}
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, access, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request("https://api.github.com" + path, method=method, data=data, headers={
            "Authorization": "Bearer " + self.tokens[access], "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
            "User-Agent": "urban-assistant-deployment-status",
        })
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Never print credentials or response bodies to Actions logs.
            raise Rejected(f"GitHub {method} returned HTTP {error.code}") from error

    def latest(self, repository, revision, context, access):
        # GitHub returns newest first; paginate because other contexts may fill
        # the first page. Fail closed if the bounded scan cannot reach a result.
        for page in range(1, 101):
            records = self.request("GET", f"/repos/{repository}/commits/{revision}/statuses?per_page=100&page={page}", access)
            if not isinstance(records, list):
                raise Rejected("unexpected GitHub status response")
            for record in records:
                if record.get("context") == context:
                    return record
            if len(records) < 100:
                return None
        raise Rejected("too many statuses to safely locate the latest context")

    def post(self, repository, revision, payload):
        return self.request("POST", f"/repos/{repository}/statuses/{revision}", "source", payload)


def authenticated(record, bot):
    if not record or record.get("creator", {}).get("login") != bot:
        raise Rejected("status was not created by the configured Argo GitHub App")


def status_payload(plan, latest, bot):
    authenticated(latest, bot)
    state, stamp, identifier = latest.get("state"), latest.get("created_at"), latest.get("id")
    if state not in STATES or not isinstance(stamp, str) or type(identifier) is not int or identifier <= 0:
        raise Rejected("invalid upstream status metadata")
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    url = latest.get("target_url")
    parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise Rejected("expected an HTTPS Argo details URL without credentials")
    description = f"Argo {state}; deploy={plan['deploy_sha']}; at={stamp}; id={identifier}"
    if len(description) > 140:
        raise Rejected("status description is too long")
    return {"context": plan["context"], "state": state, "target_url": url, "description": description}


def should_publish(root, plan, latest, previous, bot):
    if previous is None:
        return True
    authenticated(previous, bot)
    marker = PROVENANCE_RE.fullmatch(previous.get("description") or "")
    if not marker:
        raise Rejected("existing source status has no relay provenance; refusing to overwrite it")
    previous_revision, stamp, identifier = marker.groups()
    if previous_revision == plan["deploy_sha"]:
        # Re-read the current upstream state, not the possibly delayed event.
        # Re-running the same status is idempotent. New operations on the same
        # SHA can legitimately move success/failure back to pending.
        return stamp <= latest["created_at"] and int(identifier) != latest["id"]
    if ancestor(root, plan["deploy_sha"], previous_revision):
        return False
    if not ancestor(root, previous_revision, plan["deploy_sha"]):
        raise Rejected("source status points to unrelated deployment history")
    return True


def forward(root, event, plan, bot, client):
    if event.get("sender", {}).get("login") != bot:
        raise Rejected("event sender is not the configured Argo GitHub App")
    previous = client.latest(plan["source_repository"], plan["source_sha"], plan["context"], "source")
    # Fetch immediately before deciding/posting, after any queued earlier run.
    latest = client.latest(DEPLOY_REPOSITORY, plan["deploy_sha"], plan["context"], "deploy")
    payload = status_payload(plan, latest, bot)
    if not should_publish(root, plan, latest, previous, bot):
        return "Skipped duplicate or superseded deployment status"
    client.post(plan["source_repository"], plan["source_sha"], payload)
    return f"Forwarded {payload['state']} to {plan['source_repository']}@{plan['source_sha']} ({plan['context']})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["resolve", "forward"])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--event-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    event = json.loads(args.event_file.read_text(encoding="utf-8"))
    plan = resolve(root, event)
    if args.mode == "resolve":
        outputs = {"eligible": "true" if plan else "false"}
        if plan:
            owner, repository = plan["source_repository"].split("/")
            outputs.update({"service": plan["service"], "source-owner": owner,
                            "source-name": repository, "source-sha": plan["source_sha"]})
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                for key, value in outputs.items():
                    stream.write(f"{key}={value}\n")
        print(json.dumps(outputs))
        return
    if not plan:
        print("Skipped release without source provenance (legacy import)")
        return
    if (plan["source_repository"] != os.environ["EXPECTED_SOURCE_REPOSITORY"]
            or plan["source_sha"] != os.environ["EXPECTED_SOURCE_SHA"]):
        raise Rejected("release destination changed between resolve and forward jobs")
    result = forward(root, event, plan, os.environ["ARGO_STATUS_BOT_LOGIN"],
                     GitHub(os.environ["DEPLOY_STATUS_TOKEN"], os.environ["SOURCE_STATUS_TOKEN"]))
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(result + "\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
        raise SystemExit(f"status forwarding rejected: {error}") from error
