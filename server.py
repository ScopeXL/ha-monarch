#!/usr/bin/env python3
"""FastAPI web service exposing Monarch Money queries as JSON, plus a dashboard.

Bound to 127.0.0.1 by design - same trust model as the CLI. Reuses the existing
.env credentials and cached .mm/ session. Login runs in the background at
startup; if MFA is required, /api/* returns 503 until the dashboard submits a
code via POST /api/auth/mfa.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from gql import gql
from monarchmoney import LoginFailedException, MonarchMoney, RequireMFAException
from pydantic import BaseModel

from monarch import compute_net_worth

# Read .env at import so RefreshState (constructed below) sees the schedule
# config. begin_login() calls load_dotenv() again, which is harmless.
load_dotenv()
log = logging.getLogger("monarch.server")


# Mirrors the library's GetAccounts query (monarchmoney.py:188) but adds
# `limit` so we can show credit-card utilization. The library doesn't request
# it; the field exists on the Monarch GraphQL schema (probed manually).
_GET_ACCOUNTS_WITH_LIMIT = gql(
    """
    query GetAccountsWithLimit {
      accounts {
        ...AccountFields
        __typename
      }
      householdPreferences {
        id
        accountGroupOrder
        __typename
      }
    }

    fragment AccountFields on Account {
      id
      displayName
      syncDisabled
      deactivatedAt
      isHidden
      isAsset
      mask
      createdAt
      updatedAt
      displayLastUpdatedAt
      hasSyncInProgress
      currentBalance
      displayBalance
      limit
      includeInNetWorth
      hideFromList
      hideTransactionsFromReports
      includeBalanceInNetWorth
      includeInGoalBalance
      dataProvider
      dataProviderAccountId
      isManual
      transactionsCount
      holdingsCount
      manualInvestmentsTrackingMethod
      order
      logoUrl
      type {
        name
        display
        __typename
      }
      subtype {
        name
        display
        __typename
      }
      credential {
        id
        updateRequired
        disconnectedFromDataProviderAt
        dataProvider
        institution {
          id
          plaidInstitutionId
          name
          status
          __typename
        }
        __typename
      }
      institution {
        id
        name
        primaryColor
        url
        __typename
      }
      __typename
    }
    """
)


async def fetch_accounts(client: "MonarchMoney") -> dict:
    """Drop-in replacement for client.get_accounts() that also returns `limit`."""
    return await client.gql_call(
        operation="GetAccountsWithLimit",
        graphql_query=_GET_ACCOUNTS_WITH_LIMIT,
    )

STATIC_DIR = Path(__file__).parent / "static"


class AuthState:
    """Tracks login progress so HTTP requests never block on terminal input."""

    def __init__(self) -> None:
        self.status: str = "pending"  # pending | awaiting_mfa | error | ready
        self.message: str = ""
        self.client: MonarchMoney | None = None
        self._email: str = ""
        self._password: str = ""
        self._mfa_secret: str | None = None
        self._pending_mm: MonarchMoney | None = None

    async def begin_login(self) -> None:
        load_dotenv()
        email = os.environ.get("MONARCH_EMAIL")
        password = os.environ.get("MONARCH_PASSWORD")
        if not email or not password:
            self.status = "error"
            self.message = "Missing MONARCH_EMAIL or MONARCH_PASSWORD in .env"
            return
        self._email = email
        self._password = password
        self._mfa_secret = os.environ.get("MONARCH_MFA_SECRET") or None

        mm = MonarchMoney()
        try:
            await mm.login(
                email=email,
                password=password,
                use_saved_session=True,
                save_session=True,
                mfa_secret_key=self._mfa_secret,
            )
        except RequireMFAException:
            self._pending_mm = mm
            self.status = "awaiting_mfa"
            self.message = "Enter your 6-digit MFA code."
            return
        except LoginFailedException as e:
            msg = str(e)
            self.status = "error"
            if "429" in msg:
                self.message = (
                    "Monarch rate-limited the login (HTTP 429). "
                    "Wait ~15-60 minutes before retrying."
                )
            else:
                self.message = f"Login failed: {msg}"
            return

        self.client = mm
        self.status = "ready"
        self.message = ""

    async def submit_mfa(self, code: str) -> bool:
        if self.status != "awaiting_mfa":
            raise HTTPException(status_code=400, detail=f"Not awaiting MFA (status={self.status})")
        mm = self._pending_mm or MonarchMoney()
        try:
            await mm.multi_factor_authenticate(self._email, self._password, code)
            mm.save_session()
        except Exception as e:
            self.message = f"MFA failed: {e}. Try again."
            return False
        self.client = mm
        self._pending_mm = None
        self.status = "ready"
        self.message = ""
        return True


auth = AuthState()


# --- Account refresh scheduling -----------------------------------------

def _load_tz() -> tuple[ZoneInfo | None, str]:
    """Resolve MONARCH_TZ to a tzinfo. Returns (tz_or_None, label); None means
    "use the host's local time". Falls back to local on an unset/invalid name."""
    name = (os.environ.get("MONARCH_TZ") or "").strip()
    if not name:
        return None, "local"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Invalid MONARCH_TZ=%r; using host local time", name)
        return None, "local"


def _parse_times(raw: str | None) -> list[dtime]:
    """Parse "06:00,12:00,18:00" into sorted, de-duplicated times of day.
    Invalid entries are skipped with a warning."""
    out: set[dtime] = set()
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            hh, mm = tok.split(":")
            out.add(dtime(int(hh), int(mm)))
        except (ValueError, TypeError):
            log.warning("Ignoring invalid MONARCH_REFRESH_TIMES entry: %r", tok)
    return sorted(out)


class RefreshState:
    """Schedule config plus the status of the last Monarch account refresh.

    A refresh asks Monarch to re-sync every account from its institution. We
    fire the request and record that we asked; we do *not* poll to a timeout —
    a slow institution can take many minutes, and Monarch may leave a sync
    flagged in-progress long after, so a "timeout" verdict misrepresented a
    request Monarch had actually accepted. Live, per-account progress is
    surfaced instead via each account's `hasSyncInProgress` /
    `displayLastUpdatedAt` (see /api/accounts and the dashboard). The single
    asyncio.Lock keeps a manual and a scheduled request from firing at once.
    """

    def __init__(self) -> None:
        self.tz, self.tz_label = _load_tz()
        self.scheduled_times = _parse_times(os.environ.get("MONARCH_REFRESH_TIMES"))

        self.last_requested_at: datetime | None = None
        self.last_reason: str | None = None
        self.last_result: str | None = None  # requested | error
        self.last_error: str | None = None
        self.next_scheduled_at: datetime | None = None
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    def _combine(self, day, t: dtime) -> datetime:
        if self.tz:
            return datetime.combine(day, t, tzinfo=self.tz)
        # Interpret the wall-clock time in host-local time (DST-correct).
        return datetime.combine(day, t).astimezone()

    def compute_next(self, after: datetime) -> datetime | None:
        """Earliest scheduled datetime strictly after `after`, or None when no
        times are configured."""
        if not self.scheduled_times:
            return None
        for day_offset in range(0, 8):
            day = (after + timedelta(days=day_offset)).date()
            for t in self.scheduled_times:
                cand = self._combine(day, t)
                if cand > after:
                    return cand
        return None  # unreachable: 8 days always contains a future slot

    def to_dict(self) -> dict:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        return {
            "last_requested_at": iso(self.last_requested_at),
            "last_reason": self.last_reason,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "next_scheduled_at": iso(self.next_scheduled_at),
            "scheduled_times": [t.strftime("%H:%M") for t in self.scheduled_times],
            "tz": self.tz_label,
            "enabled": bool(self.scheduled_times),
        }


rs = RefreshState()


async def _do_refresh(reason: str) -> None:
    """Ask Monarch to re-sync every account from its institution, then return.

    Shared by the manual endpoint and the scheduler. We fire the force-refresh
    and record that Monarch accepted it (last_result="requested"); we do not
    wait for completion. Progress is observed per-account afterwards via
    `hasSyncInProgress` / `displayLastUpdatedAt`. Single-flight via the lock so a
    manual and a scheduled request can't fire at the same instant. Never raises —
    a failed request is recorded on `rs` (last_result="error", last_error).
    """
    if rs._lock.locked():
        log.info("Refresh (%s) skipped: a refresh request is already in flight", reason)
        return
    async with rs._lock:
        if auth.status != "ready" or auth.client is None:
            log.warning("Refresh (%s) skipped: auth not ready (status=%s)", reason, auth.status)
            return
        client = auth.client
        rs.last_reason = reason
        rs.last_error = None
        log.info("Account refresh requested (%s)", reason)
        try:
            data = await fetch_accounts(client)
            ids = [str(a["id"]) for a in data.get("accounts", []) if a.get("id") is not None]
            if not ids:
                rs.last_result = "error"
                rs.last_error = "No account IDs returned by Monarch"
                log.warning("Refresh (%s): no account IDs to refresh", reason)
                return
            await client.request_accounts_refresh(ids)
            rs.last_result = "requested"
            rs.last_requested_at = rs._now()
            log.info("Account refresh (%s): Monarch accepted the request for %d account(s)",
                     reason, len(ids))
        except Exception as e:
            rs.last_result = "error"
            rs.last_error = str(e)
            log.exception("Account refresh (%s) request failed", reason)


async def _scheduler_loop() -> None:
    """Fire _do_refresh at each configured time of day. Resilient: never dies on
    an exception, re-evaluates the next slot periodically so DST/suspend clock
    jumps can't make it drift, and waits for auth before firing."""
    if not rs.scheduled_times:
        log.info("Refresh scheduler disabled (MONARCH_REFRESH_TIMES is empty)")
        return
    log.info(
        "Refresh scheduler enabled: times=%s tz=%s",
        [t.strftime("%H:%M") for t in rs.scheduled_times], rs.tz_label,
    )
    while True:
        try:
            now = rs._now()
            nxt = rs.compute_next(now)
            rs.next_scheduled_at = nxt
            if nxt is None:
                await asyncio.sleep(3600)
                continue
            # Cap each nap so a DST change or laptop suspend/resume gets noticed
            # within ~5 min instead of letting an absolute sleep drift.
            await asyncio.sleep(min(max((nxt - now).total_seconds(), 0), 300))
            if rs._now() < nxt:
                continue  # woke early from the cap; recompute and keep waiting
            # Reached the slot. Wait (bounded) for login before firing so we
            # don't fire into a 503; if it never readies, skip and move on.
            waited = 0
            while auth.status != "ready" and waited < 600:
                await asyncio.sleep(5)
                waited += 5
            if auth.status == "ready":
                await _do_refresh(reason=f"scheduled {nxt.strftime('%H:%M')}")
            else:
                log.warning("Skipped scheduled refresh at %s: auth not ready (status=%s)",
                            nxt.strftime("%H:%M"), auth.status)
            # Advance strictly past this slot so it can never re-fire.
            after = rs._now()
            if after <= nxt:
                after = nxt + timedelta(seconds=1)
            rs.next_scheduled_at = rs.compute_next(after)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Refresh scheduler iteration failed; retrying in 60s")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Kick off login in the background; never block startup on it.
    asyncio.create_task(auth.begin_login())
    scheduler_task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task


app = FastAPI(title="Monarch dashboard", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


async def mm() -> MonarchMoney:
    if auth.status != "ready" or auth.client is None:
        raise HTTPException(
            status_code=503,
            detail=f"Auth not ready (status={auth.status}). Check /api/auth/status.",
        )
    return auth.client


def _ok(data: Any) -> JSONResponse:
    return JSONResponse(content=data)


async def _safe(coro):
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call failed: {e}")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- Auth ---------------------------------------------------------------

class MfaBody(BaseModel):
    code: str


@app.get("/api/auth/status")
async def api_auth_status():
    return {"status": auth.status, "message": auth.message}


@app.post("/api/auth/mfa")
async def api_auth_mfa(body: MfaBody):
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    ok = await auth.submit_mfa(code)
    return {"ok": ok, "status": auth.status, "message": auth.message}


# --- Refresh ------------------------------------------------------------

@app.get("/api/refresh/status")
async def api_refresh_status():
    """Schedule + last-refresh-request status: the next scheduled time, when we
    last asked Monarch to sync, and whether that request was accepted or failed.
    Reads in-memory state only, so it is safe to poll before login completes.
    (Live per-account sync progress comes from /api/accounts.)"""
    return rs.to_dict()


@app.post("/api/refresh/sync")
async def api_refresh_sync():
    """Trigger a Monarch account refresh now. Non-blocking: fires the request in
    the background and returns immediately. The dashboard then watches each
    account's hasSyncInProgress to show live, per-account progress."""
    if auth.status != "ready" or auth.client is None:
        raise HTTPException(
            status_code=503,
            detail=f"Auth not ready (status={auth.status}). Check /api/auth/status.",
        )
    asyncio.create_task(_do_refresh(reason="manual"))
    return rs.to_dict()


# --- Data ---------------------------------------------------------------

@app.get("/api/accounts")
async def api_accounts():
    client = await mm()
    return _ok(await _safe(fetch_accounts(client)))


@app.get("/api/balance")
async def api_balance():
    client = await mm()
    data = await _safe(fetch_accounts(client))
    return _ok({"totals": compute_net_worth(data.get("accounts", [])), "accounts": data.get("accounts", [])})


@app.get("/api/account-balance")
async def api_account_balance(account_id: str = Query(...)):
    client = await mm()
    data = await _safe(fetch_accounts(client))
    match = next((a for a in data.get("accounts", []) if str(a.get("id")) == str(account_id)), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No account with id {account_id}")
    return _ok({"account": match})


@app.get("/api/transactions")
async def api_transactions(
    limit: int = 25,
    start_date: str | None = None,
    end_date: str | None = None,
    search: str = "",
):
    client = await mm()
    return _ok(await _safe(client.get_transactions(
        limit=limit, start_date=start_date, end_date=end_date, search=search,
    )))


@app.get("/api/budgets")
async def api_budgets(start_date: str | None = None, end_date: str | None = None):
    client = await mm()
    return _ok(await _safe(client.get_budgets(start_date=start_date, end_date=end_date)))


@app.get("/api/cashflow")
async def api_cashflow(start_date: str | None = None, end_date: str | None = None):
    client = await mm()
    return _ok(await _safe(client.get_cashflow_summary(start_date=start_date, end_date=end_date)))


@app.get("/api/holdings")
async def api_holdings(account_id: str = Query(...)):
    client = await mm()
    return _ok(await _safe(client.get_account_holdings(int(account_id))))


@app.get("/api/recurring")
async def api_recurring(start_date: str | None = None, end_date: str | None = None):
    client = await mm()
    return _ok(await _safe(client.get_recurring_transactions(start_date=start_date, end_date=end_date)))


@app.get("/api/categories")
async def api_categories():
    client = await mm()
    return _ok(await _safe(client.get_transaction_categories()))


@app.get("/api/subscription")
async def api_subscription():
    client = await mm()
    return _ok(await _safe(client.get_subscription_details()))


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("MONARCH_HOST", "127.0.0.1")
    port = int(os.environ.get("MONARCH_PORT", "8000"))
    # Every /api/* call proxies Monarch's API and takes several seconds. Home
    # Assistant's `rest:` integration reuses a single pooled keep-alive
    # connection across the sensors it polls at each tick, so the gap between
    # two reused requests routinely exceeds uvicorn's default 5s keep-alive
    # window. uvicorn then closes the idle connection and HA's next reused
    # request fails with "Server disconnected" / "Connection reset by peer",
    # leaving the sensor "Unknown". Hold idle connections open well past that
    # gap so the client never reuses a connection the server just closed.
    keep_alive = int(os.environ.get("MONARCH_KEEPALIVE", "120"))
    uvicorn.run(
        "server:app", host=host, port=port, reload=False,
        timeout_keep_alive=keep_alive,
    )
