# Vendored third-party files

Served locally from `/web/static/`; paths below are relative to `app/webshop/static/`. No CDN is used at runtime.

| File | Package | Version | Source | License | SHA-256 |
|---|---|---|---|---|---|
| `vendor/lottie_light.min.js` | lottie-web (`build/player/lottie_light.min.js`) | 5.13.0 | npm registry tarball `https://registry.npmjs.org/lottie-web/-/lottie-web-5.13.0.tgz` (sha512 integrity verified against the registry) | MIT, see `vendor/lottie-web.LICENSE.md` | `9588432bec30c8ef8200bac4a67d8aaad881047bc2a6c9fa624d90ec96402410` |
| `fonts/Manrope-Variable.ttf` | Manrope variable (wght 200–800), renamed from `Manrope[wght].ttf` | google/fonts @ `b31870aff700ab7a1d74fa0c6887d95beb9e0037` | `https://github.com/google/fonts/tree/b31870aff700ab7a1d74fa0c6887d95beb9e0037/ofl/manrope` (git blob hash verified) | SIL OFL 1.1, see `fonts/Manrope-OFL.txt` | `3ae11c49db0455a3cc33e37d380f20fdb8c7f8b41dc07625c177e3d87a9d6ae6` |

## Why these files

- **lottie_light** plays Telegram's animated (TGS/Lottie) custom emoji
  on the website. `shop.js` loads it only when a page actually contains
  a Lottie sticker, so other pages don't download it.
- **Manrope** is the storefront's UI typeface. It covers Latin, Cyrillic
  and Greek. Khmer text falls through to the next font in the stack
  (`Noto Sans Khmer`, then system fonts), which is unchanged.

## Updating

Download from the same sources, verify the hashes, replace the files,
and update this table. The `?v=` query on their URLs must change with
the version so browsers do not keep a stale copy.
