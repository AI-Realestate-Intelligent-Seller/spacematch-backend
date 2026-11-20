# SpaceMatch Organic Reels MVP v0.4 — Free public counters with Playwright

**Platforms:** Instagram Reels · Facebook Reels · YouTube Shorts/videos · TikTok

**Python FastAPI + Vanilla JS + Postgres (Supabase) / SQLite + Playwright + Apify + YouTube Data API**

Reads the **public** counters (views, likes, comments, shares) of property videos, with no social login.
Each platform uses the source that works reliably from a cloud server:

| Platform | Source | Used when | Fallback |
|---|---|---|---|
| **Instagram** | **Apify** Instagram Reel Scraper (runs on Apify's residential proxies) | `APIFY_TOKEN` is set | Playwright browser |
| **YouTube** | **YouTube Data API v3** (official, free quota) | `YOUTUBE_API_KEY` is set | Playwright browser |
| **Facebook** | Playwright browser | always | — |
| **TikTok** | Playwright browser | always | — |

Why: Instagram and YouTube hide data from logged-out visitors on data-centre IPs (Render, any VPS), so the
browser alone returns missing Instagram views and blocked YouTube reads there. Facebook and TikTok work from Render.
You can **watch browser reads live** inside the app, or in a real Chromium window.

## Deployment

- **Live (Render):** https://space-match-zxji.onrender.com
  - Dashboard: the Next.js frontend (`spacematch-frontend`); this backend serves the API only.
  - Hub API: `https://space-match-zxji.onrender.com/v1` (health check: `/v1/health`)
  - API reference: https://space-match-zxji.onrender.com/api/docs
- On Render, set `BASE_URL=https://space-match-zxji.onrender.com` so emailed links (invites, password resets) point there.
- Postman: import `postman/SpaceMatch-Reels-API.postman_collection.json` with the **SpaceMatch Reels - Render** environment (its `baseUrl` is already set to the URL above).

### Vercel (backend for the Next.js frontend)

The frontend (https://spacematch-frontend.vercel.app) proxies `/api/*` to this backend, so the browser only ever
talks to the frontend's domain: the session cookie is first-party and no CORS setup is needed.

- Import the `spacematch-backend` repo as a new Vercel project (framework: **Other** / auto-detected FastAPI).
  Vercel serves `app/main.py` as a serverless function - no `vercel.json` needed.
- **Dependencies come from `pyproject.toml`**, not `requirements.txt`: Vercel reads `pyproject.toml` first.
  Add every new package to **both** files (the Dockerfile uses `requirements.txt`); `tests/test_vercel.py` fails if
  they differ.
- **Environment variables** (Project Settings → Environment Variables; redeploy after any change):

  | Variable | |
  |---|---|
  | `BASE_URL` | **Required here:** `https://spacematch-frontend.vercel.app` - the app people open. Used for links in emails (invites, password resets) and secure cookies. Without it, it defaults to *this* project's URL and emailed links open the old built-in pages. |
  | `DATABASE_URL` | Required. Supabase *Session pooler* string. The disk is read-only and temporary, so SQLite can't be used. |
  | `SESSION_SECRET` | Required. A long random string. Changing it logs everyone out. |
  | `TOKEN_ENCRYPTION_KEY` | Required. When sharing the database with another deployment, use the **same** key, or stored tokens can't be decrypted. |
  | `SMTP_*`, `APIFY_TOKEN`, `YOUTUBE_API_KEY` | As in `.env.example`. |

- Then in the **frontend** Vercel project set `BACKEND_URL` = this project's production URL (e.g.
  `https://spacematch-backend.vercel.app`) and **redeploy the frontend** - the proxy target is fixed at build time.
- Check: `https://<backend>/health` → `{"ok": true, "environment": "server"}`; then sign up on the frontend.
- **Rate limits** use the visitor's IP from Vercel's `X-Forwarded-For` (`app/core/security.py: client_ip`).
- **What differs from a normal server:**
  - The refresh worker and daily schedule are **off** (serverless functions can't run background threads).
    Run them on one other server, or schedule `scripts/sync_all.py`.
  - The Playwright browser reads (Facebook, TikTok, and the Instagram/YouTube fallbacks) need Chromium, which
    Vercel doesn't provide. Instagram through Apify and YouTube through the Data API work.
  - Dashboard **Fetch** runs in a background thread and its progress is kept in memory; on serverless the next
    poll can reach another instance ("Job not found"). Test it after deploying; if it's unreliable, run fetches
    from a non-serverless backend (Render / Oracle VM).
- **Troubleshooting** (Vercel → Project → Logs):

  | Error | Cause and fix |
  |---|---|
  | Every request shows `FUNCTION_INVOCATION_FAILED` | The app crashed on start-up - the log has the real error. |
  | `ModuleNotFoundError: No module named 'fastapi'` | `pyproject.toml` has no `dependencies` list, so Vercel installed nothing. |
  | `Set DATABASE_URL ...` / `Set SESSION_SECRET ...` | That environment variable is missing. Add it and redeploy. |
  | Frontend login works but you're logged out on every page | `BASE_URL` is `https://…` but the frontend is opened over `http://`, or `SESSION_SECRET` changed. |
  | Emailed links open the backend's own pages | `BASE_URL` isn't set to the frontend URL. |

## Accounts and access (v0.5)

| Role | How they join | Can do |
|---|---|---|
| **Agent** | Signs up at `/signup` | Create, edit and delete their properties; add social links (reels); fetch; export; create API keys for the Hub; invite and manage brokers |
| **Broker** | Invited by an agent on `/brokers`, sets a password from the email link (`/invite/<token>`) | *(Kept for later.)* Only properties shared with them, at the level the agent chose: **View only** (see and export) or **Full access** (also add and remove social links and fetch counters) |

- Each user sees only their own data. Social links belong to the property, so everyone with access to it sees the same links.
- **One account per email:** signing up with an email that already has an account (in any letter case) is refused with 409 "log in instead".
- Passwords are hashed with scrypt. Sessions use signed cookies with CSRF protection. Failed logins are rate limited (10 per 15 minutes).
- **Forgot password:** "Forgot password?" on the login page (`/forgot-password`) emails a reset link (`/reset-password/<token>`).
  - The link works **once, for 1 hour**; requesting a new one cancels the previous link.
  - The new password **must differ from the current one** (422 otherwise).
  - After a reset, every other session for that account is logged out, and the browser that reset it is logged in.
  - An email with no account gets 404 "No account found with this email address"; a broker who hasn't accepted their invite gets 409 (use the invite link). Requests are limited to 5 per 15 minutes per email and per IP.
  - Without SMTP settings, the reset email is printed to the server console.
  - API: `POST /api/auth/forgot-password` `{email}` · `GET /api/password-resets/{token}` · `POST /api/password-resets/{token}` `{password}`.
- Invite emails go out over SMTP (`SMTP_*` in `.env`). Without SMTP, the invite is printed to the server console and the agent gets a copyable link.
- Upgrading from v0.4: the **first agent who signs up** takes over the existing no-login data.
- Upgrading from v0.7: users with the old `owner` role become `agent` automatically when the app starts (SQLite and Postgres).
- **Recovery email (Account page, `/account`):** optional. Users add a second address they'll keep (e.g. a personal email)
  and confirm it with a 6-digit code sent to it. Changing it needs the current password and a new code (the old one keeps
  working until the new one is verified); removing it needs the current password. The login email can be changed the same way.
- **Recover account (`/recover`):** for people who can't get into their login email. Offered on the login page after
  5 failed attempts and always on the forgot-password page.
  1. Login email + recovery email → a code is emailed to the recovery email **only** if they match a verified pair.
     The answer is always the same, so nobody can discover an account's recovery email by guessing.
  2. The code → a 15-minute recovery session in that browser.
  3. A new password → every other session is logged out, alerts go to both addresses, and the user is logged in.
     They're then asked whether they can still reach their login email, and can switch it to a new one.
  - Codes: 6 digits, valid 10 minutes, 5 wrong tries, newest only, single use, stored as an HMAC.
    One code a minute and 5 an hour per account; recovery requests and wrong codes are rate-limited per email / IP.
  - Every change (recovery email added/changed/removed, login email changed, account recovered) emails an alert
    and is written to the `account_events` table.
  - API: `GET /api/account` · `POST /api/account/recovery-email` `{email, password}` · `POST /api/account/recovery-email/verify` `{code}` ·
    `POST /api/account/recovery-email/remove` `{password}` · `POST /api/account/email` `{email, password}` · `POST /api/account/email/verify` `{code}` ·
    `POST /api/account/{recovery-email|email}/resend|cancel` · `POST /api/auth/recover` `{email, recovery_email}` ·
    `POST /api/auth/recover/verify` `{email, code}` · `POST /api/auth/recover/password` `{password}`.
- **API only:** the pages (`/login`, `/properties`, `/reset-password/:token`, `/invite/:token`, …) live in the Next.js
  frontend, which proxies `/api/*` to this backend. Here those paths answer 404; `/` returns a short JSON pointer to `/api/docs`.
- Code: `app/api/auth.py` (sessions, sign-up, login, password reset, invites), `app/api/access.py` (permission rules), `app/api/brokers.py` (broker management), `app/core/security.py`, `app/core/mailer.py`.

## Hub API (v0.6, `/v1`)

The Hub server calls this API with an **API key** that the agent creates on the dashboard's **API keys** page. The key is sent as `Authorization: Bearer sk_live_…`, and it can only reach that agent's properties. The full reference is at `/api/docs` under "Hub API v1".

| Hub moment | Call |
|---|---|
| Add a property | `POST /v1/properties` with `{"address": "...", "links": [url, url, ...]}`. At least one link is required, from any platform, and several per platform are fine. One bad link returns 422 with nothing saved. New links are read right away. |
| Change links | `POST /v1/properties/{id}/links` `{"url"}` adds one; `PUT …/links` `{"links": [...]}` replaces them all; `DELETE …/links/{link_id}` removes one (the last link can't be removed). |
| Show numbers | `GET /v1/properties/{id}/analytics`, or `GET /v1/analytics?updated_since=…` for many properties |
| Refresh now | `POST /v1/properties/{id}/refresh` returns `202` with a refresh. Poll `GET /v1/refreshes/{id}`. Each property can be refreshed manually once per 10 minutes (`429` + `Retry-After`). |
| Sold / withdrawn | `POST /v1/properties/{id}/archive`, which stops daily refreshes |
| Why is a number old? | `GET /v1/properties/{id}/logs` (every attempt) · `GET /v1/properties/{id}/history` (every good read) |

- **A property is an address plus its links.** At least one link is required; several links for the same platform are allowed. Removed links stay in `/links/history`, and their numbers stay in history.
- **Failed reads keep the last good numbers.** `ok_at` is the last good read, `checked_at` the last attempt, and `error` explains the failure.
- **Background refreshes:** a worker inside the server processes refreshes one at a time. Every day at **07:00 America/Chicago** (following daylight saving) it queues every active property that has links. `REFRESH_WORKER=off` turns this off, and `python scripts/sync_all.py` runs the same daily refresh once.
- **Conventions:** errors look like `{"error": {"code", "message", "field"}}`. Paging uses `?limit=&cursor=` and returns `next_cursor`. `Idempotency-Key` works on create and refresh calls. Every response has `X-Request-Id`, and authenticated responses carry `X-RateLimit-*` headers. Times are UTC with `Z`, and unknown counts are `null`.
- Code: `app/api/api_v1.py` (endpoints), `app/api/apikeys.py` (keys and scopes), `app/services/refresh.py` (links, analytics, refresh queue, daily schedule, logs).

## Database (v0.7)

- If **`DATABASE_URL` is set**, the app uses Postgres (Supabase in production) through a psycopg 3 connection pool. If it isn't, it uses SQLite at `DB_PATH`, which suits local work.
- The schema is created and upgraded automatically on start, on both databases. On Postgres, **row level security is switched on for every table with no policies**, so Supabase's public Data API (the anon key) can't read anything. The app connects as the table owner and isn't affected.
- **Supabase connection string:** use the **Session pooler** (`postgres.<ref>@aws-1-<region>.pooler.supabase.com:5432`). The direct `db.<ref>.supabase.co` host is IPv6-only.
- **Moving existing data:** `python scripts/migrate_to_postgres.py --dry-run`, then `python scripts/migrate_to_postgres.py`. It copies SQLite at `DB_PATH` into `DATABASE_URL` and refuses to run if Postgres already holds data.
- **Tests** never touch `DATABASE_URL`. `pytest` uses temporary SQLite files. `TEST_DATABASE_URL=postgresql://… pytest` runs the same suite against a throwaway Postgres and wipes its `public` schema before every test, so never point it at real data. Example: `docker run -d --rm -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=testpass postgres:17-alpine`.

## What's new in v0.4

| | v0.3 | v0.4 |
|---|---|---|
| Public counters provider | Apify (paid token) | Playwright (free, local) |
| Instagram | Views/likes/comments (if token) | Views, likes, comments |
| Facebook | Manual only | Plays (views), reactions, comments, shares |
| Watch the automation | — | Live screenshot viewer + optional visible Chromium window |
| Bulk | — | **Fetch all public** per property, `scripts/sync_all.py` for daily cron |
| Login / OAuth / manual entry | Demo password, Meta OAuth, manual counts | Removed |
| Metrics shown | 10 (most N/A) | Views, likes, comments, shares |

Verified on Nov 11, 2026:

| Reel | Views | Likes | Comments | Shares |
|---|---|---|---|---|
| Instagram `spacematchchicago/reel/DTOz5t5jLQH` | 30,631 (matches Firecrawl's 30,630) | 408 | 9 | N/A |
| Facebook `share/r/1cHAH3Wqdg` (BMW) | 1,799,282 plays | 36,459 | 1,361 | 637 |
| YouTube Short `JX1mw5HWeTM` | 6 | 0 | N/A (comments turned off) | N/A |
| TikTok `vt.tiktok.com/ZSpaGf8Yp` | 118 plays | 4 | 0 | 1 |
| YouTube control video `dQw4w9WgXcQ` | 1,836,007,638 | 19,499,858 | 2,461,983 | N/A |

## How it gets the numbers

All reads (dashboard **Fetch**, **Fetch all**, Hub refreshes, the daily run, `scripts/sync_all.py`) go through
`playwright_scraper.scrape()`, which picks the source per platform. Every read prints one terminal line:
`INFO reel = <id>, date + timestamp: <UTC>, playcount = …, like_count = …, comments_count = …`.

**Instagram via Apify** (`app/sources/apify_source.py`, used when `APIFY_TOKEN` is set)
- Runs the `apify~instagram-reel-scraper` actor for the reel URL (`resultsLimit: 1`) and maps
  `videoPlayCount` → views, `likesCount` → likes, `commentsCount` → comments. Shares aren't public (null).
- About 15–20 s per reel. Each read costs one Apify result (the free plan includes $5 credit a month).
- Apify itself failing (invalid token, out of credit, timeout) → falls back to the browser; the live log says
  "Apify unavailable (…) - falling back to the browser". A reel Apify can't find → read fails as private or removed.
- Analytics show `source: "apify"`.
- Setup: create an account at apify.com → **Settings → API & Integrations** → copy the token → `APIFY_TOKEN`.

**YouTube via the YouTube Data API v3** (`app/sources/youtube_api.py`, used when `YOUTUBE_API_KEY` is set)
- `GET https://www.googleapis.com/youtube/v3/videos?part=statistics&id=<video id>` (the key goes in the
  `X-goog-api-key` header, never in the URL). Maps `viewCount`, `likeCount`, `commentCount`; hidden likes or
  disabled comments → null. YouTube has no public share count.
- Under 1 s per video; 1 quota unit of the free 10,000 a day.
- Quota or network errors → falls back to the browser. A missing video → read fails as private or removed.
- Analytics show `source: "youtube_api"`.
- Setup: Google Cloud Console → new project → **APIs & Services → Library** → enable **YouTube Data API v3** →
  **Credentials → Create credentials → API key** → restrict it to YouTube Data API v3 → `YOUTUBE_API_KEY`.

Browser methods (the fallback for Instagram and YouTube, the main source for Facebook and TikTok):

**Instagram** (`playwright_scraper.scrape_instagram`)
1. Opens the reel page → likes, comments, and the owner's username. (Instagram hides views from logged-out visitors here.)
2. Opens `instagram.com/<owner>/reels/` and scrolls until the reel's tile loads. The grid's network data
   carries the exact `play_count`. Older reels need more scrolls (the test reel took 15, ~35 s).
   Fallback: the rounded number printed on the tile (e.g. "30.6K").

**Facebook** (`playwright_scraper.scrape_facebook`)
1. Opens the reel/share link → resolves the numeric video id, reactions, comments, shares.
2. Opens `facebook.com/watch/?v=<id>` → reads `play_count` (the number shown on the reel, used as "views")
   and `video_view_count` (Facebook's stricter view metric, saved in the source note).
   Counts are only read next to this video's id, so suggested videos on the page don't leak in.

**YouTube** (`scrape_youtube`) — Shorts, watch, youtu.be links
1. Opens `youtube.com/watch?v=<id>` → `viewCount` and `likeCount` from the player data tied to the video id.
2. Scrolls to load the comments header → comment count. Comments turned off → N/A. YouTube has no public share count.

**TikTok** (`scrape_tiktok`) — full video links and `vt./vm.tiktok.com` share links
1. Opens the link (share links redirect) → page data `itemStruct.stats`: plays, likes, comments, shares (saves go in the source note).
2. Share links get their video preview saved after the first fetch.

Saved snapshots are marked `playwright_public` so they're never confused with Meta API insights.
Private metrics (reach, saves, watch time…) aren't available from public pages, so they aren't shown.

## Quick start

Python 3.11+.

```bash
cd space-match/backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env                 # Windows: Copy-Item .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API is at `http://127.0.0.1:8000` (reference: `/api/docs`). For the dashboard, run the frontend
(`spacematch-frontend`, `npm run dev`) and open `http://localhost:3000`, with `BASE_URL=http://localhost:3000` in `.env`.

1. Pick or add a property.
2. Paste a public Instagram or Facebook Reel URL (username-style Instagram links and Facebook share links work) → **Add Reel**.
3. Click **↻ Fetch** on a reel, or **▶ Fetch all** for the whole property.
4. The **Browser automation** panel opens with live frames and a step log. Closing it doesn't stop the job.
5. Tick **Show browser window** before fetching to also watch it in a real Chromium window
   (only on the computer running the server; disabled automatically when there's no screen).

## Daily refresh

```bash
python scripts/sync_all.py   # public counters for every linked Reel
```

Waits 5 s between reels. Schedule it once a day with cron / Task Scheduler.

## Settings (`.env`)

- `APIFY_TOKEN`: Instagram through Apify (empty = browser only). Optional: `APIFY_REEL_ACTOR`
  (default `apify~instagram-reel-scraper`), `APIFY_TIMEOUT_S` (default 240).
- `YOUTUBE_API_KEY`: YouTube through the official Data API (empty = browser only).
- Set both on Render as well (**Environment** → add variable → save → deploy). Never commit them.

- `PLAYWRIGHT_MAX_SCROLLS` (default 80): how far to scroll an Instagram Reels tab before giving up on an old reel.
- `SEED_DEMO_DATA`: adds two demo property addresses on first run (no fake metrics).

## Limits and caveats

- This reads public pages the way a logged-out visitor sees them. Instagram/Facebook may change their pages
  (the scraper then returns N/A rather than inventing numbers) or rate-limit heavy use. Keep it to about once a day.
- Only one browser job runs at a time; extra jobs wait in line.
- Very old Instagram reels on accounts with hundreds of reels may need a higher `PLAYWRIGHT_MAX_SCROLLS`.
- Instagram "views" and Facebook "plays" are different measurements; don't treat either as unique reach.
- Review Meta's terms for your intended commercial use.
- Single-user local tool with no login — never expose it to the internet.

## Project layout

```
app/
  main.py            FastAPI app: dashboard API (/api), Hub API (/v1), startup
  api/               HTTP routers and request guards
    auth.py            sessions, sign-up, login, password reset
    recovery.py        recovery email, login email change, account recovery
    access.py          permission rules (who can see which property/reel)
    brokers.py         broker management and invites
    apikeys.py         Hub API keys, scopes, rate limit
    api_v1.py          Hub API /v1 (links, analytics, refreshes, logs)
    api_v2.py          Hub API /v2 (profile scans)
  services/          business logic
    refresh.py         refresh queue, background worker, daily schedule
    scans.py           profile scans
    urls.py            Reel URL parsing and allowlist
  sources/           where metrics come from
    playwright_scraper.py  Instagram/Facebook/YouTube browser scrapers, live-frame jobs
    apify_source.py        Instagram via Apify
    youtube_api.py         YouTube Data API v3
  db/storage.py      SQLite / Postgres storage
  core/              settings (.env), password/token hashing, SMTP mailer
scripts/             one-off commands: sync_all.py (daily cron), migrate_to_postgres.py
tests/               pytest suite (browser mocked) - run `python -m pytest -q`
postman/             Postman collection and environments
docs/                extra setup notes
```
