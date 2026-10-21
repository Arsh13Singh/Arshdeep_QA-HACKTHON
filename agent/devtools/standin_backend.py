"""DEV ONLY: an in-memory stand-in for the DemoBank backend, for environments where FastAPI and
psycopg cannot be installed. It mirrors test-website/backend/demobank/main.py endpoint by endpoint
(same routes, status codes, error codes and logic) and loads the real seed.sql.

The QA agent never imports this. It is only a local target for developing the agent; the real
evaluation runs against the organisers' hosted app (or the real backend via LOCAL_SETUP.md).

    python devtools/standin_backend.py --seed ../test-website/database/seed.sql --port 8000
"""
from __future__ import annotations

import argparse
import re
import secrets
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

PACIFIC = ZoneInfo("America/Los_Angeles")
COOKIE = "demobank_session"
MONEY_PATTERN = re.compile(r"^-?(?:0|[1-9]\d{0,9})(?:\.\d{1,2})?$")   # copied from schemas.py
DB: dict = {}


def load_seed(path: str) -> None:
    sql = open(path).read()
    tup = re.compile(r"\(([^()]*)\)")
    def section(name):
        m = re.search(rf"INSERT INTO {name}.*?VALUES(.*?);", sql, re.S)
        rows = []
        for t in tup.findall(m.group(1)):
            vals = [v.strip().strip("'") for v in re.findall(r"'[^']*'|[-\d.]+", t)]
            rows.append(vals)
        return rows
    c = section("customers")[0]
    DB["customer"] = {"id": c[0], "display_name": c[1], "masked_email": c[2]}
    DB["accounts"] = {r[0]: {"id": r[0], "name": r[2], "type": r[3], "masked_number": r[4], "currency": r[5],
                             "balance": Decimal(r[6]), "created_at": r[7]} for r in section("accounts")}
    def ts(s): return datetime.fromisoformat(s.replace("+00", "+00:00"))
    DB["transactions"] = {r[0]: {"id": r[0], "type": r[2], "status": r[3], "amount": Decimal(r[4]), "memo": r[5],
                                 "occurred_at": ts(r[6])} for r in section("transactions")}
    DB["entries"] = [{"id": r[0], "transaction_id": r[1], "account_id": r[2], "amount_delta": Decimal(r[3]),
                      "created_at": ts(r[4])} for r in section("transaction_entries")]
    DB["sessions"] = set()


def err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def money(v: Decimal) -> str:
    return f"{v:.2f}"


def account_out(a):
    return {k: (money(a[k]) if k == "balance" else a[k]) for k in ("id", "name", "type", "masked_number", "currency", "balance")}


def hydrate(tx):
    entries = sorted([e for e in DB["entries"] if e["transaction_id"] == tx["id"]], key=lambda e: (e["created_at"], e["id"]))
    def summ(e):
        a = DB["accounts"][e["account_id"]]
        return {"id": a["id"], "name": a["name"], "masked_number": a["masked_number"]}
    src = dst = None
    if tx["type"] == "transfer" and entries:
        se = next((e for e in entries if e["amount_delta"] == -tx["amount"]), entries[0])
        de = next((e for e in entries if e is not se), entries[-1])
        src, dst = summ(se), summ(de)
    elif tx["type"] == "withdrawal" and entries:
        src = summ(entries[0])
    elif tx["type"] == "deposit" and entries:
        dst = summ(entries[0])
    return {"id": tx["id"], "type": tx["type"], "status": tx["status"], "amount": money(tx["amount"]), "memo": tx["memo"],
            "occurred_at": tx["occurred_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_account": src, "destination_account": dst,
            "account_deltas": [{"account_id": e["account_id"], "amount_delta": money(e["amount_delta"])} for e in entries]}


def authed(request: Request) -> bool:
    return request.cookies.get(COOKIE) in DB["sessions"]


def valid_uuid(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


async def health(_):
    return JSONResponse({"status": "ok"})


async def session(request: Request):
    if request.method == "POST":
        token = secrets.token_urlsafe(32)
        DB["sessions"].add(token)
        r = JSONResponse({"customer": DB["customer"]}, status_code=201)
        r.set_cookie(COOKIE, token, max_age=86400, path="/", httponly=True, samesite="lax")
        return r
    DB["sessions"].discard(request.cookies.get(COOKIE))
    r = Response(status_code=204)
    r.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
    return r


async def me(request):
    if not authed(request):
        return err(401, "unauthenticated", "A valid demo session is required.")
    return JSONResponse(DB["customer"])


async def accounts(request):
    if not authed(request):
        return err(401, "unauthenticated", "A valid demo session is required.")
    accs = sorted(DB["accounts"].values(), key=lambda a: (a["created_at"], a["id"]))
    return JSONResponse({"accounts": [account_out(a) for a in accs]})


async def account(request):
    if not authed(request):
        return err(401, "unauthenticated", "A valid demo session is required.")
    aid = request.path_params["account_id"]
    if not valid_uuid(aid):
        return err(422, "invalid_request", "The request is invalid.")
    a = DB["accounts"].get(str(uuid.UUID(aid)))
    return JSONResponse({"account": account_out(a)}) if a else err(404, "account_not_found", "The account was not found.")


async def transactions(request):
    if not authed(request):
        return err(401, "unauthenticated", "A valid demo session is required.")
    q = request.query_params
    acc, typ, st, sd, ed = (q.get(k) for k in ("account_id", "type", "status", "start_date", "end_date"))
    try:
        sd = date.fromisoformat(sd) if sd else None
        ed = date.fromisoformat(ed) if ed else None
    except ValueError:
        return err(422, "invalid_date", "Dates must use YYYY-MM-DD format.")
    if (typ and typ not in ("deposit", "withdrawal", "transfer")) or (st and st not in ("completed", "pending", "failed")):
        return err(422, "invalid_filter", "One or more transaction filters are invalid.")
    if acc and not valid_uuid(acc):
        return err(422, "invalid_request", "The request is invalid.")
    if acc and str(uuid.UUID(acc)) not in DB["accounts"]:
        return err(404, "account_not_found", "The account was not found.")
    if sd and ed and sd > ed:
        return err(422, "invalid_date_range", "Start date must not be after end date.")
    start_at = datetime.combine(sd, time.min, PACIFIC).astimezone(timezone.utc) if sd else None
    end_before = datetime.combine(ed, time.min, PACIFIC).astimezone(timezone.utc) if ed else None   # same as real
    out = []
    for t in DB["transactions"].values():
        if acc and not any(e["transaction_id"] == t["id"] and e["account_id"] == acc for e in DB["entries"]):
            continue
        if typ and t["type"] != typ: continue
        if st and t["status"] != st: continue
        if start_at and t["occurred_at"] < start_at: continue
        if end_before and t["occurred_at"] >= end_before: continue
        out.append(t)
    out.sort(key=lambda t: t["id"])
    out.sort(key=lambda t: t["occurred_at"], reverse=True)
    return JSONResponse({"transactions": [hydrate(t) for t in out]})


async def transfers(request):
    if not authed(request):
        return err(401, "unauthenticated", "A valid demo session is required.")
    try:
        body = await request.json()
    except Exception:
        return err(422, "invalid_request", "The request is invalid.")
    allowed = {"source_account_id", "destination_account_id", "amount", "memo"}
    if not isinstance(body, dict) or set(body) - allowed:
        return err(422, "invalid_request", "The request is invalid.")
    amt = body.get("amount")
    if not isinstance(amt, str) or not MONEY_PATTERN.fullmatch(amt):
        return err(422, "invalid_amount", "Amount must be a decimal string with no more than two decimal places.")
    s, d = body.get("source_account_id"), body.get("destination_account_id")
    if not (valid_uuid(s) and valid_uuid(d)):
        return err(422, "invalid_request", "The request is invalid.")
    if s == d:
        return err(422, "same_account", "Source and destination accounts must be different.")
    if s not in DB["accounts"] or d not in DB["accounts"]:
        return err(404, "account_not_found", "One or more accounts were not found.")
    amount = Decimal(amt)
    now = datetime.now(timezone.utc)
    DB["accounts"][s]["balance"] -= amount          # no balance check, exactly like repository.py
    DB["accounts"][d]["balance"] += amount
    tid = str(uuid.uuid4())
    DB["transactions"][tid] = {"id": tid, "type": "transfer", "status": "completed", "amount": amount,
                               "memo": body.get("memo") or None, "occurred_at": now}
    DB["entries"] += [{"id": str(uuid.uuid4()), "transaction_id": tid, "account_id": s, "amount_delta": -amount, "created_at": now},
                      {"id": str(uuid.uuid4()), "transaction_id": tid, "account_id": d, "amount_delta": amount,
                       "created_at": now + timedelta(microseconds=1)}]
    accs = sorted([DB["accounts"][s], DB["accounts"][d]], key=lambda a: 0 if a["id"] == s else 1)
    return JSONResponse({"transaction": hydrate(DB["transactions"][tid]), "accounts": [account_out(a) for a in accs]},
                        status_code=201)


def build(origin: str) -> Starlette:
    return Starlette(routes=[
        Route("/health", health), Route("/api/demo/session", session, methods=["POST", "DELETE"]),
        Route("/api/me", me), Route("/api/accounts", accounts), Route("/api/accounts/{account_id}", account),
        Route("/api/transactions", transactions), Route("/api/transfers", transfers, methods=["POST"]),
    ], middleware=[Middleware(CORSMiddleware, allow_origins=[origin], allow_credentials=True,
                              allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["Accept", "Content-Type"])])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--origin", default="http://localhost:5173")
    a = ap.parse_args()
    load_seed(a.seed)
    uvicorn.run(build(a.origin), host="127.0.0.1", port=a.port, log_level="warning")
