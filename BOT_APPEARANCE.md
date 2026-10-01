# Bondom Account — Custom Emoji & Appearance

This extends the existing aiogram 3 bot and authenticated Reflex admin panel.
FastAPI, the bot and admin still share the existing SQLAlchemy service layer.
Bakong KHQR, wallet arithmetic, inventory allocation, SMS and Pandora purchasing
remain in their existing services. No dependencies were added or upgraded.

## Add your first custom emoji

1. Sign in to the existing web admin panel.
2. Open **Bot Appearance → Custom Emoji**.
3. Choose **money** (or another slot).
4. Paste a numeric Telegram custom emoji ID into **Custom emoji ID**.
5. Keep **Enable this slot** and the global animation switch enabled.
6. Click **Save Changes**. The bot validates the ID with Telegram before saving.
7. Enter **your own numeric Telegram ID** in the preview field. Start the bot
   in Telegram first, then click **Send Preview**.
8. Open Add Balance in Telegram. Settings refresh within five seconds; already
   sent messages and keyboards update when you reopen their screen.

If you don't have the ID, use the optional Telegram admin editor:

1. Add your numeric Telegram ID to `.env`, for example
   `TELEGRAM_ADMIN_IDS=[123456789]`. Replace the example with your real ID.
   Existing web admin authentication remains unchanged.
2. Restart the bot, then send `/admin` in your private chat with it.
3. Open **Bot Appearance → Custom Emoji → money → Set / change ID**.
4. Send or forward a message with exactly one custom emoji (or its custom emoji
   sticker). The editor reads Telegram's official entities/sticker metadata,
   validates the ID, and saves it. Multiple different emojis require you to
   choose a single ID.
5. Alternatively, reply to a custom emoji message with `/emoji_id` to display
   its ID, then paste it into the web panel. Forwarded captions work too.

Plain Unicode emoji have no custom ID. Protected content that cannot be
forwarded must be sent directly. Only explicitly configured Telegram admin IDs
can use `/admin`, `/emoji_id` and appearance callbacks. The default list is empty.
The web admin controls work without configuring Telegram admin IDs.

Optional support contact: set `SUPPORT_USERNAME=your_support_username` in `.env`
(without `@`) and restart. Without it, Support directs customers to the store
administrator. No contact address is invented.

## Storage and migration

No schema migration, table reset, or bulk data update is needed. This uses the
existing `app_settings(key, value)` table:

- `bot_animated_emoji_enabled`: `true` or `false`, default `true` when absent.
- `bot_emoji:money` (and the other slots): JSON containing `key`,
  `custom_emoji_id` (a string), `fallback_emoji`, `enabled`, and `updated_at`
  (UTC ISO timestamp).

The 17 slots are `home`, `shop`, `product`, `cart`, `money`, `balance`, `orders`,
`success`, `warning`, `delivery`, `account`, `support`, `fire`, `gift`, `premium`,
`back`, and `refresh`. Unconfigured slots use built-in Unicode defaults and
require no database write. Settings are written only when an admin saves or
toggles a control. Restarting reloads the same durable values.

Telegram requires a custom emoji entity to wrap its corresponding Unicode
emoji. ID validation obtains that emoji from `getCustomEmojiStickers` and stores
it as the slot's fallback. **Reset to Default** clears the ID and restores the
original default. Disabling a slot or disabling animations globally retains IDs.

## Rendering and compatibility

- `shared/bot_appearance.py` owns defaults, validation and database access.
- `app/bot/appearance.py` owns `emoji("money")`, official button factories,
  a five-second settings cache, per-update context and transport fallback.
- HTML rendering uses `<tg-emoji emoji-id="ID">💰</tg-emoji>`. Dynamic customer,
  product, credential and announcement text is escaped.
- Installed library during implementation: **aiogram 3.29.1**. It supports
  `icon_custom_emoji_id` and `style` on inline and reply buttons. Inline purchase
  actions use Telegram's official `primary` and `success` styles, with `danger`
  for cancellation. Earlier libraries lacking these fields use Unicode labels.
- Missing/disabled IDs or invalid persisted configuration use Unicode. A
  Telegram rejection of emoji/style enhancements retries the same message or
  caption once with those enhancements removed. Callback data and payment
  actions are never replayed. Unrelated API errors propagate normally.
- Old reply keyboard labels remain accepted. Custom-icon buttons display plain
  action text to avoid duplicate icons. Existing inline callback data is retained;
  new delivery actions use `ui:account` and `ui:products`.
- Main menu, catalog, details, wallet, checkout, orders, account, support,
  purchase status, delivery and admin-published promotions use the shared layer.
  Callback pop-up alerts remain Unicode because they do not support HTML
  custom emoji entities. Existing SMS provider flows are retained.
- Configuration and emoji rejection logs are rate-limited and contain only an
  error category, never message bodies, credentials or tokens.

Custom emoji can be used in directly sent private/group/supergroup messages
when the bot owner has Telegram Premium (as confirmed for this bot by its owner).
Animation playback also depends on each customer's Telegram client/settings.
References: [Telegram Bot API](https://core.telegram.org/bots/api#html-style),
[official button fields](https://core.telegram.org/bots/api#inlinekeyboardbutton),
[aiogram documentation](https://docs.aiogram.dev/en/latest/api/types/inline_keyboard_button.html).

## Validation and startup

Run from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m compileall -q app/bot shared/bot_appearance.py app/web/admin scripts/check_bot_startup.py
.\.venv\Scripts\python.exe scripts/check_bot_startup.py
```

The preflight checks database access, configured slots, dispatcher construction,
live Telegram token authentication and webhook state. It does not send messages,
poll/consume updates, change the webhook or run payments. Network access is
required. The automated suite exercises the real aiogram dispatcher and polling
startup/shutdown using an isolated transport and throwaway database; purchases
and KHQR payment approval are simulated. Live animated rendering is verified
with **Send Preview** after you assign your own valid ID.

Implementation verification: **84 tests passed** (57 existing and 27 appearance/
bot tests), Python compilation and diff whitespace checks passed, and the
production Reflex frontend export succeeded. The read-only live preflight
authenticated the configured bot, loaded all 17 slots, and confirmed no webhook
blocks polling. No real payment or production polling session was started.

The automatic-delivery regression test exposed a pre-existing transaction issue:
the supplier lookup opened a transaction for local orders and the delivered
status could roll back when the session closed. Both manual payment-check and
automatic-delivery handlers now commit that status before sending credentials.
This small fix preserves the intended delivery behavior and prevents repeated
delivery after a restart. Purchasing/payment calculations are unchanged.

Use the existing startup commands (`main.py`, `run_all.py`, or the worker).
Do not start a second production polling instance while the VPS is running.

## Admin frontend deployment

The existing `deploy/admin-frontend.zip` has been rebuilt using the project's
pinned Reflex 0.9.6.post1, retaining the existing production admin backend URL
`https://admin.skshopping.store`. Deploy it using the existing procedures in
`LOCAL_TESTING.md` and `deploy/VPS_DEPLOY.md`, and restart the matching backend.
No VPS deployment or service restart was performed during implementation.

To rebuild it yourself, run from `app/web` with the deployment API URL set:

```powershell
$env:REFLEX_API_URL = 'https://admin.skshopping.store'
..\..\.venv\Scripts\python.exe -m reflex export --frontend-only --zip-dest-dir ..\..\deploy
```

The exporter produces `frontend.zip`; replace `deploy/admin-frontend.zip` with
that generated archive after verifying it uses your production backend URL.

## Changed files

- `.env.example` — optional Telegram admin IDs and support contact settings.
- `shared/config.py` — parses those settings using the existing config system.
- `shared/bot_appearance.py` — centralized, durable slot configuration.
- `app/bot/appearance.py` — renderer, button helpers, cache and safe retry.
- `app/bot/admin_appearance.py` — authorized Telegram editor and ID extraction.
- `app/bot/runner.py` — middleware, admin router, shared Telegram client setup.
- `app/bot/handlers.py` — storefront appearance, account/support screens and delivery commit fix.
- `app/web/admin/admin.py` — authenticated appearance tab and promotion formatting.
- `shared/pandora.py` — delivery notification client uses the shared renderer/HTML setup.
- `tests/test_bot_appearance.py` — persistence, rendering, routing, admin, purchase and startup tests.
- `scripts/check_bot_startup.py` — read-only live startup preflight.
- `deploy/admin-frontend.zip` — updated production admin frontend.
- `BOT_APPEARANCE.md` — this guide.
