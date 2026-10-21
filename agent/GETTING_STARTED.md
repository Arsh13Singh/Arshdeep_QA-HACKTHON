# Getting started: run the agent on your PC with VS Code

Written for someone who has never run an agent before. Total time: about 30 minutes.
Everything runs in one folder on your computer. Commands are shown for **Windows (PowerShell)**
and **Mac (Terminal)**. Use the one for your computer.

---

## Step 1: Install Python and VS Code (one time)

1. **Python 3.11 or newer**: python.org/downloads.
   **Windows: on the first installer screen, tick "Add python.exe to PATH"** before clicking Install.
2. **VS Code**: code.visualstudio.com.
3. In VS Code, open the **Extensions** panel (`Ctrl+Shift+X`, Mac `Cmd+Shift+X`), search **Python**
   and install the one by Microsoft.

## Step 2: Make a project folder and unzip the code

1. Create a folder, e.g. `Documents\qa-hackathon` (Mac: `Documents/qa-hackathon`).
2. Unzip `agentic-qa-agent.zip` into it. You'll get:
   ```
   qa-hackathon/
     agentic-qa/
       .vscode/          ← ready-made "Run" buttons for VS Code
       agent/            ← the QA agent (this is what you run)
       test-website/     ← DemoBank source + PRD.md (the spec the agent reads)
   ```

## Step 3: Open the folder in VS Code

**File → Open Folder…** and choose the **`agentic-qa`** folder (not `qa-hackathon`, and not `agent`).
If VS Code asks "Do you trust the authors?", click **Yes**.

Open a terminal inside VS Code: **Terminal → New Terminal** (or `` Ctrl+` ``).
It opens in the `agentic-qa` folder.

## Step 4: Create the virtual environment and install packages

A *virtual environment* (`.venv`) is a private set of Python packages for this project only.
Paste these lines into the VS Code terminal one at a time.

**Windows (PowerShell):**
```powershell
cd agent
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```
If you see *"running scripts is disabled on this system"*, run this once, then repeat the Activate line:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Mac:**
```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Your prompt now starts with **`(.venv)`**. Whenever you open a new terminal later, run
`cd agent` and the Activate line again.

**Tell VS Code to use it:** press `Ctrl+Shift+P` (Mac `Cmd+Shift+P`), type **Python: Select Interpreter**,
and pick the one that shows `.venv` (path ends in `agent\.venv`).

## Step 5: Check the installation

```
python -m pytest -q tests
```
Expected: **`9 passed`**. These tests need no browser, no API key and no app. They only prove
the install worked.

## Step 6: Add your free Groq API key

1. Go to **console.groq.com**, sign in, open **API Keys → Create API Key**, and copy it (starts with `gsk_`).
2. In VS Code's file explorer (left side), open the `agent` folder, right-click **`.env.example`**,
   choose **Copy**, then right-click the `agent` folder and choose **Paste**. Rename the copy to exactly **`.env`**.
   (Or in the terminal: Windows `copy .env.example .env` · Mac `cp .env.example .env`.)
3. Open `.env` and paste the key after the `=` with no spaces or quotes:
   ```
   GROQ_API_KEY=gsk_your_key_here
   ```
   Save (`Ctrl+S`).

Keep this file private and don't share it.

4. **Test the key** (takes 5 seconds):
   ```
   python -m qa_agent --check-llm
   ```
   You should see the models your key can use and `OK: <model> answered`. The agent picks the best
   available model automatically, so you don't need to choose one.

> No key yet? You can still run everything by adding `--offline`. The AI steps fall back to simple
> rules, and the report estimates what the AI calls would have cost.

## Step 7: Have DemoBank running somewhere

The agent tests a running website, so you need its URL.

- **Option A (easiest): the organisers' hosted URL.** Just copy it.
- **Option B: run DemoBank on your own computer.** On a Mac, follow **`RUN_DEMOBANK_ON_MAC.md`**
  (in the `agentic-qa` folder). It has one-click VS Code tasks. Elsewhere, see `test-website/LOCAL_SETUP.md`.
  When it's up, the URL is **`http://localhost:5173`**. Type `localhost`, not `127.0.0.1`.

Check the URL first by opening it in your normal browser. You should see the **Enter Demo** button.

## Step 8: Run the agent

**The easy way (buttons):** open the **Run and Debug** panel (`Ctrl+Shift+D`), pick
**"QA agent: full run (watch browser)"** at the top, press **F5**, and type the DemoBank URL when asked.

**Or in the terminal** (inside `agent`, with `(.venv)` active):
```
python -m qa_agent --url <DEMOBANK-URL> --prd ../test-website/PRD.md --headed
```

A Chromium window opens and you'll watch the agent click through the site. In the terminal:
```
[interpret] 36 requirements extracted from PRD.md (LLM-enriched via groq / llama-3.3-70b-versatile)
[plan] 42 scenarios: positive=17, negative=12, edge=13
[preflight] target reachable; API discovered at http://...
  ✔ SES-ENTER     passed   1.9s  Enter Demo opens the dashboard without credentials
  ✘ TRF-OVERDRAFT failed   2.2s  Amount one cent above the available balance is rejected
  ...
[report] requirements {'passed': .., 'failed': .., 'unverified': ..} | LLM cost $0.00.. (groq list price) | Claude-equivalent $0.0...
[report] runs/run-20261021-....../report.html
```
A full run takes about 2–3 minutes. Useful extras:
- drop `--headed` for a faster run without a browser window
- `--only transfers` runs one area (or use the "one suite only" Run button)
- `--chaos` renames buttons and labels while it runs, to show the agent copes with UI changes

## Step 9: Open the report

In VS Code's explorer: `agent → runs → run-<date-time> → report.html`. Right-click it and choose
**Reveal in File Explorer** (Mac: **Reveal in Finder**), then double-click to open it in your browser.

What to look at:
- **Tiles at the top:** passed / failed / unverified, and cost.
- **Defects:** each has severity, steps to reproduce, expected vs observed values and a screenshot.
- **Requirement coverage:** every PRD requirement with its verdict and the reason.
- **Cost & efficiency:** tokens per AI call, Groq price, and the Claude-equivalent price.

Each run creates a new `run-…` folder, so nothing is overwritten.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `python` / `py` is not recognized | Reinstall Python with **"Add python.exe to PATH"** ticked, then restart VS Code |
| `Activate.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again |
| `No module named qa_agent` | You're not in the `agent` folder: run `cd agent` (the Run buttons handle this for you) |
| `No module named playwright` | `.venv` isn't active (no `(.venv)` in the prompt): run the Activate line |
| `Executable doesn't exist … chromium` | `python -m playwright install chromium` |
| `[preflight] target not usable` | The URL isn't reachable. Open it in your browser; for local DemoBank, check all three parts are running |
| Terminal shows `[preflight] WARNING: could not verify the app's API` | Add `--api-url http://localhost:8000` (local DemoBank) |
| Report's fallbacks mention `API error 401` | The Groq key in `.env` is wrong, or has spaces or quotes around it |
| `API error 429` | Groq free-tier rate limit. Wait a minute and rerun; finished AI answers are cached, so reruns are cheap |
| Lots of **unverified** results | The agent couldn't find the app's API. Add `--api-url <backend-url>` (local: `http://localhost:8000`) |

If you get stuck, copy the **exact error text** from the terminal and send it to me.
