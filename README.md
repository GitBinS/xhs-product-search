# XHS Product Search

Collect product details from Xiaohongshu (小红书) product pages into structured JSON — works on **desktop and mobile** Obsidian.

[中文说明见 README.zh-CN.md](README.zh-CN.md)

## What it does

Give it a product link (or a share text / product ID), and it returns a normalized record:

- Title, current price, final price (after coupon), shipping origin
- Shop name, shop rating, follower count
- Sales (with explicit lower-bound / per-SKU caveats when applicable)
- Listing date / days-on-shelf (when the page exposes it)

It works **standalone** (ribbon icon or command palette) and exposes a small API (`checkDeps()` / `collect()` / `mode()` / `setMode()`) so other plugins can use it as a data backend — e.g. the author's goods-trading workbench plugin.

## How it works

- The collector is **built in**. It calls two **public** Xiaohongshu endpoints with Obsidian's cross-platform `requestUrl` (which bypasses CORS on both desktop and mobile):
  - Product detail: `mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc`
  - Shop page: `www.xiaohongshu.com/shop/<sellerId>` (server-rendered, product JSON embedded)
- **No Python, no external script, no `pip install`.** **No login state, no cookies, no account data.**
- Requests to `xiaohongshu.com` are made **only when you run a collection**, from your device.
- Results are returned to you inside Obsidian; the plugin does no telemetry and writes nowhere else.

## Requirements

- Obsidian 1.4.0+ — desktop (Windows / macOS / Linux) **or mobile (iOS / Android)**
- Nothing else. No Python, no Node, no external tools.

## Usage

1. Install and enable the plugin.
2. Click the shopping-bag ribbon icon (or run “采集一个商品” from the command palette).
3. Paste a product link / share text / product ID.
4. The result modal shows the collected fields and any caveats (e.g. “sales figure is a lower bound”).
5. Click **“保存为 Markdown 笔记”** to save the result as a Markdown note in your vault (folder configurable), or “复制为 JSON” to copy it.

**Standalone vs. workbench**: on its own, use “保存为 Markdown 笔记” to write a `.md` into the vault (same idea as the note-collector plugin). When used with the author's goods-trading workbench, **the confirm dialog decides which multi-dimensional tables get the data** — this plugin does not duplicate that.

Commands:

- **采集一个商品（链接或商品 ID）** — collect one product
- **采集器自检** — check that the built-in engine is ready

> Short share links (`xhslink.com`) are auto-expanded on desktop. On mobile, paste the full product link or the raw product ID instead.

## Notes & limitations

- Fields that only exist in the Xiaohongshu mobile app (e.g. shop opening date) are reported as empty rather than guessed.
- Public pages only; the collector intentionally has no login capability.
- Sales numbers are shown exactly as the page states them, including their known caveats.

## License

[MIT](LICENSE)
