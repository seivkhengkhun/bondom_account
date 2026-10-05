# Bondom V2 storefront redesign: Phase 1 audit and plan

Status: **audit complete, implementation not started.** Baseline test run:
`84 passed` (`.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`).

Scope: the customer website at skshopping.store only. The Telegram bot,
Reflex admin panel, `shared/` services, the database and the JSON API are
out of scope unless noted below.

---

## 1. Architecture as it actually is

| Layer | Implementation |
|---|---|
| Rendering | Server-side Jinja2 templates via a FastAPI router (`app/webshop/routes.py`). No SPA and no JS framework. |
| Styling | One stylesheet, `app/webshop/static/shop.css` (2,381 lines, "Vault" system: warm neutrals with an orchid/rose gradient, light and dark themes). It is cache-busted by `?v={{ asset_v }}`, which comes from the file's mtime and size. |
| Icons | An inline SVG sprite in `base.html` (`#i-*` symbols). |
| JS | Vanilla inline `<script>` per page: theme toggle, mobile nav, reveal-on-scroll, card glow, copy, toast, catalog filter, stepper, payment polling. |
| Auth | Telegram Login Widget → `/auth/telegram` → HMAC check against the bot token → stateless signed cookie `bondom_session` (30 days). Every POST form also carries an HMAC `csrf_token` bound to the session. |
| Data | `routes.py` calls only `shared/services.py`, `payment_service`, `sms_service` and `pandora`. Templates receive ORM objects and `ProductOverview` tuples. |
| Build | None. The VPS CPU has no AVX, so Node and Bun crash there. **Any build step added to the storefront would break deploys.** |

**Decision: keep this architecture.** The redesign needs templates, CSS and
small vanilla JS, not a framework migration. That keeps performance high and
deploys as simple as `git pull`.

### Routes (customer-facing)

| Route | Template | Notes |
|---|---|---|
| `GET /` | `home.html` | Hero, client-side search, category pills, and the whole catalog in one grid |
| `GET /p/{id}` | `product.html` | Detail page with a buy panel (quantity stepper → `POST /web/buy`) |
| `POST /web/buy` | none | Creates the order, allocates stock and creates the KHQR session, then redirects to `/pay/{id}` |
| `GET /pay/{id}` | `pay.html` + `_khqr_card.html` | QR, expiry bar, and polling of `GET /web/status/{id}` every 4 seconds |
| `GET /order/{id}` | `order.html` | Delivered credentials with copy buttons, plus paid, pending and canceled states |
| `GET /my/orders` | `orders.html` | Last 20 orders and the wallet balance |
| `GET /wallet`, `POST /web/wallet/topup`, `GET /wallet/pay/{id}` | `wallet*.html` | KHQR top-up and polling of `/web/wallet/status/{id}` |
| `/sms`, `/sms/{id}`, `/my/sms`, `POST /web/sms/buy` | `sms*.html` | Only when `SMS_ENABLED`; paid from the wallet |
| `/developer`, `/developer/docs` | `developer*.html` | API keys and API docs |
| `/legal/*` | `legal/*.html` | Seven policy pages |
| `/auth/telegram`, `/logout` | none | Session cookie |
| errors | `error.html` | Branded HTML for browsers, JSON for API clients |

### What the data can actually support

The `Product` model has these fields: `name`, `price`, `category` (free
text), `warranty_days` and `is_active`. `ProductOverview` adds `available`,
`description` (Pandora products only), `source`, and Pandora's
`minimum_quantity`/`maximum_quantity`. AppSettings add a per-product client
note, a per-product minimum quantity and a global "show stock" toggle.

The data does **not** include:
- **Images.** There is no image field. Cards currently show a monogram letter.
- **Variants.** Each SKU is its own product, so "Agency Netflix" exists three times.
- **Featured or promotion flags.** "Promotions" in the admin is a Telegram broadcast, not storefront content.
- **A cart.** An order is one product × quantity, paid by one KHQR. Wallet payment for products exists only in the bot.
- **Ratings, reviews or customer counts.** None exist, so none will be shown.

The local `store.db` holds 10 active products in the categories `accounts` and
`streaming`. Production categories will differ, so nothing in the design may
hard-code category names.

### The animated Telegram emoji system

It is **bot-only**. `shared/bot_appearance.py` stores 17 slots (`home`,
`shop`, `money`, `orders`, `success`, and so on) in `app_settings` as a
Telegram `custom_emoji_id` plus a Unicode fallback. The bot renders them as
`<tg-emoji>` entities. The website renders none of this today.

Browsers cannot render a `custom_emoji_id` directly. Showing these emoji on
the web would require a new read-only backend route. It would resolve the
slot ID through `getCustomEmojiStickers`, fetch the file through `getFile`,
cache it on disk and serve it, because the bot token cannot be exposed to the
browser. Animated stickers come as TGS (Lottie) or WebM. The fallback is the
slot's stored Unicode emoji, which is already available with zero backend
work. **Decision needed (see §6).**

---

## 2. Functionality the redesign must not change

These contracts are treated as frozen. The redesign changes markup and
styles around them, never the contracts themselves.

- **Form contracts:**
  - `POST /web/buy` takes `product_id`, `quantity` and `csrf_token`.
  - `POST /web/wallet/topup` takes `amount` and `csrf_token`.
  - `POST /web/sms/buy` takes `category`, `country` and `csrf_token`.
- **Polling contracts:**
  - `/web/status/{id}` returns `{status}` with values `pending`, `paid`, `delivered` or `canceled`.
  - `/web/wallet/status/{id}` and `/web/sms/{id}/status` keep their existing JSON shapes.
- **The Telegram widget embed:** `data-telegram-login`, `data-auth-url` and `data-request-access="write"`. The widget is a fixed-size iframe and can only be positioned, not restyled.
- **The KHQR card** stays light-only on purpose (it imitates a bank slip, and the QR needs a white background to scan).
- **Ownership checks** (`_owned_order`, `_owned_sms_order`), CSRF, the secure cookie flags and the payment throttling messages.
- **Server-side limits stay authoritative.** The top-up min/max, the minimum order quantity and Pandora's min/max are enforced on the server. Client-side validation is only a convenience.
- **Wallet notice content.** The "don't top up more than you need, no refunds for unused balance" wording is policy text. It must stay visible, but the scrolling marquee can go.
- **The `?error=` and `?success=` query-param message pattern** that the POST handlers redirect with.
- **The `asset_v` cache-busting.** Any new static file needs the same treatment.

### Deployment trap

`deploy/shop-nginx.conf` returns **404 for any path starting with
`/products`, `/orders`, `/users`, `/payments` or `/docs`**, because those
paths belong to the internal JSON API. A new "Products" page therefore
**cannot** live at `/products`. It will use `/shop` (or the `/#catalog`
anchor). It will work locally and 404 in production if this is missed.

---

## 3. Design problems in the current storefront

**Identity**
1. There are three competing brand identities: the CSS is orchid/rose, the README documents indigo `#6366f1`, and the requested spec is green. The orchid→rose gradient, gradient headline text, ambient page wash and pointer-follow card glow are exactly the "generated" look the brief rules out.
2. The light/dark theme toggle splits design effort. The V2 palette is dark-only.

**Untrue or unverifiable claims (must go)**

3. The hero trust row says "Warranty included", but most products have `warranty_days = 0`.
4. "Delivered in seconds" and "Delivered automatically the moment payment confirms" are false for Pandora supplier orders. Those go paid → fulfillment → delivered, and can land in `review`.
5. "Buying takes under a minute" is unmeasured.
6. The signed-in hero shows a Products / In stock / Categories stat strip. It is dashboard filler, not something a buyer needs.

**Hierarchy and density**

7. "How it works" appears three times: the hero panel, the band under the catalog, and the wallet page.
8. Product cards repeat the category chip on every card, even when that category is already filtered. The monogram letter carries no information. The card says "Buy" but only opens the detail page. Duplicate product names (three "Agency Netflix") can't be told apart at card level.
9. Search exists only inside the catalog section. The header has none, and search and category aren't reflected in the URL, so a filtered view can't be linked or bookmarked.
10. The category pills use `role="tab"` without tab panels, which is ARIA misuse. A pressed-button group or links are correct.

**Flows**

11. **Bug:** the product-page stepper ignores Pandora `minimum_quantity`/`maximum_quantity`. A Pandora product with a minimum of 5 starts at 1, and the server then rejects the order. This is fixable in the frontend with values the template already has.
12. My Orders shows "Order #123" with no product name or quantity. A buyer can't tell orders apart.
13. Signing in from a product page always lands back on `/`, which loses context. The handler supports `?next=`, but `next` is included in the HMAC check, so it can never work. Fixing this needs a small, safe auth change (see §6).
14. Wallet payment for products is bot-only. The web checkout is QR-only. **This stays as is** because it is a backend capability, not UI.

**Code quality**

15. There are about 190 inline `style=""` attributes across the templates (68 in `developer_docs.html` alone). One-off styling is everywhere.
16. Reveal-on-scroll is applied to every card and list row. That is scroll-animation overload.

**Responsive**

17. Mobile navigation is a hamburger menu only. Search, categories and account sit behind it or far down the page. There is no persistent mobile search.
18. The signed-out sign-in panel takes the hero slot on mobile and pushes products below the fold.

---

## 4. Risks

| Risk | Mitigation |
|---|---|
| **There are no webshop route tests.** All 84 tests are service-level. | Before any template work, add `TestClient` tests that render every page signed-out and signed-in, and assert form field names, CSRF presence, polling element IDs and JSON shapes. These are the regression net. |
| Inline JS depends on element IDs (`#status`, `#expiry-fill`, `#qty`, `#total`, `#buy-form`, and others) | Move page JS into one `shop.js` with `data-*` hooks. Cover the IDs in the tests above. |
| The local `.env` holds the **production** bot token | Never run `main.py` locally. For visual QA, run `uvicorn app.api.main:app` (no bot polling) through a scratch launcher that stubs the bot username, and never submit purchase or top-up forms against it. |
| The SMS, developer and legal pages share `base.html` and `shop.css` | Restyle them in the same pass (mostly by inheritance) so they don't break. |
| Web fonts | There is no CSP today. Either self-host a variable font in `static/` or use the system stack. Do not add a third-party font CDN (privacy, and Cambodia latency). |
| Khmer text in product notes | Keep the Noto Sans Khmer fallback in the font stack. |

---

## 5. Redesign plan

### Phase 2: design system (`shop.css` rewrite, dark-only)
- Tokens from the brief: canvas `#0A0D0B`, surface `#101411`, elevated `#161B17`, green `#22C55E`/`#15803D`/`#86EFAC`, text `#F3F1E8`/`#9CA39C`, border `#272D29`. Lime `#D9FF57` is reserved for one job: the price/primary action focus moment.
- Green means **action and live state**, not decoration. Surfaces are neutral, separation comes from hairline borders and spacing, and there is no glow.
- Type: one variable sans for UI and tabular numerals for prices. Radius goes 6 / 10 / 14px, and the pill shape is reserved for chips.
- Components: button, input, search field, chip/segmented filter, badge, product card, buy panel, stepper, status line, dialog/sheet, toast, empty state and skeleton. All page JS moves into `static/shop.js`.

### Phase 3: storefront
- **Header:** wordmark, Shop, SMS Numbers (only if enabled), and Developers in the footer only. A search box that submits to `/shop?q=`. Account state shows the Telegram name, a balance chip and a menu (Orders, Wallet, SMS orders, Sign out). There is no cart icon because no cart exists.
- **Hero:** compact (no taller than about 40vh on desktop) with one real line about what Bondom sells, a large search field, and quick links built from the **real categories** in the database. The sign-in widget moves out of the hero into the header and the buy panel.
- **Categories:** a horizontal link row on desktop, and a sticky, horizontally scrollable chip rail on mobile. Each item links to `/shop?cat=` so views can be shared.
- **Promotions:** a reusable `promo_banner` macro. Its data source is a decision (§6).
- **Product grid:** dense cards showing the name, a disambiguating line (category · warranty or Pandora min-qty), price, stock state and a real primary action.
- **`/shop`:** a new **read-only GET route** that reuses `_catalog()` and filters server-side by `q` and `cat`. It also gets progressive client-side filtering. No service changes.
- **Trust strip:** only true statements. KHQR works with every Cambodian bank app, and items are mirrored to the Telegram chat. "Instant" is shown per product for local stock only.

### Phase 4: customer flows
- **Product page:** fix the Pandora min/max quantity. Make the buy panel sticky on desktop and a bottom action bar on mobile. Delivery copy follows the product's source (instant vs supplier-fulfilled).
- **Pay:** the same polling logic, with a clearer status and expiry, and a "what happens next" panel.
- **Order:** a stronger delivered state with copy-all. Paid, review, pending and canceled states get distinct, honest messages.
- **My Orders:** show the product name and quantity. This needs one extra read-only query in the route (or eager-loading in the route). The existing service is unchanged.
- **Wallet and SMS:** restyle. The scrolling marquee becomes a static notice with the same wording.

### Phase 5: responsive and visual QA
- Use Chrome at 1440, 1280, 1024, 768, 430, 390 and 375px. Check for horizontal overflow, 44px touch targets, the widget iframe fitting at 375px, and the mobile bottom bar not covering forms.
- A mobile bottom nav (Shop · Search · Orders · Account) is **only worth it if** testing shows the header menu is the bottleneck. The default is a sticky header with search and no bottom nav.

### Phase 6: regression
- The full pytest suite plus the new webshop route tests.
- A manual signed-in flow on a local test DB with `PAYMENT_DEV_MODE=true` and a **test** bot token only.
- Rebuild nothing in the admin unless a §6 option requires it.

---

## 6. Decisions (answered 2026-10-04)

| # | Decision | Answer |
|---|---|---|
| 2 | Promotions source | **Admin-curated slots** in `app_settings` (no schema change), a Reflex admin tab and an `admin-frontend.zip` rebuild |
| 3 | Animated emoji on the web | **Real animated stickers** through a read-only, cached proxy route, with the Unicode fallback |
| 4 | Light theme | **Dark-only.** The theme toggle is removed on purpose. |
| 5 | `?next=` login fix | **Yes.** Exclude `next` from the hash fields and allow same-site relative paths only. |
| 1 | Reference image | **Still outstanding** |

The original options are kept below for the record.

1. **The reference image.** The "Bondom V2 reference image" was not attached. A file path or the image itself is needed.
2. **Promotions data source.**
   - (a) Derive banners from real data with no backend change, for example one banner per category.
   - (b) Admin-curated featured slots stored in the existing `app_settings` key/value table (no schema change). This needs a small admin tab in Reflex and a rebuild of `deploy/admin-frontend.zip`.
3. **Animated emoji on the web.**
   - (a) A read-only `/web/emoji/{slot}` proxy that serves the configured sticker files (WebM/WEBP, cached), with the Unicode fallback. This needs a new backend route.
   - (b) The slot's Unicode fallback only, with no backend change.
4. **The light theme.** Drop it (dark-only, per the V2 palette) or keep it.
5. **The `?next=` login fix.** Excluding `next` from the HMAC field set is a small auth change and needs a relative-path check to avoid an open redirect. Approve it or leave it out.

---

## 7. Implementation status (2026-10-04)

**Tests:** `144 passed`. That is the 84 existing tests plus 60 new ones in
`tests/test_webshop.py`. The new tests pin:
- form fields, CSRF and ownership checks
- the polling JSON shapes and element IDs
- the login return path, including attack inputs
- `/shop`, promotions and the emoji route and its cache
- the Pandora quantity bounds, order lines and the admin promotion editor

### Backend additions

No schema changes. No existing contracts changed.

| Change | Files |
|---|---|
| `/shop` catalogue (search, category, sort in the URL; read-only) | `app/webshop/routes.py`, `templates/shop.html` |
| Login returns to the page you were on (`next` excluded from the hash; same-site paths only) | `app/webshop/auth.py`, `routes.py` |
| Admin-curated promotion slots in `app_settings` | `shared/storefront_promos.py`, `app/web/admin/admin.py` (Marketing tab), `shared/audit.py` |
| Animated emoji route with a disk cache and failure back-off | `app/webshop/emoji.py`, `app/api/main.py`, `.gitignore` |
| Order lines for My orders, pay and order pages (read-only) | `shared/services.py::order_lines` |
| Product stepper honours the Pandora min/max and the `/web/buy` cap | `routes.py::_quantity_bounds` |
| Header wallet balance (best-effort; never blocks a page) | `routes.py::_header_balance` |

### Frontend changes

- `shop.css` was rewritten as a dark-only design system. The KHQR card styles were kept verbatim.
- `shop.js` is new.
- `_ui.html` macros are new.
- These templates were rebuilt: base, home, shop, product, pay, order, orders, wallet, wallet_pay and error.
- The SMS, developer and legal pages were kept and restyle through the shared classes.

### Removed on purpose

- **Light theme and its toggle.** This was decision #4.
- **The scrolling wallet marquee.** Its notice wording is unchanged, now as static text.
- **The repeated "How it works" blocks** on the home and wallet pages.
- **The signed-in stat strip.**
- **Claims the data does not support:** "Warranty included" on every product, "Delivered in seconds", and "Buying takes under a minute".

### Visual QA

QA ran in Chrome on an isolated preview: a copy of the database, a fake bot token, dev payments, and no lifespan tasks.
- Checked at 1440, 1280, 1024, 768, 660, 430, 390 and 375px.
- No horizontal overflow on any checked page.
- No sub-32px tap targets, apart from the developer page's native checkboxes, which were then enlarged.
- One `<h1>` per page and no skipped heading levels.
- The full dev-mode purchase went through: stepper, buy, pay page, auto-confirm and the delivered page.

### Follow-up (approved and done)

- **Lottie player:** lottie-web 5.13.0 `lottie_light.min.js` is vendored locally from the npm registry tarball. Its sha512 integrity was verified, and the MIT license is kept at `static/vendor/lottie-web.LICENSE.md`.
- **Manrope:** the variable TTF and its OFL license are vendored from google/fonts at commit `b31870a`, with git blob hashes verified. The Khmer fallback stack is unchanged.
- **Provenance:** sources, versions and SHA-256 hashes are recorded in `static/vendor/README.md`.
- **Font MIME types:** `.ttf` and `.woff2` are now registered explicitly, because Windows Python has no mapping for them.
- **Legal copy:** `legal/cookies.html` and `legal/privacy.html` now describe the dark-only site and no longer claim a stored theme preference. The effective date is set to 4 October 2026, so adjust it to the actual deploy date.
- **Tests:** 150 pass. Browser verification covered Lottie rendering, the emoji fallback, local-only fonts, the console, the network, the responsive matrix and admin promotions.

### Deploy

- `git pull`, then restart `main.py` as usual.
- Unzip the rebuilt `deploy/admin-frontend.zip`, then restart the admin backend.
- Make sure the app user can write to `.cache/` in the project directory.
