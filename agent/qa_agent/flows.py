"""Reusable user flows and state readers for DemoBank.

Everything here is written against what a user can see (labels, roles, visible text) so it
survives cosmetic UI changes. State is read two ways and cross-checked:

* UI state  – what the user sees (balances on cards, rows in tables)
* API state – what the app's own backend returns for the same session (the "oracle")
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .harness import Ctx

PACIFIC = ZoneInfo("America/Los_Angeles")   # the UI states "dates ... use Pacific Time"
MONEY_RE = re.compile(r"([+\-−]?)\s*\$\s?([\d,]+(?:\.\d+)?)")
MASK_RE = re.compile(r"[•*xX●·]{2,}[\s-]*\d{2,4}")


# ---------------------------------------------------------------------- parsing helpers
def parse_money(text: str) -> Decimal | None:
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    sign, digits = m.groups()
    try:
        value = Decimal(digits.replace(",", ""))
    except InvalidOperation:
        return None
    return -value if sign in ("-", "−") else value


def money_text(text: str) -> str | None:
    m = MONEY_RE.search(text or "")
    return m.group(0).strip() if m else None


def pt_date(iso: str) -> str:
    """ISO timestamp -> calendar date in Pacific Time (the date a user sees)."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PACIFIC).date().isoformat()


# ---------------------------------------------------------------------- session
def enter_demo(ctx: Ctx) -> bool:
    d = ctx.d
    d.goto("/")
    btn = d.find("Enter Demo button on landing page",
                 lambda p: p.get_by_role("button", name=re.compile(r"enter demo", re.I)),
                 lambda p: p.get_by_role("link", name=re.compile(r"enter demo", re.I)),
                 lambda p: p.get_by_text(re.compile(r"enter demo", re.I)))
    if btn is None:
        return False
    d.click(btn)
    try:
        d.page.wait_for_url(re.compile(r"/dashboard"), timeout=10_000)
    except Exception:
        return False
    d.settle()
    return True


def logout(ctx: Ctx) -> bool:
    btn = ctx.d.find("Log out button in header",
                     lambda p: p.get_by_role("button", name=re.compile(r"log\s*out|sign\s*out", re.I)),
                     lambda p: p.get_by_text(re.compile(r"log\s*out", re.I)))
    if btn is None:
        return False
    ctx.d.click(btn)
    try:
        ctx.d.page.wait_for_url(lambda url: not re.search(r"/(dashboard|transfer|transactions|profile|accounts)", url),
                                timeout=8_000)
    except Exception:
        pass
    ctx.d.settle()
    return True


def is_protected_view(ctx: Ctx) -> bool:
    """True if the page is showing private banking data (balances, account cards, tables)."""
    p = ctx.d.page
    if p.get_by_role("button", name=re.compile(r"log\s*out", re.I)).count() > 0:
        return True
    text = ctx.d.visible_text("body")
    return bool(re.search(r"available balance|\$[\d,]+\.\d{2}", text, re.I))


# ---------------------------------------------------------------------- state readers
@dataclass
class ApiState:
    ok: bool
    accounts: dict[str, dict] = field(default_factory=dict)   # id -> {name,type,masked,balance}
    txs: list[dict] = field(default_factory=list)

    def by_type(self, t: str) -> dict | None:
        return next((a for a in self.accounts.values() if a["type"] == t), None)

    def balance(self, acc_id: str) -> Decimal:
        return self.accounts[acc_id]["balance"]

    def tx_ids(self) -> set[str]:
        return {t["id"] for t in self.txs}


def api_state(ctx: Ctx) -> ApiState:
    s1, accs = ctx.d.api_get("/api/accounts")
    s2, txs = ctx.d.api_get("/api/transactions")
    if s1 != 200 or s2 != 200 or not accs or txs is None:
        return ApiState(False)
    accounts = {a["id"]: {"id": a["id"], "name": a["name"], "type": a["type"],
                          "masked": a.get("masked_number", ""), "balance": Decimal(a["balance"])}
                for a in accs.get("accounts", [])}
    return ApiState(True, accounts, txs.get("transactions", []))


def read_dashboard_cards(ctx: Ctx) -> list[dict]:
    """Parse account cards from the dashboard using visible text only."""
    d = ctx.d
    if d.path() != "/dashboard":
        d.goto("/dashboard")
    region = d.find("list of account cards on dashboard",
                    lambda p: p.get_by_role("region", name=re.compile(r"accounts", re.I)),
                    lambda p: p.locator("[data-testid=account-card]").locator(".."))
    cards = (region.get_by_role("link") if region else d.page.locator("[data-testid=account-card]"))
    out = []
    for i in range(cards.count()):
        c = cards.nth(i)
        text = c.inner_text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        type_m = re.search(r"\b(checking|savings)\b", text, re.I)
        out.append({
            "name": lines[0] if lines else "",
            "type": type_m.group(1).lower() if type_m else None,
            "masked": (MASK_RE.search(text).group(0) if MASK_RE.search(text) else None),
            "balance_text": money_text(text),
            "balance": parse_money(text),
            "currency": "USD" if "USD" in text else None,
            "text": text,
            "href": c.get_attribute("href"),
        })
    return out


TABLE_JS = """
() => {
  const table = document.querySelector('table');
  if (!table) return null;
  const headers = [...table.querySelectorAll('thead th')].map(th => th.innerText.trim().toLowerCase());
  return [...table.querySelectorAll('tbody tr')].map(tr => {
    const cells = {};
    [...tr.querySelectorAll('td')].forEach((td, i) => { cells[headers[i] || ('col' + i)] = td.innerText.trim(); });
    const t = tr.querySelector('time');
    return { id: tr.getAttribute('data-transaction-id'), datetime: t ? t.getAttribute('datetime') : null, cells };
  });
}
"""


def read_table(ctx: Ctx) -> list[dict] | None:
    """All rows of the visible transaction table in one round-trip (None = no table)."""
    return ctx.d.page.evaluate(TABLE_JS)


def cell(row: dict, *names: str) -> str:
    for key, val in row["cells"].items():
        if any(n in key for n in names):
            return val
    return ""


# ---------------------------------------------------------------------- forms
def select_by_text(select, needle: str | None) -> bool:
    """Pick the <option> whose label contains `needle` (case-insensitive). '' => first/blank option."""
    options = select.evaluate("s => [...s.options].map(o => ({value: o.value, label: o.label}))")
    if needle in (None, ""):
        select.select_option(value=options[0]["value"])
        return True
    for o in options:
        if needle.lower() in o["label"].lower():
            select.select_option(value=o["value"])
            return True
    return False


def apply_filters(ctx: Ctx, *, account: str | None = None, type: str | None = None,
                  status: str | None = None, start: str | None = None, end: str | None = None) -> bool:
    d = ctx.d
    if d.path() != "/transactions":
        d.goto("/transactions")
    fields = [
        ("Account filter", r"^account", account),
        ("Type filter", r"^type", type),
        ("Status filter", r"^status", status),
    ]
    for intent, label, val in fields:
        sel = d.find(intent, lambda p, l=label: p.get_by_label(re.compile(l, re.I)))
        if sel is None or not select_by_text(sel, val or ""):
            return False
    for intent, label, val in (("Start date filter", r"start", start), ("End date filter", r"end", end)):
        inp = d.find(intent, lambda p, l=label: p.get_by_label(re.compile(l, re.I)))
        if inp is None:
            return False
        inp.fill(val or "")
    btn = d.find("Apply filters button",
                 lambda p: p.get_by_role("button", name=re.compile(r"apply|filter|search", re.I)))
    if btn is None:
        return False
    d.click_and_wait_api(btn, "/api/transactions")
    return True


def clear_filters(ctx: Ctx) -> bool:
    btn = ctx.d.find("Clear filters button",
                     lambda p: p.get_by_role("button", name=re.compile(r"clear|reset", re.I)))
    if btn is None:
        return False
    ctx.d.click_and_wait_api(btn, "/api/transactions")
    return True


@dataclass
class TransferOutcome:
    success: bool            # UI claims success
    rejected: bool           # UI shows an error / validation message
    messages: list[str]
    api_status: int | None   # status of POST /api/transfers (None = request never sent)
    api_body: str
    hung: bool = False
    busy_after_click: dict = field(default_factory=dict)
    post_count: int = 0
    error_messages: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        state = "SUCCESS shown" if self.success else ("rejected" if self.rejected else
                                                      ("still loading" if self.hung else "no feedback"))
        api = f"API {self.api_status}" if self.api_status else "no API request"
        msg = " | ".join(m.replace("\n", " ")[:160] for m in self.messages) or "(no message)"
        return f"{state}; {api}; messages: {msg}"


def submit_transfer(ctx: Ctx, source: str, dest: str, amount: str, memo: str = "",
                    click: str = "single", before_submit=None) -> TransferOutcome:
    """Fill and submit the transfer form; classify what the UI says happened."""
    d = ctx.d
    d.goto("/transfer")
    src = d.find("transfer source account select",
                 lambda p: p.get_by_label(re.compile(r"^from|source", re.I)))
    dst = d.find("transfer destination account select",
                 lambda p: p.get_by_label(re.compile(r"^to\b|destination", re.I)))
    amt = d.find("transfer amount input",
                 lambda p: p.get_by_label(re.compile(r"amount", re.I)),
                 lambda p: p.get_by_placeholder("0.00"))
    mem = d.find("transfer memo input", lambda p: p.get_by_label(re.compile(r"memo", re.I)))
    btn = d.find("submit transfer button",
                 lambda p: p.get_by_role("button", name=re.compile(r"transfer", re.I)),
                 lambda p: p.locator("form button[type=submit]"))
    if not all([src, dst, amt, btn]):
        raise RuntimeError("transfer form controls not found")
    select_by_text(src, source)
    select_by_text(dst, dest)
    d.fill(amt, amount)
    if mem is not None:
        d.fill(mem, memo)
    ctx.step(f"Transfer form: from '{source}' to '{dest}', amount '{amount}', memo '{memo}' -> submit")
    if before_submit:
        before_submit()
    posts_before = sum(1 for e in d.net if e.method == "POST" and "/transfers" in e.url)
    if click == "double":
        btn.dblclick()
    else:
        d.click(btn)
    # Immediately after clicking: does the UI show it is busy / block a second submit?
    busy = btn.evaluate("b => ({disabled: b.disabled, text: b.innerText, "
                        "formBusy: b.form ? b.form.getAttribute('aria-busy') : null})")

    p = d.page
    success_loc = p.locator("main [role=status]").filter(has_text=re.compile(r"transfer (complete|successful)|success", re.I))
    reject_loc = p.locator("main [role=alert], main .field__error, main [aria-invalid=true]")
    success = rejected = False
    deadline = time.time() + 12
    while time.time() < deadline:
        p.wait_for_timeout(200)
        success = success_loc.count() > 0
        rejected = any(reject_loc.nth(i).is_visible() for i in range(min(reject_loc.count(), 10)))
        loading = p.locator("main [role=status]").filter(has_text=re.compile(r"submitting", re.I)).count() > 0
        if (success or rejected) and not loading:
            break
    d.settle(4_000)
    msgs = [m for m in d.messages() if "submitting" not in m.lower()]
    # Error text only (alerts + field errors) – hints and success receipts must never be graded as
    # "the explanation", otherwise a wrongly-accepted transfer could pass the clarity check.
    errors: list[str] = []
    for loc in (p.locator("main [role=alert]"), p.locator("main .field__error")):
        for i in range(min(loc.count(), 10)):
            t = loc.nth(i).inner_text().strip()
            if t and loc.nth(i).is_visible() and t not in errors:
                errors.append(t)
    for el in p.locator("main [aria-invalid=true]").all():
        for ref in (el.get_attribute("aria-describedby") or "").split():
            node = p.locator(f"#{ref}")
            if node.count() and "error" in ref:
                t = node.inner_text().strip()
                if t and t not in errors:
                    errors.append(t)
    posts = [e for e in d.net if e.method == "POST" and "/transfers" in e.url]
    new_posts = posts[posts_before:]
    api = new_posts[-1] if new_posts else None
    out = TransferOutcome(success, rejected, msgs, api.status if api else None,
                          api.body if api else "", hung=not (success or rejected))
    out.error_messages = errors
    out.busy_after_click = busy
    out.post_count = len(new_posts)
    return out


def restore_balances(ctx: Ctx, before: "ApiState") -> str:
    """Compensate any balance drift a scenario caused, so later scenarios start from known state.

    Uses the API directly with a clear memo. This is test hygiene, not part of the behaviour under
    test, and every compensation is listed in the report.
    """
    after = api_state(ctx)
    if not (before.ok and after.ok):
        return "restore skipped: state unavailable"
    diffs = {i: after.balance(i) - before.balance(i) for i in before.accounts if i in after.accounts}
    gainers = [i for i, v in diffs.items() if v > 0]
    losers = [i for i, v in diffs.items() if v < 0]
    if not gainers or not losers:
        return "no drift"
    g, l = gainers[0], losers[0]
    amount = min(diffs[g], -diffs[l])
    status, _ = ctx.d.api_post("/api/transfers", {
        "source_account_id": g, "destination_account_id": l,
        "amount": f"{amount:.2f}", "memo": f"QA-{ctx.run.run_id} restore after {ctx.sc.id}"})
    note = f"compensating transfer {amount:.2f} {after.accounts[g]['name']} -> {after.accounts[l]['name']}: HTTP {status}"
    ctx.run.ledger_moves.append({"scenario": ctx.sc.id, "action": note})
    return note
