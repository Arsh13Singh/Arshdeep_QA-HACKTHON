# Learning guide: building a spec-driven QA agent

This guide explains *why* the agent is built the way it is, so you can defend every decision
in an interview, a demo or a LinkedIn post. Each section points to the file where the idea lives.

---

## 1. The core problem: a click that works is not a pass

Most automated tests check "did the button click without an error?". DemoBank is built to punish
that. A negative-amount transfer shows a green **"Transfer complete"**, and that green message is the bug.

The real question is: **what is true in the system after the action, and does it match the spec?**
In testing this is called the **oracle problem**: you need a source of truth to compare against.

Our oracle has two parts (`flows.py → api_state`, `read_dashboard_cards`):
- what the **UI** shows (parsed from visible text)
- what the app's own **API** returns for the same session

Pattern used everywhere (`suites/transfers.py`):
```
before = snapshot()  →  act through the UI  →  after = snapshot()
assert UI said the right thing   AND   assert the right things changed (and nothing else)
```

> Interview line: "I verified side effects, not UI messages. For each transfer I checked exact
> balance deltas, that exactly one record exists in both account histories, and that it survives a refresh."

---

## 2. Requirements as data

`prd.py` turns prose into structured requirements with **stable IDs** (`TRF-04`), so results can
be compared across runs, and every assertion records which requirement it covers.

Scenarios don't hard-code IDs. They declare an **anchor phrase** ("start and end dates are inclusive")
that is resolved by substring search. Reorder the PRD and the mapping still holds; delete the
sentence and the planner warns you.

**Roll-up rule** (`models.py → roll_up`), the heart of honest reporting:
- any failed assertion → **failed**
- all assertions passed → **passed**
- nothing reliable observed → **unverified** (never a silent pass)

---

## 3. Test-design techniques used

| Technique | What it means | Where we used it |
|---|---|---|
| **Equivalence partitioning** | Group inputs that should behave the same; test one per group | amounts: blank, zero, negative, non-numeric, too precise |
| **Boundary value analysis** | Bugs cluster at edges; test exactly at and just past them | $0.01 (min), exactly the balance (allowed), balance + $0.01 (must fail), start date = end date |
| **Negative testing** | Prove invalid input is rejected *and leaves no trace* | every rejection checks balances unchanged + no new record |
| **State-transition testing** | Behaviour depends on state (logged in/out) | logout → back button → refresh → direct URL → replayed cookie |
| **Differential checking** | Compare two views that must agree | dashboard vs detail vs API balance; filtered rows vs filter applied to the full list |
| **Fault injection** | Simulate failures to test recovery | Playwright `route()` returns 500/503 inside our browser only |

A favourite: the **date-boundary** test picks a transaction stored at `2025-03-05 02:45 UTC`, which
is **Mar 4** in Pacific Time (the UI's stated zone). Off-by-one and timezone bugs hide exactly there.
The agent found that the end date is treated as exclusive: start works, end doesn't. That
precise diagnosis comes from testing start-only, end-only and start=end separately.

---

## 4. Playwright essentials

- **Contexts**: `browser.new_context()` is an isolated profile (own cookies). One per scenario, so tests can't leak state into each other.
- **Locators**: prefer `get_by_role("button", name=...)` and `get_by_label(...)`. They mirror how users and screen readers find things, so they survive CSS and DOM refactors.
- **Auto-waiting** plus our `settle()` (spinners gone, network idle) replaces `sleep()`. Fixed sleeps are slow *and* flaky.
- **Network**: `page.on("response")` records API traffic as evidence; `page.route()` fakes failures.
- **CDP**: `Network.emulateNetworkConditions` adds 1.5 s latency so the double-click test can catch a missing loading state.

---

## 5. Resilience (why it doesn't break when the UI changes)

`browser.py → Driver.find(intent, *strategies)`:
1. Try semantic strategies in order (label → placeholder → test-id …).
2. Remember which one worked and try it first next time.
3. If all fail: send the page's **ARIA snapshot** to a cheap model, which proposes `{role, name}`. Verify it is visible, then use it.

Proof: `--chaos` renames labels at runtime. Same verdicts on all 36 requirements, 38 auto-recoveries.

**Retries need idempotency thinking.** A failed read-only scenario is retried. A scenario that already
sent a transfer is **not**, because retrying could move money twice. Knowing *when not to retry* is a
senior-level signal.

---

## 6. LLMs in the loop: use them where judgement is needed

Calling an LLM for every click is slow, expensive and non-deterministic. Here the model does four jobs:

| Job | Why an LLM | Cost control |
|---|---|---|
| Decompose the PRD into atomic assertions and test ideas | Language understanding | Once per PRD, cached on disk |
| "Is this error message a *clear explanation*?" | Subjective | **One batched call** for all messages |
| Heal a broken locator | Fuzzy matching on the ARIA tree | Only on failure; cheapest model |
| Executive summary and likely root causes | Writing | One call |

Everything else is deterministic code, so verdicts are reproducible. With no API key, each job
has a fallback (heuristic, template), and the report says which ones ran.

**Cost maths** (`llm.py`): cost = input_tokens × input_price + output_tokens × output_price.
Haiku 5.5 is $0.10 / $0.50 per million tokens: a 1,000-token judge call costs about $0.0002.

---

## 7. False positives: the two bugs I found in my own agent

Judges score "few false positives" heavily. While building this, the agent itself had two bugs:

1. **The judge graded a success receipt.** When the app wrongly accepted a $0 transfer, the
   "is the message clear?" check read the *success receipt* and passed it. Fix: only grade text
   from error elements (`role=alert`, field errors), and only when the UI actually rejected the input.
2. **Stale data matched.** A memo check looked for `<b>bold</b>` and found a row left by a
   *previous* run. Fix: match on the unique memo `QA-<run-id>-<scenario>`. This matters even more on a
   shared hosted app.

3. **It worked on my machine, then failed on a real one.** My first run on a Mac (Vite dev server, real React
   Router) marked 21 requirements "unverified". Three causes: (a) API discovery grabbed a *source file*
   (`/src/api/client.ts`) as the backend, because its URL contains `/api/`; (b) React kept the *old* screen visible
   for a moment after navigating, so the agent read stale content; (c) the configured Groq model wasn't available
   to that key. Fixes: only count fetch/XHR JSON responses, wait for the screen to stop changing and for the
   triggering data request to finish, and ask the provider which models the key can use. Lesson: test the tester in
   the same environment the users have, and make it say "unverified" loudly instead of guessing.

> Interview line: "I treated my test agent as code that can be wrong. I reviewed every failing
> assertion against the evidence and found two places where the agent itself would have given wrong
> results, then fixed the root cause."

---

## 8. Reporting that people act on

Each defect has: severity (rule-based, explainable), requirement IDs, numbered repro steps,
expected vs observed values (e.g. `Everyday Checking $4369.75 → $-0.01`), screenshots and the raw API response.
Severity rule: *Critical* = money or records changed when they must not; *High* = rule violated without
data corruption; *Medium* = wrong/missing display or filter data; *Low* = wording/UX.

---

## 9. Explaining the project

**30 seconds:** "I built an AI QA agent that reads a product spec, turns it into 36 testable
requirements, and drives a real browser through 42 normal, negative and edge-case scenarios on a banking
app. It verifies side effects like exact balance changes instead of trusting success messages, and found
critical bugs such as overdrafts and negative transfers. It costs about four cents a run because the LLM is
only used for judgement calls."

**Likely interview questions**
- *Why not let the LLM drive the browser fully?* Cost, speed, reproducibility. Deterministic code for checkable facts, the LLM for fuzzy judgement; the hybrid gets the best of both.
- *How do you avoid flaky tests?* Isolated contexts, condition-based waits, semantic locators, safe retries, unique test data.
- *How do you know a "fail" is real?* Two independent views (UI and API) must agree, and the evidence (values, screenshot, HTTP response) ships with the verdict.
- *What would you add next?* Run it in CI on every deploy; diff reports between runs; let the LLM generate *new* scenarios for requirements that are still unverified; visual regression.

---

## 10. Resume bullets (pick 2–3)

- Built an autonomous, spec-driven QA agent (Python, Playwright, Claude API) that converts a PRD into 36 traceable requirements and runs 42 positive/negative/edge scenarios with 190 assertions, producing auditable pass/fail/unverified reports.
- Found 5 spec violations in a banking app, including 3 critical financial-integrity defects (overdrafts, negative and zero-value transfers), by verifying balance deltas and records instead of UI success messages.
- Designed a hybrid deterministic/LLM architecture (batched judging, disk caching, cheapest-model routing) costing ~$0.04 per run; self-healing locators kept verdicts identical under randomised UI label changes.

## 11. LinkedIn post draft

> I just built an AI agent that tests software by reading its spec. 🧪
>
> The challenge (CDF Agentic QA Hackathon): given a product spec and a demo banking app with hidden bugs, build an agent that decides whether the app actually does what the spec says.
>
> What I learned:
> 1️⃣ A green "Transfer complete" message proves nothing. My agent checks what really changed: exact balance deltas, records in both account histories, and whether they survive a refresh. That's how it caught a –$5.00 transfer that *added* money and an overdraft to –$0.01.
> 2️⃣ Use the LLM only where judgement is needed. Clicking and checking is plain Playwright code; Claude reads the spec, grades whether error messages are clear (in one batched call) and heals broken selectors. Result: ~4 cents per full run.
> 3️⃣ Test your tester. I found two bugs in my own agent that would have produced false results, and fixing them made the reports more trustworthy than adding 20 more tests would have.
>
> 36 requirements · 42 scenarios · 190 assertions · ~2 minutes per run.
>
> Repo + sample report: <link>
>
> #QA #TestAutomation #AIAgents #Playwright #Python #Hackathon

---

## 12. Exercises to go deeper

1. Add a scenario: a transfer memo of 500 characters. What *should* happen per the PRD? (Hint: the PRD is silent, so what verdict is honest?)
2. Make the planner print, for each requirement, which scenarios cover it (a traceability matrix).
3. Run with `--headed` and watch `TRF-DOUBLE`. Why does it need network throttling to be meaningful?
4. Add a GitHub Actions workflow that runs `--offline` against a local target and uploads `report.html` as an artifact.
