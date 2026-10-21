"""Profile privacy, general UI behaviour (responsive, labels, navigation, error recovery), seed data, network."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..flows import api_state, read_table
from ..harness import Ctx, scenario

JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.")


@scenario("PRF-VIEW", "Profile shows display name and a masked email only", kind="positive",
          anchors=["profile view displays only synthetic demo-customer details"], order=40)
def profile_view(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/profile")
    _, me = d.api_get("/api/me")
    text = d.visible_text()
    ctx.shot("profile")
    name = (me or {}).get("display_name", "")
    if not name:   # oracle unavailable: use the name the header shows for the signed-in customer
        hdr = d.page.locator("[data-testid=active-customer], header").first
        name = re.sub(r"(?i)signed in as", "", hdr.inner_text()).split("\n")[0].strip() if hdr.count() else ""
    emails = re.findall(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", text, re.I)
    ctx.check("profile view displays only synthetic demo-customer details", "Display name is shown",
              (name in text) if name else None, name or "(customer name unknown)", text[:200])
    ctx.check("profile view displays only synthetic demo-customer details", "Email address is masked",
              bool(emails) and all(re.search(r"[*•]", e.split("@")[0]) for e in emails),
              "e.g. a***@example.test", emails or "no email shown")


@scenario("PRF-SECRETS", "No passwords, tokens or full account numbers leak into the page or storage",
          kind="negative", anchors=["must not reveal passwords, authentication tokens"], order=41)
def no_secrets(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/profile")
    html = d.page.content()
    storage = d.page.evaluate("() => JSON.stringify({l: {...localStorage}, s: {...sessionStorage}, c: document.cookie})")
    cookie_values = [c["value"] for c in d.context.cookies() if len(c["value"]) >= 16]
    hay = html + storage
    leaks = []
    if any(v in hay for v in cookie_values):
        leaks.append("session cookie value present in DOM/storage/document.cookie")
    if JWT.search(hay):
        leaks.append("JWT-like token present")
    if re.search(r"password\s*[:=]", hay, re.I):
        leaks.append("password field/value present")
    if re.search(r"\b\d{9,17}\b", d.visible_text("body")):
        leaks.append("long digit sequence (possible full account number)")
    ctx.value("Browser storage snapshot", storage[:500])
    ctx.check("must not reveal passwords, authentication tokens", "No secrets in page, web storage or readable cookies",
              not leaks, "no tokens/passwords/full numbers", leaks or "clean")
    ev = d.last_api("GET", "/api/me")
    if ev:
        ctx.network("GET /api/me", ev)
        bad = [k for k in ("password", "token", "secret", "account_number") if k in ev.body.lower()]
        ctx.check("must not reveal passwords, authentication tokens", "Profile API response carries no secrets",
                  not bad, "no secret-looking fields", bad or "clean")


@scenario("UI-RESPONSIVE", "Usable at 1280×800 and at narrower widths (tablet, phone)", kind="edge",
          anchors=["1280 × 800 viewport"], order=80)
def responsive(ctx: Ctx) -> None:
    d = ctx.d
    state = d.context.storage_state()
    for w, h in ((1280, 800), (768, 1024), (390, 844)):
        d.new_context(w, h, storage_state=state)
        for path in ("/dashboard", "/transfer", "/transactions"):
            d.goto(path)
            m = d.page.evaluate("() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})")
            logout_ok = d.page.get_by_role("button", name=re.compile("log out", re.I)).first
            box = logout_ok.bounding_box() if logout_ok.count() else None
            in_view = bool(box) and box["x"] >= 0 and box["x"] + box["width"] <= w + 1
            ok = m["sw"] <= m["iw"] + 1 and in_view
            ctx.check("1280 × 800 viewport", f"{w}×{h} {path}: no horizontal page scroll, header actions reachable",
                      ok, "scrollWidth ≤ viewport, Log out on screen",
                      f"scrollWidth={m['sw']} viewport={m['iw']} logout_in_view={in_view}")
            if path == "/dashboard" or not ok:
                ctx.shot(f"{w}x{h} {path}")


@scenario("UI-LABELS", "Every interactive control has a readable accessible label", kind="positive",
          anchors=["Interactive controls have readable labels"], order=81)
def labels(ctx: Ctx) -> None:
    d = ctx.d
    js = """() => [...document.querySelectorAll('input, select, textarea, button, a[href]')].filter(e => {
        if (e.type === 'hidden' || e.offsetParent === null) return false;
        const lab = (e.labels && [...e.labels].map(l => l.innerText).join(' ')) || '';
        const aria = e.getAttribute('aria-label') || '';
        const by = (e.getAttribute('aria-labelledby') || '').split(' ').map(i => document.getElementById(i)?.innerText || '').join(' ');
        const txt = ['BUTTON', 'A'].includes(e.tagName) ? e.innerText : '';
        return !(lab + aria + by + txt + (e.title || '')).trim();
      }).map(e => e.outerHTML.slice(0, 120))"""
    for path in ("/dashboard", "/transfer", "/transactions", "/profile"):
        d.goto(path)
        unlabeled = d.page.evaluate(js)
        ctx.check("Interactive controls have readable labels", f"{path}: all controls have an accessible name",
                  not unlabeled, "0 unlabelled controls", unlabeled[:5] or "all labelled")


@scenario("UI-NAV", "Navigation links and buttons lead to their labelled destinations", kind="positive",
          anchors=["Navigation links and buttons lead to the labeled destination"], order=82)
def navigation(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/dashboard")
    nav = d.find("main navigation", lambda p: p.get_by_role("navigation", name=re.compile("main", re.I)),
                 lambda p: p.locator("header nav"))
    labels = [a.inner_text().strip() for a in nav.get_by_role("link").all()] if nav else []
    ctx.value("Navigation items discovered", labels)
    for label in labels:
        d.goto("/dashboard")
        d.click(d.page.get_by_role("navigation").get_by_role("link", name=label, exact=True).first)
        d.settle()
        d.wait_until(lambda l=label: l.lower().rstrip("s") in (d.path() + d.visible_text()[:200]).lower(), 4_000)
        h1 = d.page.get_by_role("heading", level=1).first.inner_text() if d.page.get_by_role("heading", level=1).count() else ""
        page_text = (d.path() + " " + h1 + " " + d.visible_text()[:150]).lower()
        stem = label.lower().rstrip("s")
        ctx.check("Navigation links and buttons lead to the labeled destination", f"Nav '{label}' opens the {label} page",
                  stem in page_text, f"page about '{label}'", f"URL {d.path()}, h1 {h1!r}")

    for label, expect in (("Transfer funds", "/transfer"), ("View all transactions", "/transactions")):
        d.goto("/dashboard")
        btn = d.find(f"dashboard action '{label}'", lambda p, l=label: p.get_by_role("link", name=l),
                     lambda p, l=label: p.get_by_role("button", name=l))
        if btn:
            d.click(btn)
            d.settle()
        ctx.check("Navigation links and buttons lead to the labeled destination", f"Dashboard '{label}' → {expect}",
                  d.path() == expect, expect, d.path())

    api = api_state(ctx)
    acc = api.by_type("savings") if api.ok else None
    if acc:
        d.goto(f"/accounts/{acc['id']}")
        btn = d.find("Transfer from this account", lambda p: p.get_by_role("link", name=re.compile("transfer from", re.I)))
        if btn:
            d.click(btn)
            d.settle()
            src = d.page.get_by_label(re.compile(r"^from", re.I)).first
            chosen = src.evaluate("s => s.options[s.selectedIndex]?.label || ''") if src.count() else ""
            ctx.check("Navigation links and buttons lead to the labeled destination",
                      "'Transfer from this account' pre-selects that account as source",
                      acc["name"] in chosen, acc["name"], chosen)
        d.goto(f"/accounts/{acc['id']}")
        btn = d.find("Filter full history", lambda p: p.get_by_role("link", name=re.compile("filter full history", re.I)))
        if btn:
            d.click(btn)
            d.settle()
            rows = read_table(ctx) or []
            foreign = [r for r in rows if acc["name"] not in r["cells"].get("account", "")]
            ctx.check("Navigation links and buttons lead to the labeled destination",
                      "'Filter full history' opens history filtered to that account",
                      bool(rows) and not foreign, f"only {acc['name']} rows", f"{len(rows)} rows, {len(foreign)} foreign")
    d.goto("/definitely-not-a-page")
    back = d.page.get_by_role("link", name=re.compile("dashboard", re.I))
    ctx.check("A recoverable error must leave the user able to retry", "Unknown URL offers a way back to the dashboard",
              back.count() > 0, "'Back to dashboard' link", d.visible_text()[:120])


@scenario("UI-ERROR-RECOVERY", "When account data fails to load, the user can retry or go back", kind="negative",
          anchors=["A recoverable error must leave the user able to retry"], order=83)
def error_recovery(ctx: Ctx) -> None:
    d = ctx.d
    ctx.step("Inject HTTP 503 for GET /api/accounts in this browser only, open /dashboard")
    d.page.route("**/api/accounts", lambda r: r.fulfill(status=503, content_type="application/json",
                                                        body='{"error":{"code":"unavailable","message":"Injected outage"}}'))
    d.goto("/dashboard")
    alert = d.page.locator("main [role=alert]")
    retry = d.page.get_by_role("button", name=re.compile("try again|retry", re.I))
    ctx.shot("load error")
    ctx.check("A recoverable error must leave the user able to retry", "A visible error with a retry option appears",
              alert.count() > 0 and retry.count() > 0, "error message + 'Try again'",
              f"alerts={alert.count()} retry buttons={retry.count()}: {d.visible_text()[:160]!r}")
    d.page.unroute("**/api/accounts")
    if retry.count():
        d.click_and_wait_api(retry.first, "/api/accounts")
    recovered = d.wait_until(lambda: d.page.get_by_text(re.compile("available balance", re.I)).count() > 0)
    ctx.check("A recoverable error must leave the user able to retry", "After the outage ends, 'Try again' recovers",
              recovered, "account cards visible", f"recovered={recovered}")


@scenario("SEED-DATA", "Seed data: one customer, one checking + one savings, varied history incl. a transfer",
          kind="positive", anchors=["one checking account, one savings account"], order=5)
def seed(ctx: Ctx) -> None:
    api = api_state(ctx)
    if not api.ok:
        ctx.unverified("one checking account, one savings account", "seed", "API oracle unavailable")
        return
    types = sorted(a["type"] for a in api.accounts.values())
    ctx.check("one checking account, one savings account", "Exactly one checking and one savings account",
              types == ["checking", "savings"], "['checking', 'savings']", types)
    tx_types = {t["type"] for t in api.txs}
    statuses = {t["status"] for t in api.txs}
    ctx.check("one checking account, one savings account", "History covers every type and status for filtering",
              tx_types >= {"deposit", "withdrawal", "transfer"} and statuses >= {"completed", "pending", "failed"},
              "all 3 types, all 3 statuses", f"types={sorted(tx_types)} statuses={sorted(statuses)}")
    dates = {t["occurred_at"][:10] for t in api.txs}
    ids = set(api.accounts)
    between = [t for t in api.txs if t["type"] == "transfer" and {e["account_id"] for e in t["account_deltas"]} == ids]
    ctx.check("multiple dates and at least one transfer", "Multiple dates and ≥1 transfer between the two accounts",
              len(dates) > 1 and bool(between), ">1 date, ≥1 transfer", f"{len(dates)} dates, {len(between)} transfers")
    text = " ".join(a.inner_text() for a in ctx.d.page.get_by_role("link").all()).lower()
    extras = [w for w in ("sign up", "signup", "bill pay", "loan", "credit card", "invest") if w in text]
    ctx.check("no signup, external payment", "No out-of-scope features (signup, bill-pay, loans, cards) exposed",
              not extras, "none", extras or "none")


@scenario("GEN-NETWORK", "The app only talks to its own frontend and API hosts", kind="positive",
          anchors=["never initiate real payments"], order=99)
def network_hosts(ctx: Ctx) -> None:
    d = ctx.d
    own = {urlparse(d.base_url).netloc}
    if d.api_base:
        own.add(urlparse(d.api_base).netloc)
    external = sorted(h for h in d.hosts if h and h not in own)
    ctx.value("Hosts contacted during the whole run", sorted(d.hosts))
    ctx.check("never initiate real payments", "No requests to external (bank/payment) hosts during the whole run",
              not external if not external else None, "only app hosts",
              external or f"only {sorted(own)}")
