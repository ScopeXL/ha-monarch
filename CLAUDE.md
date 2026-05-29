# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A **read-only** Monarch Money dashboard, CLI, and JSON API, also packaged as a
Home Assistant add-on. It queries the Monarch Money API via the community SDK
and displays accounts, balances, net worth, transactions, budgets, cashflow,
holdings, recurring transactions, and categories. Nothing here ever modifies a
Monarch account — keep it that way.

## Layout

| Path | Purpose |
|------|---------|
| `monarch.py` | CLI entry point. Async client + Rich table / JSON formatters. |
| `server.py` | FastAPI web service. All `/api/*` endpoints + auth state machine. |
| `static/index.html` | Single-page dashboard (vanilla JS + Tailwind, no build step). |
| `requirements.txt` | Python deps. |
| `monarch_addon/` | Home Assistant add-on packaging (see below). |
| `examples/` | HA REST sensors, automations, and dashboard cards. |
| `.env.example` | Credential template. Real `.env` and `.mm/` are gitignored. |

## Tech stack

- Python 3.10+, async/await throughout.
- Key deps: `monarchmoneycommunity`, `fastapi`, `uvicorn`, `rich`, `python-dotenv`.
- Frontend is plain HTML/JS/Tailwind — there is **no build step** and no bundler.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in MONARCH_EMAIL / MONARCH_PASSWORD / MONARCH_MFA_SECRET

python monarch.py --balance     # CLI (also --accounts, --transactions, --cashflow, etc.)
python server.py                # web dashboard + API on 127.0.0.1:8000
```

The server binds `127.0.0.1` by default; override with `MONARCH_HOST` / `MONARCH_PORT`.
Endpoints return 503 until the one-time MFA login completes (via dashboard or
`POST /api/auth/mfa`). Session is cached in `.mm/` and survives restarts.

## Tests & linting

There is **no test suite and no linter/formatter configured**. When changing
Python, match the existing style (async, type hints where present, Rich for CLI
output). Don't introduce a build step or framework for the frontend.

## Home Assistant add-on — version/release coupling

This is the one non-obvious gotcha. `monarch_addon/Dockerfile` pulls the app
from a GitHub release tag matching the add-on version:

```
ADD https://github.com/ScopeXL/ha-monarch/archive/refs/tags/v${BUILD_VERSION}.tar.gz
```

`BUILD_VERSION` comes from `version:` in `monarch_addon/config.yaml`. So:

- **Bumping `config.yaml` `version:` requires a matching `v<version>` git tag /
  GitHub release**, or the add-on build will fail to download the app.
- App code changes (in `monarch.py`, `server.py`, `static/`) only reach add-on
  users after a version bump + tagged release — they are not picked up from the
  branch.

When you change app code that add-on users should get, flag that a version bump
and release are needed; don't bump the version silently.

## Git workflow — commit and push proactively

Commit and push on your own whenever you reach a sensible stopping point — the
user prefers not to be asked each time. Specifically:

- After completing a self-contained change (a fix, a feature, a doc update),
  stage the relevant files, commit with a clear message, and **push to `main`**.
- Group related edits into one logical commit; don't commit half-finished work
  or leave the tree dirty across unrelated changes.
- Commit message style (from the existing history): a short imperative summary
  line, then a brief body explaining the *why* when it isn't obvious. Wrap the
  body. End commit messages with the standard `Co-Authored-By` trailer.
- **Do not commit secrets.** `.env` and `.mm/` are gitignored — keep it so, and
  never hard-code real credentials, account numbers, or session data. The
  example configs use placeholder account names/IDs on purpose.
- **Keep private things private going forward.** This is a public repo. Anything
  user-specific and sensitive — account `displayName`s/numbers, real LAN IPs,
  tokens — must not land in committed files. For Home Assistant examples, the
  pattern is to reference such values via `!secret` and document the keys (with
  placeholders only) in `examples/secrets.yaml`; the real `secrets.yaml` lives
  in the user's HA config, never here. Note `!secret` replaces a whole value and
  can't be read inside a Jinja template, so the *whole* resource string or
  per-account template is what moves into secrets. Non-sensitive values (e.g.
  `localhost` URLs) can stay inline. Already-leaked history is left as-is by
  decision — the goal is preventing new leaks, not rewriting the past.

When in doubt about whether a change is a good stopping point, prefer committing
small and often over batching everything into one large commit.
