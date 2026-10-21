"""Users and access: Enter Demo, header identity, refresh, logout, protected routes."""
from __future__ import annotations

import re

from ..flows import enter_demo, is_protected_view, logout
from ..harness import Ctx, scenario

PROTECTED = ["/dashboard", "/transactions", "/transfer", "/profile"]


@scenario("SES-ENTER", "Enter Demo opens the dashboard without credentials", kind="positive",
          anchors=["clearly labeled Enter Demo"], order=10, needs_session=False)
def enter_demo_flow(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/")
    ctx.step("Open the landing page '/' in a fresh browser profile")
    btn = d.find("Enter Demo button", lambda p: p.get_by_role("button", name=re.compile(r"^\s*enter demo\s*$", re.I)),
                 lambda p: p.get_by_role("link", name=re.compile(r"enter demo", re.I)))
    ctx.shot("landing page")
    ctx.check("clearly labeled Enter Demo", "Landing page shows a control labelled 'Enter Demo'",
              btn is not None, "visible button/link named 'Enter Demo'",
              "found" if btn else "no control named 'Enter Demo'")
    creds = d.page.locator("input[type=password], input[type=email], input[autocomplete=username]").count()
    ctx.check("clearly labeled Enter Demo", "No password or identity fields are required",
              creds == 0, "0 credential inputs", f"{creds} credential input(s)")
    ctx.step("Click 'Enter Demo'")
    ok = enter_demo(ctx)
    cards = d.page.locator("text=/available balance/i").count()
    ctx.shot("after enter demo")
    ctx.check("clearly labeled Enter Demo", "Enter Demo opens the demo dashboard",
              ok and d.path() == "/dashboard" and cards > 0,
              "URL /dashboard with account balances visible",
              f"URL {d.path()}, {cards} balance block(s) visible")


@scenario("SES-HEADER", "Header shows the active customer and a Log out action", kind="positive",
          anchors=["header identifies the active demo customer"], order=11)
def header_identity(ctx: Ctx) -> None:
    d = ctx.d
    status, me = d.api_get("/api/me")
    name = (me or {}).get("display_name", "")
    ctx.value("Customer per /api/me", me)
    header = d.find("page header / banner", lambda p: p.get_by_role("banner"), lambda p: p.locator("header"))
    header_text = header.inner_text() if header else ""
    ctx.step("Read the page header on /dashboard")
    ctx.shot("header")
    ctx.check("header identifies the active demo customer", "Header shows the signed-in customer's name",
              bool(name) and name in header_text if name else None,
              f"header contains '{name}'", header_text.replace("\n", " ")[:200] or "(no header)")
    lo = d.page.get_by_role("button", name=re.compile(r"log\s*out", re.I))
    ctx.check("header identifies the active demo customer", "Header provides a 'Log out' action",
              lo.count() > 0 and lo.first.is_visible(), "visible 'Log out' button",
              f"{lo.count()} 'Log out' button(s)")


@scenario("SES-SINGLE", "Every demo session is the same single seeded customer", kind="positive",
          anchors=["one seeded demo customer"], order=12)
def single_customer(ctx: Ctx) -> None:
    d = ctx.d
    _, me1 = d.api_get("/api/me")
    ctx.step("Session A: read /api/me")
    d.new_context()
    ctx.step("Session B (new browser profile): Enter Demo and read /api/me")
    enter_demo(ctx)
    _, me2 = d.api_get("/api/me")
    ctx.value("Session A customer", me1)
    ctx.value("Session B customer", me2)
    ctx.check("one seeded demo customer", "Two independent demo sessions resolve to the same customer",
              bool(me1 and me2) and me1.get("id") == me2.get("id") if me1 and me2 else None,
              "identical customer id", f"A={(me1 or {}).get('id')} B={(me2 or {}).get('id')}")


@scenario("SES-REFRESH", "Refreshing protected pages keeps the session", kind="positive",
          anchors=["Refreshing while in a valid demo session"], order=13)
def refresh_keeps_session(ctx: Ctx) -> None:
    d = ctx.d
    for path in ["/dashboard", "/transactions", "/profile"]:
        d.goto(path)
        ctx.step(f"Open {path} and press browser refresh")
        d.reload()
        ok = d.path() == path and is_protected_view(ctx)
        ctx.check("Refreshing while in a valid demo session", f"Refreshing {path} keeps the user signed in",
                  ok, f"still on {path} with the signed-in header", f"URL {d.path()}; protected={is_protected_view(ctx)}")
    ctx.shot("after refresh")


@scenario("SES-LOGOUT", "Log out returns to landing and locks protected routes", kind="negative",
          anchors=["Logging out returns the user to the landing page"], order=90)
def logout_locks(ctx: Ctx) -> None:
    d = ctx.d
    d.goto("/transactions")
    cookies = d.context.cookies()
    _, accounts = d.api_get("/api/accounts")
    acc_id = (accounts or {}).get("accounts", [{}])[0].get("id")
    ctx.step("On /transactions, click 'Log out'")
    logout(ctx)
    ctx.shot("after logout")
    landing = d.page.get_by_role("button", name=re.compile(r"enter demo", re.I)).count() > 0
    ctx.check("Logging out returns the user to the landing page", "Log out returns to the landing page",
              d.path() == "/" and landing, "URL '/' with 'Enter Demo' visible", f"URL {d.path()}, Enter Demo visible={landing}")

    ctx.step("Press browser Back after logout")
    d.page.go_back()
    d.settle()
    ctx.check("Protected dashboard views should not remain usable after logout",
              "Browser Back after logout does not show private data",
              not is_protected_view(ctx), "landing/access-required page", f"URL {d.path()}; protected={is_protected_view(ctx)}")

    paths = PROTECTED + ([f"/accounts/{acc_id}"] if acc_id else [])
    for path in paths:
        ctx.step(f"Navigate directly to {path} after logout")
        d.goto(path)
        text = d.visible_text("body").lower()
        ok = not is_protected_view(ctx) and (d.path() == "/" or "access" in text or "enter demo" in text)
        ctx.check("direct navigation to them should return the user to the landing page",
                  f"Direct navigation to {path} after logout is blocked", ok,
                  "landing page or access-required state", f"URL {d.path()}; protected={is_protected_view(ctx)}")
        if not ok:
            ctx.shot(f"protected {path} visible after logout")

    d.goto("/dashboard")
    ctx.step("Refresh while logged out")
    d.reload()
    ctx.check("must not regain access merely by refreshing", "Refreshing after logout does not restore access",
              not is_protected_view(ctx), "still logged out", f"URL {d.path()}; protected={is_protected_view(ctx)}")

    # Server-side check: is the old session cookie really dead, or only forgotten by the browser?
    ctx.step("Replay the pre-logout session cookie against the API from a new browser profile")
    d.new_context()
    d.context.add_cookies(cookies)
    status, _ = d.api_get("/api/accounts")
    ctx.check("Protected dashboard views should not remain usable after logout",
              "The session is revoked server-side (old cookie gets 401)",
              status == 401 if status else None, "HTTP 401 for the replayed cookie", f"HTTP {status}")


@scenario("SES-ANON", "A visitor who never entered the demo cannot open protected pages", kind="negative",
          anchors=["direct navigation to them should return the user to the landing page"], order=14,
          needs_session=False)
def anonymous_blocked(ctx: Ctx) -> None:
    d = ctx.d
    for path in PROTECTED:
        ctx.step(f"Fresh browser profile: navigate directly to {path}")
        d.goto(path)
        ctx.check("direct navigation to them should return the user to the landing page",
                  f"Anonymous visit to {path} is redirected", not is_protected_view(ctx),
                  "landing page or access-required state", f"URL {d.path()}; protected={is_protected_view(ctx)}")
    ctx.shot("anonymous redirect")
