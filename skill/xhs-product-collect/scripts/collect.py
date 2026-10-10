#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xhs-product-collect · 小红书公开商品/店铺数据采集 CLI（纯标准库）

从任意粘贴内容（商品链接 / 商品 ID / 店铺链接 / 店铺 ID / 分享文案）采集
小红书**公开页面**数据，输出结构化 JSON / Markdown，可选落盘为 .md 笔记。

- 免登录、免 Cookie、免签名、零第三方依赖（Python 3.7+ 标准库）
- 引擎逐函数移植自 Obsidian 插件 xhs-product-search（v0.3.1）的内置 JS 采集引擎

用法：
    python3 collect.py "<商品链接|商品ID|店铺链接|店铺ID>"   # 默认：JSON 打到 stdout
    python3 collect.py "<...>" --md                        # Markdown 格式打到 stdout
    python3 collect.py "<...>" --save "<目录>"             # 落盘 .md（同商品复采自动更新原文件）
    python3 collect.py "<...>" --shop                      # 强制按店铺采集
    python3 collect.py --selftest                          # 引擎自检（不发真实业务请求）

退出码：0 成功 / 1 解析失败 / 2 采集失败 / 3 网络失败 / 4 文件保存失败
"""

import argparse
import gzip
import json
import math
import re
import ssl  # noqa: F401  （网络就绪检查用）
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

# ══════════════════════════════════════════════════════════════════════════
#  常量（对照插件：UA / TOC_API / SHOP_URL / XHS_HEADERS）
# ══════════════════════════════════════════════════════════════════════════
ENGINE_NAME = "内置 Python 采集引擎（纯标准库，无需第三方依赖）"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TOC_API = "https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc"
SHOP_URL = "https://www.xiaohongshu.com/shop/"
XHS_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 20          # 单请求超时（秒），与插件一致
SHORTLINK_TIMEOUT = 12  # 短链逐跳超时（秒），与插件一致
MAX_HOP = 6           # 短链展开最大跳数，与插件一致

TRACK_HEAD = "## 每日跟踪（变动）"
TRACK_COLS = "| 日期 | 商品销量 |"


# ══════════════════════════════════════════════════════════════════════════
#  时间 / 数值小工具（对照插件：nowStr / fmtDate / first / toInt / htmlUnescape）
# ══════════════════════════════════════════════════════════════════════════
def now_str():
    d = datetime.now()
    return "%04d-%02d-%02d %02d:%02d:%02d" % (d.year, d.month, d.day, d.hour, d.minute, d.second)


def fmt_date(ts):
    d = datetime.fromtimestamp(ts)
    return "%04d-%02d-%02d" % (d.year, d.month, d.day)


def first(d, *keys):
    """返回 d 中第一个「有值」的键值；跳过 None / '' / 空数组 / 空对象（对照插件 first()）。"""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)) and len(v) == 0:
            continue
        if isinstance(v, dict) and len(v) == 0:
            continue
        return v
    return None


def _parse_float_js(s):
    """模拟 JS parseFloat：从串首解析一段合法数字（'1.2.3' → 1.2；'.5' → 0.5）。"""
    m = re.match(r"[+-]?(?:\d+\.?\d*|\.\d+)", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def to_int(v):
    """对照插件 toInt()：数字直接截断；字符串取首个数字（支持「万」，「1.5万+」→ 15000）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"([\d.]+)\s*(万)?", str(v).replace(",", ""))
    if not m:
        return None
    x = _parse_float_js(m.group(1))
    if x is None:
        return None
    if m.group(2):
        x *= 10000
    return int(x)


def html_unescape(s):
    """对照插件 htmlUnescape()：常见实体 + 数字实体，单轮链式替换。"""
    t = "" if s is None else str(s)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = t.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")

    def _rep(m):
        try:
            n = int(m.group(1))
            if 0 <= n <= 0x10FFFF:
                return chr(n)
        except ValueError:
            pass
        return m.group(0)

    return re.sub(r"&#(\d+);", _rep, t)


def _js_falsy(v):
    """JS falsy 语义（注意：空数组/空对象在 JS 里是 truthy）。供补正逻辑用。"""
    if v is None or v is False:
        return True
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)) and v == 0:
        return True
    if isinstance(v, str) and v == "":
        return True
    return False


def to_num(v):
    """对照插件 toNum()：保留数字；字符串剥掉非 [\\d.-] 后 parseFloat；无效 → None。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    n = _parse_float_js(re.sub(r"[^\d.-]", "", str(v)))
    return n if (n is not None and math.isfinite(n)) else None


def to_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s == "" else s


# ══════════════════════════════════════════════════════════════════════════
#  HTTP（对照插件 httpGet：20s 超时、错误按「网络异常 / HTTP」两类回报）
# ══════════════════════════════════════════════════════════════════════════
def _decode_body(raw, headers):
    enc = ""
    try:
        enc = (headers.get("Content-Encoding") or "").lower()
    except Exception:
        pass
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    charset = "utf-8"
    try:
        ct = headers.get("Content-Type") or ""
        mm = re.search(r"charset=([\w-]+)", ct, re.I)
        if mm:
            charset = mm.group(1)
    except Exception:
        pass
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _net_err_msg(e):
    reason = getattr(e, "reason", None)
    if reason is not None and not isinstance(reason, str):
        s = str(reason)
    else:
        s = reason if reason else str(e)
    if isinstance(e, TimeoutError) or "timed out" in (s or "").lower():
        return "请求超时"
    return s or e.__class__.__name__


def http_get(url):
    """返回 {'text': str, 'status': int|None, 'error': str|None}。
    错误前缀「网络异常：」表示网络层失败；「HTTP xxx」表示 HTTP 层失败（无正文）。"""
    req = urllib.request.Request(url, headers=XHS_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            text = _decode_body(raw, resp.headers)
            status = getattr(resp, "status", None) or resp.getcode()
            if status and status >= 400 and not text:
                return {"text": "", "status": status, "error": "HTTP %s" % status}
            return {"text": text, "status": status, "error": None}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        text = _decode_body(body, e.headers) if body else ""
        if e.code >= 400 and not text:
            return {"text": "", "status": e.code, "error": "HTTP %s" % e.code}
        return {"text": text, "status": e.code, "error": None}
    except Exception as e:
        return {"text": "", "status": None, "error": "网络异常：" + _net_err_msg(e)}


# ══════════════════════════════════════════════════════════════════════════
#  短链展开（对照插件 nodeFollow：逐跳跟随、6 跳上限、单跳 12s）
# ══════════════════════════════════════════════════════════════════════════
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟跳，交给 node_follow 逐跳记录


def node_follow(url, max_hop=MAX_HOP):
    """逐跳展开重定向，返回 {'chain': [...], 'err': str|None}（对照插件 nodeFollow）。"""
    chain = []
    result = {"chain": chain, "err": None}
    opener = urllib.request.build_opener(_NoRedirect)

    def hop(cur, n):
        if n >= max_hop:
            result["err"] = "重定向层数过多（>%d）" % max_hop
            return
        chain.append(cur)
        req = urllib.request.Request(cur, headers={
            "User-Agent": UA,
            "Referer": "https://www.xiaohongshu.com/",
            "Accept": "*/*",
        })
        try:
            with opener.open(req, timeout=SHORTLINK_TIMEOUT):
                pass  # 2xx：展开结束
        except urllib.error.HTTPError as e:
            loc = None
            try:
                loc = e.headers.get("Location") if e.headers else None
            except Exception:
                pass
            try:
                e.close()
            except Exception:
                pass
            if 300 <= e.code < 400 and loc:
                hop(urllib.parse.urljoin(cur, loc), n + 1)
            # 非重定向的 HTTP 错误 → resolve({chain, err: null})
        except Exception as e:
            result["err"] = _net_err_msg(e)

    try:
        hop(url, 0)
    except Exception as e:
        result["err"] = _net_err_msg(e)
    return result


# ══════════════════════════════════════════════════════════════════════════
#  链接解析（对照插件：parseItemId / parseSellerId / extractUrls / parseItemIdAny / parseAnyLink）
# ══════════════════════════════════════════════════════════════════════════
_RX_ITEM_ID = (
    re.compile(r"/goods-detail/([0-9a-fA-F]{16,32})"),
    re.compile(r"item_id=([0-9a-fA-F]{16,32})"),
    re.compile(r"goods/([0-9a-fA-F]{16,32})"),
)
_RX_SELLER_ID = (
    re.compile(r"/shop/([0-9a-fA-F]{16,32})"),
    re.compile(r"seller_id=([0-9a-fA-F]{16,32})"),
)
_RX_BARE_ID = re.compile(r"[0-9a-fA-F]{16,32}")
_RX_URLS = re.compile(r"https?://[^\s，,、；;）)】\]]+")
_RX_HEX_ID = re.compile(r"^[0-9a-fA-F]{16,32}$")


def parse_item_id(raw):
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return None
    for pat in _RX_ITEM_ID:
        m = pat.search(s)
        if m:
            return m.group(1)
    if _RX_HEX_ID.match(s):
        return s
    return None


def parse_seller_id(raw):
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return None
    for pat in _RX_SELLER_ID:
        m = pat.search(s)
        if m:
            return m.group(1)
    if _RX_HEX_ID.match(s):
        return s
    return None


def extract_urls(s):
    return _RX_URLS.findall(s or "")


def parse_item_id_any(raw):
    """从任意粘贴内容里找商品 ID。返回 {'id': str|None, 'why': str}。"""
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return {"id": None, "why": "内容是空的"}
    iid = parse_item_id(s)
    if iid:
        return {"id": iid, "why": "ok"}
    urls = extract_urls(s)
    for u in urls:
        iid = parse_item_id(u)
        if iid:
            return {"id": iid, "why": "ok"}
    if not urls:
        return {"id": None, "why": "里面没有 http 链接（只粘了文字？）"}
    last = ""
    for u in urls[:3]:
        nf = node_follow(u)
        chain, err = nf["chain"], nf["err"]
        if err and len(chain) <= 1:
            last = "展开失败：" + err
            continue
        if not chain:
            last = "展开失败：" + (err or "?")
            continue
        for c in chain:
            iid = parse_item_id(c)
            if iid:
                return {"id": iid, "why": "ok（短链展开后认到）"}
        for c in chain:
            if "/discovery/item/" in c or "/explore/" in c:
                return {"id": None, "why": "这是一篇「笔记」的链接，不是商品链接 —— 要去该商品的详情页分享，而不是笔记页"}
            if "/shop/" in c or "/user/profile/" in c:
                return {"id": None, "why": "这是「店铺 / 账号主页」的链接，不是单个商品 —— 要打开具体那个商品的详情页再分享"}
            if "/login" in c:
                return {"id": None, "why": "短链展开后被跳到登录页（网页端拿不到商品地址）—— 口令可能已失效，或该分享只认 App。最稳的替代：用电脑浏览器打开商品详情页、把地址栏整条复制过来；也可以直接粘商品 ID"}
        last = "展开后是：" + chain[-1][:140]
    return {"id": None, "why": last or "认不出商品 ID"}


def parse_any_link(raw):
    """从任意粘贴内容里找商品 ID 或 店铺 ID。返回 {'kind':'item'|'seller'|None, 'id', 'why'}。"""
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return {"kind": None, "id": None, "why": "内容是空的"}

    def hit(txt):
        i = parse_item_id(txt)
        if i:
            return ("item", i)
        sid = parse_seller_id(txt)
        if sid:
            return ("seller", sid)
        return (None, None)

    kv = hit(s)
    if kv[0]:
        return {"kind": kv[0], "id": kv[1], "why": "ok"}
    urls = extract_urls(s)
    for u in urls:
        kv = hit(u)
        if kv[0]:
            return {"kind": kv[0], "id": kv[1], "why": "ok"}
    if not urls:
        return {"kind": None, "id": None, "why": "里面没有 http 链接（只粘了文字？）"}
    last = ""
    for u in urls[:3]:
        nf = node_follow(u)
        chain, err = nf["chain"], nf["err"]
        if err and len(chain) <= 1:
            last = "展开失败：" + err
            continue
        if not chain:
            last = "展开失败：" + (err or "?")
            continue
        for c in chain:
            kv = hit(c)
            if kv[0]:
                return {"kind": kv[0], "id": kv[1], "why": "ok（短链展开后认到）"}
        for c in chain:
            if "/discovery/item/" in c or "/explore/" in c:
                return {"kind": None, "id": None, "why": "这是「笔记」链接，不是商品/店铺链接"}
            if "/login" in c:
                return {"kind": None, "id": None, "why": "短链展开后被跳到登录页"}
        last = "展开后是：" + chain[-1][:140]
    return {"kind": None, "id": None, "why": last or "认不出商品或店铺 ID"}


# ══════════════════════════════════════════════════════════════════════════
#  商品详情（TOC 接口，对照插件 deepFindSold / idLastDigitPlus1 / fetchGoods）
# ══════════════════════════════════════════════════════════════════════════
def deep_find_sold(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if (k == "itemAnalysisDataText" or k == "spuAnalysisDataText") \
                    and isinstance(v, str) and "已售" in v:
                return v
            r = deep_find_sold(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = deep_find_sold(v)
            if r:
                return r
    return None


def id_last_digit_plus1(item_id):
    """末位 hex +1（对照插件 idLastDigitPlus1：店铺页 itemId 与详情页 ID 的已知偏差修正）。"""
    if not item_id:
        return None
    head, last = item_id[:-1], item_id[-1]
    try:
        n = int(last, 16)
    except ValueError:
        return None
    return head + format((n + 1) % 16, "x")


def fetch_goods(item_id):
    url = TOC_API + "?version=0.0.5&item_id=" + urllib.parse.quote(item_id, safe="")
    r = http_get(url)
    if r["error"]:
        return {"rec": None, "err": r["error"]}
    text = r["text"]
    try:
        data = json.loads(text)
    except Exception:
        return {"rec": None, "err": "返回非 JSON：" + str(text)[:100]}
    if not isinstance(data, dict) or not data.get("success"):
        code = data.get("error_code") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else None
        return {"rec": None, "err": "[" + str(code) + "] " + str(msg)}

    blocks = (data.get("data") or {}).get("template_data") or []
    b = blocks[0] if blocks else {}
    desc = b.get("descriptionH5") or {}
    seller = b.get("sellerH5") or {}
    price_h5 = b.get("priceH5") or {}
    sku = b.get("skuInfo") or {}
    sel = b.get("selectedH5") or {}

    sold_txt = first(price_h5, "itemAnalysisDataText", "spuAnalysisDataText") \
        or first(desc, "itemAnalysisDataText") or deep_find_sold(b)

    deal = None
    dp = (b.get("profitBarV1") or {}).get("dealPrice")
    if isinstance(dp, dict):
        deal = dp.get("price")
    if deal is None:
        deal = (((b.get("profitBarPopupH5") or {}).get("formula") or {})
                .get("result") or {}).get("price")

    dist = b.get("goodsDistributeV4") or {}
    svc = [x.get("name") for x in (((b.get("serviceV5") or {}).get("list")) or [])
           if isinstance(x, dict) and x.get("name")]

    def tag_text(x):
        if isinstance(x, dict):
            return str(x.get("text") or x.get("name") or x.get("title") or "")
        return "" if x is None else str(x)

    tags = [tag_text(x) for x in (((b.get("descriptionMain") or {}).get("tags")) or [])]
    tags = tags + [tag_text(x) for x in (price_h5.get("tags") or [])]
    tags = [t for t in tags if t]
    promo = [t for t in ([tag_text(x) for x in (((b.get("profitBarV1") or {}).get("tags")) or [])]) if t]

    # SKU 数：data.common_data.statisticInfo.skuNum
    # 注意：common_data 可能是**转义的 JSON 字符串**（'{"statisticInfo":{"skuNum":4}}'）——
    # 字符串先 JSON.parse、对象直接用，两种形态都吃（否则 SKU 数恒为空）。
    stat = {}
    droot = data.get("data")
    if isinstance(droot, dict):
        cd = droot.get("common_data")
        if isinstance(cd, str) and cd:
            try:
                cd = json.loads(cd)
            except Exception:
                cd = None
        if isinstance(cd, dict):
            st = cd.get("statisticInfo")
            if isinstance(st, dict):
                stat = st

    dist_time = dist.get("time")
    rec = {
        "item_id": item_id,
        "标题": first(desc, "name"),
        "已售": to_int(sold_txt),
        "已售原文": sold_txt,
        "现价": price_h5.get("highlightPrice"),
        "到手价": deal,
        "发货地": dist.get("location"),
        "运费": dist.get("fee"),
        "发货时效": dist_time.get("text") if isinstance(dist_time, dict) else None,
        "服务标签": "、".join(svc) or None,
        "商品标签": "、".join(tags) or None,
        "促销标签": "、".join(promo) or None,
        "店铺名": first(seller, "name"),
        "seller_id": first(seller, "id"),
        "店铺链接": first(seller, "link"),
        "店铺评分": first(seller, "sellerScore", "grade"),
        "店铺总销量": to_int(first(seller, "salesVolume")),
        "粉丝数": to_int(first(seller, "fansAmount")),
        "库存状态": first(sku, "stockStatus"),
        "选中SKU": first(sel, "text"),
        "SKU数": stat.get("skuNum"),
        "抓取时间": now_str(),
    }
    return {"rec": rec, "err": None}


# ══════════════════════════════════════════════════════════════════════════
#  店铺页（SSR，对照插件 matchBrace / parseSoldText / fetchShopItems）
# ══════════════════════════════════════════════════════════════════════════
def match_brace(s, start):
    """从 s[start]（应为 '{'）起做花括号配对，返回配对 '}' 的下标；失败返回 -1。"""
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_sold_text(txt):
    """解析「已售1.5万+」类文案，返回 [数值|None, 清洗后原文, 是否下界]（对照插件 parseSoldText）。"""
    if not txt:
        return [None, None, None]
    s = str(txt).strip()
    m = re.search(r"([\d.]+)\s*(万)?",
                  s.replace("已售", "").replace("人购买", "").replace("人加购", ""))
    if not m:
        return [None, s, None]
    v = _parse_float_js(m.group(1))
    if v is None:
        return [None, s, s.endswith("+")]
    if m.group(2):
        v *= 10000
    return [int(v), s, s.endswith("+")]


def fetch_shop_items(seller_id):
    r = http_get(SHOP_URL + seller_id)
    raw, error = r["text"], r["error"]
    if error:
        return {"info": None, "err": error}
    if not raw or len(raw) < 2000 or raw.find("product-item") < 0:
        return {"info": None, "err": "店铺页未返回商品数据（可能店铺不存在或结构已变）"}

    m = re.search(r'class="shop-name"[^>]*>([^<]{1,60})<', raw) \
        or re.search(r'"shopName"\s*:\s*"([^"]+)"', raw)
    shop_name = html_unescape(m.group(1)).strip() if m else None
    m = re.search(r"已售\s*([\d.]+万?\+?)", raw)
    shop_total_txt = ("已售" + m.group(1)) if m else None
    m = re.search(r'"content"\s*:\s*"粉丝\s*([\d.]+万?)"', raw) \
        or re.search(r"粉丝\s*([\d.]+万?)", raw)
    fans_txt = m.group(1) if m else None

    # A 侧：可见 DOM 卡片
    bounds = [mm.start() for mm in re.finditer(r'class="product-item"', raw)]
    bounds.append(len(raw))
    dom_rows = []
    for k in range(len(bounds) - 1):
        card = raw[bounds[k]:bounds[k + 1]]
        t = re.search(r'class="title-content"[^>]*>([^<]+)<', card)
        p = re.search(r'class="num"[^>]*>([\d.]+)<', card)
        s_ = re.search(r'class="sold-num"[^>]*>([^<]+)<', card)
        badges = []
        for bm in re.finditer(r'class="text-item"[^>]*>([^<]+)<', card):
            badges.append(html_unescape(bm.group(1)).strip())
        pv = _parse_float_js(p.group(1)) if p else None
        dom_rows.append({
            "标题": html_unescape(t.group(1)).strip() if t else None,
            "价格": pv,
            "已售原文": html_unescape(s_.group(1)).strip() if s_ else None,
            "角标": badges,
        })

    # B 侧：内嵌 JSON（baseInfo，花括号配对取出后 JSON 解析）
    json_rows = []
    seen = set()
    for bm in re.finditer(r'"baseInfo"\s*:\s*\{', raw):
        start = bm.start() + len(bm.group(0)) - 1
        end = match_brace(raw, start)
        if end < 0:
            continue
        blob = raw[start:end + 1]
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        iid = obj.get("itemId")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        ts = obj.get("itemOnShelfTime")
        lm = re.search(r'"link"\s*:\s*"[^"]*?/goods/([0-9a-fA-F]{16,32})', blob)
        valid = isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 946684800
        img = obj.get("image")
        json_rows.append({
            "item_id": id_last_digit_plus1(iid) or iid,
            "item_id_店铺页原值": iid,
            "候选ID": [iid, id_last_digit_plus1(iid), lm.group(1) if lm else None],
            "标题": (html_unescape(obj.get("title") or "")).strip() or None,
            "上架时间戳": ts,
            "上架日期": fmt_date(ts) if valid else None,
            "在架天数": int((time.time() - ts) / 86400) if valid else None,
            "图片": (img or {}).get("url") if isinstance(img, dict) else None,
        })

    # 用标题连接两侧（DOM 卡片为骨架，JSON 补 item_id / 上架信息）
    by_title = {}
    for j in json_rows:
        if j["标题"] and j["标题"] not in by_title:
            by_title[j["标题"]] = j
    items = []
    unmatched = []
    for d in dom_rows:
        row = dict(d)
        ps = parse_sold_text(d["已售原文"])
        row["已售"] = ps[0]
        row["已售原文"] = ps[1]
        row["已售是下界"] = ps[2]
        j = by_title.get(d["标题"]) if d["标题"] else None
        if j:
            for kk in ("item_id", "候选ID", "上架时间戳", "上架日期", "在架天数", "图片"):
                row[kk] = j[kk]
        else:
            row["item_id"] = None
            unmatched.append("(仅 DOM) " + (d["标题"] or "?"))
        items.append(row)
    dom_titles = set(d["标题"] for d in dom_rows)
    for j in json_rows:
        if j["标题"] and j["标题"] not in dom_titles:
            unmatched.append("(仅 JSON) " + j["标题"])
    if len(dom_rows) != len(json_rows):
        unmatched.append("⚠ 两侧条数不一致（DOM %d / JSON %d）—— 配对可能错位，已售请人工复核"
                         % (len(dom_rows), len(json_rows)))

    total_v = parse_sold_text(shop_total_txt)[0] if shop_total_txt else None
    m = re.search(r'"content"\s*:\s*"好评率\s*([\d.]+%)"', raw)
    good_rate = m.group(1) if m else None
    m = re.search(r'"content"\s*:\s*"(平均\s*[\d.]+\s*(?:小时|天)发货)"', raw)
    ship_time = m.group(1) if m else None

    return {"info": {
        "seller_id": seller_id,
        "店铺名": shop_name,
        "店铺总销量": total_v,
        "店铺总销量原文": shop_total_txt,
        "粉丝数": to_int(fans_txt) if fans_txt else None,
        "粉丝数原文": ("粉丝数 " + fans_txt) if fans_txt else None,
        "好评率": good_rate,
        "发货时效": ship_time,
        "商品": items,
        "未匹配": unmatched,
    }, "err": None}


# ══════════════════════════════════════════════════════════════════════════
#  商品级销量补正（对照插件 shopSoldMap / shelfTsFromItemId / applyShopSold）
# ══════════════════════════════════════════════════════════════════════════
_SHOP_CACHE = {}


def shop_sold_map(seller_id):
    if not seller_id:
        return {}
    if seller_id in _SHOP_CACHE:
        return _SHOP_CACHE[seller_id]
    got = fetch_shop_items(seller_id)
    mp = {}
    if got["info"]:
        for it in (got["info"].get("商品") or []):
            if it.get("item_id"):
                mp[it["item_id"]] = it
    _SHOP_CACHE[seller_id] = mp
    return mp


def shelf_ts_from_item_id(item_id):
    """由商品 ID 前 8 位 hex 推断上架时间（秒级 Unix 时间戳）。拿不到返回 None。"""
    s = "" if item_id is None else str(item_id)
    if len(s) < 8:
        return None
    try:
        ts = int(s[:8], 16)
    except ValueError:
        return None
    return ts if 1420070400 < ts < 2051222400 else None


def apply_shop_sold(rec):
    """店铺页口径覆盖 API 的 SKU 级销量（对照插件 applyShopSold）。"""
    sm = shop_sold_map(rec.get("seller_id"))
    hit = sm.get(rec.get("item_id"))
    rec["已售_SKU级"] = rec.get("已售")
    rec["已售原文_SKU级"] = rec.get("已售原文")
    if hit:
        rec["已售"] = hit.get("已售")
        rec["已售原文"] = hit.get("已售原文")
        rec["已售是下界"] = hit.get("已售是下界")
        rec["已售口径"] = "商品级（店铺页）"
        for k in ("上架日期", "在架天数", "角标"):
            hk = hit.get(k)
            if hk is not None and _js_falsy(rec.get(k)):
                rec[k] = hk
    else:
        rec["已售是下界"] = None
        rec["已售口径"] = "SKU级（API）· 该品不在店铺首页前 6，口径有偏差，仅供参考"
    if not rec.get("上架日期"):
        ts = shelf_ts_from_item_id(rec.get("item_id"))
        if ts:
            rec["上架日期"] = fmt_date(ts)
            rec["在架天数"] = int((time.time() - ts) / 86400)
            rec["上架来源"] = "商品ID推断"
    if rec.get("上架日期") and not rec.get("上架来源"):
        rec["上架来源"] = "店铺页"
    return rec


# ══════════════════════════════════════════════════════════════════════════
#  归一化（对照插件 normalize：archive / track / extra / warnings 四分）
# ══════════════════════════════════════════════════════════════════════════
_KNOWN_KEYS = {"标题", "现价", "到手价", "发货地", "店铺评分", "在架天数", "已售",
               "SKU数", "选中SKU", "店铺链接", "角标", "上架日期", "店铺名",
               "店铺总销量", "粉丝数"}


def normalize(row):
    archive = {
        "商品标题": to_str(row.get("标题")),
        "商品售价": to_num(row.get("现价")),
        "对标到手价": to_num(row.get("到手价")),
        "发货地": to_str(row.get("发货地")),
        "店铺评分": to_num(row.get("店铺评分")),
        "上架天数": to_num(row.get("在架天数")),
        "上架日期": to_str(row.get("上架日期")),  # 与「上架天数」并存（日期精确）
        "来源": "小红书市集",  # 固定值
        "SKU数": to_num(row.get("SKU数")),      # 接口不保证有 → 没有就留空
        "选中SKU": to_str(row.get("选中SKU")),
        "店铺链接": to_str(row.get("店铺链接")),
        "店铺名": to_str(row.get("店铺名")),
        "店铺总销量": to_num(row.get("店铺总销量")),
        "粉丝数": to_num(row.get("粉丝数")),
        "商品角标": ("、".join([x for x in row["角标"] if x]) or None)
                    if isinstance(row.get("角标"), list) else to_str(row.get("角标")),
    }
    track = {
        "商品销量": to_num(row.get("已售")),  # 变动项 → 每日跟踪
    }
    # 采集规格**之外**的键：原样保留在 extra，供人查看（不擅自写库）
    extra = {}
    for k, v in row.items():
        if k not in _KNOWN_KEYS:
            extra[k] = v

    warnings = []
    if row.get("已售是下界"):
        warnings.append("已售是下界（原文「" + (row.get("已售原文") or "") + "」），不是精确值")
    if row.get("已售口径") and "SKU级" in str(row.get("已售口径")):
        warnings.append("已售口径是 SKU 级（只有当前选中 SKU 的量），不是商品累计")
    if not any(re.search(r"开业|开店", k) for k in row.keys()):
        # 「开店天数」网页端采不到（只在 App「店铺详情」页有）→ 如实留空、不猜
        warnings.append("「开店天数」网页端采不到（该字段只在 App 的「店铺详情」页有）→ 留空，需要手填")
    return {"archive": archive, "track": track, "extra": extra, "warnings": warnings}


# ══════════════════════════════════════════════════════════════════════════
#  Markdown 输出 / 落盘（对照插件 safeFileName / resultMarkdown / upsertTrackRow /
#  findNoteByItemId / mergeCollectedNote / writeNoteToVault / saveResultNote）
# ══════════════════════════════════════════════════════════════════════════
_RX_FN_BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')
_RX_FN_WS = re.compile(r"[\r\n\t]+")
_RX_WS = re.compile(r"\s+")


def safe_file_name(s):
    t = "" if s is None else str(s)
    t = _RX_FN_BAD.sub(" ", t)
    t = _RX_FN_WS.sub(" ", t)
    t = _RX_WS.sub(" ", t)
    return t.strip()[:80]


def track_row(day, val):
    return "| " + str(day) + " | " + str(val) + " |"


def sold_cell(r):
    v = (r.get("track") or {}).get("商品销量")
    if v is None:
        return None
    raw = r.get("raw") or {}
    return str(v) + ("+" if raw.get("已售是下界") else "")  # 下界（「已售1.5万+」）标个 +，别当精确值


def _one(v):
    return re.sub(r"[\r\n]+", " ", "" if v is None else str(v))


def _yaml(obj):
    out = []
    for k, v in obj.items():
        out.append(k + ': "' + _one(v).replace('"', '\\"') + '"')
    return "\n".join(out)


def result_markdown(r):
    if r.get("kind") == "shop":
        s = r.get("shop") or {}
        head = {
            "type": "店铺采集", "店铺名": s.get("店铺名"), "seller_id": s.get("seller_id"),
            "店铺总销量": s.get("店铺总销量"), "粉丝数": s.get("粉丝数"),
            "好评率": s.get("好评率"), "发货时效": s.get("发货时效"), "采集时间": now_str(),
        }
        md = "---\n" + _yaml(head) + "\n---\n\n# " + (s.get("店铺名") or "店铺采集") + "（店铺采集）\n\n"
        ps = r.get("products") or []
        md += "## 首页可见商品（" + str(len(ps)) + " 个）\n\n"
        if ps:
            md += "| 标题 | 价格 | 已售 | 上架日期 | 上架天数 | 角标 |\n|---|---|---|---|---|---|\n"
            for p in ps:
                title = str(p.get("标题") or p.get("item_id") or "—").replace("|", "\\|")
                price = "—" if p.get("价格") is None else p.get("价格")
                sold = p.get("已售原文") or ("—" if p.get("已售") is None else p.get("已售"))
                days = "—" if p.get("上架天数") is None else p.get("上架天数")
                md += ("| " + title + " | " + str(price) + " | " + str(sold) + " | " +
                       str(p.get("上架日期") or "—") + " | " + str(days) + " | " +
                       str(p.get("角标") or "—") + " |\n")
        else:
            md += "（该店首页可见商品 0 个）\n"
        return md

    a = r.get("archive") or {}
    t = r.get("track") or {}
    head = {"type": "商品采集", "商品ID": r.get("itemId"), "采集时间": r.get("抓取时间") or now_str()}
    head.update(a)
    head.update(t)
    md = "---\n" + _yaml(head) + "\n---\n\n# " + (a.get("商品标题") or r.get("itemId") or "商品采集") + "\n\n"
    if r.get("itemId"):
        md += "商品链接：https://www.xiaohongshu.com/goods-detail/" + str(r.get("itemId")) + "\n"
    sc = sold_cell(r)
    if sc is not None:
        md += "\n" + TRACK_HEAD + "\n\n" + TRACK_COLS + "\n|---|---|\n" + track_row(now_str()[:10], sc) + "\n"
    return md


def find_note_by_item_id(folder, item_id):
    """在目标目录内按 frontmatter 的「商品ID」找同商品的已有笔记（找不到返回 None）。
    只在给定目录内找（含子目录），不扫全盘。"""
    needle = '商品ID: "' + str(item_id) + '"'
    p = Path(folder)
    if not p.is_dir():
        return None
    for f in sorted(p.rglob("*.md")):
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(800)
        except OSError:
            continue
        if needle in head:
            return f
    return None


def upsert_track_row(body, day, val):
    """在正文里 upsert「每日跟踪」表：今天已有行就改值，没有就追加一行（对照插件 upsertTrackRow）。"""
    row = track_row(day, val)
    lines = body.split("\n")
    hi = -1
    for i, l in enumerate(lines):
        if l.strip() == TRACK_HEAD:
            hi = i
            break
    if hi < 0:
        return body.rstrip() + "\n\n" + TRACK_HEAD + "\n\n" + TRACK_COLS + "\n|---|---|\n" + row + "\n"
    start = -1
    end = -1
    for i in range(hi + 1, len(lines)):
        t = lines[i].strip()
        if t.startswith("|"):
            if start < 0:
                start = i
            end = i
        elif start >= 0:
            break
    if start < 0:
        lines[hi + 1:hi + 1] = ["", TRACK_COLS, "|---|---|", row]
        return "\n".join(lines)
    hit = -1
    rx = re.compile(r"^\|\s*" + re.escape(day) + r"\s*\|")
    for i in range(start, end + 1):
        if rx.match(lines[i]):
            hit = i
            break
    if hit >= 0:
        lines[hit] = row
    else:
        lines.insert(end + 1, row)
    return "\n".join(lines)


def merge_collected_note(old_txt, res, fresh_md):
    """同商品二次采集：刷新 frontmatter + 正文「每日跟踪」upsert 今天那行；正文手工内容保留。"""
    m = re.match(r"^---\n[\s\S]*?\n---\n", fresh_md)
    new_fm = m.group(0) if m else ""
    body = re.sub(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", "", old_txt, count=1)
    sc = sold_cell(res)
    if sc is not None:
        body = upsert_track_row(body, now_str()[:10], sc)
    body = re.sub(r"^\s*\n", "\n", body, count=1)
    return new_fm + body


def write_note(folder, base_name, content):
    """写入 .md（自动建目录、重名加序号）；返回实际路径。保持 LF 行尾。"""
    p = Path(folder)
    p.mkdir(parents=True, exist_ok=True)
    base = safe_file_name(base_name) or "商品采集"
    path = p / (base + ".md")
    i = 1
    while path.exists():
        path = p / (base + "-" + str(i) + ".md")
        i += 1
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


def save_result(res, folder):
    """落盘采集结果；同商品复采 → 更新原文件（刷新 frontmatter + upsert 当天跟踪行）。"""
    is_shop = res.get("kind") == "shop"
    if is_shop:
        shop = res.get("shop") or {}
        name = "店铺-" + (shop.get("店铺名") or shop.get("seller_id") or "采集")
    else:
        name = ((res.get("archive") or {}).get("商品标题") or res.get("itemId") or "商品采集")
    if not is_shop and res.get("itemId"):
        prev = find_note_by_item_id(folder, res["itemId"])
        if prev is not None:
            old = prev.read_text(encoding="utf-8", errors="replace")
            prev.write_text(merge_collected_note(old, res, result_markdown(res)),
                            encoding="utf-8", newline="")
            return {"path": str(prev), "updated": True}
    path = write_note(folder, name, result_markdown(res))
    return {"path": path, "updated": False}


# ══════════════════════════════════════════════════════════════════════════
#  采集主流程（对照插件 collect / collectShop）
# ══════════════════════════════════════════════════════════════════════════
def collect(input_raw):
    """采集一个商品（链接 / ID / 分享文案均可）；认出是店铺链接时自动转店铺采集。"""
    raw = to_str(input_raw)

    def fail(error):
        return {"ok": False, "itemId": None, "链接": raw, "archive": {}, "track": {},
                "extra": {}, "warnings": [], "error": error, "raw": None}

    if not raw:
        return fail("没给链接或商品 ID")
    parsed = parse_item_id_any(raw)
    if not parsed["id"]:
        # 认出是「店铺 / 账号主页」→ 转店铺采集
        if re.search(r"店铺|账号主页", parsed.get("why") or ""):
            return collect_shop(raw)
        return fail("认不出商品链接 —— " + (parsed.get("why") or "认不出商品 ID"))

    got = fetch_goods(parsed["id"])
    if not got["rec"]:
        return fail("没采到：" + str(got["err"]))
    rec = got["rec"]
    try:
        apply_shop_sold(rec)  # 店铺页补正失败不影响主记录
    except Exception:
        pass

    n = normalize(rec)
    return {
        "ok": True,
        "kind": "item",
        "itemId": rec.get("item_id") or parsed["id"],
        "链接": raw,
        "店铺链接": rec.get("店铺链接") or None,
        "sellerId": rec.get("seller_id") or None,
        "archive": n["archive"],
        "track": n["track"],
        "extra": n["extra"],
        "warnings": n["warnings"],
        "抓取时间": rec.get("抓取时间") or None,
        "raw": rec,
    }


def collect_shop(raw, force=False):
    """店铺采集：店铺页地址 / 店铺分享 → 店铺级信息 + 首页可见商品列表。
    force=True（CLI --shop）时：裸 ID 按店铺 ID 处理（默认按商品 ID）。"""
    raw = to_str(raw)
    p = parse_any_link(raw)
    if not p["id"]:
        return {"ok": False, "kind": "shop", "error": "无法解析店铺 —— " + (p.get("why") or "认不出店铺 ID")}
    if p["kind"] == "item":
        if force and _RX_HEX_ID.match((raw or "").strip()):
            p = {"kind": "seller", "id": (raw or "").strip(), "why": "ok（--shop 显式按店铺 ID 处理）"}
        else:
            return {"ok": False, "kind": "shop", "error": "这是商品链接 —— 采单品请去掉 --shop（默认模式会自动识别）"}

    got = fetch_shop_items(p["id"])
    if not got["info"]:
        return {"ok": False, "kind": "shop", "error": "店铺采集失败 —— " + str(got["err"])}
    s = got["info"]
    prods = s.get("商品") or []
    warnings = ["「开店天数」网页端采不到（店铺页数据里没有这个字段）→ 档案留空，需要手填"]
    if not prods:
        warnings.append("该店首页可见商品 0 个 —— 建不了候选品，可能需要从商品详情页分享")
    return {
        "ok": True,
        "kind": "shop",
        "itemId": None,
        "链接": raw,
        "sellerId": s.get("seller_id") or None,
        "shop": {
            "店铺名": s.get("店铺名") or None,
            "seller_id": s.get("seller_id") or None,
            "店铺总销量": s.get("店铺总销量"),
            "店铺总销量原文": s.get("店铺总销量原文") or None,
            "粉丝数": s.get("粉丝数"),
            "粉丝数原文": s.get("粉丝数原文") or None,
            "好评率": s.get("好评率") or None,
            "发货时效": s.get("发货时效") or None,
        },
        "products": [{
            "item_id": q.get("item_id") or None,
            "标题": q.get("标题") or None,
            "价格": q.get("价格"),
            "已售": q.get("已售"),
            "已售原文": q.get("已售原文") or None,
            "已售是下界": bool(q.get("已售是下界")),
            "上架日期": q.get("上架日期") or None,
            "上架天数": q.get("在架天数"),
            "角标": ("、".join([x for x in q["角标"] if x]) or None)
                    if isinstance(q.get("角标"), list) else (q.get("角标") or None),
        } for q in prods],
        "warnings": warnings,
        "抓取时间": now_str(),
        "raw": got["info"],
    }


# ══════════════════════════════════════════════════════════════════════════
#  自检（不发真实业务请求）
# ══════════════════════════════════════════════════════════════════════════
def run_selftest():
    checks = []

    def add(name, fn):
        try:
            got = fn()
            if got is True:
                checks.append({"name": name, "ok": True})
            else:
                checks.append({"name": name, "ok": False, "got": got})
        except Exception as e:
            checks.append({"name": name, "ok": False, "error": (str(e) or e.__class__.__name__)})

    def eq(actual, expected):
        return True if actual == expected else {"actual": actual, "expected": expected}

    add("python >= 3.7", lambda: True if sys.version_info >= (3, 7) else {"v": sys.version.split()[0]})

    add("parse_item_id: goods-detail URL",
        lambda: eq(parse_item_id("https://www.xiaohongshu.com/goods-detail/67a00b00cdef1234a0987654"),
                   "67a00b00cdef1234a0987654"))
    add("parse_item_id: 裸 ID",
        lambda: eq(parse_item_id("67a00b00cdef1234a0987654"), "67a00b00cdef1234a0987654"))
    add("parse_item_id: item_id 参数",
        lambda: eq(parse_item_id("https://mall.xiaohongshu.com/x?item_id=abcdef1234567890&y=1"),
                   "abcdef1234567890"))
    add("parse_seller_id: shop URL",
        lambda: eq(parse_seller_id("https://www.xiaohongshu.com/shop/0123456789abcdef01234567"),
                   "0123456789abcdef01234567"))
    add("extract_urls: 2 条",
        lambda: eq(len(extract_urls("看这个 https://xhslink.com/a1b2c3 ，和 "
                                    "https://www.xiaohongshu.com/goods-detail/67a00b00cdef1234a0987654")), 2))
    add("parse_item_id_any（无网络路径）",
        lambda: eq(parse_item_id_any("https://www.xiaohongshu.com/goods-detail/67a00b00cdef1234a0987654").get("id"),
                   "67a00b00cdef1234a0987654"))
    add("parse_any_link（店铺 URL）",
        lambda: eq(parse_any_link("https://www.xiaohongshu.com/shop/0123456789abcdef01234567").get("kind"),
                   "seller"))

    def _t_ints():
        if to_int("1.5万+") != 15000 or to_int("2,345") != 2345 or to_int("9402") != 9402:
            return {"v": [to_int("1.5万+"), to_int("2,345"), to_int("9402")]}
        return True
    add("to_int（含「万」/千分位）", _t_ints)

    def _t_nums():
        if to_num("¥12.9") != 12.9 or to_num("") is not None or to_num("abc") is not None:
            return {"v": [to_num("¥12.9"), to_num(""), to_num("abc")]}
        return True
    add("to_num", _t_nums)

    def _t_tostr():
        if to_str("  x  ") != "x" or to_str("") is not None or to_str(None) is not None:
            return {"v": [to_str("  x  "), to_str(""), to_str(None)]}
        return True
    add("to_str", _t_tostr)

    add("html_unescape（单轮链式）", lambda: eq(html_unescape("&lt;b&gt;&amp;amp;"), "<b>&amp;"))
    add("parse_sold_text（1.5万+ 下界）",
        lambda: eq(parse_sold_text("已售1.5万+"), [15000, "已售1.5万+", True]))
    add("safe_file_name", lambda: eq(safe_file_name('a/b*c?d"e<f>g|h#i^j[k]l'), "a b c d e f g h i j k l"))
    add("match_brace 花括号配对", lambda: eq(match_brace('{"a":{"b":1}}', 0), 12))
    add("id_last_digit_plus1", lambda: eq(id_last_digit_plus1("abc123f"), "abc1230"))

    def _t_shelf():
        ts = shelf_ts_from_item_id("67a00b00cdef1234a0987654")
        return True if (ts is not None and 1420070400 < ts < 2051222400) else {"ts": ts}
    add("shelf_ts_from_item_id（前8位hex→时间戳）", _t_shelf)

    def _t_falsy():
        bad = [v for v in [None, False, 0, "", 0.0] if not _js_falsy(v)]
        good = [v for v in ["x", 1, -1, [], {}, True] if _js_falsy(v)]
        return True if (not bad and not good) else {"bad": bad, "good": good}
    add("_js_falsy（JS 语义：空数组/空对象为真）", _t_falsy)

    def _t_upsert():
        body = "# T\n\n商品链接：https://x\n"
        out1 = upsert_track_row(body, "2026-10-10", "100")
        if "## 每日跟踪（变动）" not in out1 or "| 2026-10-10 | 100 |" not in out1:
            return {"step": "new", "out": out1}
        out2 = upsert_track_row(out1, "2026-10-10", "200")
        if "| 2026-10-10 | 200 |" not in out2 or "| 2026-10-10 | 100 |" in out2:
            return {"step": "update", "out": out2}
        out3 = upsert_track_row(out2, "2026-10-11", "300")
        if out3.count("| 2026-10-1") != 2:
            return {"step": "append", "out": out3}
        return True
    add("upsert_track_row（新建/更新/追加）", _t_upsert)

    def _t_normalize():
        n = normalize({"标题": "T", "现价": "12.9", "已售": 100, "已售原文": "已售100+",
                       "已售是下界": True, "已售口径": "SKU级（API）· 该品不在店铺首页前 6，口径有偏差，仅供参考"})
        if n["archive"].get("商品标题") != "T" or n["archive"].get("商品售价") != 12.9:
            return {"archive": n["archive"]}
        if n["archive"].get("来源") != "小红书市集" or n["track"].get("商品销量") != 100:
            return {"archive": n["archive"], "track": n["track"]}
        if len(n["warnings"]) != 3 or "已售原文" not in n["extra"]:
            return {"warnings": n["warnings"], "extra_keys": sorted(n["extra"].keys())}
        return True
    add("normalize（四分结构 + 3 条警告）", _t_normalize)

    def _t_md_item():
        r = {"kind": "item", "itemId": "67a00b00cdef1234a0987654", "抓取时间": now_str(),
             "archive": {"商品标题": "测试商品", "商品售价": 39.9},
             "track": {"商品销量": 100}, "raw": {"已售是下界": False}}
        md = result_markdown(r)
        ok = (md.startswith("---\n") and 'type: "商品采集"' in md
              and '商品ID: "67a00b00cdef1234a0987654"' in md and "# 测试商品" in md
              and "商品链接：https://www.xiaohongshu.com/goods-detail/67a00b00cdef1234a0987654" in md
              and "## 每日跟踪（变动）" in md and "| " + now_str()[:10] + " | 100 |" in md)
        return True if ok else {"md": md}
    add("result_markdown: item 结构", _t_md_item)

    def _t_md_shop():
        r = {"kind": "shop",
             "shop": {"店铺名": "测试店", "seller_id": "abc", "店铺总销量": 100, "粉丝数": 50,
                      "好评率": "99%", "发货时效": "平均6小时发货"},
             "products": [{"item_id": "id1", "标题": "商品A", "价格": 9.9, "已售": 10,
                           "已售原文": "已售10+", "上架日期": "2026-01-01", "上架天数": 100, "角标": "新品"}]}
        md = result_markdown(r)
        ok = ('type: "店铺采集"' in md and "# 测试店（店铺采集）" in md
              and "## 首页可见商品（1 个）" in md and "| 商品A | 9.9 | 已售10+ |" in md)
        return True if ok else {"md": md}
    add("result_markdown: shop 结构", _t_md_shop)

    def _t_merge():
        res = {"kind": "item", "itemId": "abc123", "track": {"商品销量": 222},
               "raw": {"已售是下界": False}}
        old = ('---\ntype: "商品采集"\n商品ID: "abc123"\n---\n\n# T\n\n## 每日跟踪（变动）\n\n'
               '| 日期 | 商品销量 |\n|---|---|\n| 2026-10-09 | 111 |\n')
        out = merge_collected_note(old, res, result_markdown(res))
        ok = (out.startswith("---\n") and '商品ID: "abc123"' in out
              and "| 2026-10-09 | 111 |" in out and "| " + now_str()[:10] + " | 222 |" in out)
        return True if ok else {"out": out}
    add("merge_collected_note（fm 刷新 + 跟踪行 upsert）", _t_merge)

    def _t_net():
        try:
            import ssl as _ssl  # noqa: F811
            import urllib.request as _ur  # noqa: F401, F811
            ctx = _ssl.create_default_context()
            return True if ctx is not None else False
        except Exception as e:
            return {"error": str(e)}
    add("网络模块就绪（urllib + ssl）", _t_net)

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "engine": ENGINE_NAME, "python": sys.version.split()[0], "checks": checks}


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════
def _force_utf8_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _exit_code_for(res):
    err = str(res.get("error") or "")
    if "网络异常" in err:
        return 3
    if (err.startswith("认不出商品链接") or err.startswith("无法解析店铺")
            or err.startswith("没给链接") or err.startswith("这是商品链接")):
        return 1
    return 2


class _ArgParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(1)  # 用法错误归入「解析失败」类（不用 argparse 默认的 2）


def main(argv=None):
    _force_utf8_stdio()
    ap = _ArgParser(
        prog="collect.py",
        description="小红书公开商品/店铺数据采集（免登录 / 免 Cookie / 纯标准库）",
    )
    ap.add_argument("input", nargs="?", help="商品链接 / 商品 ID / 店铺链接 / 店铺 ID / 分享文案")
    ap.add_argument("--md", action="store_true", help="以 Markdown 格式打到 stdout（默认 JSON）")
    ap.add_argument("--save", metavar="DIR", help="落盘 .md 到指定目录（同商品复采自动更新原文件）")
    ap.add_argument("--shop", action="store_true", help="强制按店铺采集")
    ap.add_argument("--selftest", action="store_true", help="引擎自检（不发真实业务请求）")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    if args.selftest:
        res = run_selftest()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if not args.input:
        sys.stderr.write("缺少输入：请给一个商品/店铺链接或 ID（或运行 --selftest）\n")
        return 1

    if args.shop:
        res = collect_shop(args.input, force=True)
    else:
        res = collect(args.input)

    if res.get("ok") and args.save:
        try:
            res["saved"] = save_result(res, args.save)
        except Exception as e:
            sys.stderr.write("文件保存失败：" + (str(e) or e.__class__.__name__) + "\n")
            return 4

    if res.get("ok"):
        if args.md:
            md = result_markdown(res)
            sys.stdout.write(md)
            if not md.endswith("\n"):
                sys.stdout.write("\n")
        else:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    # 失败：stdout 给 JSON 失败对象（便于脚本消费），stderr 给原因原文
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.stderr.write(str(res.get("error") or "采集失败（无错误详情）") + "\n")
    return _exit_code_for(res)


if __name__ == "__main__":
    sys.exit(main())
