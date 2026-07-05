# Bondom Account — Unified Platform

One Python codebase, three faces: a **FastAPI** backend, an **aiogram 3**
Telegram bot, and a **Reflex** admin panel — all reading and writing the
same PostgreSQL database through one shared service layer.

## Architecture

```
Bondom Account/
├── shared/                  ← SOURCE OF TRUTH (imported by everything)
│   ├── config.py            Settings (.env) shared by all components
│   ├── database.py          Async engine, AsyncSessionLocal, Base, get_db
│   ├── models.py            User, Product, Inventory, Order, Payment
│   ├── schemas.py           Pydantic request/response models
│   ├── services.py          Business logic + transactions (atomic stock allocation)
│   └── payment_service.py   Bakong KHQR: QR generation, verification, polling
├── app/
│   ├── api/                 ← FastAPI routes (thin HTTP layer over services)
│   │   └── main.py
│   ├── bot/                 ← aiogram 3.x Telegram bot
│   │   ├── handlers.py      /start, /products, buy + payment callbacks
│   │   └── runner.py        Bot/Dispatcher bootstrap
│   └── web/                 ← Reflex admin panel (pure Python, no HTML/CSS/JS)
│       ├── rxconfig.py
│       └── admin/admin.py   Products/Orders/Users tables, bulk upload, user toggle
├── run_all.py               Supervisor: DB init + API + bot (+ optional web)
├── requirements.txt
└── .env.example
```

**Rule: only `shared/` talks to the database schema.** The API, the bot
and the admin panel never write SQL or business rules — they call
`shared/services.py` / `shared/payment_service.py` with a session from
`shared/database.py`. That is what keeps three processes consistent over
one database.

## Running

```powershell
# 1. Setup
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env     # then fill in DATABASE_URL, BOT_TOKEN, ...

# 2. API + Telegram bot (one process, one event loop)
python run_all.py

# 3. Admin panel (separate process — Reflex manages its own servers)
cd app\web
reflex run                 # http://localhost:3000
#   ...or let the supervisor spawn it:  python run_all.py --with-web
```

## Payment flow (Bakong KHQR)

1. Buyer taps **Buy** in Telegram → `create_order_and_allocate_stock`
   reserves inventory atomically (`FOR UPDATE SKIP LOCKED`).
2. `payment_service.create_payment_session` generates the KHQR string,
   stores its **md5** in the `payments` table (15-min expiry).
3. Payment is confirmed either by the buyer tapping *"I've paid"* (bot),
   by `POST /payments/{order_id}/check`, or by the background poll loop
   started from `POST /payments/create`.
4. On confirmation the order flips to `paid`, the bot delivers the
   inventory `data`, and the order becomes `delivered`.

`PAYMENT_DEV_MODE=true` simulates QR + verification so the whole flow
works without a Bakong merchant account.

## Production notes

- Use **Alembic** for migrations (`init_db()`/`create_all` is dev-only).
- `Inventory.data` must get field-level encryption before real
  credentials are stored (see the NOTE in `shared/models.py`).
- The in-process payment poller dies with the process; for durability
  move it to Celery/arq using the same `verify_payment`/`confirm_payment`
  functions.
- Stock is reserved at order time; add a scheduled job that releases
  inventory from orders whose payments expired if you want strict
  anti-hoarding.
