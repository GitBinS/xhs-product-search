/* 冒烟测试（dev 工具，不随插件分发）—— 只测不依赖 Obsidian 的纯函数。
   用法：node dev/smoke.js
   说明：main.js 顶部 require("obsidian")，这里用 vm + 桩模块加载它，
        顶层 `function` 声明会挂到 vm 上下文，可直接调用。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const obsidianStub = {
  Plugin: class {}, PluginSettingTab: class {}, Setting: class {},
  Notice: class {}, Modal: class {},
  requestUrl: async () => ({ status: 200, text: "" }),
};
const sandbox = {
  require: (m) => (m === "obsidian" ? obsidianStub : require(m)),
  module: { exports: {} },
  console,
  window: { setTimeout: () => 0, clearTimeout: () => {} },
  setTimeout, clearTimeout, Promise, Date, Math, JSON, Set, Map, URL,
  encodeURIComponent, Object, Array, String, Number, RegExp, Error, isNaN, parseInt, parseFloat,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const G = sandbox;
const eq = (a, b, m) => {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error(m + " \u2192 \u671f\u671b " + JSON.stringify(b) + "\uff0c\u5b9e\u5f97 " + JSON.stringify(a));
  }
};
const t = (name, fn) => {
  try { fn(); console.log("  \u2713 " + name); }
  catch (e) { console.log("  \u2717 " + name + " \u2014 " + e.message); process.exitCode = 1; }
};

const ID = "6a83ca5ffa5d5c0001a98ca5";

console.log("\u91c7\u96c6\u5f15\u64ce\u7eaf\u51fd\u6570\u5192\u70df\u6d4b\u8bd5\uff1a");

t("parseItemId 完整商品链接", () => eq(G.parseItemId("https://www.xiaohongshu.com/goods-detail/" + ID + "?x=y"), ID, "goods-detail"));
t("parseItemId 裸 ID", () => eq(G.parseItemId(ID), ID, "bare"));
t("parseItemId item_id=", () => eq(G.parseItemId("https://mall.xiaohongshu.com/x?item_id=" + ID), ID, "item_id"));
t("parseItemId 认不出", () => eq(G.parseItemId("\u8fd9\u4e0d\u662f\u94fe\u63a5"), null, "garbage"));
t("parseSellerId 店铺链接", () => eq(G.parseSellerId("https://www.xiaohongshu.com/shop/6a8276edf4861a0015578413"), "6a8276edf4861a0015578413", "shop"));
t("extractUrls 从分享文案抽链接", () => eq(G.extractUrls("\u770b\u770b\u8fd9\u4e2a http://xhslink.com/a/xx \u590d\u5236"), ["http://xhslink.com/a/xx"], "urls"));
t("toInt 万", () => eq(G.toInt("2.4\u4e07"), 24000, "wan"));
t("toInt 数字", () => eq(G.toInt(59), 59, "num"));
t("toInt 空", () => eq(G.toInt(null), null, "null"));
t("parseSoldText 下界", () => eq(G.parseSoldText("\u5df2\u552e2.4\u4e07+"), [24000, "\u5df2\u552e2.4\u4e07+", true], "lower"));
t("parseSoldText 无 +", () => eq(G.parseSoldText("\u5df2\u552e59"), [59, "\u5df2\u552e59", false], "exact"));
t("htmlUnescape", () => eq(G.htmlUnescape("a&amp;b&lt;c&gt;d"), "a&b<c>d", "esc"));
t("matchBrace", () => eq(G.matchBrace('{"a":{"b":1}}', 0), 12, "brace"));
t("idLastDigitPlus1 末位 +1", () => eq(G.idLastDigitPlus1(ID), "6a83ca5ffa5d5c0001a98ca6", "plus1"));
t("shelfTsFromItemId 前 8 位 hex", () => eq(G.shelfTsFromItemId(ID), parseInt(ID.slice(0, 8), 16), "shelf"));
t("shelfTsFromItemId 太短", () => eq(G.shelfTsFromItemId("abc"), null, "short"));
t("first 首个非空", () => eq(G.first({ a: null, b: "", c: 5 }, "a", "b", "c"), 5, "first"));
t("normalize 契约", () => {
  const n = G.normalize({ "\u6807\u9898": "T", "\u73b0\u4ef7": 39.9, "\u5230\u624b\u4ef7": 35, "\u53d1\u8d27\u5730": "\u6cc9\u5dde", "\u5e97\u94fa\u8bc4\u5206": 4.8, "\u5728\u67b6\u5929\u6570": 10, "\u5df2\u552e": 24000, "SKU\u6570": 3, "\u9009\u4e2dSKU": "\u4e00\u74f6", "\u5e97\u94fa\u94fe\u63a5": "u", "\u89d2\u6807": ["\u65b0\u54c1"] });
  eq(n.archive["\u5546\u54c1\u6807\u9898"], "T", "title");
  eq(n.archive["\u5546\u54c1\u552e\u4ef7"], 39.9, "price");
  eq(n.archive["\u6765\u6e90"], "\u5c0f\u7ea2\u4e66\u5e02\u96c6", "src");
  eq(n.track["\u5546\u54c1\u9500\u91cf"], 24000, "sold");
  eq(n.archive["\u5546\u54c1\u89d2\u6807"], "\u65b0\u54c1", "badge");
});
t("normalize 已售下界 \u2192 warning", () => {
  const n = G.normalize({ "\u5df2\u552e\u662f\u4e0b\u754c": true, "\u5df2\u552e\u539f\u6587": "\u5df2\u552e2.4\u4e07+" });
  if (!n.warnings.some((w) => /\u4e0b\u754c/.test(w))) throw new Error("\u7f3a\u5c11\u4e0b\u754c\u63d0\u9192");
});
t("normalize SKU \u7ea7\u53e3\u5f84 \u2192 warning", () => {
  const n = G.normalize({ "\u5df2\u552e\u53e3\u5f84": "SKU\u7ea7\uff08API\uff09\u00b7 x" });
  if (!n.warnings.some((w) => /SKU/.test(w))) throw new Error("\u7f3a\u5c11\u53e3\u5f84\u63d0\u9192");
});
t("\u6a21\u5757\u5bfc\u51fa\u63d2\u4ef6\u7c7b", () => {
  if (typeof G.module.exports !== "function") throw new Error("module.exports \u4e0d\u662f\u7c7b");
});

console.log(process.exitCode ? "\n\u6709\u65ad\u8a00\u5931\u8d25" : "\n\u5192\u70df\u6d4b\u8bd5\u5168\u90e8\u901a\u8fc7");
