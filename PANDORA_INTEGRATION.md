# Pandora Digital integration

Pandora is an additive supplier beside the existing local-inventory flow.
Customer-facing products remain normal `products` rows; `supplier_products`
holds supplier originals, stock, pricing rules, and the permanent Pandora ID.
Existing users, wallets, products, inventory, orders, and payments are not
altered or replaced.

## Configuration

Set these only in the server-side `.env`:

```dotenv
PANDORA_API_KEY=sk_live_...
PANDORA_API_BASE_URL=https://api.pandoradigital.shop/api/v1
PANDORA_SYNC_ENABLED=true
PANDORA_SYNC_INTERVAL_MINUTES=15
PANDORA_AUTO_FULFILLMENT_ENABLED=false
PANDORA_WEBHOOK_SECRET=the-one-time-secret-returned-by-PUT-webhook
```

Keep automatic fulfillment disabled through catalog/pricing verification and a
zero-cost supplier test. The admin's Pandora tab can then enable it durably.

Configure Pandora's endpoint as:

`https://skshopping.store/webhooks/pandora`

The route is accepted by the existing catch-all nginx proxy while the legacy
internal API remains blocked. It verifies the exact raw body, five-minute
timestamp window, every `v1=` signature, and persistent `Webhook-Id` dedupe.

## Safe deployment

Run each command separately in the VPS console:

```bash
/home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/scripts/backup_db.py
```

```bash
cd /home/ubuntu/bondom_account && git pull
```

```bash
cd /home/ubuntu/bondom_account && /home/ubuntu/bondom_account/.venv/bin/python scripts/migrate_pandora.py
```

```bash
sudo systemctl restart bondom
```

Because the admin source changed, build the Reflex frontend locally with the
documented `REFLEX_API_URL`, replace `deploy/admin-frontend.zip`, then deploy
that archive and restart the backend as described in `deploy/VPS_DEPLOY.md`.

Verify the API connection and run **Sync all products** in the Pandora tab.
Review imported names, local categories, retail prices, minimum profit blocks,
and stock before enabling sales or automatic fulfillment.

## Rollback

Disable `PANDORA_SYNC_ENABLED` and `PANDORA_AUTO_FULFILLMENT_ENABLED`, restart
the app, and disable imported products in the admin panel. Do not drop supplier
tables: they contain the durable idempotency keys, paid-order state, webhook
dedupe records, and delivered-order mapping needed for reconciliation. Revert
application code only after all paid supplier orders are terminal and the
pre-deploy database backup has been verified.
