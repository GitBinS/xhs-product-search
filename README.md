# XHS Product Search

Collect data from Xiaohongshu (小红书) **product and shop** pages into structured JSON — works on **desktop and mobile** Obsidian.

[中文说明见 README.zh-CN.md](README.zh-CN.md)

## What it does

**A product link** (or a share text / product ID) → a normalized product record:

- Title, price, final price (after coupon), shipping origin, shipping fee, dispatch time
- Shop name, shop rating, shop total sales, follower count, shop link
- Listing date / days on shelf (only when the page exposes it)
- SKU count, selected SKU, product badges, sales

**A shop link** → shop info + the product list visible on the shop homepage (shop name, total sales, followers, positive-review rate, dispatch time; each product with title, price, sales, listing date/age, badges).

Sales figures are kept exactly as the page states them: if the page only shows a lower bound ("1.5万+") or a per-SKU figure, the record keeps the raw text and a scope marker instead of pretending it is exact.

It works **standalone** (ribbon icon or command palette) and exposes a small API (`checkDeps()` / `collect()` / `mode()` / `setMode()`) so other plugins can use it as a data backend — e.g. the author's goods-trading workbench plugin.

## Save as Markdown note

When used standalone (no workbench), click **“保存为 Markdown 笔记”** after collecting:

- Data goes into the note's **frontmatter (properties)** — visible in Obsidian and queryable;
- The body holds only: title, product link, and a **“每日跟踪（变动）”** table (date / sales);
- **Re-collecting the same product updates the existing note** (refreshes properties + updates that day's row, appends a new row on another day) — no duplicate notes;
- The save folder is configurable (default `小红书商品采集`).

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
3. Paste a product link / share text / product ID (a **shop homepage link** collects the shop instead).
4. The result modal shows the collected fields.
5. Click **“保存为 Markdown 笔记”** to save the result as a Markdown note in your vault (folder configurable), or “复制为 JSON” to copy it.

**Standalone vs. workbench**: on its own, use “保存为 Markdown 笔记” to write a `.md` into the vault (same idea as the note-collector plugin). When used with the author's goods-trading workbench, **the confirm dialog decides which multi-dimensional tables get the data** — this plugin does not duplicate that.

Commands:

- **采集一个商品（链接或商品 ID）** — collect one product
- **采集器自检** — check that the built-in engine is ready

> Short share links (`xhslink.com`) are auto-expanded on desktop. On mobile, paste the full product link or the raw product ID instead.

## Notes & limitations

- Fields that only exist in the Xiaohongshu mobile app (e.g. shop opening date) are reported as empty rather than guessed.
- Shop collection returns only the products visible on the **shop homepage**, not the full catalogue.
- Public pages only; the collector intentionally has no login capability.
- Sales numbers are shown exactly as the page states them, together with their raw text and scope marker.

## License

[MIT](LICENSE)
