/* 冒烟测试：桩掉 obsidian 模块 → 载入 main.js → 验证内嵌释放逻辑（市场安装场景）
   用法：cd dev && node smoke.js   （自动建临时目录，跑完自清）
   ⚠️ 本机铁律：spawnSync/execFileSync 一律 EBUSY → 一律异步 spawn */
const fs = require("fs"), os = require("os"), path = require("path"), crypto = require("crypto");
const { spawn } = require("child_process");
const assert = (c, m) => { if (!c) { console.error("  ✗ " + m); process.exitCode = 1; } else console.log("  ✓ " + m); };
function runPy(cmd, args) {
  return new Promise((resolve) => {
    const c = spawn(cmd, args);
    let err = "";
    if (c.stderr) c.stderr.on("data", (d) => (err += d));
    c.on("error", () => resolve(-1));
    c.on("close", (code) => resolve(code === 0 ? 0 : (err.split("\n")[0] || -1)));
  });
}
async function main() {
  // ① 桩 obsidian（必须放仓库根 node_modules：require 从 main.js 所在目录向上解析）
  const REPO = path.join(__dirname, "..");
  const nm = path.join(REPO, "node_modules", "obsidian");
  fs.mkdirSync(nm, { recursive: true });
  fs.writeFileSync(path.join(nm, "index.js"),
    "class Plugin { constructor() {} }\n" +
    "class PluginSettingTab { constructor() {} }\n" +
    "class Setting { constructor() { const f = {}; ['setName','setDesc','addText','addToggle','addButton','addExtraButton','addSlider'].forEach(k => f[k] = () => f); return f; } }\n" +
    "class Notice { constructor(t, d) {} }\n" +
    "class Modal { constructor(app) { this.titleEl = {}; this.contentEl = {}; } open() {} close() {} }\n" +
    "module.exports = { Plugin, PluginSettingTab, Setting, Notice, Modal };\n", "utf8");
  fs.writeFileSync(path.join(nm, "package.json"), JSON.stringify({ name: "obsidian", main: "index.js" }));

  const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), "wbsmoke-"));
  const plugDir = path.join(stubDir, "plugins", "xhs-product-search");
  fs.mkdirSync(plugDir, { recursive: true });
  const CollectorPlugin = require(path.join(REPO, "main.js"));
  const Ctor = CollectorPlugin.default || CollectorPlugin;

  const plugin = Object.create(Ctor.prototype);
  plugin.manifest = { dir: plugDir };
  plugin.app = { vault: { adapter: { getBasePath() { return path.dirname(plugDir); } } } };
  plugin.settings = {};

  console.log("① 首次加载（无 vendor）→ 应释放脚本");
  plugin.ensureVendorScript();
  const f = path.join(plugDir, "vendor", "xhs.py");
  assert(fs.existsSync(f), "vendor/xhs.py 已释放");
  const want = fs.readFileSync(path.join(REPO, "vendor", "xhs.py"));
  const got = fs.readFileSync(f);
  assert(crypto.createHash("md5").update(got).digest("hex") === crypto.createHash("md5").update(want).digest("hex"), "释放内容与仓库 vendor/xhs.py 逐字节一致");
  const code = await runPy("C:/Program Files/python/python.exe", ["-c", "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)", f]);
  assert(code === 0, "释放出的脚本 py_compile 通过" + (code === 0 ? "" : "（" + code + "）"));

  console.log("② 二次加载（已有 vendor）→ 不覆盖");
  const before = fs.statSync(f).mtimeMs;
  plugin.ensureVendorScript();
  assert(fs.statSync(f).mtimeMs === before, "已有副本未被触碰");

  console.log("③ 指纹漂移 → 只警告不覆盖");
  fs.writeFileSync(f, "# drift\n", "utf8");
  plugin.ensureVendorScript();
  assert(fs.readFileSync(f, "utf8") === "# drift\n", "漂移副本未被覆盖");

  console.log("④ manifest.dir 为相对路径 → 用 vault 基路径拼绝对目录释放");
  const base2 = path.join(stubDir, "vault-root");
  const plugin2 = Object.create(Ctor.prototype);
  plugin2.manifest = { dir: ".obsidian/plugins/xhs-product-search" };
  plugin2.app = { vault: { adapter: { getBasePath() { return base2; } } } };
  plugin2.ensureVendorScript();
  assert(fs.existsSync(path.join(base2, ".obsidian", "plugins", "xhs-product-search", "vendor", "xhs.py")), "相对 manifest.dir 也能落对位置");

  fs.rmSync(stubDir, { recursive: true, force: true });
  fs.rmSync(path.join(REPO, "node_modules"), { recursive: true, force: true });
  console.log(process.exitCode ? "\n有断言失败" : "\n冒烟测试全部通过");
}
main();
