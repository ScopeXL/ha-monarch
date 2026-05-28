#!/usr/bin/env python3
"""FastAPI web service exposing Monarch Money queries as JSON, plus a dashboard.

Bound to 127.0.0.1 by design - same trust model as the CLI. Reuses the existing
.env credentials and cached .mm/ session. Login runs in the background at
startup; if MFA is required, /api/* returns 503 until the dashboard submits a
code via POST /api/auth/mfa.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from gql import gql
from monarchmoney import LoginFailedException, MonarchMoney, RequireMFAException
from pydantic import BaseModel

from monarch import compute_net_worth


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Kick off login in the background; never block startup on it.
    asyncio.create_task(auth.begin_login())
    yield


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
    host = os.environ.get("MONARCH_HOST", "127.0.0.1")
    port = int(os.environ.get("MONARCH_PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=False)
