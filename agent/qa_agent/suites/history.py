"""Transaction history: row content, filters, inclusive date boundaries, empty state, clear."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from ..flows import (api_state, apply_filters, cell, clear_filters, parse_money, pt_date, read_table)
from ..harness import Ctx, scenario

TYPE_LABEL = {"deposit": "Deposit", "withdrawal": "Withdrawal", "transfer": "Transfer"}
STATUS_LABEL = {"completed": "Completed", "pending": "Pending", "failed": "Failed"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _ids(rows) -> set[str]:
    return {r["id"] for r in rows or [] if r.get("id")}


def _compare_sets(ctx: Ctx, anchor: str, label: str, rows, expected: list[dict]) -> None:
    """Every displayed row must match the filter, and no matching row may be missing."""
    exp_ids = {t["id"] for t in expected}
    got = _ids(rows)
    if rows and not got:          # table without row IDs: fall back to counts
        ctx.check(anchor, f"{label}: row count matches", len(rows) == len(expected),
                  f"{len(expected)} rows", f"{len(rows)} rows")
        return
    extra = got - exp_ids
    missing = exp_ids - got
    ctx.check(anchor, f"{label}: no row outside the filter is shown", not extra,
              "0 unexpected rows", f"{len(extra)} unexpected row(s)")
    ctx.check(anchor, f"{label}: every matching transaction is shown", not missing,
              f"{len(exp_ids)} rows", f"{len(got)} rows; missing {len(missing)}: "
              + ", ".join(next((t.get('memo') or t['type']) + ' @ ' + t['occurred_at'] for t in expected if t['id'] == m)
                          for m in list(missing)[:4]))


@scenario("HIS-ROWS", "Combined history shows every transaction with complete, correct row details",
          kind="positive", anchors=["combined transaction history"], order=30)
def combined_rows(ctx: Ctx) -> None:
    d = ctx.d
    api = api_state(ctx)
    d.goto("/transactions")
    rows = read_table(ctx)
    ctx.shot("all transactions")
    ctx.step("Open /transactions without filters and read every table row")
    if rows is None or not api.ok:
        ctx.unverified("combined transaction history", "Combined history readable", "table or API unavailable")
        return
    ctx.check("combined transaction history", "Combined history lists all transactions",
              len(rows) == len(api.txs), f"{len(api.txs)} rows (per backend)", f"{len(rows)} rows")

    by_id = {t["id"]: t for t in api.txs}
    missing_fields, wrong_amount, wrong_meta, wrong_dir, wrong_date = [], [], [], [], []
    for r in rows:
        t = by_id.get(r["id"])
        fields = {k: cell(r, k) for k in ("date", "desc", "type", "account", "amount", "status")}
        empty = [k for k, v in fields.items() if not v or v == "—"]
        if empty:
            missing_fields.append(f"{fields['desc'] or r['id']}: missing {empty}")
        if not t:
            continue
        shown = parse_money(fields["amount"])
        if shown is None or abs(shown) != abs(Decimal(t["amount"])):
            wrong_amount.append(f"{fields['desc']} ({t['status']} {t['type']}): shows {fields['amount']!r}, record amount ${t['amount']}")
        if TYPE_LABEL[t["type"]].lower() not in fields["type"].lower() or STATUS_LABEL[t["status"]].lower() not in fields["status"].lower():
            wrong_meta.append(f"{fields['desc']}: type/status shown {fields['type']}/{fields['status']} vs {t['type']}/{t['status']}")
        if t["type"] in ("deposit", "withdrawal") and shown not in (None, 0):
            if (t["type"] == "deposit") != (shown > 0):
                wrong_dir.append(f"{fields['desc']}: {t['type']} shown as {fields['amount']}")
        if t["type"] == "transfer" and not re.search(r"from.*to", fields["account"], re.I | re.S):
            wrong_dir.append(f"{fields['desc']}: transfer without From/To accounts")
        d_pt = date.fromisoformat(pt_date(t["occurred_at"]))
        if f"{MONTHS[d_pt.month - 1]} {d_pt.day}," not in fields["date"]:
            wrong_date.append(f"{fields['desc']}: shows {fields['date']!r}, Pacific date {d_pt}")

    ctx.check("Each row shows date and time", "Every row shows date/time, description, type, account, amount, status",
              not missing_fields, "all six fields populated", missing_fields[:5] or "all populated")
    ctx.check("Each row shows date and time", "Each row's amount equals the transaction amount",
              not wrong_amount, "row amount = record amount", wrong_amount[:5] or "all match")
    ctx.check("Each row shows date and time", "Each row's type and status match the record",
              not wrong_meta, "labels match", wrong_meta[:5] or "all match")
    ctx.check("Amount direction should be clear", "Deposits read as money in, withdrawals as money out, transfers show From/To",
              not wrong_dir, "+ for deposits, − for withdrawals, From→To for transfers", wrong_dir[:5] or "all clear")
    ctx.check("Each row shows date and time", "Displayed date matches the transaction's Pacific-time date",
              not wrong_date, "date in Pacific Time (as the UI states)", wrong_date[:5] or "all match")


@scenario("HIS-FILTER-SINGLE", "Each single filter (account, type, status) shows exactly the matching rows",
          kind="positive", anchors=["every displayed row must satisfy all selected filters"], order=31)
def single_filters(ctx: Ctx) -> None:
    api = api_state(ctx)
    if not api.ok:
        ctx.unverified("every displayed row must satisfy all selected filters", "filters", "API oracle unavailable")
        return
    for t, label in TYPE_LABEL.items():
        apply_filters(ctx, type=label)
        ctx.step(f"Filter Type = {label}")
        rows = read_table(ctx) or []
        _compare_sets(ctx, "every displayed row must satisfy all selected filters", f"Type={label}", rows,
                      [x for x in api.txs if x["type"] == t])
    for s, label in STATUS_LABEL.items():
        apply_filters(ctx, status=label)
        ctx.step(f"Filter Status = {label}")
        rows = read_table(ctx) or []
        _compare_sets(ctx, "every displayed row must satisfy all selected filters", f"Status={label}", rows,
                      [x for x in api.txs if x["status"] == s])
    for acc in api.accounts.values():
        apply_filters(ctx, account=acc["name"])
        ctx.step(f"Filter Account = {acc['name']}")
        rows = read_table(ctx) or []
        _compare_sets(ctx, "every displayed row must satisfy all selected filters", f"Account={acc['name']}", rows,
                      [x for x in api.txs if any(e["account_id"] == acc["id"] for e in x["account_deltas"])])
    ctx.shot("last single filter")


@scenario("HIS-FILTER-COMBO", "Combined filters (account + type + status + dates) intersect correctly",
          kind="edge", anchors=["every displayed row must satisfy all selected filters"], order=32)
def combined_filters(ctx: Ctx) -> None:
    api = api_state(ctx)
    if not api.ok or not api.txs:
        ctx.unverified("every displayed row must satisfy all selected filters", "combined filters", "API oracle unavailable")
        return
    checking = api.by_type("checking") or next(iter(api.accounts.values()))
    dates = sorted(pt_date(t["occurred_at"]) for t in api.txs)
    start, end = dates[0], dates[len(dates) // 2]
    apply_filters(ctx, account=checking["name"], type="Withdrawal", status="Completed", start=start, end=end)
    ctx.step(f"Filter Account={checking['name']}, Type=Withdrawal, Status=Completed, {start}..{end}")
    rows = read_table(ctx) or []
    expected = [t for t in api.txs if t["type"] == "withdrawal" and t["status"] == "completed"
                and any(e["account_id"] == checking["id"] for e in t["account_deltas"])
                and start <= pt_date(t["occurred_at"]) <= end]
    ctx.shot("combined filter")
    _compare_sets(ctx, "every displayed row must satisfy all selected filters", "Account+Type+Status+Dates", rows, expected)


@scenario("HIS-DATE-BOUNDARY", "Start and end dates are inclusive (including Pacific-time edge cases)",
          kind="edge", anchors=["start and end dates are inclusive"], order=33)
def date_boundaries(ctx: Ctx) -> None:
    api = api_state(ctx)
    if not api.ok or not api.txs:
        ctx.unverified("start and end dates are inclusive", "date boundaries", "API oracle unavailable")
        return
    # Prefer transactions whose UTC date differs from their Pacific date: classic off-by-one territory.
    shifted = [t for t in api.txs if t["occurred_at"][:10] != pt_date(t["occurred_at"])]
    normal = [t for t in api.txs if t not in shifted]
    sample = (shifted[:1] + normal[:2])[:3]
    for t in sample:
        day = pt_date(t["occurred_at"])
        label = f"{t.get('memo') or t['type']} ({t['occurred_at']} UTC = {day} PT)"
        same_day = [x for x in api.txs if pt_date(x["occurred_at"]) == day]
        for start, end, desc in ((day, day, "start = end = that day"), (day, "", "start = that day"),
                                 ("", day, "end = that day")):
            apply_filters(ctx, start=start or None, end=end or None)
            rows = read_table(ctx) or []
            ctx.step(f"Filter dates: {desc} [{start or '…'} → {end or '…'}] and look for {label}")
            found = t["id"] in _ids(rows)
            ctx.check("start and end dates are inclusive", f"{desc}: includes {label}", found,
                      "transaction included", "included" if found else f"missing ({len(rows)} rows shown)")
            if not found:
                ctx.shot(f"boundary miss {day} {desc}")
        apply_filters(ctx, start=day, end=day)
        rows = read_table(ctx) or []
        outside = [r for r in rows if r.get("datetime") and pt_date(r["datetime"]) != day]
        ctx.check("every displayed row must satisfy all selected filters", f"Single-day filter {day} shows no other days",
                  not outside, f"only {day}", f"{len(outside)} row(s) from other days")
        _ = same_day


@scenario("HIS-EMPTY", "A filter with no matches shows a clear empty state", kind="edge",
          anchors=["clear empty state"], order=34)
def empty_state(ctx: Ctx) -> None:
    api = api_state(ctx)
    earliest = min((pt_date(t["occurred_at"]) for t in api.txs), default="2020-01-01") if api.ok else "2020-01-01"
    day = (date.fromisoformat(earliest) - timedelta(days=400)).isoformat()
    apply_filters(ctx, start=day, end=day)
    ctx.step(f"Filter a single day with no transactions ({day})")
    rows = read_table(ctx)
    text = ctx.d.visible_text()
    alerts = ctx.d.page.locator("main [role=alert]").count()
    ctx.shot("empty state")
    ctx.check("clear empty state", "No rows are shown", not rows, "0 rows", f"{len(rows or [])} rows")
    ctx.check("clear empty state", "An explicit empty-state message is shown, not an error",
              bool(re.search(r"no (transactions|results|matching)|nothing (to show|found)", text, re.I)) and alerts == 0,
              "'No transactions match…' style message, no error alert",
              f"alerts={alerts}; text: {text[-200:].strip()!r}")


@scenario("HIS-CLEAR", "Clearing filters restores the full history", kind="positive",
          anchors=["Clearing filters restores"], order=35)
def clear_restores(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/transactions")
    full = read_table(ctx) or []
    apply_filters(ctx, type="Deposit", status="Completed")
    filtered = read_table(ctx) or []
    ctx.step("Apply Type=Deposit, Status=Completed, then click 'Clear filters'")
    clear_filters(ctx)
    restored = read_table(ctx) or []
    controls = d.page.evaluate("() => [...document.querySelectorAll('form select, form input')].map(e => e.value)")
    ctx.shot("after clear")
    ctx.check("Clearing filters restores", "After clearing, the unfiltered row set is back",
              _ids(restored) == _ids(full) and len(restored) == len(full) and len(filtered) < len(full),
              f"{len(full)} rows", f"filtered {len(filtered)} → cleared {len(restored)}")
    ctx.check("Clearing filters restores", "All filter controls are reset", all(v == "" for v in controls),
              "all controls empty", controls)


@scenario("HIS-DATE-INVERTED", "Start date after end date gives a clear message (no misleading rows)",
          kind="negative", anchors=["communicate loading, success, validation, and failure states"], order=36)
def inverted_dates(ctx: Ctx) -> None:
    apply_filters(ctx, start="2025-06-30", end="2025-01-01")
    ctx.step("Filter with start 2025-06-30 and end 2025-01-01")
    rows = read_table(ctx)
    msgs = ctx.d.messages()
    ctx.shot("inverted dates")
    ctx.check("communicate loading, success, validation, and failure states",
              "Inverted range shows no transactions", not rows, "0 rows", f"{len(rows or [])} rows")
    ctx.judge_clarity("communicate loading, success, validation, and failure states",
                      "Inverted range explains the problem clearly", "Filter with start date after end date",
                      " | ".join(msgs), "Tell the user the start date must not be after the end date")
