#!/usr/bin/env bash
# 把本仓库的插件产物同步到 Obsidian 插件目录（本机测试用）。
# 用法：bash sync-to-vault.sh
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
VAULT_PLUGIN="/e/第二大脑/.obsidian/plugins/xhs-product-search"

if [ ! -d "$VAULT_PLUGIN" ]; then
  echo "目标目录不存在，正在创建：$VAULT_PLUGIN"
  mkdir -p "$VAULT_PLUGIN"
fi

for f in main.js manifest.json styles.css versions.json; do
  if [ ! -f "$REPO/$f" ]; then
    echo "❌ 缺少文件：$f"
    exit 1
  fi
  cp "$REPO/$f" "$VAULT_PLUGIN/$f"
  echo "  synced: $f"
done

echo "✅ 已同步到 $VAULT_PLUGIN"
echo "   在 Obsidian 中重新加载插件（设置 → 第三方插件 → 关开一次）即可生效"
