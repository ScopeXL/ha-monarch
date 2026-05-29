# monarch

A small read-only CLI for querying [Monarch Money](https://www.monarchmoney.com/) from the terminal. Renders results as rich tables by default, or raw JSON with `--json`.

## Install

Requires Python 3.10+.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the env template and fill in your Monarch credentials:

```sh
cp .env.example .env
# edit .env
```

`.env` variables:

- `MONARCH_EMAIL` — your Monarch login email
- `MONARCH_PASSWORD` — your Monarch password
- `MONARCH_MFA_SECRET` — *(optional)* TOTP secret from Monarch → Settings → Security → "Set up multi-factor authentication". If set, MFA is handled automatically; otherwise the script prompts for a code on first run.

The session is cached in `.mm/` after the first successful login, so subsequent runs skip the login flow.

## Usage

```sh
python monarch.py --<action> [options]
```

Pick exactly one action flag.

### Actions

| Flag | Description |
| --- | --- |
| `--accounts` | List all accounts with balances |
| `--balance` | Net-worth summary (assets, liabilities, net) |
| `--account-balance` | Balance for a single account (requires `--account-id`) |
| `--transactions` | Recent transactions |
| `--budgets` | Budgets vs. actuals |
| `--cashflow` | Income / expense / savings summary |
| `--holdings` | Brokerage holdings (requires `--account-id`) |
| `--recurring` | Upcoming recurring transactions |
| `--categories` | Transaction categories |
| `--subscription` | Monarch subscription details |

### Options

| Flag | Description |
| --- | --- |
| `--json` | Emit raw JSON instead of a table |
| `--limit N` | Row limit for `--transactions` (default 25) |
| `--start-date YYYY-MM-DD` | Used by `--transactions`, `--budgets`, `--cashflow`, `--recurring` |
| `--end-date YYYY-MM-DD` | Used by `--transactions`, `--budgets`, `--cashflow`, `--recurring` |
| `--search TERM` | Search term for `--transactions` |
| `--account-id ID` | Required with `--holdings` or `--account-balance`; get IDs from `--accounts` |

### Examples

```sh
python monarch.py --accounts
python monarch.py --balance
python monarch.py --transactions --limit 50 --search "amazon"
python monarch.py --transactions --start-date 2026-04-01 --end-date 2026-04-30
python monarch.py --cashflow --start-date 2026-01-01
python monarch.py --holdings --account-id 1234567890
python monarch.py --account-balance --account-id 1234567890
python monarch.py --budgets --json
```

## Web service + dashboard

The same data is also available over HTTP, with a single-page dashboard for at-a-glance viewing.

```sh
python server.py
```

Then open <http://127.0.0.1:8000>. The dashboard pulls from the API on load and includes a Refresh button.

The server reuses the same `.env` credentials and cached `.mm/` session as the CLI — no separate login.

### API endpoints

All return JSON. Query params mirror the CLI flags.

| Endpoint | Notes |
| --- | --- |
| `GET /api/accounts` | All accounts with balances |
| `GET /api/balance` | `{ totals: {assets, liabilities, net}, accounts }` |
| `GET /api/account-balance?account_id=…` | Single account by ID |
| `GET /api/transactions?limit=&start_date=&end_date=&search=` | Recent transactions |
| `GET /api/budgets?start_date=&end_date=` | Budgets vs actuals |
| `GET /api/cashflow?start_date=&end_date=` | Cashflow summary |
| `GET /api/holdings?account_id=…` | Brokerage holdings |
| `GET /api/recurring?start_date=&end_date=` | Upcoming recurring |
| `GET /api/categories` | Transaction categories |
| `GET /api/subscription` | Monarch subscription details |
| `GET /api/docs` | Auto-generated Swagger UI |

The server binds to `127.0.0.1` only by default and has no auth — don't expose it to a network. If you need remote access, put it behind a tunnel (e.g. `ssh -L`). The bind address/port can be overridden with `MONARCH_HOST` / `MONARCH_PORT` (the Home Assistant add-on sets `MONARCH_HOST=0.0.0.0`).

## Home Assistant add-on

This repo doubles as a [Home Assistant add-on repository](https://developers.home-assistant.io/docs/add-ons). The add-on runs the web service under HA's Supervisor, embeds the dashboard in the sidebar via [Ingress](https://developers.home-assistant.io/docs/add-ons/presentation/#ingress), and stores the cached `.mm/` session on the add-on's persistent `/data` volume.

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add this repo's URL.
2. Install **Monarch Money**, then enter your `email` / `password` in the **Configuration** tab (leave `mfa_secret` blank to enter the code manually).
3. Start it and open the **Monarch** sidebar panel. On first start, enter your MFA code once; the session is cached and survives restarts.

Credentials come from the add-on options (no `.env` needed). The add-on publishes port `8000` on the host so Home Assistant and other LAN devices can reach the JSON API — it still has no auth, so only run it on a trusted network.

### Home Assistant sensors

To surface the data as HA entities, use [REST sensors](https://www.home-assistant.io/integrations/sensor.rest/) against the API. A ready-to-edit example — net worth, per-account balances, cashflow, and an auth-health binary sensor — is in [`examples/homeassistant-rests.yaml`](examples/homeassistant-rests.yaml). Include it from your config (e.g. `rest: !include rests.yaml`).

Your account `displayName`s are referenced via `!secret`, so they stay out of the YAML you commit. Copy the keys from [`examples/secrets.yaml`](examples/secrets.yaml) into your Home Assistant `secrets.yaml` (in your main config dir) and fill in your real account names. HA's `!secret` can only replace a whole value and can't be read inside a Jinja template, so each full per-account template is stored as its own secret. Keep your real `secrets.yaml` out of source control.

Optionally, [`examples/homeassistant-automation.yaml`](examples/homeassistant-automation.yaml) notifies you when the add-on loses its login and needs a re-auth (otherwise the sensors just go stale silently). For dashboards, [`examples/homeassistant-dashboard-cards.yaml`](examples/homeassistant-dashboard-cards.yaml) charts daily spending (last 7 days) as a bar chart and lists your 10 most recent transactions as a table.

## Notes

- If Monarch rate-limits login (HTTP 429), wait ~15–60 minutes before retrying. Once you log in successfully, the cached session in `.mm/` avoids the login endpoint entirely.
- `.env` and `.mm/` are gitignored — don't commit them.
