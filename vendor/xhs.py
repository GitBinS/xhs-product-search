#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书选品雷达（纯 HTTP，无需登录 / 无需浏览器 / 无需付费 API）

三个子命令：

  python xhs.py goods            查 links.txt 里的商品详情 + 日增量
  python xhs.py shop <店铺链接>   扫一个店铺的商品（发现候选品）
  python xhs.py radar            扫 shops.txt 全部店铺 → 出候选品清单 Markdown

原理（2026-09-19 实测，两个未公开接口，都不需要 Cookie / 签名）：
  1) 商品详情  GET https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc
              ?version=0.0.5&item_id=<商品ID>
  2) 店铺首页  GET https://www.xiaohongshu.com/shop/<sellerId>   ← SSR，HTML 内嵌商品 JSON，
              含 itemId / 标题 / 价格 / 上架时间戳（itemOnShelfTime）
              注意：该页只给"综合排序前 6 个"商品；排序/翻页需 App，无接口。

风险：均为未公开接口，随时可能变。异常会明确报错，不静默返回空值。
"""

import argparse
import html as htmllib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

TOC_API = "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc"
SHOP_URL = "https://www.xiaohongshu.com/shop/{sid}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(HERE, "snapshots")


# ================================================================ HTTP

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"网络异常 {type(e).__name__}: {e}"


# ================================================================ 解析工具

def parse_item_id(raw):
    s = (raw or "").strip()
    if not s:
        return None
    for pat in (r"/goods-detail/([0-9a-fA-F]{16,32})",
                r"item_id=([0-9a-fA-F]{16,32})",
                r"goods/([0-9a-fA-F]{16,32})"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{16,32}", s):
        return s
    return None


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟重定向 —— 我们要自己逐跳看，才能捡起中间那跳。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoAutoRedirect)


def resolve_chain(url, max_hop=6, timeout=12):
    """逐跳跟随重定向，返回 (途经 URL 列表, 错误)。

    🔴 为什么不用 urlopen 自动跟随：**小红书未登录时，最后一跳会落到 login 页**，
       自动跟到底就把中间真正的 `goods-detail/<id>` 地址丢掉了（2026-09-21 实测）。
       所以必须自己一跳一跳走，把链上每个 URL 都留下来逐个解析。

    链的第一个元素是原始 URL，最后一个是能到达的最后一跳。
    """
    cur, seen = (url or "").strip(), []
    if not cur:
        return [], "空链接"
    for _ in range(max_hop):
        seen.append(cur)
        try:
            req = urllib.request.Request(cur, headers={
                "User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"})
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as r:
                return seen, None                      # 到底了
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = (e.headers or {}).get("Location")
                if not loc:
                    return seen, None
                cur = urllib.parse.urljoin(cur, loc)
                continue
            return seen, "HTTP %d" % e.code
        except Exception as e:                          # noqa: BLE001
            return seen, "%s: %s" % (type(e).__name__, e)
    return seen, "重定向层数过多（>%d）" % max_hop


def parse_any_link(raw, allow_net=True, timeout=6):
    """从任意粘贴内容里找**商品ID 或 店铺ID**。

    返回 (kind, id, 说明)，kind ∈ {"item", "seller", None}。

    🔴 为什么单独做一个：`shops.txt` 这条线（挖新候选）原来只用 `parse_item_id` /
       `parse_seller_id`，**只认干净地址** → 手机分享的短链/口令整段一律"无法识别"。
       2026-09-21 实测：大哥粘分享口令 → 报"无法识别 1 条"、shops.txt 没写进去 → 采集 0 个商品。
    """
    s = (raw or "").strip()
    if not s:
        return None, None, "内容是空的"

    def _hit(txt):
        i = parse_item_id(txt)
        if i:
            return "item", i
        sid = parse_seller_id(txt)
        if sid:
            return "seller", sid
        return None, None

    k, v = _hit(s)
    if k:
        return k, v, "ok"
    urls = re.findall(r"https?://[^\s，,、；;）)】\]]+", s)
    for u in urls:
        k, v = _hit(u)
        if k:
            return k, v, "ok"
    if not urls:
        return None, None, "里面没有 http 链接（只粘了文字？）"
    if not allow_net:
        return None, None, "短链需要联网展开，但当前不允许联网"
    last = ""
    for u in urls[:3]:
        chain, err = resolve_chain(u, timeout=timeout)
        if err and len(chain) <= 1:
            last = "展开失败：%s" % err
            continue
        for c in chain:
            k, v = _hit(c)
            if k:
                return k, v, "ok（短链展开后认到）"
        for c in chain:
            if "/discovery/item/" in c or "/explore/" in c:
                return None, None, "这是「笔记」链接，不是商品/店铺链接"
            if "/login" in c:
                return None, None, "短链展开后被跳到登录页"
        last = "展开后是：%s" % chain[-1][:140]
    return None, None, last or "认不出商品或店铺 ID"


def parse_item_id_any(raw, allow_net=True, timeout=12):
    """从任意粘贴内容里找**商品** ID —— 分享口令 / 短链 / 带参数的完整链接都尽量认。

    返回 (item_id 或 None, 说明)。说明会带上"究竟认到了什么"，
    好让上层把失败原因原样回显给用户（不然只能干说"不是商品链接"）。
    """
    s = (raw or "").strip()
    if not s:
        return None, "内容是空的"
    iid = parse_item_id(s)
    if iid:
        return iid, "ok"
    urls = re.findall(r"https?://[^\s，,、；;）)】\]]+", s)
    for u in urls:
        iid = parse_item_id(u)
        if iid:
            return iid, "ok"
    if not urls:
        return None, "里面没有 http 链接（只粘了文字？）"
    if not allow_net:
        return None, "短链需要联网展开，但当前不允许联网"
    last = ""
    for u in urls[:3]:
        chain, err = resolve_chain(u, timeout=timeout)
        if err and len(chain) <= 1:           # 一跳都没走过去 = 展开失败，不是"认不出"
            last = "展开失败：%s" % err
            continue
        if not chain:
            last = "展开失败：%s" % (err or "?")
            continue
        for c in chain:                       # 逐跳找 —— 中间那跳可能就带着 itemId
            iid = parse_item_id(c)
            if iid:
                return iid, "ok（短链展开后认到）"
        for c in chain:                       # 认不出就判断它到底是什么链接
            if "/discovery/item/" in c or "/explore/" in c:
                return None, ("这是一篇「笔记」的链接，不是商品链接 —— "
                              "要去那个商品的详情页分享，而不是笔记页")
            if "/shop/" in c or "/user/profile/" in c:
                return None, ("这是「店铺 / 账号主页」的链接，不是单个商品 —— "
                              "要打开具体那个商品的详情页再分享")
            if "/login" in c:
                return None, ("短链展开后被跳到登录页（网页端拿不到商品地址）—— "
                              "口令可能已失效，或该分享只认 App。最稳的替代：用电脑浏览器打开"
                              "商品详情页、把地址栏整条复制过来；也可以直接粘商品 ID")
        # 展开到「首页 / explore / 登录页」这类兜底页 = 这条分享口令在网页端没有对应地址。
        # 🔴 2026-09-29 实测：同一条口令重复 3 次、换 UA、http 变体，都只回首页/explore；
        #    而旧口令同轮仍能 1 跳命中 goods-detail ⇒ **是这条口令本身没解析出目标**，
        #    不是展开机制坏了。所以要给出"能用"的替代路径，不能只说"认不出"。
        tail = chain[-1]
        if re.search(r"^https?://[^/]*(/)?$", tail) or "/explore" in tail or "/login" in tail:
            last = ("这条分享口令**在网页端展开不出商品地址**（服务端只回了首页/登录页）—— "
                    "口令可能已失效，或该分享只认 App。最稳的替代：用电脑浏览器打开商品详情页、"
                    "把地址栏整条复制过来（形如 https://www.xiaohongshu.com/goods-detail/<商品ID>）；"
                    "也可以直接粘商品 ID")
        else:
            last = "展开后是：%s" % tail[:140]
    return None, last or "认不出商品 ID"


def parse_seller_id(raw):
    s = (raw or "").strip()
    if not s:
        return None
    for pat in (r"/shop/([0-9a-fA-F]{16,32})",
                r"seller_id=([0-9a-fA-F]{16,32})"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{16,32}", s):
        return s
    return None


def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"([\d.]+)\s*(万)?", str(v).replace(",", ""))
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2):
        x *= 10000
    return int(x)


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def _deep_find_sold(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("itemAnalysisDataText", "spuAnalysisDataText") \
                    and isinstance(v, str) and "已售" in v:
                return v
            r = _deep_find_sold(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _deep_find_sold(v)
            if r:
                return r
    return None


# ================================================================ 商品详情

def _id_last_digit_plus1(item_id):
    """小红书商品 ID 末位十六进制 +1 恒等变换（实测：店铺页给的 itemId 需要 +1 才有效）。"""
    if not item_id:
        return None
    head, last = item_id[:-1], item_id[-1]
    try:
        n = int(last, 16)
    except ValueError:
        return None
    return head + format((n + 1) % 16, "x")


def fetch_goods_robust(candidates):
    """依次尝试多个候选 ID，返回 (record, err)。candidates 去重后按序尝试。"""
    seen, last_err = set(), "无候选 ID"
    for cid in candidates:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        rec, err = fetch_goods(cid)
        if rec:
            if cid != candidates[0]:
                rec["ID修正"] = f"{candidates[0]} → {cid}"
            return rec, None
        last_err = err
    return None, last_err


def fetch_goods(item_id):
    qs = urllib.parse.urlencode({"version": "0.0.5", "item_id": item_id})
    raw, err = http_get(f"{TOC_API}?{qs}")
    if err:
        return None, err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"返回非 JSON：{raw[:100]}"
    if not data.get("success"):
        return None, f"[{data.get('error_code')}] {data.get('msg')}"

    blocks = data.get("data", {}).get("template_data") or []
    b = blocks[0] if blocks else {}
    desc = b.get("descriptionH5") or {}
    seller = b.get("sellerH5") or {}
    price_h5 = b.get("priceH5") or {}
    sku = b.get("skuInfo") or {}
    sel = b.get("selectedH5") or {}

    sold_txt = _first(price_h5, "itemAnalysisDataText", "spuAnalysisDataText") \
        or _first(desc, "itemAnalysisDataText") or _deep_find_sold(b)

    deal = None
    dp = (b.get("profitBarV1") or {}).get("dealPrice")
    if isinstance(dp, dict):
        deal = dp.get("price")
    if deal is None:
        deal = ((b.get("profitBarPopupH5") or {}).get("formula") or {}).get("result", {}).get("price")

    # --- 2026-09-21 新增：以下四组都是**实测确认存在于接口响应里**的字段 ---
    #     goodsDistributeV4 : 发货地 / 包邮 / 发货时效   （实测：福建泉州·包邮·48小时内发货）
    #     serviceV5.list    : 服务标签（退货包运费/极速退款/7天无理由/晚发必赔）
    #     tags              : 商品标签（"店铺新品销量TOP1"这类；⚠️ 实测两个样本均为空）
    dist = b.get("goodsDistributeV4") or {}
    svc = [x.get("name") for x in ((b.get("serviceV5") or {}).get("list") or [])
           if isinstance(x, dict) and x.get("name")]

    def _tag_text(x):
        if isinstance(x, dict):
            return str(x.get("text") or x.get("name") or x.get("title") or "")
        return str(x or "")

    tags = [_tag_text(x) for x in ((b.get("descriptionMain") or {}).get("tags") or [])]
    tags += [_tag_text(x) for x in ((price_h5.get("tags")) or [])]
    tags = [t for t in tags if t]

    # 促销标签（「限时立减6.1」这类）—— 实测在 profitBarV1.tags 里，是**真有值**的那一个。
    # ⚠️ 关于「商品小标签」（App 上看到的「店铺新品销量TOP1 / 近期店铺销量TOP2 / 店铺商品回购TOP3」）：
    #    2026-09-21 实测**这个接口拿不到** —— descriptionMain.tags / descriptionH5.tags / priceH5.tags
    #    三个数组在两个样本里都是空的，goods-detail H5 页面全文也搜不到那几个字符串（那是 App 私有接口）。
    #    能拿到的近似物是**店铺页卡片角标**里的「当月加购第N名」——在 fetch_shop_items 的 角标 里。
    promo = [_tag_text(x) for x in ((b.get("profitBarV1") or {}).get("tags") or [])]
    promo = [p for p in promo if p]

    # SKU 数（2026-09-29 B2.8 补，老哥要的字段）：data.common_data.statisticInfo.skuNum
    # 🔴 必须逐层 isinstance 防御 —— 实测同一接口不同请求里 common_data 可能是 dict 也可能是 str
    droot = data.get("data")
    stat = {}
    if isinstance(droot, dict):
        cd = droot.get("common_data")
        if isinstance(cd, dict):
            st = cd.get("statisticInfo")
            if isinstance(st, dict):
                stat = st
    sku_num = stat.get("skuNum")

    return {
        "item_id": item_id,
        "标题": _first(desc, "name"),
        "已售": to_int(sold_txt),
        "已售原文": sold_txt,
        "现价": price_h5.get("highlightPrice"),
        "到手价": deal,
        "发货地": dist.get("location"),
        "运费": dist.get("fee"),
        "发货时效": (dist.get("time") or {}).get("text"),
        "服务标签": "、".join(svc) or None,
        "商品标签": "、".join(tags) or None,
        "促销标签": "、".join(promo) or None,
        "店铺名": _first(seller, "name"),
        "seller_id": _first(seller, "id"),
        "店铺链接": _first(seller, "link"),
        "店铺评分": _first(seller, "sellerScore", "grade"),
        "店铺总销量": to_int(_first(seller, "salesVolume")),
        "粉丝数": to_int(_first(seller, "fansAmount")),
        "库存状态": _first(sku, "stockStatus"),
        "选中SKU": _first(sel, "text"),
        "SKU数": sku_num,
        "抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, None


# ================================================================ 店铺扫描

_SOLD_RE = re.compile(r"([\d.]+)\s*(万)?")


def parse_sold_text(txt):
    """把「已售2.4万+」这类文案解析成 (数值, 原文, 是否为下界)。

    返回 (24000, '已售2.4万+', True) —— 带「+」时数值是下限，用的时候要标明。
    """
    if not txt:
        return None, None, None
    s = txt.strip()
    m = _SOLD_RE.search(s.replace("已售", "").replace("人购买", "").replace("人加购", ""))
    if not m:
        return None, s, None
    v = float(m.group(1))
    if m.group(2):
        v *= 10000
    return int(v), s, s.endswith("+")


def _match_brace(s, start):
    """s[start] 应为 '{'；返回与它配对的 '}' 的下标，找不到返回 -1。"""
    depth, i = 0, start
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def fetch_shop_items(seller_id):
    """抓店铺首页 SSR HTML，两侧解析后用「标题」连接：

      A 侧（可见 DOM，按 ``product-item`` 卡片切分）
        → 标题 / 价格 / **商品级已售** / 角标
      B 侧（内嵌 JSON ``baseInfo``）
        → itemId / 候选ID / 标题 / 上架时间戳 / 图片

    🔴 两条不可违反的规矩（都是实测踩出来的）：
      1. **不要按"位置"配对** —— 实测 DOM 侧与 JSON 侧顺序不一致（同一组值、顺序不同），
         按位置 zip 会把价格和销量配到别的商品上。
      2. **「已售」取 DOM 卡片里的商品级值**（`sold-num`），**不要用商品详情 API 的
         `itemAnalysisDataText`** —— 那个是 *当前选中 SKU* 的销量，量级完全不同
         （实测：同一商品 商品级「已售2.4万+」 vs SKU 级「已售59」）。
    """
    raw, err = http_get(SHOP_URL.format(sid=seller_id))
    if err:
        return None, err
    if len(raw) < 2000 or "product-item" not in raw:
        return None, "店铺页未返回商品数据（可能店铺不存在或结构已变）"

    # ---- 店铺信息 ----
    shop_name = None
    m = (re.search(r'class="shop-name"[^>]*>([^<]{1,60})<', raw)
         or re.search(r'"shopName"\s*:\s*"([^"]+)"', raw))
    if m:
        shop_name = htmllib.unescape(m.group(1)).strip()

    m = re.search(r'已售\s*([\d.]+万?\+?)', raw)
    shop_total_txt = ("已售" + m.group(1)) if m else None
    m = (re.search(r'"content"\s*:\s*"粉丝\s*([\d.]+万?)"', raw)
         or re.search(r'粉丝\s*([\d.]+万?)', raw))
    fans_txt = m.group(1) if m else None
    m = re.search(r'"content"\s*:\s*"店铺评价([^"]{0,12})"', raw)
    shop_rep = m.group(1) if m else None

    # ---- A 侧：DOM 商品卡片 ----
    bounds = [mm.start() for mm in re.finditer(r'class="product-item"', raw)]
    bounds.append(len(raw))
    dom_rows = []
    for k in range(len(bounds) - 1):
        card = raw[bounds[k]:bounds[k + 1]]
        t = re.search(r'class="title-content"[^>]*>([^<]+)<', card)
        p = re.search(r'class="num"[^>]*>([\d.]+)<', card)
        s = re.search(r'class="sold-num"[^>]*>([^<]+)<', card)
        badges = re.findall(r'class="text-item"[^>]*>([^<]+)<', card)
        dom_rows.append({
            "标题": htmllib.unescape(t.group(1)).strip() if t else None,
            "价格": float(p.group(1)) if p else None,
            "已售原文": htmllib.unescape(s.group(1)).strip() if s else None,
            "角标": [htmllib.unescape(b).strip() for b in badges],
        })

    # ---- B 侧：内嵌 JSON ----
    json_rows, seen = [], set()
    for bm in re.finditer(r'"baseInfo"\s*:\s*\{', raw):
        start = bm.end() - 1
        end = _match_brace(raw, start)
        if end < 0:
            continue
        blob = raw[start:end + 1]
        try:
            obj = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        iid = obj.get("itemId")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        ts = obj.get("itemOnShelfTime")
        lm = re.search(r'"link"\s*:\s*"[^"]*?/goods/([0-9a-fA-F]{16,32})', blob)
        json_rows.append({
            # 🔴 店铺页给的 itemId 一律要末位 +1 才是可用 ID（公开链接与 API 都只认 +1 后的）。
            #    实测：...7b87 → 404/空白；...7b88 → 正常。所以 item_id 存 +1 后的值。
            "item_id": _id_last_digit_plus1(iid) or iid,
            "item_id_店铺页原值": iid,
            "候选ID": [iid, _id_last_digit_plus1(iid), lm.group(1) if lm else None],
            "标题": htmllib.unescape(obj.get("title") or "").strip() or None,
            "上架时间戳": ts,
            "上架日期": (datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                     if isinstance(ts, (int, float)) and ts > 946684800 else None),
            "在架天数": (int((time.time() - ts) / 86400)
                     if isinstance(ts, (int, float)) and ts > 946684800 else None),
            "图片": (obj.get("image") or {}).get("url"),
        })

    # ---- 用「标题」连接两侧 ----
    by_title = {}
    for j in json_rows:
        if j["标题"]:
            by_title.setdefault(j["标题"], j)

    items, unmatched = [], []
    for d in dom_rows:
        row = dict(d)
        row["已售"], row["已售原文"], row["已售是下界"] = parse_sold_text(d.get("已售原文"))
        j = by_title.get(d["标题"]) if d["标题"] else None
        if j:
            row.update({k: j[k] for k in
                        ("item_id", "候选ID", "上架时间戳", "上架日期", "在架天数", "图片")})
        else:
            row["item_id"] = None
            unmatched.append("(仅 DOM) " + (d["标题"] or "?"))
        items.append(row)

    dom_titles = {d["标题"] for d in dom_rows}
    for j in json_rows:
        if j["标题"] and j["标题"] not in dom_titles:
            unmatched.append("(仅 JSON) " + j["标题"])

    # 🔴 防御：DOM 卡片数 ≠ 内嵌 JSON 条数时**必须报出来**。
    #    两侧数量不一致时按标题连接可能把 A 的标题配上 B 的销量（2026-09-22 加的护栏）。
    if len(dom_rows) != len(json_rows):
        unmatched.append("⚠ 两侧条数不一致（DOM %d / JSON %d）—— 配对可能错位，已售请人工复核"
                         % (len(dom_rows), len(json_rows)))

    total_v = parse_sold_text(shop_total_txt)[0] if shop_total_txt else None
    # 🔴 好评率 / 发货时效（2026-09-29 B2.7 补）：都在店铺页头部的 content 标签里
    #    （旧正则找"店铺评价"标签 → 恒 None；实测标签是 "好评率 100%" / "平均 6 小时发货"）
    m = re.search(r'"content"\s*:\s*"好评率\s*([\d.]+%)"', raw)
    good_rate = m.group(1) if m else None
    m = re.search(r'"content"\s*:\s*"(平均\s*[\d.]+\s*(?:小时|天)发货)"', raw)
    ship_time = m.group(1) if m else None
    return {"seller_id": seller_id, "店铺名": shop_name,
            "店铺总销量": total_v, "店铺总销量原文": shop_total_txt,
            "粉丝数": to_int(fans_txt) if fans_txt else None,
            "粉丝数原文": ("粉丝数 " + fans_txt) if fans_txt else None,
            "好评率": good_rate, "发货时效": ship_time,
            "商品": items, "未匹配": unmatched}, None


# ================================================================ 商品级销量补正
#
# 商品详情 API 给的 `itemAnalysisDataText` 是 **当前选中 SKU 的销量**，
# 而选品要用的是 **商品级累计销量**（店铺卡片上的 sold-num）。
# 实测差距极大：同一商品 商品级「已售2.4万+」 vs SKU 级「已售59」。
# 所以凡是能查到所属店铺的，都尽量用店铺页口径覆盖 API 口径。

_shop_sold_cache = {}


def shop_sold_map(seller_id):
    """返回 {item_id: 店铺页商品 dict}。同一 seller 只抓一次店铺页。

    ⚠️ 店铺页只给综合排序前 6 个商品 —— 不在前 6 的品查不到，返回空。
    （2026-09-22 改：原来只返回 (已售, 原文, 下界) 三元组，现在直接给整个商品 dict，
      好让 apply_shop_sold 顺带把「上架日期 / 上架天数 / 角标」也补上 —— 同一次请求，不多发。）
    """
    if not seller_id:
        return {}
    if seller_id in _shop_sold_cache:
        return _shop_sold_cache[seller_id]
    info, _err = fetch_shop_items(seller_id)
    m = {}
    if info:
        for it in info.get("商品", []):
            if it.get("item_id"):
                m[it["item_id"]] = it
    _shop_sold_cache[seller_id] = m
    return m


def shelf_ts_from_item_id(item_id):
    """由商品 ID 推断上架时间（秒级 Unix 时间戳）。拿不到就返回 None。

    🔴 2026-09-22 实测发现：**小红书商品 ID = 24 位 hex，前 8 位就是商品创建时间戳。**
       在「一枝小只儿」店铺页对照内嵌 JSON 的 `itemOnShelfTime`，3 个样本：
         · 毛被吹歪了  ID 前8位 → 2026-09-20 11:45 ／ 店铺页 11:47  → 差 72 秒
         · 森之物猫猫  ID 前8位 → 2026-09-07 15:57 ／ 店铺页 15:58  → 差 75 秒
         · 你的胆子    ID 前8位 → 2026-08-28 22:55 ／ 店铺页 22:57  → 差 72 秒
       （加上舒眠精油 / 星空小夜灯两个已跟踪的品，共 5 例，误差稳定 ≈ 73 秒 / 约 1 分钟
        —— 上架动作比创建晚约 1 分钟。对"上架天数"（整天粒度取整）零影响。）

    为什么需要它：`itemOnShelfTime` **只在店铺页内嵌 JSON 里，而且店铺页只返回前 6 个商品**
      → 品不在前 6 时上架时间拿不到（原来就留空）。有了这个兜底，任何商品都能算出上架时间。
    ⚠️ 这是**推断值**，不是平台权威值 → 调用方必须把 `上架来源` 标成「商品ID推断」，
       UI 上露出「≈」让大哥知道来源不同。
    """
    try:
        s = str(item_id or "")
        if len(s) < 8:
            return None
        ts = int(s[:8], 16)
        # 合理性校验：必须落在 2015-01-01 ~ 2035-01-01，防把非时间戳的 ID 误当时间
        return ts if 1420070400 < ts < 2051222400 else None
    except (ValueError, TypeError):
        return None


def apply_shop_sold(rec):
    """就地把 rec['已售'] 从 SKU 级换成商品级（若能查到）；并写入 rec['已售口径']。

    顺带补「上架日期 / 在架天数 / 角标」——这些也只在店铺页有，同一次请求里捎回来。
    ⚠️ 店铺页只覆盖前 6 个商品；拿不到时用「商品ID推断」兜底上架时间（见 shelf_ts_from_item_id）。
    """
    sm = shop_sold_map(rec.get("seller_id"))
    hit = sm.get(rec.get("item_id"))
    rec["已售_SKU级"] = rec.get("已售")
    rec["已售原文_SKU级"] = rec.get("已售原文")
    if hit:
        rec["已售"], rec["已售原文"], rec["已售是下界"] = (
            hit.get("已售"), hit.get("已售原文"), hit.get("已售是下界"))
        rec["已售口径"] = "商品级（店铺页）"
        for k in ("上架日期", "在架天数", "角标"):
            if hit.get(k) is not None and not rec.get(k):
                rec[k] = hit[k]
    else:
        rec["已售是下界"] = None
        rec["已售口径"] = "SKU级（API）· 该品不在店铺首页前 6，口径有偏差，仅供参考"
    # 上架时间兜底（无论走哪条路）：店铺页没有 → 商品 ID 推断，并标明来源
    if not rec.get("上架日期"):
        ts = shelf_ts_from_item_id(rec.get("item_id"))
        if ts:
            rec["上架日期"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            rec["在架天数"] = int((time.time() - ts) / 86400)
            rec["上架来源"] = "商品ID推断"
    if rec.get("上架日期") and not rec.get("上架来源"):
        rec["上架来源"] = "店铺页"
    return rec


# ================================================================ 单品采集
#
# 🔴 2026-09-21 改：**发商品链接就只采这一个商品**，不再反查店铺拉整店。
#    这里只额外发**一次店铺页请求**用于口径补正（商品级已售 / 在架天数 / 角标），
#    不会把整店商品列表带出来。

def fetch_item_row(item_id, prev=None):
    """采**单个商品**，返回 (雷达行 dict, err)。行的形状与整店采集一致。

    口径：
      · 「已售」优先取**商品级**（店铺页卡片 `sold-num`）；
      · 详情 API 的 `itemAnalysisDataText` 是**当前选中 SKU 的销量**，量级差几十倍
        （实测同一商品 商品级「已售2.4万+」 vs SKU 级「已售59」）→
        只有店铺页查不到该品时才退回 SKU 级，并在「已售口径」里写明。
      · 「在架天数」只有店铺页内嵌 JSON 有 → 该品不在店铺首页前 6 时拿不到，日均留空。
    """
    g, err = fetch_goods_robust([item_id, _id_last_digit_plus1(item_id)])
    if not g:
        return None, "商品详情取不到：%s" % err

    iid = g.get("item_id") or item_id
    sid = g.get("seller_id")
    shop_name = g.get("店铺名")
    # 用 shop_sold_map（带缓存）拿店铺页那一份——同一 seller 只抓一次，不重复请求
    hit = shop_sold_map(sid).get(iid) if sid else None

    if hit:
        sold, sold_txt, lower = hit.get("已售"), hit.get("已售原文"), hit.get("已售是下界")
        days, badges = hit.get("在架天数"), (hit.get("角标") or [])
        listed = hit.get("上架日期")
        kou = "商品级（店铺页）"
        shelf_src = "店铺页"
    else:
        sold, sold_txt, lower = g.get("已售"), g.get("已售原文"), None
        days, badges, listed = None, [], None
        kou = "SKU级（详情API）· 不在店铺首页前 6，口径有偏差，仅供参考"
        shelf_src = None
        # 上架时间兜底：店铺页覆盖不到前 6 之外的品 → 用商品 ID 推断（误差 ≈1 分钟）
        ts = shelf_ts_from_item_id(iid)
        if ts:
            listed = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            days = int((time.time() - ts) / 86400)
            shelf_src = "商品ID推断"

    p = prev.get(iid) if (prev and iid) else None
    delta = (sold - p["已售"]) if (p and p.get("已售") is not None
                                  and sold is not None) else None

    price = g.get("到手价")
    if price is None:
        price = g.get("现价")
    return {
        "标题": g.get("标题"),
        "item_id": iid,
        "价格": price,
        "标价": g.get("现价"),
        "到手价": g.get("到手价"),
        "已售": sold,
        "已售原文": sold_txt,
        "已售是下界": lower,
        "已售口径": kou,
        "上架日期": listed,
        "在架天数": days,
        "上架来源": shelf_src,
        "日均销量": (round(sold / days, 1)
                 if (sold is not None and days and days > 0) else None),
        "日增": delta,
        "角标": "、".join(badges),
        "店铺名": shop_name,
        "店铺链接": (f"https://www.xiaohongshu.com/shop/{sid}" if sid else None),
        "商品链接": f"https://www.xiaohongshu.com/goods-detail/{iid}",
        # ---- 详情接口独有的字段（2026-09-21 实测确认可采）----
        "发货地": g.get("发货地"),
        "运费": g.get("运费"),
        "发货时效": g.get("发货时效"),
        "服务标签": g.get("服务标签"),
        "促销标签": g.get("促销标签"),
        "商品标签": g.get("商品标签"),
        "店铺评分": g.get("店铺评分"),
        "店铺总销量": g.get("店铺总销量"),
        "粉丝数": g.get("粉丝数"),
    }, None


# 详情接口能补的字段：{行里的键: 详情接口里的键}
# ⚠️ 注意「标价」在详情里叫 **现价**（highlightPrice）—— 店铺页卡片价是另一回事，不能混。
# 🔴 **绝不包含 已售 / 已售原文 / 已售是下界 / 已售口径 / 角标** —— 那四个必须用店铺页口径
#    （详情接口的「已售」是 SKU 级，量级差几十倍）。
_ENRICH_MAP = {
    "标价": "现价",
    "到手价": "到手价",
    "发货地": "发货地",
    "运费": "运费",
    "发货时效": "发货时效",
    "服务标签": "服务标签",
    "促销标签": "促销标签",
    "商品标签": "商品标签",
    "店铺评分": "店铺评分",
    "店铺总销量": "店铺总销量",
    "粉丝数": "粉丝数",
}


def enrich_rows_with_detail(rows, delay=1.0, only_missing=True):
    """给整店采集出来的行**补详情字段**（到手价/标价/发货地/标签/店铺评分…）。

    🔴 为什么需要：整店采集只发 1 次店铺页，拿得到商品级已售/在架天数/角标，
       但拿不到**到手价、标价、发货地、服务标签、店铺评分** —— 那些只在商品详情接口里。
       这里按 item_id 逐条补详情（每条约 1 个请求）。

    🔴 铁律：**绝不覆盖 已售 / 已售原文 / 已售是下界 / 已售口径 / 角标**。

    返回 (补充成功的条数, 失败列表)。
    """
    done, errs = 0, []
    for r in rows:
        iid = r.get("item_id")
        if not iid:
            continue
        if only_missing and r.get("到手价") is not None and r.get("发货地"):
            continue                      # 已经有的（单品链接采的）不重复请求
        g, e = fetch_goods_robust([iid, _id_last_digit_plus1(iid)])
        if not g:
            errs.append((iid, e or "详情取不到"))
            time.sleep(delay)
            continue
        for rk, gk in _ENRICH_MAP.items():
            if g.get(gk) is not None:
                r[rk] = g[gk]
        if not r.get("店铺名"):
            r["店铺名"] = g.get("店铺名")
        if not r.get("店铺链接") and g.get("seller_id"):
            r["店铺链接"] = "https://www.xiaohongshu.com/shop/%s" % g["seller_id"]
        done += 1
        time.sleep(delay)
    return done, errs


# ================================================================ 快照

def load_prev():
    if not os.path.isdir(SNAP_DIR):
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    for f in sorted(os.listdir(SNAP_DIR), reverse=True):
        if not f.endswith(".json") or f.startswith(today):
            continue
        try:
            with open(os.path.join(SNAP_DIR, f), encoding="utf-8") as fh:
                return {r["item_id"]: r for r in json.load(fh) if r.get("item_id")}
        except Exception:  # noqa: BLE001
            continue
    return {}


def save_snapshot(records, tag=""):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, datetime.now().strftime("%Y-%m-%d") + tag + ".json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    return path


# ================================================================ 输出

def fmt(v, dash="-"):
    return dash if v is None else v


# ---------------------------------------------------------------- HTML 工作台

_WB_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px 28px;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     background:#f6f7f9;color:#1f2328}
h1{margin:0 0 4px;font-size:22px;font-weight:600}
.meta{color:#6b7280;font-size:13px;margin-bottom:16px}
.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.toolbar input[type=text]{padding:7px 12px;border:1px solid #d0d7de;border-radius:6px;width:280px;font-size:13px;background:#fff}
.toolbar label{font-size:13px;color:#374151;cursor:pointer;user-select:none}
.toolbar .count{color:#6b7280;font-size:13px;margin-left:auto}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #f0f1f3;font-size:13px;vertical-align:top}
th{background:#fafbfc;font-weight:600;color:#374151;cursor:pointer;white-space:nowrap;user-select:none;position:sticky;top:0}
th:hover{background:#f0f2f5}
th.num,td.num{text-align:right}
tbody tr:hover{background:#fafcff}
tbody tr:last-child td{border-bottom:none}
.title{max-width:380px}
.title a{color:#1a56b8;text-decoration:none}
.title a:hover{text-decoration:underline}
.shop{color:#6b7280;white-space:nowrap}
.sold{font-variant-numeric:tabular-nums;white-space:nowrap}
.sold .lb{color:#9ca3af;font-size:11px;margin-left:3px}
.daily{font-weight:700;color:#c81e1e;font-variant-numeric:tabular-nums}
.new{display:inline-block;background:#fff4e5;color:#b45309;border-radius:4px;font-size:11px;
     padding:1px 5px;margin-right:5px;vertical-align:1px}
.badge{color:#8a6d3b;font-size:12px;white-space:nowrap}
.delta.up{color:#c81e1e;font-weight:600}
.delta.down{color:#0f7b3e;font-weight:600}
.empty{padding:32px;text-align:center;color:#6b7280}
footer{margin-top:16px;color:#6b7280;font-size:12px;line-height:1.9}
footer b{color:#374151}
.warn{background:#fff8e1;border:1px solid #f0e0a0;border-radius:6px;padding:10px 14px;margin-top:14px;font-size:12.5px;color:#7a5b00}
"""

_WB_JS = """
(function(){
  var tb=document.querySelector('#t tbody');
  var q=document.getElementById('q');
  var onlyNew=document.getElementById('fresh');
  var cnt=document.getElementById('count');
  var rows=Array.prototype.slice.call(tb.querySelectorAll('tr'));
  var dir={};
  function apply(){
    var s=(q.value||'').trim().toLowerCase();
    var n=0;
    rows.forEach(function(tr){
      var okS = !s || tr.dataset.search.indexOf(s)>=0;
      var okN = !onlyNew.checked || tr.dataset.fresh==='1';
      var show = okS && okN;
      tr.style.display = show ? '' : 'none';
      if(show) n++;
    });
    cnt.textContent = '显示 ' + n + ' / ' + rows.length + ' 条';
  }
  q.addEventListener('input', apply);
  onlyNew.addEventListener('change', apply);
  document.querySelectorAll('#t th').forEach(function(th,i){
    if(th.dataset.nosort) return;
    th.addEventListener('click', function(){
      var num = th.dataset.type==='num';
      dir[i] = !dir[i];
      var sorted = rows.slice().sort(function(a,b){
        var x=a.children[i].dataset.v||'', y=b.children[i].dataset.v||'';
        if(num){ x=parseFloat(x); y=parseFloat(y);
                 if(isNaN(x))x=-Infinity; if(isNaN(y))y=-Infinity; return dir[i]? y-x : x-y; }
        return dir[i] ? y.localeCompare(x,'zh') : x.localeCompare(y,'zh');
      });
      sorted.forEach(function(tr){ tb.appendChild(tr); });
      apply();
    });
  });
  apply();
})();
"""


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;") \
        .replace(">", "&gt;").replace('"', "&quot;")


_WB_COLS = [("序号", "num"), ("标题", "text"), ("店铺", "text"),
            ("到手价", "num"), ("标价", "num"), ("已售(商品级)", "num"), ("上架日期", "text"),
            ("上架天数", "num"), ("日均销量", "num"), ("日增", "num"),
            ("发货地", "text"), ("角标", "text"), ("链接", "text")]


def render_html(rows, meta, errs):
    """把雷达结果渲染成单文件 HTML 工作台（零外部依赖，双击即开）。"""
    head = "".join(f'<th class="{t}" data-type="{t}">{_esc(n)}</th>'
                   for n, t in _WB_COLS)

    body = []
    for i, r in enumerate(rows, 1):
        days = r.get("在架天数")
        is_fresh = days is not None and days <= 60
        link = r.get("商品链接")
        lb = ('<span class="lb">≥</span>'
              if (r.get("已售是下界") and r.get("已售") is not None) else "")
        t_html = _esc(r.get("标题") or "?")
        if link:
            t_html = f'<a href="{_esc(link)}" target="_blank" rel="noopener">{t_html}</a>'
        if is_fresh:
            t_html = '<span class="new">🆕新品</span>' + t_html
        link_cell = (f'<a href="{_esc(link)}" target="_blank" rel="noopener">打开</a>'
                     if link else "-")

        d = r.get("日增")
        if d is None:
            d_cell = "-"
        else:
            cls = "up" if d > 0 else ("down" if d < 0 else "")
            d_cell = f'<span class="delta {cls}" data-v="{d}">{d:+d}</span>'

        body.append(
            f'<tr data-fresh="{1 if is_fresh else 0}" data-search="{_esc(search_key(r))}">'
            f'<td class="num" data-v="{i}">{i}</td>'
            f'<td class="title">{t_html}</td>'
            f'<td class="shop">{_esc(r.get("店铺名"))}</td>'
            f'<td class="num" data-v="{fmt(r.get("到手价") if r.get("到手价") is not None else r.get("价格"), 0)}">'
            f'{fmt(r.get("到手价") if r.get("到手价") is not None else r.get("价格"))}</td>'
            f'<td class="num" data-v="{fmt(r.get("标价"), 0)}">{fmt(r.get("标价"))}</td>'
            f'<td class="num sold" data-v="{fmt(r.get("已售"), 0)}">{lb}{fmt(r.get("已售"))}</td>'
            f'<td data-v="{_esc(fmt(r.get("上架日期")))}">{_esc(fmt(r.get("上架日期")))}</td>'
            f'<td class="num" data-v="{fmt(days, 0)}">{fmt(days)}</td>'
            f'<td class="num" data-v="{fmt(r.get("日均销量"), 0)}">'
            f'<span class="daily">{fmt(r.get("日均销量"))}</span></td>'
            f'<td class="num" data-v="{fmt(d, 0)}">{d_cell}</td>'
            f'<td>{_esc(fmt(r.get("发货地"), ""))}{(" · " + _esc(str(r.get("运费")))) if r.get("运费") else ""}</td>'
            f'<td class="badge">{_esc(fmt(r.get("角标"), ""))}</td>'
            f'<td>{link_cell}</td>'
            f'</tr>')

    err_html = ""
    if errs:
        err_html = ('<div class="warn"><b>抓取异常</b><br>'
                    + "<br>".join(_esc(f"{a}：{b}") for a, b in errs) + "</div>")

    rows_html = "\n".join(body) if body else (
        '<tr><td colspan="11" class="empty">没有数据 —— 先在 shops.txt 里填对标链接</td></tr>')

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>候选品雷达 · {_esc(meta.get('time'))}</title>
<style>{_WB_CSS}</style></head><body>
<h1>候选品雷达</h1>
<div class="meta">{_esc(meta.get('desc'))}</div>
<div class="toolbar">
  <input type="text" id="q" placeholder="搜标题 / 店铺 / 角标…">
  <label><input type="checkbox" id="fresh"> 只看新品（在架 ≤60 天）</label>
  <span class="count" id="count"></span>
</div>
<table id="t"><thead><tr>{head}</tr></thead><tbody>
{rows_html}
</tbody></table>
<footer>
  <b>口径</b>：「已售」= <b>商品级累计销量</b>（店铺卡片值），不是 SKU 级。带 <b>≥</b> 的是下限（如 <code>2.4万+</code> → 记 24000）。<br>
  <b>上架天数</b> = 商品上架那一刻到现在的整天数（向下取整）；<b>日均销量</b> = 已售 ÷ 上架天数（粗略，未剔除淡旺季、也未折算下界）—— <b>它比累计已售更早暴露趋势</b>。<br>
  <b>点列头可排序</b>；可搜标题/店铺/角标；可筛只看新品。<br>
  <b>未覆盖</b>：「万单链接数」（需 App 搜索）、「1688 成本 → 利润」—— 这两项仍需手动补。
</footer>
{err_html}
<script>{_WB_JS}</script>
</body></html>"""


def search_key(r):
    return ((r.get("标题") or "") + " " + (r.get("店铺名") or "") + " "
            + (r.get("角标") or "")).lower()


def cmd_goods(args):
    path = args.links
    if not os.path.isfile(path):
        print(f"找不到清单：{path}", file=sys.stderr)
        return 1
    raw_lines = [l.strip() for l in open(path, encoding="utf-8")
                 if l.strip() and not l.startswith("#")]

    # 🔴 这里必须用 parse_item_id_any —— 不能用 parse_item_id（2026-09-29 修）
    #    parse_item_id 只认"干净地址"（/goods-detail/<id>、item_id=<id>、裸 ID）；
    #    手机 App 分享出来的是**整段分享文案 + xhslink 短链**，它一律认不出 →
    #    于是直接报"清单里没有有效商品链接"，**哪怕短链本身是好的**（实测：README 里
    #    实证过的那条 /m/17EgfMeZ4m8 走这条老路一样被拒）。
    #    parse_item_id_any 正是 2026-09-21 为同一个问题写的（shops.txt 那条线在用），
    #    cmd_goods 当时漏接 —— 这就是"粘分享文案必失败"的真因。
    ids, errs = [], []
    for l in raw_lines:
        iid, why = parse_item_id_any(l, allow_net=True)
        if iid:
            ids.append((l, iid))
        else:
            m = re.findall(r"https?://[^\s，,、；;）)】\]]+", l)
            errs.append(((m[0] if m else l)[:80], why or "认不出商品 ID"))

    if not ids:
        detail = "；".join(f"{a}：{b}" for a, b in errs) or "清单里没有有效商品链接。"
        if args.json:
            # 仍然给结构化输出：上层（工作台）要把每条的失败原因原样回显给用户，
            # 不能让用户只看到"采集失败"四个字。
            print(json.dumps({"ok": [], "errors": [{"来源": a, "原因": b} for a, b in errs]},
                             ensure_ascii=False, indent=2))
        print("认不出商品链接 —— " + detail, file=sys.stderr)
        return 1

    prev = load_prev()
    rows = []
    for n, (src, iid) in enumerate(ids):
        if n:
            time.sleep(args.delay + random.uniform(0, 1.2))
        r, e = fetch_goods(iid)
        if r:
            apply_shop_sold(r)          # ← 尽量把 SKU 级销量换成商品级
            rows.append(r)
        else:
            errs.append((src[:60], e))

    if args.json:
        print(json.dumps({"ok": rows, "errors": [{"来源": a, "原因": b} for a, b in errs]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n{'标题':<30}{'已售':>10}{'日增':>8}{'口径':>8}{'到手':>9}{'粉丝':>9}")
        print("-" * 82)
        for r in rows:
            d = "-"
            p = prev.get(r["item_id"])
            if p and p.get("已售") is not None and r.get("已售") is not None:
                d = f"{r['已售'] - p['已售']:+d}"
            deal = r.get("到手价")
            sold = fmt(r.get("已售"))
            if r.get("已售是下界") and r.get("已售") is not None:
                sold = f"≥{sold}"
            kou = "商品级" if "商品级" in (r.get("已售口径") or "") else "SKU级"
            print(f"{(r.get('标题') or r['item_id'])[:28]:<30}"
                  f"{str(sold):>10}{d:>8}{kou:>8}"
                  f"{(f'{deal:.1f}' if isinstance(deal, (int, float)) else '-'):>9}"
                  f"{fmt(r.get('粉丝数')):>9}")
        print()
        print("  ⚠️ 「SKU级」= API 只给了当前选中 SKU 的销量、不是商品累计；"
              "该品不在所属店铺首页前 6，无法自动补正 → 日增会同口径自比，但仍不如商品级准。")
        for a, b in errs:
            print(f"  !! {a}: {b}")
        print()
    if rows:
        print(f"快照：{save_snapshot(rows)}")
    return 0 if rows else 1


def cmd_shop(args):
    # 🔴 用 parse_any_link（2026-09-29 B2.5）：老哥粘的往往是**分享口令/短链/整段文字**，
    #    裸 parse_seller_id 只认干净地址 → 一律"无法解析店铺 ID"（与 cmd_goods 同一个问题）。
    #    parse_any_link 还能区分"这是商品不是店铺"，避免拿商品链接来采店铺。
    kind, sid, why = parse_any_link(args.target, allow_net=True)
    if not sid:
        print("无法解析店铺 —— %s" % (why or "认不出店铺 ID"), file=sys.stderr)
        return 1
    if kind == "item":
        print("这是**商品**链接（goods-detail）—— 采单品请用 goods 命令；shop 要店铺页或店铺分享。", file=sys.stderr)
        return 1
    info, err = fetch_shop_items(sid)
    if err:
        print(f"失败：{err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"店铺": {k: v for k, v in info.items() if k not in ("商品", "未匹配")},
                          "商品": info.get("商品", []),
                          "未匹配": info.get("未匹配", [])},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"\n店铺：{info['店铺名'] or sid}  总销量 {info.get('店铺总销量原文') or '-'}  "
          f"{info.get('粉丝数原文') or '-'}"
          + (f"  评价：{info['店铺评价']}" if info.get("店铺评价") else "") + "\n")
    print(f"{'上架日期':<12}{'上架天数':>9}{'价格':>9}{'已售(商品级)':>14}  标题")
    print("-" * 116)
    for it in sorted(info["商品"], key=lambda x: x.get("上架时间戳") or 0, reverse=True):
        sold = it.get("已售原文") or "-"
        if it.get("已售是下界") and it.get("已售"):
            sold += f" (≥{it['已售']})"
        print(f"{fmt(it.get('上架日期')):<12}{fmt(it.get('在架天数')):>9}"
              f"{fmt(it.get('价格')):>9}{sold:>14}  {(it.get('标题') or '')[:48]}")
    if info.get("未匹配"):
        print("\n!! 两侧未连接上的条目（不会静默丢，请检查）：")
        for u in info["未匹配"]:
            print("   - " + u)
    print(f"\n（店铺页只给综合排序前 {len(info['商品'])} 个商品；排序/翻页需 App）")
    print("（「已售」= 商品级累计销量，取自店铺卡片；不是 SKU 级）")
    return 0


def cmd_radar(args):
    shops_path = args.shops
    if not os.path.isfile(shops_path):
        print(f"找不到店铺清单：{shops_path}", file=sys.stderr)
        return 1
    shop_lines = [l.strip() for l in open(shops_path, encoding="utf-8")
                  if l.strip() and not l.startswith("#")]

    # 支持两种输入，**发什么采什么**（2026-09-21 改）：
    #   · 店铺链接/ID → 采整个店铺
    #   · 商品链接    → **只采这一个商品**
    #     改前：商品链接会反查店铺再整店采集 → 发一个商品却拉出整店，与预期不符。
    sids, item_ids, resolve_errs = [], [], []
    seen_shop, seen_item = set(), set()
    for line in shop_lines:
        sid = parse_seller_id(line)
        if sid:
            if sid not in seen_shop:
                seen_shop.add(sid)
                sids.append((line, sid, None))
            continue
        iid = parse_item_id(line)
        if not iid:
            resolve_errs.append((line[:60], "既不是店铺链接也不是商品链接"))
            continue
        if iid not in seen_item:
            seen_item.add(iid)
            item_ids.append((line, iid))

    if not sids and not item_ids:
        print("清单里没有可用的店铺/商品链接。", file=sys.stderr)
        for a, b in resolve_errs:
            print(f"  !! {a}: {b}", file=sys.stderr)
        return 1

    prev = load_prev()
    all_rows, errs = list(resolve_errs), []

    for n, (src, sid, _nm) in enumerate(sids):
        if n:
            time.sleep(args.delay)
        info, err = fetch_shop_items(sid)
        if err:
            errs.append((src[:60], err))
            continue
        shop_name = info["店铺名"] or sid
        print(f"  扫店：{shop_name} → {len(info['商品'])} 个商品", file=sys.stderr)

        if info.get("未匹配"):
            errs.append((f"{shop_name} 两侧未连接", "；".join(info["未匹配"])[:120]))

        for it in info["商品"]:
            days = it.get("在架天数")
            sold = it.get("已售")                      # ← 商品级（DOM 卡片），不是 SKU 级
            daily = round(sold / days, 1) if (sold is not None and days and days > 0) else None
            iid = it.get("item_id")
            p = prev.get(iid) if iid else None
            delta = (sold - p["已售"]) if (p and p.get("已售") is not None
                                          and sold is not None) else None
            all_rows.append({
                "标题": it.get("标题"),
                "item_id": iid,
                "价格": it.get("价格"),
                "已售": sold,
                "已售原文": it.get("已售原文"),
                "已售是下界": it.get("已售是下界"),
                "上架日期": it.get("上架日期"),
                "在架天数": days,
                "日均销量": daily,
                "日增": delta,
                "角标": "、".join(it.get("角标") or []),
                "店铺名": shop_name,
                "店铺链接": f"https://www.xiaohongshu.com/shop/{sid}",
                "商品链接": (f"https://www.xiaohongshu.com/goods-detail/{iid}" if iid else None),
            })

    # ---- 商品链接：只采这一个品（不展开整店）----
    for src, iid in item_ids:
        time.sleep(args.delay)
        rec, err2 = fetch_item_row(iid, prev)
        if err2:
            errs.append((src[:60], err2))
            continue
        print(f"  单品：{(rec.get('标题') or '')[:36]} · 已售 {fmt(rec.get('已售'))}"
              f"（{rec.get('已售口径')}）", file=sys.stderr)
        all_rows.append(rec)

    # ---- 补详情：到手价 / 标价 / 发货地 / 标签 / 店铺评分（整店采的行缺这些）----
    if all_rows:
        print("  补详情（到手价 / 标价 / 发货地 / 标签）…", file=sys.stderr)
        done, derrs = enrich_rows_with_detail(all_rows, delay=max(args.delay, 1.0))
        print("  补详情完成：%d 条" % done, file=sys.stderr)
        for a, b in derrs[:5]:
            errs.append((str(a)[:40], b))
    for r in all_rows:                      # 价格口径统一：能拿到到手价就用到手价
        if r.get("到手价") is not None:
            r["价格"] = r["到手价"]

    if not all_rows:
        print("没扫到任何商品。", file=sys.stderr)
        return 1

    def score(r):
        d = r.get("日均销量") or 0
        fresh = 1 if (r.get("在架天数") or 999) <= 60 else 0
        return (fresh, d)

    all_rows.sort(key=score, reverse=True)

    out = [f"# 候选品雷达 · {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
           f"来源：{len(sids)} 个对标店铺 + {len(item_ids)} 个单独商品链接，共 {len(all_rows)} 个商品",
           "排序：新品（在架 ≤60 天）优先，再按日均销量降序", "",
           "> **口径**：「已售」= **商品级累计销量**（店铺卡片值），不是 SKU 级。带 `+` 的是下限（如 `2.4万+` → 记 24000 并标 ≥）。",
           "> **上架天数** = 上架那一刻到现在的整天数（向下取整），旁边一列是上架年月日；**日均销量** = 已售 ÷ 上架天数。",
           "> 单品链接采到的品若不在店铺首页前 6，拿不到商品级值 → 已售后面标 `⚠SKU`。", "",
           "| 排序 | 标题 | 到手价 | 标价 | 已售(商品级) | 上架日期 | 上架天数 | 日均销量 | 日增 | 发货地 | 角标 | 店铺 | 链接 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(all_rows, 1):
        flag = "🆕" if (r.get("在架天数") or 999) <= 60 else ""
        sold = fmt(r.get("已售"))
        if r.get("已售是下界") and r.get("已售") is not None:
            sold = f"≥{sold}"
        if str(r.get("已售口径") or "").startswith("SKU"):
            sold = f"{sold} ⚠SKU"
        link = f"[打开]({r['商品链接']})" if r.get("商品链接") else "—"
        ship = fmt(r.get("发货地"), "")
        if r.get("运费"):
            ship += f" · {r['运费']}"
        out.append(
            f"| {i} | {flag}{(r['标题'] or '')[:40]} | {fmt(r.get('到手价') or r.get('价格'))} | "
            f"{fmt(r.get('标价'))} | "
            f"{sold} | {fmt(r.get('上架日期'))} | {fmt(r.get('在架天数'))} | "
            f"**{fmt(r.get('日均销量'))}** | {fmt(r.get('日增'))} | {ship} | "
            f"{fmt(r.get('角标'),'')} | {r['店铺名']} | {link} |")
    out += ["", "> 🆕 = 在架 ≤60 天（新品）",
            "> ⚠️ **未覆盖的硬门槛**：万单链接数（需 App 搜索）、1688 成本 → 利润 → 这两项仍需手动补。", ""]
    if errs:
        out += ["## 抓取异常", ""] + [f"- {a}：{b}" for a, b in errs] + [""]

    md = "\n".join(out)
    stamp = datetime.now().strftime("%Y-%m-%d")
    outfile = args.out or os.path.join(HERE, "候选品雷达-" + stamp + ".md")
    with open(outfile, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    print(md)
    print(f"\n已写入：{outfile}")

    # 单页 HTML 工作台（零依赖、双击即开）
    if not args.no_html:
        html_path = os.path.join(HERE, "工作台-候选品雷达.html")
        meta = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "desc": (f"{len(sids)} 个对标店铺 + {len(item_ids)} 个单品链接 · {len(all_rows)} 个商品 · "
                         f"按「新品优先 + 日均销量降序」排 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")}
        try:
            with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_html(all_rows, meta, errs))
            print(f"工作台：{html_path}")
        except Exception as e:  # noqa: BLE001
            print(f"!! HTML 生成失败（md 仍已写入）：{type(e).__name__}: {e}", file=sys.stderr)

    print(f"快照：{save_snapshot(all_rows, '-radar')}")
    return 0


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description="小红书选品雷达")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("goods", help="查 links.txt 商品详情 + 日增")
    g.add_argument("--links", default=os.path.join(HERE, "links.txt"))
    g.add_argument("--json", action="store_true")
    g.add_argument("--delay", type=float, default=3.0)
    g.set_defaults(func=cmd_goods)

    s = sub.add_parser("shop", help="扫一个店铺的商品")
    s.add_argument("target", help="店铺链接 / 店铺分享文案 / sellerId")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_shop)

    r = sub.add_parser("radar", help="扫 shops.txt 全部店铺 → 候选品清单 Markdown + 工作台")
    r.add_argument("--shops", default=os.path.join(HERE, "shops.txt"))
    r.add_argument("--out", default=None)
    r.add_argument("--no-html", action="store_true", help="不生成 HTML 工作台")
    r.add_argument("--delay", type=float, default=2.5)
    r.set_defaults(func=cmd_radar)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
