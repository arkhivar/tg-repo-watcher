#!/usr/bin/env python3
"""
Daily poller for public repos we don't own.

Reads config.yml for entries with `trigger: cron`, fetches each repo's
releases from the GitHub API, diffs against state/last_seen.json, and
runs any *new* releases through the same summarizer pipeline as the
webhook path.

State bootstrap: on first run for a repo (no prior state), record the
current latest release and DO NOT notify (avoids a burst of stale
notifications).

Writes state/last_seen.json back to disk. The workflow commits it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
import yaml

# Reuse the webhook path's helpers
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from summarize import (  # noqa: E402
    build_message,
    build_prompt,
    call_deepseek,
    gh_get,
    resolve_destinations,
    send_telegram,
)

REPO_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = REPO_ROOT / "config.yml"
STATE_DIR = REPO_ROOT / "state"
STATE_PATH = STATE_DIR / "last_seen.json"

# How many releases to consider "new" in a single poll (safety cap so
# a repo that publishes 20 releases in a day doesn't flood).
MAX_NEW_PER_REPO = 5


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            print(f"[warn] {STATE_PATH} corrupt \u2014 starting fresh", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def fetch_releases(repo: str, token: str) -> list[dict]:
    """Return releases newest-first, excluding drafts and prereleases."""
    data = gh_get(f"https://api.github.com/repos/{repo}/releases?per_page=20", token) or []
    return [r for r in data if not r.get("draft") and not r.get("prerelease")]


def process_repo(name: str, project: dict, state: dict, tokens: dict) -> None:
    repo = project.get("repo")
    if not repo:
        print(f"[skip] {name}: no repo set")
        return

    destinations = resolve_destinations(project)
    if not destinations:
        print(f"[skip] {repo}: no valid destinations yet")
        return

    releases = fetch_releases(repo, tokens["gh"])
    if not releases:
        print(f"[info] {repo}: no releases found")
        # Record a sentinel so we don't keep hitting the API cold
        state.setdefault(repo, {"last_tag": None})
        return

    latest_tag = releases[0].get("tag_name")
    prior = (state.get(repo) or {}).get("last_tag")

    if prior is None:
        # First time seeing this repo \u2014 bootstrap silently.
        state[repo] = {"last_tag": latest_tag}
        print(f"[bootstrap] {repo}: recorded {latest_tag}, no notification")
        return

    if latest_tag == prior:
        print(f"[info] {repo}: no new releases (still {latest_tag})")
        return

    # Collect releases strictly newer than `prior` (i.e. above it in the list)
    new_releases: list[dict] = []
    for r in releases:
        if r.get("tag_name") == prior:
            break
        new_releases.append(r)
    new_releases = list(reversed(new_releases[:MAX_NEW_PER_REPO]))  # oldest -> newest

    if not new_releases:
        # `prior` isn't in the current release list at all (deleted?).
        # Treat everything visible as new, capped.
        new_releases = list(reversed(releases[:MAX_NEW_PER_REPO]))
        print(f"[warn] {repo}: prior tag {prior} not in current releases, notifying latest {len(new_releases)}")

    print(f"[new] {repo}: {len(new_releases)} release(s) to notify")

    for rel in new_releases:
        tag = rel.get("tag_name", "")
        payload = {
            "repo": repo,
            "event": "release",
            "action": "published",
            "release_id": rel.get("id"),
        }
        ctx = {
            "tag": tag,
            "name": rel.get("name", "") or tag,
            "body": rel.get("body", "") or "",
            "url": rel.get("html_url", ""),
        }

        prompt = build_prompt(repo, "release", payload, ctx)
        summary = call_deepseek(prompt, tokens["ds"])
        print(f"[llm] {repo} {tag}: {summary[:120]}")

        msg = build_message(repo, "release", payload, ctx, summary)
        for chat_id, thread_id in destinations:
            send_telegram(tokens["tg"], chat_id, thread_id, msg)

    # Update state to newest tag we just processed
    state[repo] = {"last_tag": new_releases[-1].get("tag_name")}


def main() -> int:
    gh_pat = os.environ.get("GH_PAT", "")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")

    if not (gh_pat and tg_token and ds_key):
        print("[fatal] missing secrets in env", file=sys.stderr)
        return 1

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    projects = (cfg.get("projects") or {})

    cron_projects = {
        name: spec for name, spec in projects.items()
        if (spec or {}).get("trigger") == "cron"
    }

    if not cron_projects:
        print("[info] no cron-triggered projects in config.yml")
        return 0

    print(f"[start] polling {len(cron_projects)} repo(s)")
    state = load_state()
    tokens = {"gh": gh_pat, "tg": tg_token, "ds": ds_key}

    for name, spec in cron_projects.items():
        try:
            process_repo(name, spec, state, tokens)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {name}: {type(e).__name__}: {e}", file=sys.stderr)

    save_state(state)
    print(f"[done] state written to {STATE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
