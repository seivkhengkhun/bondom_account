# VPS deployment — payment fix + systemd

Run these on the VPS as `ubuntu`, one block at a time.

## 1. Pull the fix

```bash
cd /home/ubuntu/bondom_account
git pull
```

## 2. Update the Bakong token in .env

The token was renewed on 2026-07-05 (07:50 UTC). The VPS `.env` most
likely still holds the OLD token — the Bakong API answers 401 for it, and
the old library code silently reported that as "UNPAID".

Open `.env` and make sure `BAKONG_TOKEN=` is exactly the current token
(same value as the working local machine's `.env`), and:

```
PAYMENT_DEV_MODE=false
BAKONG_ACCOUNT_ID=khun_seivkheng@bkrt
```

## 3. Run the diagnostic (proof of root cause)

```bash
/home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/scripts/vps_payment_diagnose.py
```

Read the `VERDICT` line:

- `TOKEN REJECTED` → step 2 wasn't done; paste the fresh token again.
- `IP BLOCKED (HTTP 403)` → the VPS is outside Cambodia and Bakong
  refuses it. Payment checks must then run from a Cambodia IP (move the
  VPS to a Cambodian provider, or route Bakong calls through a proxy in
  Cambodia).
- `Transaction not found` → token + IP fine; that md5 simply has no
  payment. Do a fresh end-to-end bot test (step 4).
- `Bakong says PAID` → everything works; retest the bot.

## 4. Restart and test end-to-end

```bash
pkill -f '/home/ubuntu/bondom_account/main.py' || true
nohup /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/main.py >/tmp/bondom.log 2>&1 &
sleep 3
tail -n 40 /tmp/bondom.log
```

Then in Telegram: buy a $0.01 test product, pay the QR, press
"✅ I've paid — check". Watch the log:

```bash
grep "Bakong check" /tmp/bondom.log | tail -n 5
```

Every check now logs `http=... responseCode=... errorCode=... message=...`.
If the token or IP is the problem, the bot now shows
"⚠️ Payment system error on our side" instead of the misleading
"not detected yet", and the log shows the exact reason.

## 5. Only after payment works: systemd

The unit file is in the repo (`deploy/bondom.service`) so no more
copy-paste corruption — install it with `cp`:

```bash
sudo cp /home/ubuntu/bondom_account/deploy/bondom.service /etc/systemd/system/bondom.service
sudo touch /var/log/bondom.log && sudo chown ubuntu:ubuntu /var/log/bondom.log
pkill -f '/home/ubuntu/bondom_account/main.py' || true
sudo systemctl daemon-reload
sudo systemctl enable --now bondom
systemctl status bondom --no-pager
```

Follow logs with `tail -f /var/log/bondom.log` (Ctrl+C to exit — never
`less` on the VPS).
