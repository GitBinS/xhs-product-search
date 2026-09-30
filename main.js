/* ══════════════════════════════════════════════════════════════════════════
   小红书商品搜索 · 独立插件
   ──────────────────────────────────────────────────────────────────────────
   定位（结构文档 §13.2）：**独立插件 + 带货工作台调用它**。

   🔑 v0.2.0（2026-09-30 老哥要求「只装插件就能用、手机也能采」）：
     原来采集逻辑在 `vendor/xhs.py`（Python，靠本机 Python 解释器 spawn 跑）。
     现**整套移植成内置 JS**，走 Obsidian 官方 `requestUrl`（跨平台、绕 CORS）：
       · 商品详情  GET https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc?version=0.0.5&item_id=<ID>
       · 店铺首页  GET https://www.xiaohongshu.com/shop/<sellerId>   （SSR，HTML 内嵌商品 JSON）
     两个都是**公开接口、无 Cookie / 无签名**，只读公开页面。
     → 不再需要 Python、不再需要任何外部脚本；桌面 + 手机都能用。

   ⚠️ 本插件**只采集，不写库**。写库由工作台负责（预览 → 用户确认 → 才写）。

   对外接口（工作台用 `app.plugins.getPlugin('xhs-product-search')` 调，**不用 require**）：
     · checkDeps()  → { ok, engine, problems[] }
     · collect(input) → { ok, itemId, 链接, archive{…}, track{…}, extra{…}, warnings[], raw }
     · mode() / setMode()  → '全部采写' | '变动才采写'
   ══════════════════════════════════════════════════════════════════════════ */

const { Plugin, PluginSettingTab, Setting, Notice, Modal, requestUrl } = require("obsidian");

const ENGINE_NAME = "内置 JS 采集引擎（无需 Python）";

/* ══════════════════════════════════════════════════════════════════════════
   采集引擎（纯 JS）—— 由 vendor/xhs.py 逐函数移植（2026-09-30）
   ══════════════════════════════════════════════════════════════════════════ */
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const TOC_API = "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc";
const SHOP_URL = "https://www.xiaohongshu.com/shop/";
const XHS_HEADERS = {
  "User-Agent": UA,
  "Referer": "https://www.xiaohongshu.com/",
  "Accept": "application/json, text/html, */*",
  "Accept-Language": "zh-CN,zh;q=0.9",
};

/* ── 时间/数值小工具 ── */
const _pad2 = (n) => String(n).padStart(2, "0");
function nowStr() {
  const d = new Date();
  return d.getFullYear() + "-" + _pad2(d.getMonth() + 1) + "-" + _pad2(d.getDate()) +
    " " + _pad2(d.getHours()) + ":" + _pad2(d.getMinutes()) + ":" + _pad2(d.getSeconds());
}
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + _pad2(d.getMonth() + 1) + "-" + _pad2(d.getDate());
}
function first() {
  const args = Array.prototype.slice.call(arguments);
  const d = args.shift();
  if (!d || typeof d !== "object") return null;
  for (const k of args) {
    const v = d[k];
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v) && v.length === 0) continue;
    if (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) continue;
    return v;
  }
  return null;
}
function toInt(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return Math.trunc(v);
  const m = String(v).replace(/,/g, "").match(/([\d.]+)\s*(万)?/);
  if (!m) return null;
  let x = parseFloat(m[1]);
  if (m[2]) x *= 10000;
  return Math.trunc(x);
}
function htmlUnescape(s) {
  return String(s == null ? "" : s)
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (m, d) => String.fromCharCode(Number(d)));
}

/* ── HTTP（Obsidian requestUrl：跨平台、绕 CORS）── */
async function httpGet(url) {
  try {
    const r = await Promise.race([
      requestUrl({ url, headers: XHS_HEADERS, throw: false }),
      new Promise((_, rej) => window.setTimeout(() => rej(new Error("请求超时")), 20000)),
    ]);
    if (r.status >= 400 && !r.text) return { text: "", status: r.status, error: "HTTP " + r.status };
    return { text: r.text || "", status: r.status };
  } catch (e) {
    return { text: "", error: "网络异常：" + (e && e.message ? e.message : e) };
  }
}

/* ── 短链展开：桌面用 Node http/https 逐跳跟随（requestUrl 不返回重定向后的 URL）──
   移动端无 require → 返回 noNode 标记，引导改用完整链接。 */
function nodeFollow(url, maxHop = 6) {
  return new Promise((resolve) => {
    let lib = null;
    try { lib = require(String(url).indexOf("https:") === 0 ? "https" : "http"); } catch { /* 移动端无 Node */ }
    if (!lib) return resolve({ chain: [url], err: null, noNode: true });
    const chain = [];
    const hop = (cur, n) => {
      if (n >= maxHop) return resolve({ chain, err: "重定向层数过多（>" + maxHop + "）" });
      chain.push(cur);
      let req;
      try {
        const u = new URL(cur);
        req = lib.request({
          hostname: u.hostname,
          path: u.pathname + u.search,
          method: "GET",
          headers: { "User-Agent": UA, "Referer": "https://www.xiaohongshu.com/" },
        }, (res) => {
          const code = res.statusCode;
          if (code >= 300 && code < 400 && res.headers.location) {
            res.resume();
            let next;
            try { next = new URL(res.headers.location, cur).href; } catch { next = res.headers.location; }
            hop(next, n + 1);
          } else { res.resume(); resolve({ chain, err: null }); }
        });
      } catch (e) { return resolve({ chain, err: String((e && e.message) || e) }); }
      req.on("error", (e) => resolve({ chain, err: String((e && e.message) || e) }));
      req.setTimeout(12000, () => { try { req.destroy(); } catch { /* ignore */ } resolve({ chain, err: "超时" }); });
      req.end();
    };
    hop(url, 0);
  });
}

/* ── 链接解析 ── */
function parseItemId(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return null;
  for (const pat of [/\/goods-detail\/([0-9a-fA-F]{16,32})/, /item_id=([0-9a-fA-F]{16,32})/, /goods\/([0-9a-fA-F]{16,32})/]) {
    const m = s.match(pat);
    if (m) return m[1];
  }
  if (/^[0-9a-fA-F]{16,32}$/.test(s)) return s;
  return null;
}
function parseSellerId(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return null;
  for (const pat of [/\/shop\/([0-9a-fA-F]{16,32})/, /seller_id=([0-9a-fA-F]{16,32})/]) {
    const m = s.match(pat);
    if (m) return m[1];
  }
  if (/^[0-9a-fA-F]{16,32}$/.test(s)) return s;
  return null;
}
function extractUrls(s) {
  return s.match(/https?:\/\/[^\s，,、；;）)】\]]+/g) || [];
}
/** 从任意粘贴内容里找商品 ID。返回 { id, why }（why 会带"究竟认到了什么"，供上层回显） */
async function parseItemIdAny(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return { id: null, why: "内容是空的" };
  let iid = parseItemId(s);
  if (iid) return { id: iid, why: "ok" };
  const urls = extractUrls(s);
  for (const u of urls) { iid = parseItemId(u); if (iid) return { id: iid, why: "ok" }; }
  if (!urls.length) return { id: null, why: "里面没有 http 链接（只粘了文字？）" };
  let last = "";
  for (const u of urls.slice(0, 3)) {
    const { chain, err, noNode } = await nodeFollow(u);
    if (noNode) return { id: null, why: "手机端暂不支持展开「分享短链」—— 请改用完整商品链接（形如 https://www.xiaohongshu.com/goods-detail/<商品ID>），或直接粘商品 ID" };
    if (err && chain.length <= 1) { last = "展开失败：" + err; continue; }
    if (!chain.length) { last = "展开失败：" + (err || "?"); continue; }
    for (const c of chain) { iid = parseItemId(c); if (iid) return { id: iid, why: "ok（短链展开后认到）" }; }
    for (const c of chain) {
      if (c.indexOf("/discovery/item/") >= 0 || c.indexOf("/explore/") >= 0) return { id: null, why: "这是一篇「笔记」的链接，不是商品链接 —— 要去该商品的详情页分享，而不是笔记页" };
      if (c.indexOf("/shop/") >= 0 || c.indexOf("/user/profile/") >= 0) return { id: null, why: "这是「店铺 / 账号主页」的链接，不是单个商品 —— 要打开具体那个商品的详情页再分享" };
      if (c.indexOf("/login") >= 0) return { id: null, why: "短链展开后被跳到登录页（网页端拿不到商品地址）—— 口令可能已失效，或该分享只认 App。最稳的替代：用电脑浏览器打开商品详情页、把地址栏整条复制过来；也可以直接粘商品 ID" };
    }
    last = "展开后是：" + chain[chain.length - 1].slice(0, 140);
  }
  return { id: null, why: last || "认不出商品 ID" };
}
/** 从任意粘贴内容里找商品 ID 或 店铺 ID。返回 { kind:'item'|'seller'|null, id, why } */
async function parseAnyLink(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return { kind: null, id: null, why: "内容是空的" };
  const hit = (txt) => {
    const i = parseItemId(txt);
    if (i) return ["item", i];
    const sid = parseSellerId(txt);
    if (sid) return ["seller", sid];
    return [null, null];
  };
  let kv = hit(s);
  if (kv[0]) return { kind: kv[0], id: kv[1], why: "ok" };
  const urls = extractUrls(s);
  for (const u of urls) { kv = hit(u); if (kv[0]) return { kind: kv[0], id: kv[1], why: "ok" }; }
  if (!urls.length) return { kind: null, id: null, why: "里面没有 http 链接（只粘了文字？）" };
  let last = "";
  for (const u of urls.slice(0, 3)) {
    const { chain, err, noNode } = await nodeFollow(u);
    if (noNode) return { kind: null, id: null, why: "手机端暂不支持展开「分享短链」—— 请改用完整链接，或直接粘 ID" };
    if (err && chain.length <= 1) { last = "展开失败：" + err; continue; }
    for (const c of chain) { kv = hit(c); if (kv[0]) return { kind: kv[0], id: kv[1], why: "ok（短链展开后认到）" }; }
    for (const c of chain) {
      if (c.indexOf("/discovery/item/") >= 0 || c.indexOf("/explore/") >= 0) return { kind: null, id: null, why: "这是「笔记」链接，不是商品/店铺链接" };
      if (c.indexOf("/login") >= 0) return { kind: null, id: null, why: "短链展开后被跳到登录页" };
    }
    last = "展开后是：" + chain[chain.length - 1].slice(0, 140);
  }
  return { kind: null, id: null, why: last || "认不出商品或店铺 ID" };
}

/* ── 商品详情（TOC 接口）── */
function deepFindSold(node) {
  if (node && typeof node === "object" && !Array.isArray(node)) {
    for (const k of Object.keys(node)) {
      const v = node[k];
      if ((k === "itemAnalysisDataText" || k === "spuAnalysisDataText") && typeof v === "string" && v.indexOf("已售") >= 0) return v;
      const r = deepFindSold(v);
      if (r) return r;
    }
  } else if (Array.isArray(node)) {
    for (const v of node) { const r = deepFindSold(v); if (r) return r; }
  }
  return null;
}
function idLastDigitPlus1(itemId) {
  if (!itemId) return null;
  const head = itemId.slice(0, -1), last = itemId.slice(-1);
  const n = parseInt(last, 16);
  if (isNaN(n)) return null;
  return head + ((n + 1) % 16).toString(16);
}
async function fetchGoods(itemId) {
  const url = TOC_API + "?version=0.0.5&item_id=" + encodeURIComponent(itemId);
  const { text, error } = await httpGet(url);
  if (error) return { rec: null, err: error };
  let data;
  try { data = JSON.parse(text); } catch { return { rec: null, err: "返回非 JSON：" + String(text).slice(0, 100) }; }
  if (!data || !data.success) return { rec: null, err: "[" + (data && data.error_code) + "] " + (data && data.msg) };
  const blocks = (data.data && data.data.template_data) || [];
  const b = blocks[0] || {};
  const desc = b.descriptionH5 || {};
  const seller = b.sellerH5 || {};
  const priceH5 = b.priceH5 || {};
  const sku = b.skuInfo || {};
  const sel = b.selectedH5 || {};

  const soldTxt = first(priceH5, "itemAnalysisDataText", "spuAnalysisDataText") ||
    first(desc, "itemAnalysisDataText") || deepFindSold(b);

  let deal = null;
  const dp = (b.profitBarV1 || {}).dealPrice;
  if (dp && typeof dp === "object") deal = dp.price;
  if (deal === null || deal === undefined) {
    deal = ((((b.profitBarPopupH5 || {}).formula || {}).result || {}).price);
  }

  const dist = b.goodsDistributeV4 || {};
  const svc = (((b.serviceV5 || {}).list) || []).filter((x) => x && x.name).map((x) => x.name);
  const tagText = (x) => (x && typeof x === "object") ? String(x.text || x.name || x.title || "") : String(x == null ? "" : x);
  let tags = (((b.descriptionMain || {}).tags) || []).map(tagText);
  tags = tags.concat((priceH5.tags || []).map(tagText)).filter(Boolean);
  const promo = (((b.profitBarV1 || {}).tags) || []).map(tagText).filter(Boolean);

  // SKU 数：data.common_data.statisticInfo.skuNum
  // 🔴 2026-09-30 修：实测 `common_data` 是**转义的 JSON 字符串**（"{\"statisticInfo\":{\"skuNum\":4}}"），
  //    旧代码只认 typeof==="object" → 字符串被静默跳过 → SKU 数恒为空（老哥截图里那个「（留空）」）。
  //    两种形态都吃：字符串先 JSON.parse，对象直接用。
  let stat = {};
  const droot = data.data;
  if (droot && typeof droot === "object") {
    let cd = droot.common_data;
    if (typeof cd === "string" && cd) { try { cd = JSON.parse(cd); } catch { cd = null; } }
    if (cd && typeof cd === "object") {
      const st = cd.statisticInfo;
      if (st && typeof st === "object") stat = st;
    }
  }

  return { rec: {
    "item_id": itemId,
    "标题": first(desc, "name"),
    "已售": toInt(soldTxt),
    "已售原文": soldTxt,
    "现价": priceH5.highlightPrice,
    "到手价": deal,
    "发货地": dist.location,
    "运费": dist.fee,
    "发货时效": (dist.time || {}).text,
    "服务标签": svc.join("、") || null,
    "商品标签": tags.join("、") || null,
    "促销标签": promo.join("、") || null,
    "店铺名": first(seller, "name"),
    "seller_id": first(seller, "id"),
    "店铺链接": first(seller, "link"),
    "店铺评分": first(seller, "sellerScore", "grade"),
    "店铺总销量": toInt(first(seller, "salesVolume")),
    "粉丝数": toInt(first(seller, "fansAmount")),
    "库存状态": first(sku, "stockStatus"),
    "选中SKU": first(sel, "text"),
    "SKU数": stat.skuNum,
    "抓取时间": nowStr(),
  }, err: null };
}

/* ── 店铺页（SSR）── */
function matchBrace(s, start) {
  let depth = 0;
  for (let i = start; i < s.length; i++) {
    const ch = s[i];
    if (ch === "{") depth++;
    else if (ch === "}") { depth--; if (depth === 0) return i; }
  }
  return -1;
}
function parseSoldText(txt) {
  if (!txt) return [null, null, null];
  const s = String(txt).trim();
  const m = s.replace(/已售/g, "").replace(/人购买/g, "").replace(/人加购/g, "").match(/([\d.]+)\s*(万)?/);
  if (!m) return [null, s, null];
  let v = parseFloat(m[1]);
  if (m[2]) v *= 10000;
  return [Math.trunc(v), s, s.endsWith("+")];
}
async function fetchShopItems(sellerId) {
  const { text: raw, error } = await httpGet(SHOP_URL + sellerId);
  if (error) return { info: null, err: error };
  if (!raw || raw.length < 2000 || raw.indexOf("product-item") < 0) {
    return { info: null, err: "店铺页未返回商品数据（可能店铺不存在或结构已变）" };
  }
  let m = raw.match(/class="shop-name"[^>]*>([^<]{1,60})</) || raw.match(/"shopName"\s*:\s*"([^"]+)"/);
  const shopName = m ? htmlUnescape(m[1]).trim() : null;
  m = raw.match(/已售\s*([\d.]+万?\+?)/);
  const shopTotalTxt = m ? ("已售" + m[1]) : null;
  m = raw.match(/"content"\s*:\s*"粉丝\s*([\d.]+万?)"/) || raw.match(/粉丝\s*([\d.]+万?)/);
  const fansTxt = m ? m[1] : null;

  // A 侧：可见 DOM 卡片
  const bounds = [];
  { const re = /class="product-item"/g; let mm; while ((mm = re.exec(raw))) bounds.push(mm.index); }
  bounds.push(raw.length);
  const domRows = [];
  for (let k = 0; k < bounds.length - 1; k++) {
    const card = raw.slice(bounds[k], bounds[k + 1]);
    const t = card.match(/class="title-content"[^>]*>([^<]+)</);
    const p = card.match(/class="num"[^>]*>([\d.]+)</);
    const s = card.match(/class="sold-num"[^>]*>([^<]+)</);
    const badges = [];
    { const re = /class="text-item"[^>]*>([^<]+)</g; let bm; while ((bm = re.exec(card))) badges.push(htmlUnescape(bm[1]).trim()); }
    domRows.push({
      "标题": t ? htmlUnescape(t[1]).trim() : null,
      "价格": p ? parseFloat(p[1]) : null,
      "已售原文": s ? htmlUnescape(s[1]).trim() : null,
      "角标": badges,
    });
  }

  // B 侧：内嵌 JSON（baseInfo）
  const jsonRows = [];
  const seen = new Set();
  { const re = /"baseInfo"\s*:\s*\{/g; let bm;
    while ((bm = re.exec(raw))) {
      const start = bm.index + bm[0].length - 1;
      const end = matchBrace(raw, start);
      if (end < 0) continue;
      const blob = raw.slice(start, end + 1);
      let obj; try { obj = JSON.parse(blob); } catch { continue; }
      const iid = obj.itemId;
      if (!iid || seen.has(iid)) continue;
      seen.add(iid);
      const ts = obj.itemOnShelfTime;
      const lm = blob.match(/"link"\s*:\s*"[^"]*?\/goods\/([0-9a-fA-F]{16,32})/);
      const valid = (typeof ts === "number" && ts > 946684800);
      jsonRows.push({
        "item_id": idLastDigitPlus1(iid) || iid,
        "item_id_店铺页原值": iid,
        "候选ID": [iid, idLastDigitPlus1(iid), lm ? lm[1] : null],
        "标题": htmlUnescape(obj.title || "").trim() || null,
        "上架时间戳": ts,
        "上架日期": valid ? fmtDate(ts) : null,
        "在架天数": valid ? Math.trunc((Date.now() / 1000 - ts) / 86400) : null,
        "图片": (obj.image || {}).url,
      });
    }
  }

  // 用标题连接两侧
  const byTitle = {};
  for (const j of jsonRows) if (j["标题"] && !byTitle[j["标题"]]) byTitle[j["标题"]] = j;
  const items = [];
  const unmatched = [];
  for (const d of domRows) {
    const row = Object.assign({}, d);
    const ps = parseSoldText(d["已售原文"]);
    row["已售"] = ps[0]; row["已售原文"] = ps[1]; row["已售是下界"] = ps[2];
    const j = d["标题"] ? byTitle[d["标题"]] : null;
    if (j) { ["item_id", "候选ID", "上架时间戳", "上架日期", "在架天数", "图片"].forEach((k) => { row[k] = j[k]; }); }
    else { row["item_id"] = null; unmatched.push("(仅 DOM) " + (d["标题"] || "?")); }
    items.push(row);
  }
  const domTitles = new Set(domRows.map((d) => d["标题"]));
  for (const j of jsonRows) if (j["标题"] && !domTitles.has(j["标题"])) unmatched.push("(仅 JSON) " + j["标题"]);
  if (domRows.length !== jsonRows.length) {
    unmatched.push("⚠ 两侧条数不一致（DOM " + domRows.length + " / JSON " + jsonRows.length + "）—— 配对可能错位，已售请人工复核");
  }

  const totalV = shopTotalTxt ? parseSoldText(shopTotalTxt)[0] : null;
  m = raw.match(/"content"\s*:\s*"好评率\s*([\d.]+%)"/);
  const goodRate = m ? m[1] : null;
  m = raw.match(/"content"\s*:\s*"(平均\s*[\d.]+\s*(?:小时|天)发货)"/);
  const shipTime = m ? m[1] : null;

  return { info: {
    "seller_id": sellerId,
    "店铺名": shopName,
    "店铺总销量": totalV,
    "店铺总销量原文": shopTotalTxt,
    "粉丝数": fansTxt ? toInt(fansTxt) : null,
    "粉丝数原文": fansTxt ? ("粉丝数 " + fansTxt) : null,
    "好评率": goodRate,
    "发货时效": shipTime,
    "商品": items,
    "未匹配": unmatched,
  }, err: null };
}

/* ── 商品级销量补正（店铺页口径覆盖 API 的 SKU 级）── */
const _shopCache = new Map();
async function shopSoldMap(sellerId) {
  if (!sellerId) return {};
  if (_shopCache.has(sellerId)) return _shopCache.get(sellerId);
  const { info } = await fetchShopItems(sellerId);
  const map = {};
  if (info) for (const it of (info["商品"] || [])) if (it["item_id"]) map[it["item_id"]] = it;
  _shopCache.set(sellerId, map);
  return map;
}
/** 由商品 ID 前 8 位 hex 推断上架时间（秒级 Unix 时间戳）。拿不到返回 null。 */
function shelfTsFromItemId(itemId) {
  const s = String(itemId == null ? "" : itemId);
  if (s.length < 8) return null;
  const ts = parseInt(s.slice(0, 8), 16);
  return (ts > 1420070400 && ts < 2051222400) ? ts : null;
}
async function applyShopSold(rec) {
  const sm = await shopSoldMap(rec["seller_id"]);
  const hit = sm[rec["item_id"]];
  rec["已售_SKU级"] = rec["已售"];
  rec["已售原文_SKU级"] = rec["已售原文"];
  if (hit) {
    rec["已售"] = hit["已售"]; rec["已售原文"] = hit["已售原文"]; rec["已售是下界"] = hit["已售是下界"];
    rec["已售口径"] = "商品级（店铺页）";
    ["上架日期", "在架天数", "角标"].forEach((k) => {
      if (hit[k] !== null && hit[k] !== undefined && !rec[k]) rec[k] = hit[k];
    });
  } else {
    rec["已售是下界"] = null;
    rec["已售口径"] = "SKU级（API）· 该品不在店铺首页前 6，口径有偏差，仅供参考";
  }
  if (!rec["上架日期"]) {
    const ts = shelfTsFromItemId(rec["item_id"]);
    if (ts) {
      rec["上架日期"] = fmtDate(ts);
      rec["在架天数"] = Math.trunc((Date.now() / 1000 - ts) / 86400);
      rec["上架来源"] = "商品ID推断";
    }
  }
  if (rec["上架日期"] && !rec["上架来源"]) rec["上架来源"] = "店铺页";
  return rec;
}

/* ── 独立使用：把采集结果写成 Markdown 笔记（老哥 09-30：JSON 在 Obsidian 里看不了，要能存成 MD）──
   与「爆款笔记采集」插件同一思路：不接工作台时，采集结果落成库内 .md 文件。 */
function safeFileName(s) {
  return String(s == null ? "" : s)
    .replace(/[\\/:*?"<>|#^[\]]/g, " ")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80);
}
function resultMarkdown(r) {
  const one = (v) => String(v == null ? "" : v).replace(/[\r\n]+/g, " ");
  const yaml = (obj) => Object.keys(obj)
    .map((k) => k + ': "' + one(obj[k]).replace(/"/g, '\\"') + '"').join("\n");
  const kvTable = (obj) => {
    const rows = Object.keys(obj).map((k) => "| " + k + " | " +
      (obj[k] == null || obj[k] === "" ? "（留空）" : String(obj[k]).replace(/\|/g, "\\|")) + " |");
    return ["| 字段 | 值 |", "|---|---|"].concat(rows).join("\n");
  };
  const warnBlock = (ws) => (ws && ws.length ? "\n> **口径提醒**\n" + ws.map((w) => "> - " + w).join("\n") + "\n" : "");

  if (r.kind === "shop") {
    const s = r.shop || {};
    const head = {
      type: "店铺采集", 店铺名: s.店铺名, seller_id: s.seller_id,
      店铺总销量: s.店铺总销量, 粉丝数: s.粉丝数, 好评率: s.好评率, 发货时效: s.发货时效, 采集时间: nowStr(),
    };
    let md = "---\n" + yaml(head) + "\n---\n\n# " + (s.店铺名 || "店铺采集") + "（店铺采集）\n\n" + kvTable(s) + "\n\n";
    const ps = r.products || [];
    md += "## 首页可见商品（" + ps.length + " 个）\n\n";
    if (ps.length) {
      md += "| 标题 | 价格 | 已售 | 上架日期 | 上架天数 | 角标 |\n|---|---|---|---|---|---|\n";
      ps.forEach((p) => {
        md += "| " + String(p.标题 || p.item_id || "—").replace(/\|/g, "\\|") + " | " +
          (p.价格 == null ? "—" : p.价格) + " | " + (p.已售原文 || (p.已售 == null ? "—" : p.已售)) + " | " +
          (p.上架日期 || "—") + " | " + (p.上架天数 == null ? "—" : p.上架天数) + " | " + (p.角标 || "—") + " |\n";
      });
    } else md += "（该店首页可见商品 0 个）\n";
    return md + warnBlock(r.warnings);
  }

  const a = r.archive || {}, t = r.track || {};
  const head = Object.assign({ type: "商品采集", 商品ID: r.itemId, 采集时间: r.抓取时间 || nowStr() }, a, t);
  let md = "---\n" + yaml(head) + "\n---\n\n# " + (a.商品标题 || r.itemId || "商品采集") + "\n\n";
  md += "## 测品档案（固定 / 低频）\n\n" + kvTable(a) + "\n\n";
  md += "## 每日跟踪（变动）\n\n" + kvTable(t) + "\n";
  return md + warnBlock(r.warnings);
}
/** 写入 vault（自动建目录、重名加序号）。返回实际路径。 */
async function writeNoteToVault(app, folder, baseName, content) {
  const clean = String(folder || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  if (clean) {
    let cur = "";
    for (const seg of clean.split("/")) {
      cur = cur ? cur + "/" + seg : seg;
      if (!app.vault.getAbstractFileByPath(cur)) {
        try { await app.vault.createFolder(cur); } catch { /* 并发下已存在，忽略 */ }
      }
    }
  }
  const base = safeFileName(baseName) || "商品采集";
  const prefix = clean ? clean + "/" : "";
  let path = prefix + base + ".md";
  let i = 1;
  while (app.vault.getAbstractFileByPath(path)) path = prefix + base + "-" + (i++) + ".md";
  await app.vault.create(path, content);
  return path;
}

const DEFAULTS = {
  mode: "全部采写",
  noteFolder: "小红书商品采集", // 独立使用（无工作台）时，「保存为 Markdown 笔记」存到这里
};

/* ───────────────── 字段归一化（结构文档 §13.3 采集规格 + §13.4 字段最终落位） ─────────────────
   §13.4 判据：**固定/低频 → 测品档案（一品一行）**；**变动（每天） → 每日跟踪（一天一行）**。
   2026-09-29 实跑 xhs.py 得到的真实 JSON 键（舒眠精油那条）：
     标题 · 现价 · 到手价 · 发货地 · 运费 · 店铺名 · 店铺评分 · 粉丝数 · 已售 · 已售原文
     · 已售是下界 · 已售口径 · 上架日期 · 在架天数 · 角标 …
   ⚠️ `开店天数` 这次**采不到**（JSON 里没有该键）—— 结构文档 §13.3 当时就标了「能否采到未验证」，
      所以**如实留空**，不猜。                                                              */
function toNum(v) {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : parseFloat(String(v).replace(/[^\d.-]/g, ""));
  return Number.isFinite(n) ? n : null;
}
function toStr(v) {
  if (v == null) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
}

function normalize(row) {
  const archive = {
    "商品标题": toStr(row["标题"]),
    "商品售价": toNum(row["现价"]),
    "对标到手价": toNum(row["到手价"]),
    "发货地": toStr(row["发货地"]),
    "店铺评分": toNum(row["店铺评分"]),
    "上架天数": toNum(row["在架天数"]),
    "来源": "小红书市集", // 结构文档 §13.3：来源固定写「小红书市集」
    // B2.8（老哥选定的字段）：
    "SKU数": toNum(row["SKU数"]),          // 接口不保证有 → 没有就留空
    "选中SKU": toStr(row["选中SKU"]),
    "店铺链接": toStr(row["店铺链接"]),
    "商品角标": Array.isArray(row["角标"]) ? (row["角标"].filter(Boolean).join("、") || null) : toStr(row["角标"]),
  };
  const track = {
    "商品销量": toNum(row["已售"]), // 变动项 → 每日跟踪
  };
  // 本次采集规格**之外**的键：原样保留在 extra，供人看/以后决定要不要用（不擅自写库）
  const extra = {};
  const KNOWN = new Set(["标题", "现价", "到手价", "发货地", "店铺评分", "在架天数", "已售", "SKU数", "选中SKU", "店铺链接", "角标"]);
  Object.keys(row).forEach((k) => {
    if (!KNOWN.has(k)) extra[k] = row[k];
  });

  const warnings = [];
  if (row["已售是下界"]) {
    // ⚠️ 弹窗是纯文本渲染：不要用 **加粗** / `代码` 这类 markdown 标记（会原样露出来）
    warnings.push("已售是下界（原文「" + (row["已售原文"] || "") + "」），不是精确值");
  }
  if (row["已售口径"] && String(row["已售口径"]).indexOf("SKU级") >= 0) {
    warnings.push("已售口径是 SKU 级（只有当前选中 SKU 的量），不是商品累计");
  }
  if (!Object.keys(row).some((k) => /开业|开店/.test(k))) {
    // 结构文档 §13.3 的 (b)「开店天数」当时就标了「能否采到未验证」→ 如实留空、不猜
    warnings.push("「开店天数」网页端采不到（该字段只在 App 的「店铺详情」页有 —— 之前的真机调研就是从那采的）→ 留空，需要手填");
  }
  return { archive, track, extra, warnings };
}

class CollectorPlugin extends Plugin {
  async onload() {
    await this.loadSettings();

    /* 独立可用：一条命令 / 侧边栏图标就能采一个商品（不依赖工作台） */
    this.addRibbonIcon("shopping-bag", "采集小红书商品", () => this.promptCollect());
    this.addCommand({ id: "collect-one", name: "采集一个商品（链接或商品 ID）", callback: () => this.promptCollect() });
    this.addCommand({ id: "collector-selftest", name: "采集器自检（内置引擎是否就绪）", callback: () => this.selftest() });
    this.addSettingTab(new CollectorSettingTab(this.app, this));
  }

  async loadSettings() { this.settings = Object.assign({}, DEFAULTS, (await this.loadData()) || {}); }
  async saveSettings() { await this.saveData(this.settings); }

  /* 采写模式（结构文档 §13.5）：供工作台读取，决定"全采"还是"变了才写" */
  mode() { return this.settings.mode; }
  async setMode(v) {
    if (v !== "全部采写" && v !== "变动才采写") return this.settings.mode;
    this.settings.mode = v;
    await this.saveSettings();
    return v;
  }

  /* 依赖自检：内置 JS 引擎永远就绪（保留 version/scriptSource 字段供工作台显示） */
  async checkDeps() {
    return {
      ok: true,
      engine: ENGINE_NAME,
      version: ENGINE_NAME,
      scriptSource: "内置（无需脚本文件 / Python）",
      python: null,
      script: null,
      problems: [],
    };
  }

  async selftest() {
    const d = await this.checkDeps();
    new Notice("采集器自检：" + (d.ok ? "内置引擎就绪（无需 Python，桌面/手机均可用）" : d.problems.join("；")), d.ok ? 4000 : 9000);
    return d;
  }

  /* ── 采集一个商品：一条链接 / 一个商品 ID 都行 ── */
  async collect(input) {
    const raw = toStr(input);
    const fail = (error, extra) => Object.assign({
      ok: false, itemId: null, 链接: raw, archive: {}, track: {}, extra: {}, warnings: [], error, raw: null,
    }, extra || {});
    if (!raw) return fail("没给链接或商品 ID");

    const parsed = await parseItemIdAny(raw);
    if (!parsed.id) {
      // 认出是「店铺 / 账号主页」→ 转店铺采集（B2.5）
      if (/店铺|账号主页/.test(parsed.why || "")) return this.collectShop(raw);
      return fail("认不出商品链接 —— " + (parsed.why || "认不出商品 ID"));
    }

    const got = await fetchGoods(parsed.id);
    if (!got.rec) return fail("没采到：" + got.err);
    const rec = got.rec;
    try { await applyShopSold(rec); } catch { /* 店铺页补正失败不影响主记录 */ }

    const n = normalize(rec);
    return {
      ok: true,
      kind: "item",
      itemId: rec["item_id"] || parsed.id,
      链接: raw,
      店铺链接: rec["店铺链接"] || null,
      sellerId: rec["seller_id"] || null,
      archive: n.archive,
      track: n.track,
      extra: n.extra,
      warnings: n.warnings,
      抓取时间: rec["抓取时间"] || null,
      raw: rec,
    };
  }

  /* ── 店铺采集：店铺页地址 / 店铺分享 → 店铺级信息 + 首页可见商品列表 ──
     实测边界：店铺页内嵌数据里没有「开店时长」字段 → 该字段采不到，档案留空手填。 */
  async collectShop(raw) {
    const p = await parseAnyLink(raw);
    if (!p.id) return { ok: false, kind: "shop", error: "无法解析店铺 —— " + (p.why || "认不出店铺 ID") };
    if (p.kind === "item") return { ok: false, kind: "shop", error: "这是商品链接 —— 采单品请用「采集一个商品」" };

    const got = await fetchShopItems(p.id);
    if (!got.info) return { ok: false, kind: "shop", error: "店铺采集失败 —— " + got.err };
    const s = got.info;
    const prods = s["商品"] || [];
    const warnings = ["「开店天数」网页端采不到（店铺页数据里没有这个字段，已实测核实）→ 档案留空，需要手填"];
    if (!prods.length) warnings.push("该店首页可见商品 0 个 —— 建不了候选品，可能需要从商品详情页分享");
    return {
      ok: true,
      kind: "shop",
      itemId: null,
      链接: raw,
      shop: {
        店铺名: s["店铺名"] || null,
        seller_id: s["seller_id"] || null,
        店铺总销量: s["店铺总销量"],
        店铺总销量原文: s["店铺总销量原文"] || null,
        粉丝数: s["粉丝数"],
        粉丝数原文: s["粉丝数原文"] || null,
        好评率: s["好评率"] || null,
        发货时效: s["发货时效"] || null,
      },
      products: prods.map((q) => ({
        item_id: q["item_id"] || null,
        标题: q["标题"] || null,
        价格: q["价格"],
        已售: q["已售"],
        已售原文: q["已售原文"] || null,
        已售是下界: !!q["已售是下界"],
        上架日期: q["上架日期"] || null,
        上架天数: q["在架天数"],
        角标: Array.isArray(q["角标"]) ? (q["角标"].filter(Boolean).join("、") || null) : (q["角标"] || null),
      })),
      warnings,
      raw: got.info,
    };
  }

  /* 独立用：弹输入框 → 采集 → 预览（工作台侧有自己的 UI，这里只是"不开工作台也能用"） */
  promptCollect() {
    const m = new CollectInputModal(this.app, async (val) => {
      new Notice("采集中…（内置引擎，几秒）", 2500);
      const r = await this.collect(val);
      if (!r.ok) { new Notice("采集失败：" + r.error, 9000); return; }
      new CollectPreviewModal(this.app, r, this).open();
    });
    m.open();
  }

  /** 独立使用（无工作台）：把结果写成库内 Markdown 笔记（目录见设置）。
      接工作台时不用这个 —— 那边由确认弹窗决定写哪张 duowei 表。 */
  async saveResultNote(res) {
    const folder = (this.settings.noteFolder || "").trim();
    const name = res.kind === "shop"
      ? ("店铺-" + ((res.shop && res.shop.店铺名) || (res.shop && res.shop.seller_id) || "采集"))
      : ((res.archive && res.archive.商品标题) || res.itemId || "商品采集");
    return await writeNoteToVault(this.app, folder, name, resultMarkdown(res));
  }
}
/* ───────────────── UI：输入 / 预览 ───────────────── */
class CollectInputModal extends Modal {
  constructor(app, onOk) { super(app); this.onOk = onOk; }
  onOpen() {
    const { contentEl } = this;
    contentEl.createEl("h3", { text: "采集小红书商品" });
    contentEl.createEl("p", { cls: "mod-muted", text: "粘贴商品链接（整条带参数的也行）或裸商品 ID。" });
    const ta = contentEl.createEl("textarea", { cls: "xgc-input" });
    ta.rows = 3; ta.placeholder = "https://www.xiaohongshu.com/goods-detail/…  或  6a83ca5ffa5d5c0001a98ca5";
    const bar = contentEl.createDiv({ cls: "xgc-bar" });
    const ok = bar.createEl("button", { text: "开始采集", cls: "mod-cta" });
    ok.onclick = () => { const v = ta.value.trim(); if (!v) return; this.close(); this.onOk(v); };
    ta.focus();
  }
  onClose() { this.contentEl.empty(); }
}

class CollectPreviewModal extends Modal {
  constructor(app, res, plugin) { super(app); this.res = res; this.plugin = plugin; }
  onOpen() {
    const { contentEl } = this;
    const r = this.res;
    contentEl.createEl("h3", { text: "采集结果（只读·未写库）" });

    const table = (title, obj) => {
      if (!obj) return;
      contentEl.createEl("h4", { text: title });
      const t = contentEl.createEl("table", { cls: "xgc-tb" });
      Object.keys(obj).forEach((k) => {
        const tr = t.createEl("tr");
        tr.createEl("td", { text: k, cls: "xgc-k" });
        tr.createEl("td", { text: obj[k] == null ? "（留空）" : String(obj[k]) });
      });
    };

    if (r.kind === "shop") {
      contentEl.createEl("p", { cls: "mod-muted", text: "店铺「" + ((r.shop && r.shop.店铺名) || "—") + "」· 首页可见商品 " + ((r.products || []).length) + " 个" });
      table("→ 店铺信息", r.shop);
      if (r.products && r.products.length) {
        contentEl.createEl("h4", { text: "→ 首页商品（前 " + r.products.length + " 个）" });
        const t = contentEl.createEl("table", { cls: "xgc-tb" });
        r.products.forEach((q) => {
          const tr = t.createEl("tr");
          tr.createEl("td", { cls: "xgc-k", text: q["标题"] || q.item_id || "—" });
          tr.createEl("td", { text: (q["价格"] != null ? "¥" + q["价格"] : "—") + " · " + (q["已售原文"] || "—") + (q["上架日期"] ? " · " + q["上架日期"] : "") });
        });
      }
    } else {
      contentEl.createEl("p", { cls: "mod-muted", text: "商品 ID " + (r.itemId || "—") + " · 抓取时间 " + (r.抓取时间 || "—") });
      table("→ 测品档案（固定/低频 · 一品一行）", r.archive);
      table("→ 每日跟踪（变动 · 一天一行）", r.track);
    }

    if (r.warnings && r.warnings.length) {
      contentEl.createEl("h4", { text: "⚠️ 口径提醒" });
      const ul = contentEl.createEl("ul");
      r.warnings.forEach((w) => ul.createEl("li", { text: w }));
    }

    const bar = contentEl.createDiv({ cls: "xgc-bar" });
    if (this.plugin) {
      const saveBtn = bar.createEl("button", { text: "保存为 Markdown 笔记", cls: "mod-cta" });
      saveBtn.onclick = async () => {
        saveBtn.disabled = true;
        try {
          const p = await this.plugin.saveResultNote(r);
          new Notice("已保存笔记：" + p, 6000);
          this.close();
        } catch (e) {
          saveBtn.disabled = false;
          new Notice("保存失败：" + (e && e.message ? e.message : e), 9000);
        }
      };
    }
    const copy = bar.createEl("button", { text: "复制为 JSON" });
    copy.onclick = () => {
      const payload = r.kind === "shop" ? { shop: r.shop, products: r.products } : { archive: r.archive, track: r.track };
      navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
      new Notice("已复制");
    };
    bar.createEl("button", { text: "关闭", cls: this.plugin ? "" : "mod-cta" }).onclick = () => this.close();
  }
  onClose() { this.contentEl.empty(); }
}
/* ───────────────── 设置页（结构文档 §13.5 的「采写模式」开关落在这里） ───────────────── */
class CollectorSettingTab extends PluginSettingTab {
  constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
  display() {
    const { containerEl } = this;
    containerEl.empty();
    new Setting(containerEl).setName("小红书商品搜索").setHeading();

    new Setting(containerEl)
      .setName("采集引擎")
      .setDesc("内置 JS 引擎（v0.2.0 起）—— 走 Obsidian 网络接口直接采集，不需要安装 Python，也不需要任何外部脚本。桌面版与移动版（手机 / 平板）都能用。")
      .addButton((b) => b.setButtonText("跑一次自检").onClick(async () => { await this.plugin.selftest(); }));

    new Setting(containerEl)
      .setName("采写模式")
      .setDesc("结构文档 §13.5：省的是写入动作。商品销量每天写；固定项与档案里现有值 diff，变了才写。")
      .addDropdown((d) => d
        .addOption("全部采写", "全采（默认）—— 采到的每项都写库")
        .addOption("变动才采写", "变动才采 —— 固定项 diff 命中才写（在每日跟踪备注记一句）")
        .setValue(this.plugin.settings.mode)
        .onChange(async (v) => { this.plugin.settings.mode = v; await this.plugin.saveSettings(); }));

    new Setting(containerEl)
      .setName("独立使用 · 笔记保存目录")
      .setDesc("不接工作台时，预览弹窗里「保存为 Markdown 笔记」存到这个目录（库内相对路径；留空 = 库根）。接工作台时怎么写由工作台的确认弹窗决定，这里不影响。")
      .addText((t) => t.setValue(this.plugin.settings.noteFolder)
        .onChange(async (v) => { this.plugin.settings.noteFolder = v.trim(); await this.plugin.saveSettings(); }));

    const box = containerEl.createDiv({ cls: "xgc-resolved" });
    box.createEl("div", { cls: "xgc-resolved-line", text: "数据来源：小红书公开商品页 / 店铺页（无登录态、无 Cookie、无遥测）。" });
    box.createEl("div", { cls: "xgc-resolved-line", text: "分享短链（xhslink）在桌面端会自动展开；移动端请改用完整商品链接，或直接粘商品 ID。" });
  }
}
module.exports = CollectorPlugin;
