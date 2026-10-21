# Run DemoBank on your Mac (with VS Code)

You don't have the hosted URL yet, so you'll run the website yourself. DemoBank has three parts
that start in this order:

```
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │  1 Database  │ ←── │  2 Backend   │ ←── │  3 Frontend  │ ←── your browser / the QA agent
 │  PostgreSQL  │     │  Python API  │     │  React site  │
 │  (Docker)    │     │  :8000       │     │  :5173       │
 └──────────────┘     └──────────────┘     └──────────────┘
```

When all three are running, the website is at **http://localhost:5173**. That's the URL you give the agent.

---

## Part A: Install three things (one time, ~15 min)

1. **Docker Desktop** (runs the database): docker.com/products/docker-desktop.
   Pick the right Mac download: Apple menu → About This Mac. "Chip: Apple M…" → **Apple Silicon**;
   "Processor: Intel" → **Intel**. Install it, **open Docker Desktop**, accept the terms, and wait until
   it says **Engine running** (green, bottom-left). Docker must be open whenever you use DemoBank.
2. **Node.js 20 or newer** (runs the website): nodejs.org → the **LTS** installer.
3. **Python 3.11 or newer**: you may have it already from the agent setup. Check in a VS Code terminal:
   ```bash
   python3 --version     # needs 3.11 or higher
   node --version        # needs v20 or higher
   docker --version      # any version; "command not found" means Docker Desktop isn't installed/open
   ```
   If Python is older than 3.11, install the latest from python.org/downloads, then open a **new** terminal.

## Part B: One-time setup (~3 min)

Open the `agentic-qa` folder in VS Code (File → Open Folder…).

**The easy way:** press `Cmd+Shift+P` → type **Tasks: Run Task** → choose **DemoBank: 1. one-time setup**.
Wait for "DemoBank setup complete".

> **If VS Code asks "Do you want to enable grunt task detection for all workspaces?", click No.**
> Grunt is an unrelated build tool that DemoBank doesn't use. The project's `.vscode/settings.json` turns this
> detection off, so the question shouldn't come back. If the task list shows categories instead of task
> names, choose **Show All Tasks…** and the DemoBank tasks are listed there.

<details><summary>What that task does (or do it by hand)</summary>

```bash
cd test-website/backend
python3 -m venv .venv                 # private Python environment for the backend
.venv/bin/pip install -e .            # installs FastAPI, the Postgres driver, uvicorn
printf 'DATABASE_URL=postgresql://demobank:demobank_local@localhost:5432/demobank\nSESSION_SECRET=%s\n' "$(openssl rand -hex 32)" > .env
cd ../frontend
npm ci                                # installs React, Vite, etc. from package-lock.json
```
The `.env` file tells the backend how to reach the database. The password `demobank_local` is the
local-only demo password from `database/compose.yaml`.
</details>

## Part C: Start DemoBank (every time)

1. Make sure **Docker Desktop is open** (Engine running).
2. `Cmd+Shift+P` → **Tasks: Run Task** → **DemoBank: 2. start**.

VS Code opens terminal tabs for the database, backend and frontend. It's ready when you see
`Application startup complete` (backend) and `Local: http://localhost:5173/` (frontend).
The first database start downloads the Postgres image (~100 MB), so give it a minute.

<details><summary>Or start the three parts by hand (good for learning)</summary>

Open three terminals in VS Code (click **+** in the terminal panel for each):

**Terminal 1 (database):**
```bash
cd test-website/database
docker compose up -d --wait
```

**Terminal 2 (backend):**
```bash
cd test-website/backend
source .venv/bin/activate
set -a; source .env; set +a          # loads DATABASE_URL and SESSION_SECRET
uvicorn demobank.main:app --host 0.0.0.0 --port 8000 --reload
```

**Terminal 3 (frontend):**
```bash
cd test-website/frontend
npm run dev
```
</details>

### Check it works
- http://localhost:8000/health → shows `{"status":"ok"}`
- http://localhost:5173 → the DemoBank page with an **Enter Demo** button. Click it and you should see
  *Everyday Checking $4,369.75* and *Rainy Day Savings $8,275.00*.

Always type **`localhost`**, not `127.0.0.1`: the backend only accepts the site from `http://localhost:5173`.

## Part D: Run the QA agent against it

Leave DemoBank running. Then either:
- **Run and Debug** panel (`Cmd+Shift+D`) → **QA agent: full run (watch browser)** → **F5** → press Enter to accept the default `http://localhost:5173`, or
- open another terminal:
  ```bash
  cd agent
  source .venv/bin/activate
  python -m qa_agent --url http://localhost:5173 --prd ../test-website/PRD.md --headed
  ```
  The terminal should say `API discovered at http://localhost:8000 (verified)`. If it shows a WARNING instead,
  add `--api-url http://localhost:8000` to the command.

(Haven't set up the agent yet? Run the task **Agent: one-time setup**, then put your Groq key in `agent/.env`.)

## Part E: Reset, stop, restart

- **Reset the data** to the original balances and 12 transactions (do this before a "clean" run, e.g. for
  your video): Tasks: Run Task → **DemoBank: reset data to the original seed**.
- **Stop the backend/frontend:** click the trash-can icon on their terminal tabs (or `Ctrl+C` in each).
- **Stop the database:** Tasks: Run Task → **DemoBank: stop database**. Your data is kept for next time.
- **Next day:** open Docker Desktop → Tasks: Run Task → **DemoBank: 2. start**. No setup needed again.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `docker: command not found` or `Cannot connect to the Docker daemon` | Open Docker Desktop and wait for **Engine running**, then retry |
| `port is already allocated` / `address already in use :5432` | Another Postgres is running on your Mac. Quit Postgres.app, or run `brew services stop postgresql`, then retry |
| Backend: `Missing required environment variable(s): DATABASE_URL` | The `.env` wasn't loaded. Run `set -a; source .env; set +a` in the backend terminal, or rerun the setup task |
| Backend: `connection refused` / `could not connect to server` | The database isn't up yet. Run the **DemoBank: database** task, wait, then start the backend again |
| Backend: `password authentication failed` | An old database volume has other data. In `test-website/database` run `docker compose down -v`, then start again (this wipes local DemoBank data only) |
| `npm ci` errors / `Unsupported engine` | Node is too old. Install the LTS from nodejs.org, open a new terminal, rerun setup |
| Site loads but **Enter Demo** fails / "Could not reach the DemoBank service" | Backend isn't running, or you used `127.0.0.1`. Use `http://localhost:5173` and check http://localhost:8000/health |
| `python3 -m venv` fails or Python < 3.11 | Install Python from python.org, open a new terminal, rerun setup |
| Port 5173 already in use | An old frontend is still running. Close its terminal tab (trash icon) and start again |

If you're stuck, copy the exact error from the terminal and send it to me.
