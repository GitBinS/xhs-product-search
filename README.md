# Xiaohongshu Goods Collector

Collect product details from Xiaohongshu (小红书) product pages into structured JSON — designed for desktop Obsidian.

[中文说明见 README.zh-CN.md](README.zh-CN.md)

## What it does

Give it a product link (or a share text / product ID), and it returns a normalized record:

- Title, current price, final price (after coupon), shipping origin
- Shop name, shop rating, follower count
- Sales (with explicit lower-bound / per-SKU caveats when applicable)
- Listing date / days-on-shelf (when the page exposes it)

It works **standalone** (ribbon icon or command palette) and exposes a small API (`checkDeps()` / `collect()` / `mode()` / `setMode()`) so other plugins can use it as a data backend — e.g. the author's goods-trading workbench plugin.

## How it works

- The collection logic lives in a bundled Python script (`vendor/xhs.py`, plain-text in the repo, base64-embedded in `main.js` and written out at first run so marketplace installs work).
- The script uses **only the Python standard library** — no `pip install` needed — and reads **public product pages** via plain HTTP requests. **No login state, no cookies, no account data** is involved.
- Requests to `xiaohongshu.com` are made **only when you run a collection**, from your machine.
- Python 3 must be available; the plugin auto-detects (`python` / `python3` / `py -3`), or set an explicit path in settings.
- Results are returned to you inside Obsidian; the plugin itself does not write anywhere else and does no telemetry.

## Requirements

- Obsidian 1.4.0+ on desktop (Windows / macOS / Linux)
- Python 3 (any recent version, standard library only)

## Usage

1. Install and enable the plugin.
2. Click the shopping-bag ribbon icon (or run "采集一个商品" from the command palette).
3. Paste a product link / share text / product ID.
4. The result modal shows the collected fields and any caveats (e.g. "sales figure is a lower bound").

Commands:

- **采集一个商品（链接或商品 ID）** — collect one product
- **采集器自检** — verify Python + script availability and show which was used

Settings:

- **项目目录** — optional directory containing your own `xhs.py`; leave empty to always use the bundled copy
- **Python 路径** — optional explicit Python executable; leave empty for auto-detection

## Notes & limitations

- Fields that only exist in the Xiaohongshu mobile app (e.g. shop opening date) are reported as empty rather than guessed.
- Public pages only; the collector intentionally has no login capability.
- Sales numbers are shown exactly as the page states them, including their known caveats.

## License

[MIT](LICENSE)
