# tg-repo-watcher

Sends AI-generated Telegram summaries of GitHub activity (pushes, PRs,
releases, issues) to per-repo topics in a Telegram supergroup, or to a
Telegram channel.

Two delivery paths:

- **Webhook path** — repos you own dispatch a `repository_dispatch` event
  on every push/PR/release/issue. Fast, near-instant.
- **Cron path** — external repos you don't own are polled once a day
  for new **releases only**. State is tracked in `state/last_seen.json`.

**LLM:** DeepSeek V4-Flash, non-thinking mode (cheap).

---

## Architecture

```
┌─ webhook path ─────────────────────────────────────────────┐
│                                                            │
│  [owned repo]  push / PR / release / issue                 │
│       │                                                    │
│       ▼                                                    │
│  .github/workflows/notify.yml  (see caller.yml template)   │
│       │  fires repository_dispatch("repo-update")          │
│       ▼                                                    │
│  tg-repo-watcher / summarize.yml                           │
│       │                                                    │
│       ▼                                                    │
│  scripts/summarize.py                                      │
│       ├─ fetches diff/PR/release/issue via GitHub API      │
│       ├─ asks DeepSeek V4-Flash for a terse summary        │
│       └─ posts to Telegram destination(s) from config.yml  │
└────────────────────────────────────────────────────────────┘

┌─ cron path ────────────────────────────────────────────────┐
│                                                            │
│  Daily 09:00 UTC                                           │
│       │                                                    │
│       ▼                                                    │
│  tg-repo-watcher / poll.yml                                │
│       │                                                    │
│       ▼                                                    │
│  scripts/poll_releases.py                                  │
│       ├─ reads config.yml projects with trigger: cron      │
│       ├─ compares latest release vs state/last_seen.json   │
│       ├─ summarizes new releases only                      │
│       ├─ posts via same Telegram path as webhook           │
│       └─ commits updated state back to the repo            │
└────────────────────────────────────────────────────────────┘
```

## Files

| Path | Purpose |
|---|---|
| `.github/workflows/summarize.yml` | Runs on `repository_dispatch` from owned repos |
| `.github/workflows/poll.yml` | Daily cron for external repos |
| `.github/workflows/notify.yml` | Makes this watcher repo also watch itself |
| `scripts/summarize.py` | Shared summarizer/Telegram sender |
| `scripts/poll_releases.py` | Cron poller (reuses summarize.py helpers) |
| `config.yml` | All watched projects and their destinations |
| `state/last_seen.json` | Per-repo last-seen release tag (auto-committed) |
| `caller.yml` | Template — copy to each owned repo as `.github/workflows/notify.yml` |

## Secrets

### In `tg-repo-watcher` (this repo)

`Settings → Secrets and variables → Actions → Repository secrets`:

| Name | Purpose |
|---|---|
| `WATCHER_DISPATCH_PAT` | Classic PAT, `repo` scope. Used to (a) receive dispatches and (b) fetch diffs/PRs via the GitHub API. |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. |
| `DEEPSEEK_API_KEY` | From platform.deepseek.com. |

The workflows map `WATCHER_DISPATCH_PAT` to the `GH_PAT` env var that the
scripts read — you only need one PAT.

### In each owned repo you want to watch

| Name | Purpose |
|---|---|
| `WATCHER_DISPATCH_PAT` | Same PAT, `repo` scope on this watcher repo. Used to fire the dispatch event. |

`secrets.GITHUB_TOKEN` **cannot** fire cross-repo dispatches, which is why
each caller needs its own copy of a PAT.

## Adding a repo to watch (owned, webhook path)

1. Add an entry to `config.yml`:
   ```yaml
   my-new-project:
     repo: arkhivar/my-new-project
     chat_id: -1003947505610
     thread_id: 42
     trigger: webhook
   ```
2. Add `WATCHER_DISPATCH_PAT` secret to that repo.
3. Copy [`caller.yml`](./caller.yml) into that repo at
   `.github/workflows/notify.yml`. If you forked this repo, **edit the
   `owner:` and `repo:` fields in the JS script** to point at your fork.
4. Done. Next push triggers a summary.

## Adding an external repo (release-only, cron path)

```yaml
ext-something:
  repo: owner/name
  trigger: cron
  chat_id: -100...
  channel: true       # if it's a Telegram channel (no topics)
  # thread_id: 42     # OR set this if posting to a group topic
```

First cron run **silently bootstraps** the current latest release into
`state/last_seen.json` so you don't get a burst of stale notifications.
Anything published *after* bootstrap gets summarized.

## Multiple destinations per repo

Any project (webhook or cron) can fan out to several destinations:

```yaml
my-project:
  repo: arkhivar/my-project
  trigger: webhook
  destinations:
    - chat_id: -1003947505610
      thread_id: 12
    - chat_id: -1002859087635
      thread_id: 39
    - chat_id: -1002627184483
      channel: true
```

If any single Telegram send fails, the others still get delivered.

## Telegram setup notes

- **Groups with topics** — supergroup with "Topics" enabled. Get
  `chat_id` and `thread_id` by forwarding a message from the topic to
  `@RawDataBot`, or by inspecting bot updates.
- **Channels** — need `channel: true` in config. Bot must be added
  to the channel **as an admin** with post-message permission.
- **Groups** — bot must be a member; admin usually needed too so
  it can post into arbitrary topics.

## Manual runs

- **Cron poller now** (don't wait 24h): Actions tab → *Poll External
  Releases* → *Run workflow*, or:
  ```bash
  gh workflow run poll.yml
  ```
- **Trigger a specific summarizer run** by pushing anything to any
  configured webhook repo. To simulate one without pushing, use the
  GitHub UI's *Trigger workflow* against `repository_dispatch` (not
  possible via the standard workflow_dispatch button — you'd need to
  `gh api` a dispatch event manually).

## Forking this repo for a new project

Search-and-replace `arkhivar/tg-repo-watcher` in these files with your
new fork's `owner/repo`:

- `caller.yml` — the `owner:` and `repo:` values in the JS script
- `README.md` — for hygiene

Then:

1. Reset `config.yml` to just the entries you actually want.
2. Delete `state/last_seen.json` so the cron path bootstraps fresh.
3. Set the three secrets in your fork.
4. Push a test commit to any watched repo.

## Local test (optional, webhook path)

You need a real, recent commit SHA pair for the compare API to work:

```bash
export GH_PAT=ghp_...
export TELEGRAM_BOT_TOKEN=...
export DEEPSEEK_API_KEY=...

# grab two recent SHAs from a real repo
BEFORE=$(gh api /repos/OWNER/REPO/commits --jq '.[1].sha')
AFTER=$( gh api /repos/OWNER/REPO/commits --jq '.[0].sha')

export DISPATCH_PAYLOAD=$(jq -nc \
  --arg r "OWNER/REPO" --arg b "$BEFORE" --arg a "$AFTER" \
  '{repo:$r, event:"push", ref:"refs/heads/main", actor:"you",
    before:$b, after:$a}')

python scripts/summarize.py
```

## Local test (cron path)

```bash
export GH_PAT=ghp_...
export TELEGRAM_BOT_TOKEN=...
export DEEPSEEK_API_KEY=...
python scripts/poll_releases.py
```

First run silently bootstraps state; subsequent runs will notify on
any new releases.
