"""Transfers between owned accounts: the riskiest area, so it gets the most scenarios.

Pattern used by every mutating scenario:
    before = oracle snapshot  →  act through the UI  →  after = oracle snapshot
    →  assert on what the UI *said*  AND  on what actually changed (balances, records)
The runner then compensates any balance drift so the next scenario starts from known state.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..flows import (ApiState, api_state, cell, parse_money, read_dashboard_cards, read_table,
                     submit_transfer)
from ..harness import Ctx, scenario

ANCHOR_AMOUNT = "positive number greater than zero"
ANCHOR_NO_SIDE = "Rejected input must not create a transaction"
ANCHOR_MSG = "clear validation message"
ANCHOR_NO_SUCCESS = "must not show a success state"


def _accounts(ctx: Ctx):
    api = api_state(ctx)
    if not api.ok:
        raise RuntimeError("API oracle unavailable – cannot verify balances")
    chk, sav = api.by_type("checking"), api.by_type("savings")
    if not chk or not sav:
        raise RuntimeError("expected one checking and one savings account")
    return api, chk, sav


def _new(before: ApiState, after: ApiState) -> list[dict]:
    return [t for t in after.txs if t["id"] not in before.tx_ids()]


def _no_side_effects(ctx: Ctx, anchor: str, before: ApiState, label: str) -> ApiState:
    after = api_state(ctx)
    changed = {a["name"]: f"${before.balance(i)} → ${after.balance(i)}"
               for i, a in before.accounts.items() if i in after.accounts and after.balance(i) != before.balance(i)}
    new = _new(before, after)
    ctx.check(anchor, f"{label}: balances are unchanged", not changed, "both balances unchanged",
              changed or "unchanged")
    ctx.check(anchor, f"{label}: no transaction record is created", not new, "0 new records",
              "; ".join(f"{t['type']} {t['status']} ${t['amount']}" for t in new) or "none")
    return after


def _expect_rejection(ctx: Ctx, outcome, label: str, expectation: str, anchor: str = ANCHOR_AMOUNT) -> None:
    ctx.value("UI outcome", outcome.summary)
    if outcome.api_status:
        ctx.value("POST /api/transfers response", f"HTTP {outcome.api_status}: {outcome.api_body[:400]}")
    ctx.check(anchor, f"{label}: the transfer is rejected", outcome.rejected and not outcome.success,
              "validation/error message, no success", outcome.summary)
    ctx.check(ANCHOR_NO_SUCCESS, f"{label}: no success state is shown", not outcome.success,
              "no 'Transfer complete'", outcome.summary)
    # Only grade the wording when the UI actually rejected the input; if it accepted it, the
    # rejection assertion above has already failed and there is no explanation to grade.
    if outcome.rejected and not outcome.success:
        ctx.judge_clarity(ANCHOR_MSG if anchor == ANCHOR_AMOUNT else anchor,
                          f"{label}: the message explains the problem", f"Submit transfer with {label}",
                          " | ".join(outcome.error_messages), expectation)


# ---------------------------------------------------------------------- form structure
@scenario("TRF-FORM", "Transfer form has source, destination, amount and optional memo", kind="positive",
          anchors=["form includes source account"], order=50)
def form_fields(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/transfer")
    found = {}
    for key, rx in (("source", r"^from|source"), ("destination", r"^to\b|destination"),
                    ("amount", r"amount"), ("memo", r"memo")):
        found[key] = d.find(f"transfer {key} field", lambda p, r=rx: p.get_by_label(re.compile(r, re.I)),
                            lambda p, k=key: p.locator(f"main form [name*={k}]")) is not None
    memo_label = d.page.locator("label", has_text=re.compile("memo", re.I)).first
    optional = "optional" in (memo_label.inner_text().lower() if memo_label.count() else "")
    ctx.shot("transfer form")
    ctx.check("form includes source account", "Form exposes labelled source, destination, amount and memo fields",
              all(found.values()), "4 labelled fields", found)
    ctx.check("form includes source account", "Memo is marked optional", optional, "'optional' on memo label",
              memo_label.inner_text() if memo_label.count() else "(no memo label)")


# ---------------------------------------------------------------------- happy paths
@scenario("TRF-VALID", "Valid transfer with memo: balances, records, histories and refresh all agree",
          kind="positive", anchors=["valid transfer is atomic"], mutates=True, order=51)
def valid_transfer(ctx: Ctx) -> None:
    d = ctx.d
    before, chk, sav = _accounts(ctx)
    ui_before = {c["name"]: c["balance"] for c in read_dashboard_cards(ctx)}
    amount, memo = Decimal("1.37"), ctx.memo("valid")
    out = submit_transfer(ctx, chk["name"], sav["name"], f"{amount}", memo)
    ctx.shot("receipt")
    ctx.value("UI outcome", out.summary)
    after = api_state(ctx)
    new = _new(before, after)
    mine = [t for t in new if t.get("memo") == memo]
    foreign = [t for t in new if t.get("memo") != memo]
    if foreign:
        ctx.unverified("valid transfer is atomic", "Isolation from other users",
                       f"{len(foreign)} unexpected new transaction(s) appeared – shared environment?")

    ctx.check("transfer funds between the checking and savings accounts",
              f"User can transfer {chk['name']} → {sav['name']}", out.success and len(mine) == 1,
              "transfer completes", out.summary)
    ctx.check("UI reports success only after the transfer has been committed",
              "Success is shown and the backend confirms the commit",
              out.success and out.api_status in (200, 201) and len(mine) == 1,
              "'Transfer complete' + HTTP 201 + record exists", f"{out.summary}; records with memo: {len(mine)}")
    d_src, d_dst = after.balance(chk["id"]) - before.balance(chk["id"]), after.balance(sav["id"]) - before.balance(sav["id"])
    ctx.check("valid transfer is atomic", "Source decreases and destination increases by exactly the amount",
              d_src == -amount and d_dst == amount, f"{chk['name']} −{amount}, {sav['name']} +{amount}",
              f"{chk['name']} {d_src:+}, {sav['name']} {d_dst:+}")
    receipt = " ".join(out.messages)
    ctx.check("UI reports success only after", "Receipt shows amount, source, destination and memo",
              all(x in receipt for x in ("1.37", chk["name"], sav["name"], memo)), "all four in receipt", receipt[:300])

    if len(mine) == 1:
        t = mine[0]
        ctx.check("creates one completed transfer record", "Exactly one completed transfer record was created",
                  t["type"] == "transfer" and t["status"] == "completed" and Decimal(t["amount"]) == amount,
                  "1 × transfer/completed/$1.37", f"{t['type']}/{t['status']}/${t['amount']}")
        for acc, sign in ((chk, "−"), (sav, "+")):
            d.goto(f"/accounts/{acc['id']}")
            rows = [r for r in (read_table(ctx) or []) if memo in cell(r, "desc")]
            amt = cell(rows[0], "amount") if rows else ""
            ctx.check("found in both relevant account histories", f"{acc['name']} history shows the transfer once as {sign}$1.37",
                      len(rows) == 1 and parse_money(amt) == (-amount if sign == "−" else amount)
                      and "completed" in cell(rows[0], "status").lower(),
                      f"1 row, {sign}$1.37, Completed", f"{len(rows)} row(s); amount {amt!r}")
            if len(rows) != 1:
                ctx.shot(f"{acc['name']} history missing transfer")
        d.goto("/transactions")
        rows = [r for r in (read_table(ctx) or []) if memo in cell(r, "desc")]
        r = rows[0] if rows else None
        ctx.check("record shows the amount, date and time, source, destination, status, and memo",
                  "Combined history row shows amount, date/time, source, destination, status and memo",
                  bool(r) and "1.37" in cell(r, "amount") and bool(cell(r, "date")) and chk["name"] in cell(r, "account")
                  and sav["name"] in cell(r, "account") and "completed" in cell(r, "status").lower(),
                  "all fields present", r["cells"] if r else "row not found")

    ui_after = {c["name"]: c["balance"] for c in read_dashboard_cards(ctx)}
    ctx.shot("dashboard after transfer")
    ok = all(ui_after.get(a["name"]) == after.balance(i) for i, a in after.accounts.items())
    ctx.check("must agree with the latest successful transactions", "Dashboard balances reflect the transfer",
              ok and ui_after.get(chk["name"]) == (ui_before.get(chk["name"]) or 0) - amount,
              {a["name"]: f"${after.balance(i)}" for i, a in after.accounts.items()}, ui_after)

    ctx.step("Refresh the dashboard and the history page")
    d.reload()
    ui_refresh = {c["name"]: c["balance"] for c in read_dashboard_cards(ctx)}
    d.goto("/transactions")
    d.reload()
    still = any(memo in cell(r, "desc") for r in (read_table(ctx) or []))
    ctx.check("Refreshing after a successful transfer must preserve", "After refresh, balances and the record persist",
              ui_refresh == ui_after and still, f"balances {ui_after}, record present", f"balances {ui_refresh}, record present={still}")


@scenario("TRF-VALID-NOMEMO", "Valid transfer without memo (memo is optional), savings → checking",
          kind="positive", anchors=["optional memo"], mutates=True, order=52)
def valid_no_memo(ctx: Ctx) -> None:
    before, chk, sav = _accounts(ctx)
    amount = Decimal("2.05")
    out = submit_transfer(ctx, sav["name"], chk["name"], f"{amount}", "")
    after = api_state(ctx)
    new = [t for t in _new(before, after) if t["type"] == "transfer"]
    ctx.shot("no memo receipt")
    ctx.check("optional memo", "Transfer without memo succeeds", out.success and len(new) == 1,
              "success + 1 record", out.summary)
    ctx.check("transfer funds between the checking and savings accounts",
              f"User can transfer {sav['name']} → {chk['name']}", out.success and len(new) == 1,
              "transfer completes", out.summary)
    ctx.check("valid transfer is atomic", "Savings → checking moves exactly the amount",
              after.balance(sav["id"]) - before.balance(sav["id"]) == -amount
              and after.balance(chk["id"]) - before.balance(chk["id"]) == amount,
              f"−{amount}/+{amount}", f"{after.balance(sav['id']) - before.balance(sav['id']):+}/"
                                      f"{after.balance(chk['id']) - before.balance(chk['id']):+}")


@scenario("TRF-MIN", "Smallest valid amount ($0.01) is accepted and moved exactly", kind="edge",
          anchors=["no more than two digits after the decimal point"], mutates=True, order=53)
def minimum_amount(ctx: Ctx) -> None:
    before, chk, sav = _accounts(ctx)
    out = submit_transfer(ctx, chk["name"], sav["name"], "0.01", ctx.memo("min"))
    after = api_state(ctx)
    ctx.check("no more than two digits after the decimal point", "$0.01 (two decimals) is accepted",
              out.success, "success", out.summary)
    ctx.check("valid transfer is atomic", "$0.01 moves exactly one cent",
              after.balance(chk["id"]) - before.balance(chk["id"]) == Decimal("-0.01")
              and after.balance(sav["id"]) - before.balance(sav["id"]) == Decimal("0.01"),
              "−0.01/+0.01", f"{after.balance(chk['id']) - before.balance(chk['id']):+}/"
                             f"{after.balance(sav['id']) - before.balance(sav['id']):+}")


@scenario("TRF-EXACT-BALANCE", "Transferring exactly the full available balance is allowed", kind="edge",
          anchors=["must not exceed the source account's available balance"], mutates=True, order=54)
def exact_balance(ctx: Ctx) -> None:
    before, chk, sav = _accounts(ctx)
    full = before.balance(chk["id"])
    if full <= 0:
        ctx.unverified("must not exceed the source account's available balance", "Exact balance transfer",
                       f"checking balance is {full}")
        return
    out = submit_transfer(ctx, chk["name"], sav["name"], f"{full:.2f}", ctx.memo("full"))
    after = api_state(ctx)
    ctx.shot("exact balance")
    ctx.check("must not exceed the source account's available balance",
              f"Amount equal to the balance (${full}) is accepted and leaves $0.00",
              out.success and after.balance(chk["id"]) == 0, "success, source = $0.00",
              f"{out.summary}; source now ${after.balance(chk['id'])}")


# ---------------------------------------------------------------------- negative paths
@scenario("TRF-OVERDRAFT", "Amount one cent above the available balance is rejected", kind="negative",
          anchors=["If funds are insufficient"], mutates=True, order=60)
def overdraft(ctx: Ctx) -> None:
    before, chk, sav = _accounts(ctx)
    too_much = before.balance(chk["id"]) + Decimal("0.01")
    out = submit_transfer(ctx, chk["name"], sav["name"], f"{too_much:.2f}", ctx.memo("over"))
    ctx.shot("overdraft attempt")
    _expect_rejection(ctx, out, f"amount ${too_much} (balance + $0.01)",
                      "Explain that the amount exceeds the available balance", anchor="If funds are insufficient")
    after = _no_side_effects(ctx, "If funds are insufficient", before, "Insufficient funds")
    if after.ok and after.balance(chk["id"]) < 0:
        ctx.value("Overdrawn balance", f"{chk['name']} is now ${after.balance(chk['id'])}")


@scenario("TRF-SAME", "Same source and destination account is rejected", kind="negative",
          anchors=["Same-account transfers must be rejected"], mutates=True, order=61)
def same_account(ctx: Ctx) -> None:
    before, chk, _ = _accounts(ctx)
    out = submit_transfer(ctx, chk["name"], chk["name"], "5.00", ctx.memo("same"))
    ctx.shot("same account")
    _expect_rejection(ctx, out, "the same account as source and destination",
                      "Explain that source and destination must be different accounts",
                      anchor="Same-account transfers must be rejected")
    _no_side_effects(ctx, "Same-account transfers must be rejected", before, "Same-account transfer")


INVALID_AMOUNTS = [
    ("BLANK", "", "a blank amount", "negative"),
    ("ZERO", "0", "zero ('0')", "negative"),
    ("ZERO2", "0.00", "zero ('0.00')", "edge"),
    ("NEG", "-5.00", "a negative amount ('-5.00')", "negative"),
    ("NAN", "abc", "a non-numeric amount ('abc')", "negative"),
    ("PREC", "1.234", "three decimal places ('1.234')", "edge"),
    ("SCI", "1e2", "scientific notation ('1e2')", "edge"),
    ("SPACE", "   ", "whitespace only", "edge"),
]


def _make_invalid(value: str, desc: str):
    def run(ctx: Ctx) -> None:
        before, chk, sav = _accounts(ctx)
        out = submit_transfer(ctx, chk["name"], sav["name"], value, ctx.memo("amt"))
        ctx.shot(f"amount {desc}")
        _expect_rejection(ctx, out, desc, "Explain what a valid amount is (positive, max two decimals)")
        _no_side_effects(ctx, ANCHOR_NO_SIDE, before, desc.capitalize())
    return run


for _code, _value, _desc, _kind in INVALID_AMOUNTS:
    scenario(f"TRF-AMT-{_code}", f"Reject {_desc}", kind=_kind, anchors=[ANCHOR_AMOUNT],
             mutates=True, order=62)(_make_invalid(_value, _desc))


# ---------------------------------------------------------------------- failure handling & resilience
@scenario("TRF-SERVER-FAIL", "A server failure is reported as not completed and the user can retry",
          kind="negative", anchors=["If a transfer is rejected or fails"], mutates=True, order=70)
def server_failure(ctx: Ctx) -> None:
    d = ctx.d
    before, chk, sav = _accounts(ctx)
    ctx.step("Inject a client-side HTTP 500 for POST /api/transfers (fault injection in our own browser only)")
    d.page.route("**/api/transfers", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"error":{"code":"internal_error","message":"Injected failure"}}'))
    memo = ctx.memo("fail")
    out = submit_transfer(ctx, chk["name"], sav["name"], "3.00", memo)
    ctx.shot("server failure")
    _expect_rejection(ctx, out, "a failed request (HTTP 500)",
                      "Say the transfer did not complete and nothing was moved",
                      anchor="If a transfer is rejected or fails")
    btn = d.find("submit transfer button", lambda p: p.get_by_role("button", name=re.compile("transfer", re.I)),
                 lambda p: p.locator("main form button[type=submit]")) or d.page.locator("main form button").first
    ctx.check("A recoverable error must leave the user able to retry", "After the failure the form is still usable",
              btn.is_enabled(), "submit button enabled", f"enabled={btn.is_enabled()}")
    d.page.unroute("**/api/transfers")
    ctx.step("Remove the fault and retry the same transfer")
    retry = submit_transfer(ctx, chk["name"], sav["name"], "3.00", memo)
    ctx.check("A recoverable error must leave the user able to retry", "Retrying after the failure succeeds",
              retry.success, "success on retry", retry.summary)


@scenario("TRF-DOUBLE", "Double-clicking submit on a slow network creates only one transfer", kind="edge",
          anchors=["prevent accidental duplicate submission"], mutates=True, order=71)
def double_submit(ctx: Ctx) -> None:
    d = ctx.d
    before, chk, sav = _accounts(ctx)
    cdp = d.context.new_cdp_session(d.page)

    def slow():
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions",
                 {"offline": False, "latency": 1500, "downloadThroughput": -1, "uploadThroughput": -1})

    memo = ctx.memo("dbl")
    ctx.step("Throttle network to 1.5 s latency, then double-click 'Transfer funds'")
    out = submit_transfer(ctx, chk["name"], sav["name"], "1.11", memo, click="double", before_submit=slow)
    cdp.send("Network.emulateNetworkConditions",
             {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    ctx.value("Button state right after click", out.busy_after_click)
    after = api_state(ctx)
    mine = [t for t in _new(before, after) if t.get("memo") == memo]
    busy = out.busy_after_click
    ctx.check("should show a loading state", "UI shows a busy state immediately after submit",
              bool(busy.get("disabled") or busy.get("formBusy") == "true" or re.search("submitting|…", busy.get("text", ""), re.I)),
              "button disabled / 'Submitting…'", busy)
    ctx.check("prevent accidental duplicate submission", "Double-click produces exactly one transfer",
              len(mine) == 1 and out.post_count <= 1, "1 record, 1 POST", f"{len(mine)} record(s), {out.post_count} POST(s)")


@scenario("TRF-MEMO-TEXT", "Memo with HTML/special characters is stored and shown literally", kind="edge",
          anchors=["memo if supplied"], mutates=True, order=72)
def memo_literal(ctx: Ctx) -> None:
    d = ctx.d
    _, chk, sav = _accounts(ctx)
    memo = f'<b>bold</b> & "quote" {ctx.memo("html")}'
    out = submit_transfer(ctx, chk["name"], sav["name"], "0.50", memo)
    d.goto("/transactions")
    # Match on the full unique memo (contains run ID): earlier runs may have left similar rows behind.
    rows = [r for r in (read_table(ctx) or []) if memo.strip() in cell(r, "desc")]
    injected = d.page.locator("table b").count()
    ctx.shot("html memo")
    ctx.check("memo if supplied", "Memo is displayed exactly as typed, not interpreted as HTML",
              out.success and len(rows) == 1 and injected == 0, "1 row with literal '<b>bold</b>'",
              f"success={out.success}; rows={len(rows)}; injected <b> elements={injected}")
