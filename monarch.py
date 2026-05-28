#!/usr/bin/env python3
"""Query Monarch Money from the terminal.

Pick one action flag (e.g. --accounts, --transactions). Default output is a
table; add --json for raw JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Callable

from dotenv import load_dotenv
from monarchmoney import LoginFailedException, MonarchMoney, RequireMFAException
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True, style="bold red")


async def get_client() -> MonarchMoney:
    load_dotenv()
    try:
        email = os.environ["MONARCH_EMAIL"]
        password = os.environ["MONARCH_PASSWORD"]
    except KeyError as e:
        err_console.print(f"Missing env var {e}. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    mfa_secret = os.environ.get("MONARCH_MFA_SECRET") or None

    mm = MonarchMoney()
    try:
        await mm.login(
            email=email,
            password=password,
            use_saved_session=True,
            save_session=True,
            mfa_secret_key=mfa_secret,
        )
    except RequireMFAException:
        code = input("MFA code: ").strip()
        await mm.multi_factor_authenticate(email, password, code)
        mm.save_session()
    except LoginFailedException as e:
        msg = str(e)
        if "429" in msg:
            err_console.print(
                "Monarch rate-limited the login (HTTP 429). Wait ~15–60 minutes before retrying.\n"
                "Tip: once you log in successfully once, the session is cached in .mm/ and future runs skip login."
            )
        else:
            err_console.print(f"Login failed: {msg}")
        sys.exit(1)
    return mm


def emit_json(data: Any) -> None:
    console.print_json(json.dumps(data, default=str))


def money(val: Any) -> str:
    if val is None:
        return ""
    try:
        n = float(val)
    except (TypeError, ValueError):
        return str(val)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def fmt_accounts(data: dict) -> None:
    accounts = data.get("accounts", [])
    table = Table(title=f"Accounts ({len(accounts)})")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Subtype")
    table.add_column("Institution")
    table.add_column("Balance", justify="right")
    table.add_column("ID", style="dim")
    for a in accounts:
        atype = (a.get("type") or {}).get("display") or (a.get("type") or {}).get("name", "")
        subtype = (a.get("subtype") or {}).get("display") or (a.get("subtype") or {}).get("name", "")
        inst = ((a.get("institution") or {}) or {}).get("name", "") or ((a.get("credential") or {}).get("institution") or {}).get("name", "")
        bal = a.get("displayBalance") if a.get("displayBalance") is not None else a.get("currentBalance")
        table.add_row(
            str(a.get("displayName") or a.get("name", "")),
            str(atype),
            str(subtype),
            str(inst),
            money(bal),
            str(a.get("id", "")),
        )
    console.print(table)


def compute_net_worth(accounts: list[dict]) -> dict:
    assets = 0.0
    liabilities = 0.0
    for a in accounts:
        bal = a.get("displayBalance") if a.get("displayBalance") is not None else a.get("currentBalance")
        try:
            n = float(bal or 0)
        except (TypeError, ValueError):
            continue
        if a.get("isAsset", n >= 0):
            assets += n
        else:
            liabilities += n
    return {"assets": assets, "liabilities": -abs(liabilities), "net": assets - abs(liabilities)}


def fmt_balance(data: dict) -> None:
    totals = compute_net_worth(data.get("accounts", []))
    assets = totals["assets"]
    liabilities = totals["liabilities"]
    net = totals["net"]
    table = Table(title="Net worth")
    table.add_column("Bucket")
    table.add_column("Amount", justify="right")
    table.add_row("Assets", money(assets))
    table.add_row("Liabilities", money(liabilities))
    table.add_row("Net worth", money(net), style="bold")
    console.print(table)


def fmt_account_balance(data: dict) -> None:
    account = data.get("account")
    if not account:
        err_console.print(f"No account found with id {data.get('requested_id', '?')}")
        sys.exit(1)
    bal = account.get("displayBalance") if account.get("displayBalance") is not None else account.get("currentBalance")
    atype = (account.get("type") or {}).get("display") or (account.get("type") or {}).get("name", "")
    inst = (account.get("institution") or {}).get("name", "") or ((account.get("credential") or {}).get("institution") or {}).get("name", "")
    table = Table(title="Account balance")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Name", str(account.get("displayName") or account.get("name", "")))
    table.add_row("Type", str(atype))
    table.add_row("Institution", str(inst))
    table.add_row("Balance", money(bal), style="bold")
    table.add_row("ID", str(account.get("id", "")), style="dim")
    console.print(table)


def fmt_transactions(data: dict) -> None:
    results = (data.get("allTransactions") or {}).get("results") or data.get("results") or []
    total = (data.get("allTransactions") or {}).get("totalCount", len(results))
    table = Table(title=f"Transactions (showing {len(results)} of {total})")
    table.add_column("Date")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Account")
    table.add_column("Amount", justify="right")
    for t in results:
        merchant = (t.get("merchant") or {}).get("name", "")
        category = (t.get("category") or {}).get("name", "")
        account = (t.get("account") or {}).get("displayName") or (t.get("account") or {}).get("name", "")
        amt = t.get("amount")
        style = "red" if isinstance(amt, (int, float)) and amt < 0 else "green"
        table.add_row(
            str(t.get("date", "")),
            str(merchant),
            str(category),
            str(account),
            f"[{style}]{money(amt)}[/{style}]",
        )
    console.print(table)


def fmt_budgets(data: dict) -> None:
    rows = (
        data.get("budgetData", {}).get("monthlyAmountsByCategory")
        or data.get("categoryGroups")
        or []
    )
    if not rows:
        emit_json(data)
        return
    table = Table(title="Budgets")
    table.add_column("Category")
    table.add_column("Budgeted", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Remaining", justify="right")
    for r in rows:
        name = (r.get("category") or {}).get("name") or r.get("name", "")
        amounts = r.get("monthlyAmounts") or [{}]
        latest = amounts[-1] if amounts else {}
        planned = latest.get("plannedCashFlowAmount", 0)
        actual = latest.get("actualAmount", 0)
        remaining = latest.get("remainingAmount", (planned or 0) - (actual or 0))
        table.add_row(str(name), money(planned), money(actual), money(remaining))
    console.print(table)


def fmt_cashflow(data: dict) -> None:
    summary = (data.get("summary") or [{}])[0] if isinstance(data.get("summary"), list) else data.get("summary", {})
    income = (summary or {}).get("sumIncome") or (summary or {}).get("summary", {}).get("sumIncome")
    expense = (summary or {}).get("sumExpense") or (summary or {}).get("summary", {}).get("sumExpense")
    savings = (summary or {}).get("savings") or (summary or {}).get("summary", {}).get("savings")
    savings_rate = (summary or {}).get("savingsRate") or (summary or {}).get("summary", {}).get("savingsRate")
    if income is None and expense is None:
        emit_json(data)
        return
    table = Table(title="Cashflow summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Income", money(income))
    table.add_row("Expenses", money(expense))
    table.add_row("Savings", money(savings))
    if savings_rate is not None:
        try:
            table.add_row("Savings rate", f"{float(savings_rate) * 100:.1f}%")
        except (TypeError, ValueError):
            table.add_row("Savings rate", str(savings_rate))
    console.print(table)


def fmt_holdings(data: dict) -> None:
    holdings = (
        (data.get("portfolio") or {}).get("aggregateHoldings", {}).get("edges")
        or data.get("holdings")
        or []
    )
    if not holdings:
        emit_json(data)
        return
    table = Table(title=f"Holdings ({len(holdings)})")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Quantity", justify="right")
    table.add_column("Value", justify="right")
    for h in holdings:
        node = h.get("node", h)
        sec = node.get("security") or {}
        ticker = sec.get("ticker", "")
        name = sec.get("name", "")
        qty = node.get("quantity") or node.get("totalQuantity")
        val = node.get("totalValue") or node.get("value")
        table.add_row(str(ticker), str(name), str(qty or ""), money(val))
    console.print(table)


def fmt_recurring(data: dict) -> None:
    items = data.get("recurringTransactionItems") or data.get("recurringStreams") or []
    if not items:
        emit_json(data)
        return
    table = Table(title=f"Recurring ({len(items)})")
    table.add_column("Next date")
    table.add_column("Merchant")
    table.add_column("Account")
    table.add_column("Amount", justify="right")
    for r in items:
        stream = r.get("stream") or r
        merchant = (stream.get("merchant") or {}).get("name", "") or stream.get("name", "")
        account = (r.get("account") or stream.get("account") or {}).get("displayName", "")
        amount = r.get("amount") or stream.get("amount")
        date = r.get("date") or r.get("nextDate") or stream.get("nextForecastedTransactionDate", "")
        table.add_row(str(date), str(merchant), str(account), money(amount))
    console.print(table)


def fmt_categories(data: dict) -> None:
    cats = data.get("categories", [])
    table = Table(title=f"Categories ({len(cats)})")
    table.add_column("Group")
    table.add_column("Name")
    table.add_column("ID", style="dim")
    for c in cats:
        group = (c.get("group") or {}).get("name", "")
        table.add_row(str(group), str(c.get("name", "")), str(c.get("id", "")))
    console.print(table)


def fmt_subscription(data: dict) -> None:
    sub = data.get("subscription", data)
    table = Table(title="Subscription")
    table.add_column("Field")
    table.add_column("Value")
    if isinstance(sub, dict):
        for k, v in sub.items():
            table.add_row(str(k), str(v))
    else:
        emit_json(data)
        return
    console.print(table)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monarch",
        description="Read-only Monarch Money queries. Pick one action flag.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--accounts", action="store_true", help="List all accounts with balances")
    group.add_argument("--balance", action="store_true", help="Net-worth summary")
    group.add_argument("--account-balance", action="store_true", help="Balance for a single account (requires --account-id)")
    group.add_argument("--transactions", action="store_true", help="Recent transactions")
    group.add_argument("--budgets", action="store_true", help="Budgets vs actuals")
    group.add_argument("--cashflow", action="store_true", help="Cashflow summary")
    group.add_argument("--holdings", action="store_true", help="Brokerage holdings (requires --account-id)")
    group.add_argument("--recurring", action="store_true", help="Upcoming recurring transactions")
    group.add_argument("--categories", action="store_true", help="Transaction categories")
    group.add_argument("--subscription", action="store_true", help="Monarch subscription details")

    p.add_argument("--json", dest="as_json", action="store_true", help="Emit raw JSON")
    p.add_argument("--limit", type=int, default=25, help="Limit for --transactions (default 25)")
    p.add_argument("--start-date", help="YYYY-MM-DD, used by --transactions/--budgets/--cashflow/--recurring")
    p.add_argument("--end-date", help="YYYY-MM-DD, used by --transactions/--budgets/--cashflow/--recurring")
    p.add_argument("--search", default="", help="Search term for --transactions")
    p.add_argument("--account-id", help="Account ID, required with --holdings or --account-balance")
    return p


async def run(args: argparse.Namespace) -> tuple[Any, Callable[[dict], None]]:
    mm = await get_client()
    if args.accounts:
        return await mm.get_accounts(), fmt_accounts
    if args.balance:
        return await mm.get_accounts(), fmt_balance
    if args.account_balance:
        if not args.account_id:
            err_console.print("--account-balance requires --account-id (get one from --accounts)")
            sys.exit(2)
        data = await mm.get_accounts()
        match = next((a for a in data.get("accounts", []) if str(a.get("id")) == str(args.account_id)), None)
        return {"account": match, "requested_id": args.account_id}, fmt_account_balance
    if args.transactions:
        data = await mm.get_transactions(
            limit=args.limit,
            start_date=args.start_date,
            end_date=args.end_date,
            search=args.search,
        )
        return data, fmt_transactions
    if args.budgets:
        return await mm.get_budgets(start_date=args.start_date, end_date=args.end_date), fmt_budgets
    if args.cashflow:
        return await mm.get_cashflow_summary(start_date=args.start_date, end_date=args.end_date), fmt_cashflow
    if args.holdings:
        if not args.account_id:
            err_console.print("--holdings requires --account-id (get one from --accounts)")
            sys.exit(2)
        return await mm.get_account_holdings(int(args.account_id)), fmt_holdings
    if args.recurring:
        return await mm.get_recurring_transactions(start_date=args.start_date, end_date=args.end_date), fmt_recurring
    if args.categories:
        return await mm.get_transaction_categories(), fmt_categories
    if args.subscription:
        return await mm.get_subscription_details(), fmt_subscription
    raise AssertionError("argparse should have caught this")


async def main() -> None:
    args = build_parser().parse_args()
    data, formatter = await run(args)
    if args.as_json:
        emit_json(data)
    else:
        formatter(data)


if __name__ == "__main__":
    asyncio.run(main())
