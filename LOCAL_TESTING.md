# Testing locally before deploying to the VPS

The local machine (Windows) and the VPS are fully independent: separate
`.env`, separate `store.db`. Nothing you do locally can touch production
data — test freely.

## One-time setup

1. **Create a TEST bot**: message @BotFather → `/newbot` → e.g.
   "Bondom Test" / `bondom_test_bot`. Copy its token.
2. In the **local** `.env` (never the VPS one) set:
   ```
   BOT_TOKEN=<the TEST bot token>
   PAYMENT_DEV_MODE=true
   ```
   - `PAYMENT_DEV_MODE=true` simulates Bakong: every payment
     auto-approves after a few seconds, so you can test the full buy →
     deliver flow without sending real money.
   - Set it back to `false` only if you specifically want to test real
     KHQR payments locally (works — this machine has a Cambodia IP).

⚠ NEVER put the production bot token in the local `.env` while the VPS
is running — Telegram allows only one process per bot, and your local
run would hijack the live bot from customers.

## Test the bot + API locally

```powershell
cd "c:\Users\Admin\Documents\Bondom Account"
& .venv\Scripts\python.exe main.py
```

- Talk to your TEST bot in Telegram — full flow: browse, buy, "I've
  paid — check" (auto-approves in dev mode), delivery, My Orders.
- API docs: http://127.0.0.1:8000/docs
- Stop with Ctrl+C.

## Test the admin panel locally

```powershell
cd "c:\Users\Admin\Documents\Bondom Account\app\web"
& ..\..\.venv\Scripts\python.exe -m reflex run
```

- Opens at http://localhost:3000 (first run builds for a few minutes —
  this machine can run Bun; the VPS cannot).
- Password = `ADMIN_PASSWORD` in the local `.env`.
- It edits the LOCAL store.db only.

## When it works locally → deploy to VPS

1. Commit + push (Claude usually does this part).
2. On the VPS, the standard procedure:
   ```bash
   /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/scripts/backup_db.py
   cd /home/ubuntu/bondom_account && git pull
   ```
3. If **bot/API** changed:
   ```bash
   pkill -9 -f main.py
   nohup /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py >/tmp/bondom.log 2>&1 &
   tail -n 5 /tmp/bondom.log
   ```
4. If the **admin panel** changed (needs the rebuilt
   `deploy/admin-frontend.zip`, exported on the local machine):
   ```bash
   sudo unzip -o deploy/admin-frontend.zip -d /var/www/bondom-admin
   pkill -f reflex
   cd /home/ubuntu/bondom_account/app/web && nohup /home/ubuntu/bondom_account/.venv/bin/python -m reflex run --backend-only --backend-host 127.0.0.1 >/tmp/bondom-admin.log 2>&1 &
   ```
5. Verify: test bot with a real $0.01 purchase, check
   https://admin.skshopping.store.
