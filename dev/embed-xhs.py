#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新生成 main.js 里内嵌的 vendor/xhs.py（base64 payload + MD5 指纹）。

用法：在仓库根目录跑  python dev/embed-xhs.py
⚠️ JS 的 ASI 陷阱：相邻字符串字面量会被拆成多条语句（const 只拿到第一行）！
   所以每行必须以 + 结尾连接。改了 vendor/xhs.py 之后重跑本工具即可。"""
import base64
import hashlib
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "vendor", "xhs.py")
MAIN = os.path.join(ROOT, "main.js")

raw = open(SCRIPT, "rb").read()
md5 = hashlib.md5(raw).hexdigest()
b64 = base64.b64encode(raw).decode("ascii")
lines = [b64[i : i + 96] for i in range(0, len(b64), 96)]
# 每行 "..." +，最后一行 "..."；
body = "\n".join(
    ('  "%s" +' % l) if i < len(lines) - 1 else ('  "%s";' % l)
    for i, l in enumerate(lines)
)

t = io.open(MAIN, encoding="utf-8").read()
t2, n1 = re.subn(r'const VENDOR_MD5 = "[0-9a-f]{32}";', 'const VENDOR_MD5 = "%s";' % md5, t)
t2, n2 = re.subn(
    r"const VENDOR_B64 =\n(?:  \"[A-Za-z0-9+/=]+\" ?\+?\n)*  \"[A-Za-z0-9+/=]+\";",
    "const VENDOR_B64 =\n" + body,
    t2,
)
if n1 != 1 or n2 != 1:
    sys.exit("替换失败：VENDOR_MD5 命中 %d 次，VENDOR_B64 命中 %d 次 —— main.js 的内嵌块结构变了，手工检查" % (n1, n2))
io.open(MAIN, "w", encoding="utf-8", newline="\n").write(t2)

# 自检：node 解码回读，必须与 vendor 副本逐字节一致
import subprocess
chk = subprocess.run(
    ["node", "-e",
     "const fs=require('fs'),c=require('crypto');"
     "const t=fs.readFileSync(process.argv[1],'utf8');"
     "const m=t.match(/const VENDOR_B64 =\\n[\\s\\S]*?\\n  \\\"[A-Za-z0-9+\\/=]+\\\";/);"
     "const s=m[0].slice(m[0].indexOf('\\n')+1).split('\\n').map(l=>l.trim().replace(/[+;]$/,'').replace(/^\\\"|\\\"$/g,'')).join('');"
     "const b=Buffer.from(s,'base64');"
     "console.log(c.createHash('md5').update(b).digest('hex'), b.length);",
     MAIN],
    capture_output=True, text=True)
out = chk.stdout.strip()
print("node 回读:", out)
if not out.startswith(md5 + " "):
    sys.exit("❌ node 解码回读与指纹不一致！")
print("OK  md5=%s  bytes=%d  b64lines=%d" % (md5, len(raw), len(lines)))
