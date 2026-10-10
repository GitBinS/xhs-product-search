# xhs-product-collect

把小红书**公开商品 / 店铺页面**的数据采集成结构化 JSON / Markdown 的小工具。
免登录、免 Cookie、无签名、零第三方依赖（纯 Python 标准库）。

**作者：GitBinS**

---

## 安装（30 秒）

把整个 `xhs-product-collect/` 目录拷进你所用 AI 工具的 skills 目录即可：

| 工具 | 安装位置 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/` |
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.codex/skills/`（或你的 prompts / skills 目录） |
| 其他支持 SKILL.md 规范的工具 | 其约定的 skills 目录 |

> `~` 是用户主目录：Windows `C:\Users\<你>`；macOS / Linux `/Users/<你>` 或 `/home/<你>`。

安装后的目录结构：

```
xhs-product-collect/
├── SKILL.md          # 给 AI 读的技能说明
├── scripts/
│   └── collect.py    # 采集引擎（也可直接命令行独立使用）
└── README.md
```

## 直接命令行使用（不依赖 AI）

```bash
python3 scripts/collect.py "<商品链接|商品ID|店铺链接|店铺ID>"
```

常用参数：

```bash
python3 scripts/collect.py "<...>" --md            # Markdown 格式输出
python3 scripts/collect.py "<...>" --save "<目录>" # 落盘 .md（同商品复采自动更新原文件）
python3 scripts/collect.py "<...>" --shop          # 强制按店铺采集
python3 scripts/collect.py --selftest              # 自检（不发网络请求）
```

Windows 环境把 `python3` 换成 `python`。

## 依赖

**仅 Python 3.7+**，纯标准库，无 `pip install` 步骤。

## 版本
当前 1.0.0（首发）。采集引擎逐函数移植自 Obsidian 插件 xhs-product-search v0.3.1；
插件接口变更时本 skill 同步升版，对应关系见仓库 CHANGELOG。

## 兼容性（诚实声明）

- 需要 AI 工具具备**本地命令执行能力**（能运行 python）——纯云端对话工具（无本地 shell）**不适用**；
- 需要**能直连小红书的网络环境**；
- 走的是小红书公开页面 / 公开接口，**非官方 API**，平台改版可能导致失效——报错时请以 stderr 原文为准，别让 AI 猜。

## 输出与退出码

- 默认输出 JSON（字段口径见 `SKILL.md`）；`--md` 输出 Markdown；`--save` 落盘 .md 笔记。
- 退出码：`0` 成功 / `1` 解析失败 / `2` 采集失败 / `3` 网络失败 / `4` 保存失败。

## 合规

只采集公开页面数据：无登录态、无 Cookie、无签名、不绕任何风控；不采集个人信息。
数据仅作参考，请遵守平台服务条款。
