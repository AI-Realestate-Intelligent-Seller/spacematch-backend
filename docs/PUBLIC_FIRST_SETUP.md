# SpaceMatch Hub - Public Reels MVP Setup & Usage

**v0.4 - November 11, 2026** — see README.md for the free Playwright public counters.

## What you can do today (without Meta developer credentials)

1. Download and extract the public-first ZIP.
2. Install **Python 3.11+** if necessary.
3. Open a terminal in the `spacematch-reels-mvp` folder.
4. Create a virtual environment and install requirements (commands below).
5. Copy `.env.example` to `.env`.
6. Run the app and open `http://127.0.0.1:8000`.
7. (No sign-in needed.)
8. Choose a property (or add one), paste a public Reel URL, and click **Add Reel**.
9. See the Reel embedded when Meta permits it. You can also open the original URL.
10. Click **Fetch** (or **Fetch all**) to read public counters automatically and watch the browser live. Export your property report as CSV.

**Mac/Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Windows PowerShell**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

If PowerShell blocks script activation, try `.venv\Scripts\python.exe -m pip install -r requirements.txt` and then `.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000` without activating.

## Important restrictions

- Public Instagram/Facebook Reels can be linked without connecting any account.
- **Fetch Public** reads views, likes, comments and shares the way a logged-out visitor sees them, using a local browser.
- Private metrics (reach, saves, watch time, skip rate, replays) aren't available from public pages and aren't shown.
- There is no login and no Meta OAuth in v0.4. Run it only on your own computer (`127.0.0.1`).
