# Bondom Account — Unified Digital Store Platform

A digital-goods store for the Cambodian market (accounts, credentials,
keys) selling through **three synchronized channels** over one database:

| Channel | Tech | Where |
|---|---|---|
| 🤖 Telegram bot | aiogram 3 | Production bot (token in `.env`) |
| 🌐 Customer website | FastAPI + Jinja2 | https://skshopping.store |
| 🛠 Admin dashboard | Reflex (static build + Python backend) | https://admin.skshopping.store |

Customers pay with **Bakong KHQR** (scannable by ABA, ACLEDA, Wing and
every Cambodian banking app) or a prepaid wallet. Delivery is automatic
and instant. Website users sign in with the **Telegram Login Widget**,
so the same account/wallet/orders work in the bot and on the site.

> **This README is the handoff document.** Read it first in a new
> session. Older context lives in `AGENT_HANDOFF.md` (payment-debugging
> era) and `LOCAL_TESTING.md` / `deploy/VPS_DEPLOY.md` (procedures).

---

## 1. Architecture — one source of truth

**Rule: only `shared/` touches the database.** Every channel is a thin
face over `shared/services.py` + `shared/payment_service.py`. There is
no sync layer because there is nothing to sync — all channels read and
write the same rows live.

```
Bondom Account/
├── shared/                   ← SOURCE OF TRUTH (imported by everything)
│   ├── config.py             Pydantic settings from .env (absolute path)
│   ├── database.py           Async engine, AsyncSessionLocal
│   ├── models.py             User, Product, Inventory, Order, Payment, WalletTopup
│   ├── schemas.py            Pydantic request/response models
│   ├── services.py           ALL business logic; atomic stock allocation
│   └── payment_service.py    Bakong KHQR: QR gen, raw verification, polling
├── app/
│   ├── api/main.py           FastAPI app: JSON API + mounts webshop
│   ├── bot/                  aiogram bot (handlers.py, runner.py)
│   ├── webshop/              Customer website (routes, auth, templates/, static/)
│   └── web/                  Reflex ADMIN panel (admin/admin.py, rxconfig.py)
├── deploy/                   nginx configs, systemd unit, VPS procedures
│   ├── VPS_DEPLOY.md         ★ Golden rules + update procedure
│   ├── admin-frontend.zip    Prebuilt admin frontend (built locally!)
│   ├── admin-nginx.conf      admin.skshopping.store (static + ws proxy)
│   ├── shop-nginx.conf       skshopping.store (proxy :8000, API blocked)
│   └── bondom.service        systemd unit (NOT yet installed)
├── scripts/
│   ├── backup_db.py          SQLite online backup, 14-copy rotation
│   └── vps_payment_diagnose.py  One-line Bakong root-cause verdict
├── main.py                   Entrypoint: uvicorn (API+web) + bot polling
├── run_all.py                Dev supervisor (--with-web adds admin panel)
├── LOCAL_TESTING.md          Test-locally-before-deploy workflow
└── AGENT_HANDOFF.md          Historical debugging notes (July 2026)
```

Key flows:
- **Purchase**: order created (inventory rows locked `FOR UPDATE SKIP
  LOCKED`) → KHQR payment session (md5 stored) → background watcher
  polls Bakong → confirm → mark delivered → items sent to Telegram chat
  *and* shown on web. Bot and web both spawn
  `_watch_payment_and_auto_deliver` from `app/bot/handlers.py`.
- **Web identity**: Telegram Login Widget → HMAC verified against bot
  token (`app/webshop/auth.py`) → same `users` row as the bot → session
  is a stateless signed cookie (no session table).
- **Per-product delivery notes** are `AppSetting` rows keyed
  `product_note:{id}`; delivery groups items per product so each carries
  its own note/warranty (bot message, order file, and web page).

## 2. Production environment (VPS)

| Item | Value |
|---|---|
| Host | DP Data Center "STARTUP PRO VPS", Ubuntu 24.04, **1 CPU / 2 GB RAM / 20 GB** |
| IP | 208.122.28.210 (Cambodia — required by the Bakong API) |
| Code | `/home/ubuntu/bondom_account` (git pull from GitHub `seivkhengkhun/bondom_account`) |
| Database | **`/home/ubuntu/data/store.db`** — OUTSIDE the repo, git can never touch it |
| Backups | `/home/ubuntu/backups/` — cron daily 03:00 + manual before each deploy |
| Main app | `nohup .venv/bin/python main.py` → port 8000 (API + website + bot) |
| Admin backend | `reflex run --backend-only --backend-host 127.0.0.1` → port 8001 |
| Admin frontend | Static files in `/var/www/bondom-admin` (from `deploy/admin-frontend.zip`) |
| nginx | sites `shop` (skshopping.store → :8000) and `admin` (admin.… → static + :8001) |
| TLS | certbot, auto-renews; both domains + www |
| Swap | 2 GB `/swapfile` (in fstab) — 2 GB RAM alone was not enough |

### ⚠ Hard constraints (learned the hard way)

1. **VPS CPU has NO AVX** → Bun/Node segfault (SIGILL). Reflex frontend
   builds must run on the local Windows machine
   (`REFLEX_API_URL=https://admin.skshopping.store reflex export
   --frontend-only` inside `app/web`), shipped via
   `deploy/admin-frontend.zip`. Never run Reflex dev mode on the VPS.
2. **`main.py` ignores SIGTERM** → always restart with
   `pkill -9 -f main.py` (plain pkill leaves a zombie holding port 8000).
3. **The only VPS access is the DP web console, which corrupts long
   pastes** (random uppercase, injected chars, collapsed newlines).
   Give the user commands ONE short line at a time; ship any multi-line
   file through the git repo, never through paste.
4. **Bakong quirks**: the `bakong-khqr` lib masks auth/geo errors as
   "UNPAID" — our `payment_service._raw_check_transaction` calls the API
   directly and raises `PaymentError` on 401/403/errorCode 6 instead
   (errorCode 1 = genuinely unpaid). BAKONG_TOKEN is a 90-day JWT —
   **expires 2026-10-03**; renew at https://api-bakong.nbc.gov.kh/register
   and update `.env` (VPS *and* local).

### Standard update procedure (NEVER skip the backup)

```bash
/home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/scripts/backup_db.py
cd /home/ubuntu/bondom_account && git pull

# if bot/API/website changed:
pkill -9 -f main.py
nohup /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py >/tmp/bondom.log 2>&1 &

# if the admin panel changed (zip must be rebuilt locally first):
sudo unzip -o deploy/admin-frontend.zip -d /var/www/bondom-admin
pkill -f reflex
cd /home/ubuntu/bondom_account/app/web && nohup /home/ubuntu/bondom_account/.venv/bin/python -m reflex run --backend-only --backend-host 127.0.0.1 >/tmp/bondom-admin.log 2>&1 &
```

## 3. Completed features

- [x] **Payments (real money verified in production)** — KHQR
  generation, raw Bakong verification with honest error classification,
  15-min expiry, poll watchers, wallet top-ups; real sales delivered.
- [x] **Telegram bot** — category → paginated list → product card
  catalog (navigation edits one message in place); KHQR + one-tap
  wallet purchase; My Orders + resend delivered items
  (ownership-checked); wallet top-up/balance; per-product delivery
  notes; blocked-user enforcement; bilingual EN/KM welcome.
- [x] **Customer website** (skshopping.store) — category filter pills,
  product pages with sticky buy panel + quantity stepper + live total,
  3-step checkout with QR status polling + animated expiry bar, success
  page with copy-to-clipboard credentials, My Orders, Telegram login
  (forged-hash rejection tested), signed-cookie sessions, cross-user
  ownership isolation (tested), internal API blocked by nginx on the
  public domain. Full flow covered by an automated 10-step local test.
- [x] **Admin dashboard** (admin.skshopping.store) — password login
  (`ADMIN_PASSWORD` in VPS `.env`) with server-side guards on every
  mutating handler; tabbed UI (Products/Orders/Users/Marketing) with
  KPI cards, search boxes, status badges; product add/manage
  (price/warranty/rename/delete); category management in both create
  and edit flows (pick existing or type new); per-product delivery
  notes (switching products reloads the note — 1 product = 1 note);
  bulk stock upload; user suspend/block + wallet credit/debit;
  Telegram broadcast announcements.
- [x] **Data safety** — DB moved outside the repo; `*.db` gitignored
  and untracked; `scripts/backup_db.py` + daily cron 03:00 (keeps 14);
  golden rules documented; survived a live feature deploy with a real
  paid order intact.
- [x] **Infra** — nginx + HTTPS (certbot) for both domains; 2 GB swap;
  payment diagnostic script; deploy + local-testing docs.

## 4. Pending / next steps (priority order)

1. **systemd auto-start** ⚠ — if the VPS reboots, bot + website + admin
   backend ALL stay down until manually restarted. `deploy/bondom.service`
   exists for the main app (install steps in `deploy/VPS_DEPLOY.md` §5);
   a second unit for the admin backend still needs writing. Use
   `KillSignal=SIGKILL` (see hard constraint #2). Keep the backup cron.
2. **Rotate leaked tokens** ⚠ — the real `BOT_TOKEN` and `BAKONG_TOKEN`
   are in the PUBLIC repo's git history (old `.env.example` commits),
   and the admin password appeared in chat screenshots. When stable:
   BotFather `/revoke`, renew the Bakong token, change
   `ADMIN_PASSWORD`, update `.env` on VPS + local. Consider making the
   repo private (VPS pulls unauthenticated today — needs a deploy token).
3. **Test bot for local dev** — create a second bot via BotFather and
   put its token in the LOCAL `.env` (see LOCAL_TESTING.md). Never run
   the production token locally while the VPS is live.
4. **Bakong token renewal** before **2026-10-03** or payments stop
   (they now fail loudly instead of silently, but still stop).
5. Nice-to-haves discussed, not built: wallet payment on the website
   (bot-only today), storefront search box, admin sales charts, Alembic
   migrations (schema changes are additive/create-only today),
   encryption-at-rest for `Inventory.data`, rate limiting, releasing
   reserved stock from long-expired pending orders via scheduled job.

## 5. Conventions

- **Code**: all business rules in `shared/services.py`; channels stay
  thin. Every mutating admin handler starts with
  `if not self.authed: return`. Bot callback prefixes: `buy:`, `wb1:`,
  `chk:`, `tchk:`, `rsnd:`, `pcat:`, `pview:`, `pcats`, `cancelpay:`,
  `buy_cancel`.
- **Storefront UI**: design tokens in `shop.css` `:root` (dark, indigo
  accent `#6366f1`, green prices, amber notes); inline SVG sprite in
  `base.html` (`#i-cart #i-bolt #i-shield #i-check #i-copy #i-tg #i-qr
  #i-package`); components: `.card`, `.btn(-primary/-outline/-ghost/
  -sm/-lg/-block)`, `.badge(.ok/.out/.warn/.warranty)`, `.chip`,
  `.pill`, `.note`, `.alert`, `.stepper`; focus-visible outlines and
  `prefers-reduced-motion` are respected — keep them.
- **Admin UI**: Reflex/Radix theme (indigo accent, slate gray, large
  radius); helpers `card_header(icon, title, subtitle)`,
  `section_message()` callouts, `search_box()`; tables
  `variant="surface"` inside `overflow_x=auto` boxes.
- **Bilingual**: customer-facing text is English, Khmer where provided
  (welcome message, product notes). Notes render with
  `white-space: pre-line` and a Noto Sans Khmer font fallback.
- **Git**: single `master` branch; pushing to GitHub IS the deployment
  channel (VPS pulls). Never commit `.env`, `*.db`, or real tokens.

## 6. Known issues & assumptions

- The FastAPI JSON API has **no authentication**. nginx blocks it on
  skshopping.store, but raw `IP:8000` is reachable. Acceptable for now;
  add auth before exposing it further.
- SQLite is fine at current volume; `shared/database.py` already
  handles Postgres URLs if scale demands a migration.
- In-process payment watchers die on restart; recovery paths exist
  (bot "I've paid — check" button, web page polls DB state), but a
  restart mid-payment delays auto-delivery until the customer prompts.
- `Inventory.data` (sold credentials) is stored in plaintext SQLite.
- Local `.env` currently holds the PRODUCTION bot token with
  `PAYMENT_DEV_MODE=false` — do NOT run `main.py` locally until a test
  token replaces it (Telegram allows one polling process per bot).
- DNS for skshopping.store was repointed from Hostinger hosting to the
  VPS on 2026-07-06; the local PC's Wi-Fi DNS was switched to 8.8.8.8
  to skip propagation lag.

## 7. Running locally

See **LOCAL_TESTING.md**. Short version: put a TEST bot token +
`PAYMENT_DEV_MODE=true` in the local `.env`, then
`& .venv\Scripts\python.exe main.py` → bot + API + website on
http://127.0.0.1:8000, and `reflex run` inside `app/web` → admin on
http://localhost:3000. The local `store.db` is isolated test data —
experiment freely.
