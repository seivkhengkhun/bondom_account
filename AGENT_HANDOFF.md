# Bondom Account - Agent Handoff

## Update 2026-07-05 (payment root cause found)

- Real Bakong payments were verified WORKING end-to-end from the local
  machine (Cambodia IP, token renewed 2026-07-05 07:50 UTC): payments #1-3
  and #5 in local `store.db` are `paid`/`delivered`, and direct
  `check_transaction_by_md5` calls return `responseCode: 0` with full
  transaction data. App logic, merchant account, and token are all fine.
- Root cause of "unpaid" on the VPS: the `bakong-khqr` library maps EVERY
  nonzero responseCode — including 401 "token expired" and 403 "IP not in
  Cambodia" — to the string `UNPAID`, so a stale token in the VPS `.env`
  (or a geo-blocked VPS IP) looks identical to a customer who hasn't paid.
- Fix shipped: `shared/payment_service.py` now calls the Bakong endpoint
  directly, logs `http/responseCode/errorCode/message` for every check, and
  raises `PaymentError` on 401/403/errorCode 6 instead of returning UNPAID.
  Bot handlers show "⚠️ Payment system error" for those, keeping
  "not detected yet" only for genuine errorCode 1 (transaction not found).
- `scripts/vps_payment_diagnose.py` prints a one-line VERDICT on the VPS
  (token expiry decode, server IP country, DB rows, raw API responses).
- `deploy/VPS_DEPLOY.md` + `deploy/bondom.service` contain the exact
  deployment and systemd steps (unit file is copied from the repo, no more
  paste corruption).
- Remaining manual steps (need VPS shell — no SSH key/host exists on this
  Windows machine): `git pull`, paste the fresh `BAKONG_TOKEN` into VPS
  `.env`, run the diagnostic, test a $0.01 payment, then install systemd.

## Current Situation
- Project runs as one process: FastAPI + Telegram bot in one Python runtime.
- Local testing works for app startup and bot startup.
- VPS deployment is partially working:
	- API is reachable at `/docs`.
	- Telegram bot receives commands and order flow starts.
	- Payment verification is still returning "not detected yet" in bot check flow.
- User also struggled with terminal/pager exits on VPS (less/nano/tail foreground hangs).

## Important Commits Already Pushed
- `c98473c` - Log Telegram bot startup failures.
- `087b513` - Start Telegram bot in app lifespan.
- `117cd51` - Avoid double-importing app on startup.
- `51cc214` - Track bot payment tasks and improve payment logs.

## Key Code Changes Made

### 1) Startup/lifespan fixes
- File: `main.py`
	- Wrapped existing API lifespan with a combined lifespan that also starts Telegram polling.
	- Added bot task crash logging callback.
	- Changed `uvicorn.run("main:app", ...)` to `uvicorn.run(app, ...)` under `if __name__ == "__main__"`.
	- Reason: avoid double import and aiogram "Router is already attached" crash.

### 2) Bot runner robustness
- File: `app/bot/runner.py`
	- Added `delete_webhook()` before polling.
	- Added exception logging around polling.

### 3) Payment flow diagnostics/reliability
- File: `shared/payment_service.py`
	- Added explicit log line of Bakong check result (`PAID`/`UNPAID`).
- File: `app/bot/handlers.py`
	- Added tracked background task set for payment/top-up watchers.
	- Added done-callback logging for task failures/cancellations.

## What Has Been Verified
- Local compile/import smoke checks pass for:
	- `main.py`
	- `run_all.py`
	- `app/bot/runner.py`
	- `app/bot/handlers.py`
	- `app/api/main.py`
	- `shared/payment_service.py`
- Local payment service logic works in `PAYMENT_DEV_MODE=true`.
- On VPS, token validity for Telegram was confirmed via `getMe` earlier.

## Known Problems Still Open

### A) Real Bakong payment check still fails
- Symptom from bot UI: "Payment not detected yet — give it a few seconds and try again."
- Logs showed endpoints returning:
	- `POST /payments/{id}/check` -> `402 Payment Required`
	- `POST /payments/create` -> `400 Bad Request` (likely malformed API request body from manual docs testing)
	- `GET /payments/create` -> `405 Method Not Allowed` (expected, endpoint is POST-only)
- This means app is running but Bakong check for that md5 is still returning unpaid or not matching paid transaction yet.

### B) VPS command usage confusion
- User repeatedly ran `python3 main.py` instead of venv python and got `ModuleNotFoundError`.
- Correct command is always absolute venv python path on VPS.

### C) systemd unit setup remains unreliable
- Service file got malformed multiple times because multiline command paste collapsed into one line.
- Runtime currently often done via `nohup` as workaround.

## What the Next Agent Should Do (Priority Order)

1. **Stabilize runtime command on VPS**
	 - Use only:
	 - `/home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py`
	 - Then background mode:
	 - `nohup /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py >/tmp/bondom.log 2>&1 &`

2. **Collect authoritative payment debug evidence on VPS**
	 - Query latest payments/orders rows from `store.db`.
	 - For latest payment md5, run Bakong library checks directly:
		 - `check_payment(md5)`
		 - `get_payment(md5)`
	 - Confirm `.env` has expected `PAYMENT_DEV_MODE`, `BAKONG_TOKEN`, `BAKONG_ACCOUNT_ID`.

3. **Differentiate two flows clearly**
	 - Bot flow (`chk:` callback) using latest payment of that order.
	 - Manual docs API calls (`/payments/create` with JSON body) which user may be invoking incorrectly.

4. **If payment is still unpaid while user claims transfer done**
	 - Investigate merchant account/token validity at Bakong side.
	 - Validate payer actually paid exact QR of same `md5` and amount before expiry.
	 - Consider polling interval or verification retries UX messaging improvements.

5. **Only after payment is confirmed stable, fix systemd cleanly**
	 - Recreate `/etc/systemd/system/bondom.service` line-by-line safely.
	 - `daemon-reload`, `enable --now`, verify `active (running)`.

## Commands Frequently Needed

### Quick app restart (VPS)
1. `cd ~/bondom_account`
2. `pkill -f '/home/ubuntu/bondom_account/main.py'`
3. `nohup /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py >/tmp/bondom.log 2>&1 &`
4. `tail -n 120 /tmp/bondom.log`

### Terminal escape (when stuck)
1. `q` (exit pager)
2. `Ctrl+C`
3. `reset`

## Repo State Notes
- Working tree likely still has unrelated local changes such as `store.db` and `.vscode/`.
- Do not commit those unless explicitly requested.

## Security Note
- Sensitive tokens were exposed in chat/screenshots previously.
- Recommend rotating `BOT_TOKEN` and `BAKONG_TOKEN` after stabilization.
