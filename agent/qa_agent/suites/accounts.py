"""Dashboard and account detail: cards, masking, formatting, balance agreement, detail history."""
from __future__ import annotations

import re

from ..flows import MASK_RE, api_state, cell, parse_money, read_dashboard_cards, read_table
from ..harness import Ctx, scenario

LONG_DIGITS = re.compile(r"\d[\d\s-]{7,}\d")   # 8+ digits, possibly spaced: looks like a full account number


def _digit_runs(text: str) -> list[str]:
    hits = []
    for m in LONG_DIGITS.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        # Ignore dates/times and money like "2025" or "8,275.00"; keep 8+ contiguous digits.
        if len(digits) >= 8 and not re.fullmatch(r"20\d{2}\d{4}", digits):
            hits.append(m.group(0))
    return hits


@scenario("DSH-CARDS", "Dashboard lists checking and savings with masked IDs and USD balances",
          kind="positive", anchors=["checking and savings accounts"], order=20)
def dashboard_cards(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/dashboard")
    cards = read_dashboard_cards(ctx)
    api = api_state(ctx)
    ctx.shot("dashboard")
    ctx.value("Cards parsed from UI", [{k: v for k, v in c.items() if k != "text"} for c in cards])
    types = sorted({c["type"] for c in cards if c["type"]})
    ctx.check("checking and savings accounts", "Dashboard shows both a checking and a savings account",
              "checking" in types and "savings" in types, "checking + savings cards", f"types shown: {types}")

    for c in cards:
        label = c["name"] or "(unnamed card)"
        readable = bool(c["name"]) and not re.fullmatch(r"[0-9a-f-]{20,}", c["name"])
        ctx.check("readable account name", f"{label}: readable name, masked id, type and USD balance shown",
                  readable and bool(c["masked"]) and bool(c["type"]) and c["balance"] is not None and c["currency"] == "USD",
                  "name, masked identifier (e.g. •••• 1234), type, $ balance + USD",
                  f"name={c['name']!r} masked={c['masked']!r} type={c['type']!r} balance={c['balance_text']!r} currency={c['currency']!r}")
        two_dp = bool(c["balance_text"] and re.search(r"\.\d{2}$", c["balance_text"]))
        ctx.check("displayed to two decimal places", f"{label}: balance has exactly two decimals",
                  two_dp, "e.g. $1,234.50", c["balance_text"])
        if c["masked"]:
            visible_digits = len(re.sub(r"\D", "", c["masked"]))
            ctx.check("must not expose a full account identifier", f"{label}: identifier is masked",
                      visible_digits <= 4, "at most last 4 digits visible", c["masked"])

    leaks = _digit_runs(d.visible_text("body"))
    ctx.check("must not expose a full account identifier", "No full-length account number anywhere on the dashboard",
              not leaks, "no 8+ digit sequences in visible text", leaks or "none found")

    if api.ok:
        for c in cards:
            match = next((a for a in api.accounts.values() if a["name"] == c["name"]), None)
            if match:
                ctx.check("must agree with the latest successful transactions",
                          f"{c['name']}: dashboard balance matches the backend balance",
                          c["balance"] == match["balance"], f"${match['balance']}", c["balance_text"])
    else:
        ctx.unverified("must agree with the latest successful transactions", "Dashboard vs backend balance",
                       "API oracle unavailable")


@scenario("DSH-DETAIL", "Selecting each account opens a detail view with balance and its own history",
          kind="positive", anchors=["opens an account detail view"], order=21)
def account_detail(ctx: Ctx) -> None:
    d = ctx.d
    cards = read_dashboard_cards(ctx)
    api = api_state(ctx)
    for c in cards:
        d.goto("/dashboard")
        link = d.find(f"account card for {c['name']}",
                      lambda p, n=c["name"]: p.get_by_role("link", name=re.compile(re.escape(n), re.I)))
        ctx.step(f"Click the '{c['name']}' account card")
        if link is None:
            ctx.check("opens an account detail view", f"{c['name']}: card is clickable", False,
                      "card opens detail view", "card link not found")
            continue
        d.click_and_wait_api(link, "/api/accounts/")
        d.wait_until(lambda n=c["name"]: n.lower() in d.page.get_by_role("heading", level=1).first.inner_text().lower())
        heading = d.page.get_by_role("heading", level=1).first.inner_text() if d.page.get_by_role("heading", level=1).count() else ""
        panel = d.find("available balance on detail page",
                       lambda p: p.get_by_text(re.compile("available balance", re.I)).locator(".."))
        detail_balance = parse_money(panel.inner_text()) if panel else None
        rows = read_table(ctx) or []
        ctx.shot(f"detail {c['name']}")
        ctx.check("opens an account detail view", f"{c['name']}: detail view opens with its name and balance",
                  c["name"].lower() in heading.lower() and detail_balance is not None,
                  f"heading '{c['name']}' + balance", f"URL {d.path()}, heading {heading!r}, balance {detail_balance}")
        ctx.check("must agree with the latest successful transactions",
                  f"{c['name']}: detail balance equals dashboard balance", detail_balance == c["balance"],
                  c["balance_text"], f"{detail_balance}")

        acc = next((a for a in api.accounts.values() if a["name"] == c["name"]), None) if api.ok else None
        if acc:
            expected = [t for t in api.txs if any(x["account_id"] == acc["id"] for x in t["account_deltas"])]
            ctx.check("account-specific history", f"{c['name']}: history lists every transaction of this account",
                      {r["id"] for r in rows if r["id"]} == {t["id"] for t in expected} if all(r["id"] for r in rows)
                      else len(rows) == len(expected),
                      f"{len(expected)} rows", f"{len(rows)} rows")
            foreign = [r for r in rows if c["name"].lower() not in cell(r, "account").lower()
                       and (c["masked"] or "@@") not in cell(r, "account")]
            ctx.check("account-specific history", f"{c['name']}: every history row involves this account",
                      not foreign, "all rows reference this account",
                      f"{len(foreign)} foreign row(s): " + "; ".join(cell(r, "desc") for r in foreign[:3]))


@scenario("DSH-MASK-API", "Masked identifier format is consistent across dashboard, detail and forms",
          kind="edge", anchors=["masked account identifier"], order=22)
def masks_everywhere(ctx: Ctx) -> None:
    d = ctx.d
    for path in ["/dashboard", "/transfer", "/transactions"]:
        d.goto(path)
        text = d.visible_text("body")
        masks = sorted(set(MASK_RE.findall(text)))
        leaks = _digit_runs(text)
        ctx.check("must not expose a full account identifier", f"{path}: no full account number visible",
                  not leaks, "only masked identifiers", f"masks={masks} leaks={leaks or 'none'}")
