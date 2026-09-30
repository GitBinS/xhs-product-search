import { defineConfig } from "eslint/config";
import obsidianmd from "eslint-plugin-obsidianmd";

export default defineConfig([
  ...obsidianmd.configs.recommended,
  {
    // 零构建 CommonJS 插件：顶层 require 是架构选择（无打包器），关闭 require 风格规则
    files: ["main.js"],
    languageOptions: {
      // 插件运行在 Obsidian 的 Node/Electron 环境：require / module 是合法的
      globals: { require: "readonly", module: "writable" },
    },
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
  {
    ignores: ["dev/**", "vendor/**"],
    rules: {
      // 插件 UI 为中文，英文 sentence-case 规则不适用
      "obsidianmd/ui/sentence-case": "off",
    },
    languageOptions: {
      parserOptions: {
        projectService: {
          allowDefaultProject: ["eslint.config.*", "main.js"],
        },
      },
    },
  },
]);
