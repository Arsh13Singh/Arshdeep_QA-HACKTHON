"""Browser layer: Playwright wrapper with self-healing locators and evidence capture.

Resilience techniques used here (the "autonomous resilience" rubric item):

* Semantic locators first: get_by_role / get_by_label / get_by_text, the same cues a human
  or screen reader uses. Class names and DOM structure can change without breaking them.
* Multiple strategies per element: each `find()` call lists fallbacks in priority order.
* LLM healing: if every strategy fails, the page's ARIA snapshot is sent to a cheap model
  which proposes a role+name. The fix is verified (must be visible) and memoised for the run.
* Settle waits: we wait for loading indicators to disappear instead of sleeping.
* Network observation: every /api/ response is recorded (method, URL, status, body). It is
  used as evidence and to discover the API base URL without being told it.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .llm import LLM
from .models import Evidence

Strategy = Callable[[Page], Locator]

# Cheap fingerprint of "what the user currently sees": URL + hash of the main content's text.
DOM_FINGERPRINT_JS = """() => {
  const t = (document.querySelector('main') || document.body).innerText;
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0;
  return location.href + '|' + t.length + '|' + h;
}"""

# --chaos: rename UI copy at runtime to prove the agent doesn't depend on exact wording.
CHAOS_JS = """
(() => {
  // Only form controls are renamed, so navigation-label checks stay meaningful.
  const swaps = [[/^Amount \\(USD\\)$/, 'Value'], [/^Transfer funds$/, 'Send money'], [/^Apply filters$/, 'Search']];
  const fix = (root) => {
    if (!root) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n; (n = w.nextNode());) {
      if (!n.parentElement || !n.parentElement.closest('form')) continue;
      for (const [re, to] of swaps) if (re.test(n.nodeValue.trim())) n.nodeValue = to;
    }
  };
  new MutationObserver(() => fix(document.body)).observe(document, {subtree: true, childList: true, characterData: true});
})();
"""
LOADING_RE = re.compile(r"loading|checking your|submitting|entering demo|logging out", re.I)


@dataclass
class NetEvent:
    method: str
    url: str
    status: int
    body: str


class HealEvent(dict):
    """intent, strategy that finally worked, whether the LLM was involved."""


class Driver:
    def __init__(self, base_url: str, out_dir: Path, llm: LLM, *, headless: bool = True,
                 api_base: str | None = None, chaos: bool = False) -> None:
        self.chaos = chaos
        self.base_url = base_url.rstrip("/")
        self.out_dir = out_dir
        self.shots = out_dir / "screenshots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.llm = llm
        self.headless = headless
        self.api_base = api_base.rstrip("/") if api_base else None
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.net: list[NetEvent] = []
        self.hosts: set[str] = set()
        self.heals: list[HealEvent] = []
        self._healed: dict[str, tuple[str, str]] = {}
        self._winner: dict[str, int] = {}
        self._shot_n = 0
        self.actions = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.headless)

    def stop(self) -> None:
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()

    def new_context(self, width: int = 1280, height: int = 800,
                    storage_state: dict | None = None) -> Page:
        """Fresh, isolated browser profile (no cookies) – every scenario starts clean."""
        if self.context:
            self.context.close()
        self.context = self.browser.new_context(viewport={"width": width, "height": height},
                                                storage_state=storage_state)
        self.context.set_default_timeout(10_000)
        if self.chaos:
            self.context.add_init_script(CHAOS_JS)
        self.page = self.context.new_page()
        self.page.on("response", self._on_response)
        self.page.on("request", lambda req: self.hosts.add(urlparse(req.url).netloc))
        return self.page

    def _on_response(self, response) -> None:
        url = response.url
        # Only data calls count. In dev mode the site also loads source files such as
        # /src/api/client.ts, which contain "/api/" but are JavaScript, not the backend.
        if response.request.resource_type not in ("fetch", "xhr"):
            return
        if "/api/" not in url and not url.endswith("/health"):
            return
        ctype = (response.headers or {}).get("content-type", "")
        if self.api_base is None and "/api/" in url and "json" in ctype:
            self.api_base = url.split("/api/")[0]
        try:
            body = response.text()[:4000]
        except Exception:
            body = ""
        self.net.append(NetEvent(response.request.method, url, response.status, body))

    # ------------------------------------------------------------------ navigation & waits
    def goto(self, path: str) -> None:
        self.actions += 1
        self.page.goto(self.base_url + path, wait_until="domcontentloaded")
        self.settle()

    def reload(self) -> None:
        self.actions += 1
        self.page.reload(wait_until="domcontentloaded")
        self.settle()

    def settle(self, timeout_ms: int = 12_000) -> bool:
        """Wait until the page has really finished updating. Returns False on timeout.

        Three conditions, all required:
        1. network quiet (no requests in flight)
        2. no visible loading indicator ("Loading…", "Submitting…", aria-busy)
        3. the DOM has stopped changing: the URL plus a fingerprint of the main content is identical
           across 3 samples ~150 ms apart. React may keep the *old* screen visible briefly after a
           navigation (concurrent rendering / transitions), so "network idle" alone is not enough.
        """
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5_000))
        except PWTimeout:
            pass
        deadline = time.time() + timeout_ms / 1000
        last, stable = None, 0
        while time.time() < deadline:
            try:
                busy = self.page.locator("[role=status], [aria-busy=true], button[disabled]").filter(
                    has_text=LOADING_RE).count() > 0
                sig = self.page.evaluate(DOM_FINGERPRINT_JS)
            except Exception:          # page navigating: try again shortly
                busy, sig = True, None
            if not busy and sig is not None and sig == last:
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
            last = sig
            self.page.wait_for_timeout(150)
        return False

    def wait_until(self, predicate: Callable[[], bool], timeout_ms: int = 8_000) -> bool:
        """Poll a condition (e.g. 'heading shows the account name') instead of reading once."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(200)
        return False

    def path(self) -> str:
        return urlparse(self.page.url).path

    # ------------------------------------------------------------------ locating
    def find(self, intent: str, *strategies: Strategy, timeout_ms: int = 4_000) -> Locator | None:
        """Return the first visible element matching any strategy, healing with the LLM if needed."""
        per = max(500, timeout_ms // max(1, len(strategies)))
        if intent in self._healed:
            role, name = self._healed[intent]
            strategies = (lambda p, r=role, n=name: p.get_by_role(r, name=n),) + strategies
        # Try the strategy that worked last time for this intent first (learned during the run).
        order = list(range(len(strategies)))
        if intent in self._winner:
            order.remove(self._winner[intent])
            order.insert(0, self._winner[intent])
        for rank, i in enumerate(order):
            try:
                loc = strategies[i](self.page).first
                loc.wait_for(state="visible", timeout=per if rank else max(per, 1500))
                if i > 0:
                    self.heals.append(HealEvent(intent=intent, via=f"fallback strategy #{i + 1}", llm=False))
                self._winner[intent] = i
                return loc
            except Exception:
                continue
        return self._llm_heal(intent)

    def _llm_heal(self, intent: str) -> Locator | None:
        try:
            snapshot = self.page.locator("body").aria_snapshot()[:6000]
        except Exception:
            return None
        answer = self.llm.json(
            "locator_healing",
            "You map a UI intent to an element in an ARIA snapshot. Reply only with JSON "
            '{"role": "<aria role>", "name": "<accessible name, exact>"} or {"role": null} if absent.',
            f"Intent: {intent}\n\nARIA snapshot of the current page:\n{snapshot}",
            max_tokens=150,
        )
        if not isinstance(answer, dict) or not answer.get("role"):
            return None
        try:
            loc = self.page.get_by_role(answer["role"], name=answer.get("name") or None).first
            loc.wait_for(state="visible", timeout=2_000)
        except Exception:
            return None
        self._healed[intent] = (answer["role"], answer.get("name", ""))
        self.heals.append(HealEvent(intent=intent, via=f"LLM: role={answer['role']} name={answer.get('name')}",
                                    llm=True))
        return loc

    def click(self, loc: Locator) -> None:
        self.actions += 1
        loc.click()

    def click_and_wait_api(self, loc: Locator, fragment: str, timeout_ms: int = 5_000) -> bool:
        """Click, then wait for the app's own data request (e.g. /api/transactions) to come back,
        then for the screen to settle. Returns False if no such request was seen (e.g. nothing changed)."""
        self.actions += 1
        seen = True
        try:
            with self.page.expect_response(
                    lambda r: fragment in r.url and r.request.resource_type in ("fetch", "xhr"),
                    timeout=timeout_ms):
                loc.click()
        except PWTimeout:
            seen = False
        self.settle()
        return seen

    def fill(self, loc: Locator, value: str) -> None:
        self.actions += 1
        loc.fill(value)

    # ------------------------------------------------------------------ evidence
    def screenshot(self, label: str, full_page: bool = True) -> Evidence:
        self._shot_n += 1
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:50]
        path = self.shots / f"{self._shot_n:03d}-{slug}.png"
        try:
            self.page.screenshot(path=str(path), full_page=full_page)
        except Exception as exc:  # page may be mid-navigation
            return Evidence("note", label, f"screenshot failed: {exc}")
        return Evidence("screenshot", label, str(path.relative_to(self.out_dir)))

    def visible_text(self, selector: str = "main") -> str:
        try:
            return self.page.locator(selector).first.inner_text(timeout=3_000)
        except Exception:
            try:
                return self.page.locator("body").inner_text(timeout=3_000)
            except Exception:
                return ""

    def messages(self) -> list[str]:
        """All visible alert/status/field-error messages – how the UI talks back to the user."""
        texts: list[str] = []
        scope = self.page.locator("main").first if self.page.locator("main").count() else self.page.locator("body")
        loc = scope.locator("[role=alert], [role=status], .field__error, [aria-live]")
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    t = el.inner_text().strip()
                    if t and t not in texts:
                        texts.append(t)
            except Exception:
                continue
        # Inline messages linked via aria-describedby on invalid fields.
        for el in self.page.locator("[aria-invalid=true]").all():
            ids = (el.get_attribute("aria-describedby") or "").split()
            for i in ids:
                try:
                    t = self.page.locator(f"#{i}").inner_text().strip()
                    if t and t not in texts:
                        texts.append(t)
                except Exception:
                    pass
        return texts

    def last_api(self, method: str, fragment: str) -> NetEvent | None:
        for ev in reversed(self.net):
            if ev.method == method and fragment in ev.url:
                return ev
        return None

    # ------------------------------------------------------------------ API oracle
    def api_get(self, path: str) -> tuple[int, dict | None]:
        """GET an API endpoint with the current browser session's cookies (read-only oracle)."""
        if not self.api_base:
            return 0, None
        try:
            r = self.context.request.get(self.api_base + path, timeout=10_000)
            try:
                return r.status, r.json()
            except Exception:
                return r.status, None
        except Exception:
            return 0, None

    def api_post(self, path: str, payload: dict) -> tuple[int, dict | None]:
        """Used only for compensating (restore) transfers – never for the behaviour under test."""
        if not self.api_base:
            return 0, None
        r = self.context.request.post(self.api_base + path, data=json.dumps(payload),
                                      headers={"Content-Type": "application/json"}, timeout=10_000)
        try:
            return r.status, r.json()
        except Exception:
            return r.status, None
