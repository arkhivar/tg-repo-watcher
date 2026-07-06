# tg-repo-watcher

Sends AI-generated Telegram summaries of GitHub activity (pushes, PRs,
releases, issues) to per-repo topics in a single Telegram supergroup.

**Model:** DeepSeek V4-Flash, non-thinking mode.
**Trigger:** `repository_dispatch` from each watched repo.

## How it works

```
[watched repo]  push/PR/release/issue
       │
       ▼
[notify.yml]    → fires repository_dispatch("repo-update")
       │            with a small JSON payload
       ▼
[tg-repo-watcher / summarize.yml]
       │
       ▼
[scripts/summarize.py]
       ├── fetches diff/PR/release/issue via GitHub API (GH_PAT)
       ├── asks DeepSeek V4-Flash for a terse summary
       └── sends to Telegram group + thread from config.yml
```

## Secrets (only in **this** repo)

`Settings → Secrets and variables → Actions → Repository secrets`:

| Name | Purpose |
|---|---|
| `GH_PAT` | Classic PAT, `repo` scope. Used to fetch diffs/PRs from any of your repos (public or private). |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. |
| `DEEPSEEK_API_KEY` | From platform.deepseek.com. |

## Secret in **each watched repo**

Just one:

| Name | Purpose |
|---|---|
| `WATCHER_DISPATCH_PAT` | Classic PAT with `repo` scope on `arkhivar/tg-repo-watcher`. Used to fire the dispatch. |

You can reuse the same PAT as `GH_PAT` — it needs `repo` scope either way.

## Adding a repo to watch

1. Add an entry to `config.yml`:
   ```yaml
   my-new-project:
     repo: arkhivar/my-new-project
     chat_id: -1003947505610
     thread_id: 42
     trigger: webhook
   ```
2. Add the `WATCHER_DISPATCH_PAT` secret to that repo.
3. Copy [`caller.yml`](./caller.yml) into that repo at `.github/workflows/notify.yml`.
4. Done. Next push will trigger a summary.

## Local test (optional)

```bash
export GH_PAT=...
export TELEGRAM_BOT_TOKEN=...
export DEEPSEEK_API_KEY=...
export DISPATCH_PAYLOAD='{"repo":"arkhivar/tg-repo-watcher","event":"push","ref":"refs/heads/main","actor":"you","before":"<sha>","after":"<sha>"}'
python scripts/summarize.py
```
