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

The server binds to `127.0.0.1` only and has no auth — don't expose it to a network. If you need remote access, put it behind a tunnel (e.g. `ssh -L`).

## Notes

- If Monarch rate-limits login (HTTP 429), wait ~15–60 minutes before retrying. Once you log in successfully, the cached session in `.mm/` avoids the login endpoint entirely.
- `.env` and `.mm/` are gitignored — don't commit them.
