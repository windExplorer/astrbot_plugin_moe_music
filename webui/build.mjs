#!/usr/bin/env node
/**
 * 前端构建脚本：npm install（缺依赖时）+ vite build → ../pages/moe-console/
 * 由 scripts/build_webui.py 或手动 npm run build 调用。跨平台（Node >= 18）。
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// webui/ 目录（本文件所在目录）
const root = fileURLToPath(new URL("./", import.meta.url));

if (!existsSync(join(root, "node_modules"))) {
  console.log("==> 首次构建，安装依赖（npm install）...");
  const r = spawnSync("npm", ["install"], { stdio: "inherit", shell: true, cwd: root });
  if (r.status !== 0) process.exit(1);
}

console.log("==> vite build...");
const b = spawnSync("npm", ["run", "build"], { stdio: "inherit", shell: true, cwd: root });
process.exit(b.status ?? 1);
