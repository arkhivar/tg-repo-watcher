#!/usr/bin/env python3
"""
Repository-dispatch summarizer.

Reads the caller's payload from DISPATCH_PAYLOAD (JSON), fetches the diff /
PR / release / issue details via the GitHub API using GH_PAT, asks DeepSeek
V4-Flash to summarize (non-thinking mode), and posts to the right Telegram
topic per config.yml.

All secrets come from env; caller supplies only public metadata.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
import yaml

# ---------- config ----------
WATCHER_DIR = Path(__file__).resolve().parent.parent  # scripts/ -> repo root
CONFIG_PATH = WATCHER_DIR / "config.yml"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

NOISY_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock",
    ".min.js", ".min.css", "dist/", "build/", "node_modules/",
)

MAX_COMMITS = 20
MAX_FILES_LISTED = 15
MAX_DIFF_CHARS = 6000


# ---------- helpers ----------
def resolve_destinations(project: dict) -> list[tuple[int, int | None]]:
    """Accept both legacy (chat_id + thread_id) and new (destinations: [...]) schemas.

    Returns a list of (chat_id, thread_id_or_None) tuples with all invalid
    entries filtered out.
    """
    out: list[tuple[int, int | None]] = []

    def _add(chat_id, thread_id):
        if chat_id in (None, "", 0):
            return
        if thread_id in (None, "", 0):
            return  # keep old behaviour: skip until a real thread is set
        try:
            out.append((int(chat_id), int(thread_id)))
        except (TypeError, ValueError):
            print(f"[warn] bad destination {chat_id=} {thread_id=} — skipped", file=sys.stderr)

    dests = project.get("destinations")
    if isinstance(dests, list) and dests:
        for d in dests:
            if isinstance(d, dict):
                _add(d.get("chat_id"), d.get("thread_id"))
    else:
        _add(project.get("chat_id"), project.get("thread_id"))
    return out


def load_project_cfg(repo: str) -> dict | None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    for _, spec in (cfg.get("projects") or {}).items():
        if (spec or {}).get("repo") == repo:
            return spec
    return None


def gh_get(url: str, token: str) -> dict | list | None:
    r = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"[warn] GitHub {r.status_code} on {url}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json()


def is_noisy(path: str) -> bool:
    return any(p in path for p in NOISY_PATTERNS)


# ---------- context gatherers ----------
def gather_push(repo: str, before: str, after: str, token: str) -> dict:
    if not before or before.startswith("0000000"):
        data = gh_get(f"https://api.github.com/repos/{repo}/commits/{after}", token) or {}
        commits = [{
            "sha": (data.get("sha") or "")[:7],
            "message": (data.get("commit") or {}).get("message", ""),
            "author": ((data.get("commit") or {}).get("author") or {}).get("name", ""),
        }] if data else []
        files = data.get("files") or []
        compare_url = data.get("html_url", "")
    else:
        data = gh_get(f"https://api.github.com/repos/{repo}/compare/{before}...{after}", token) or {}
        commits = [
            {
                "sha": (c.get("sha") or "")[:7],
                "message": (c.get("commit") or {}).get("message", ""),
                "author": ((c.get("commit") or {}).get("author") or {}).get("name", ""),
            }
            for c in (data.get("commits") or [])[:MAX_COMMITS]
        ]
        files = data.get("files") or []
        compare_url = data.get("html_url", "")

    signal = [f for f in files if not is_noisy(f.get("filename", ""))]
    file_list = [f.get("filename", "") for f in signal[:MAX_FILES_LISTED]]

    diff_parts, running = [], 0
    for f in signal:
        patch = f.get("patch") or ""
        if not patch:
            continue
        chunk = f"--- {f.get('filename')}\n{patch}\n"
        if running + len(chunk) > MAX_DIFF_CHARS:
            diff_parts.append("... [diff truncated] ...")
            break
        diff_parts.append(chunk)
        running += len(chunk)

    return {
        "commits": commits,
        "file_list": file_list,
        "diff": "\n".join(diff_parts),
        "compare_url": compare_url,
        "total_files": len(files),
        "signal_files": len(signal),
    }


def gather_pr(repo: str, pr_number: int, token: str) -> dict:
    pr = gh_get(f"https://api.github.com/repos/{repo}/pulls/{pr_number}", token) or {}
    files = gh_get(f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=50", token) or []
    signal = [f for f in files if not is_noisy(f.get("filename", ""))]
    return {
        "title": pr.get("title", ""),
        "body": pr.get("body", "") or "",
        "merged": pr.get("merged", False),
        "state": pr.get("state", ""),
        "url": pr.get("html_url", ""),
        "file_list": [f.get("filename", "") for f in signal[:MAX_FILES_LISTED]],
        "total_files": len(files),
        "signal_files": len(signal),
    }


def gather_release(repo: str, release_id: int, token: str) -> dict:
    rel = gh_get(f"https://api.github.com/repos/{repo}/releases/{release_id}", token) or {}
    return {
        "tag": rel.get("tag_name", ""),
        "name": rel.get("name", ""),
        "body": rel.get("body", "") or "",
        "url": rel.get("html_url", ""),
    }


def gather_issue(repo: str, issue_number: int, token: str) -> dict:
    iss = gh_get(f"https://api.github.com/repos/{repo}/issues/{issue_number}", token) or {}
    return {
        "title": iss.get("title", ""),
        "body": iss.get("body", "") or "",
        "state": iss.get("state", ""),
        "url": iss.get("html_url", ""),
    }


# ---------- prompt + LLM ----------
def build_prompt(repo: str, event: str, payload: dict, ctx: dict) -> str:
    parts = [f"Repository: {repo}", f"Event: {event}"]
    if event == "push":
        branch = (payload.get("ref") or "").replace("refs/heads/", "")
        parts.append(f"Branch: {branch}")
        parts.append(f"Author: {payload.get('actor', '')}")
        parts.append(f"Commits ({len(ctx.get('commits', []))}):")
        for c in ctx.get("commits", []):
            first_line = (c["message"].splitlines() or [""])[0]
            parts.append(f"  - {c['sha']} {first_line}")
        parts.append(f"Files ({ctx.get('signal_files', 0)} signal / {ctx.get('total_files', 0)} total):")
        for f in ctx.get("file_list", []):
            parts.append(f"  - {f}")
        if ctx.get("diff"):
            parts.append("\nDiff excerpt:\n" + ctx["diff"])
    elif event == "pull_request":
        parts.append(f"PR #{payload.get('pr_number')} — {ctx.get('title', '')}")
        parts.append(f"Action: {payload.get('action', '')} (merged={ctx.get('merged', False)})")
        if ctx.get("body"):
            parts.append(f"PR body:\n{ctx['body'][:1500]}")
        parts.append(f"Files ({ctx.get('signal_files', 0)} signal / {ctx.get('total_files', 0)} total):")
        for f in ctx.get("file_list", []):
            parts.append(f"  - {f}")
    elif event == "release":
        parts.append(f"Release: {ctx.get('tag', '')} — {ctx.get('name', '')}")
        parts.append(f"Notes:\n{ctx.get('body', '')[:2000]}")
    elif event == "issues":
        parts.append(f"Issue #{payload.get('issue_number')} — {ctx.get('title', '')}")
        parts.append(f"Action: {payload.get('action', '')} (state={ctx.get('state', '')})")
        if ctx.get("body"):
            parts.append(f"Body:\n{ctx['body'][:1200]}")
    return "\n".join(parts)


def call_deepseek(prompt: str, api_key: str) -> str:
    system_msg = (
        "You write terse Telegram summaries of code changes for a solo maintainer. "
        "Rules: 2-4 short sentences, no emoji, no marketing tone, no headers, "
        "no bullet lists unless truly needed. Focus on WHAT changed and WHY it matters, "
        "not a file inventory. If the change is trivial (typo, formatting), say so briefly."
    )
    r = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            # CRITICAL: v4-flash defaults to thinking=on. Disable to save $$.
            "thinking": {"type": "disabled"},
            "temperature": 0.3,
            "max_tokens": 400,
        },
        timeout=90,
    )
    if r.status_code >= 400:
        print(f"[error] DeepSeek {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return "(summary unavailable — DeepSeek API error)"
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip() or "(empty summary)"


# ---------- Telegram ----------
def tg_escape(s: str) -> str:
    if not s:
        return ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s


def build_message(repo: str, event: str, payload: dict, ctx: dict, summary: str) -> str:
    bits = [f"*{tg_escape(repo)}*", tg_escape(event)]
    if event == "push":
        branch = (payload.get("ref") or "").replace("refs/heads/", "")
        if branch:
            bits.append(f"`{tg_escape(branch)}`")
    header = " · ".join(bits)

    body = tg_escape(summary)

    link = ""
    if event == "push" and ctx.get("compare_url"):
        link = f"[compare]({ctx['compare_url']})"
    elif event == "pull_request" and ctx.get("url"):
        link = f"[PR \\#{payload.get('pr_number')}]({ctx['url']})"
    elif event == "release" and ctx.get("url"):
        link = f"[release]({ctx['url']})"
    elif event == "issues" and ctx.get("url"):
        link = f"[issue \\#{payload.get('issue_number')}]({ctx['url']})"

    return f"{header}\n\n{body}" + (f"\n\n{link}" if link else "")


def send_telegram(token: str, chat_id: int, thread_id: int | None, text: str) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    r = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=30)
    if r.status_code >= 400:
        print(f"[error] Telegram {r.status_code}: {r.text[:500]}", file=sys.stderr)
        # Fallback: strip markdown and retry as plain text
        payload.pop("parse_mode", None)
        payload["text"] = text.replace("\\", "")
        r2 = requests.post(TELEGRAM_API.format(token=token), json=payload, timeout=30)
        print(f"[fallback] plain-text retry -> {r2.status_code}")
    else:
        print(f"[ok] Telegram delivered ({r.status_code})")


# ---------- main ----------
def main() -> int:
    gh_pat = os.environ.get("GH_PAT", "")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    raw = os.environ.get("DISPATCH_PAYLOAD", "{}")

    if not (gh_pat and tg_token and ds_key):
        print("[fatal] missing secrets in env", file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        print(f"[fatal] bad DISPATCH_PAYLOAD JSON: {e}", file=sys.stderr)
        return 1

    repo = payload.get("repo") or ""
    event = payload.get("event") or ""
    if not repo or not event:
        print(f"[fatal] payload missing repo/event: {payload}", file=sys.stderr)
        return 1

    project = load_project_cfg(repo)
    if not project:
        print(f"[skip] {repo} not in config.yml")
        return 0

    destinations = resolve_destinations(project)
    if not destinations:
        print(f"[skip] {repo} has no valid destinations set yet in config.yml")
        return 0

    ctx: dict = {}
    if event == "push":
        ctx = gather_push(repo, payload.get("before", ""), payload.get("after", ""), gh_pat)
        if not ctx.get("commits"):
            print("[skip] no commits (branch delete?)")
            return 0
    elif event == "pull_request":
        action = payload.get("action", "")
        if action not in {"opened", "reopened", "closed", "ready_for_review"}:
            print(f"[skip] pull_request action={action}")
            return 0
        pr_num = payload.get("pr_number")
        if not pr_num:
            print("[fatal] pr_number missing", file=sys.stderr)
            return 1
        ctx = gather_pr(repo, int(pr_num), gh_pat)
    elif event == "release":
        rid = payload.get("release_id")
        if not rid:
            print("[fatal] release_id missing", file=sys.stderr)
            return 1
        ctx = gather_release(repo, int(rid), gh_pat)
    elif event == "issues":
        action = payload.get("action", "")
        if action not in {"opened", "closed", "reopened"}:
            print(f"[skip] issues action={action}")
            return 0
        num = payload.get("issue_number")
        if not num:
            print("[fatal] issue_number missing", file=sys.stderr)
            return 1
        ctx = gather_issue(repo, int(num), gh_pat)
    else:
        print(f"[skip] event {event} not handled")
        return 0

    prompt = build_prompt(repo, event, payload, ctx)
    print("=== prompt ===")
    print(prompt[:2000])
    print("=== end prompt ===")

    summary = call_deepseek(prompt, ds_key)
    print(f"[llm] {summary}")

    msg = build_message(repo, event, payload, ctx, summary)
    for chat_id, thread_id in destinations:
        send_telegram(tg_token, chat_id, thread_id, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
