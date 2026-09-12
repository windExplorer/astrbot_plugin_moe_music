import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import cssInjectedByJs from "vite-plugin-css-injected-by-js";
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

// AstrBot 插件 Pages 前端构建。
//
// ⚠️ 兼容性关键（参考 astrbot_plugin_comfyui_anima 的实战经验）：
// AstrBot 的 plugin_page_service 只对「入口 index.html 直接引用的资源」重写为
// 带 asset_token 的路径；旧版本对「JS 内部动态 import 的 chunk」重写不可靠，
// 会导致 chunk 请求 401 → 页面空白。因此强制单文件构建：
//   - inlineDynamicImports: true（路由/组件懒加载合并进单 JS）
//   - cssCodeSplit: false + cssInjectedByJs（CSS 内联进 JS）
// 产物：index.html + 单个 assets/index-*.js。
// 页面使用 hash 切换，无需 SPA fallback；必须用相对 base（./）。

function pluginVersion() {
  try {
    const meta = readFileSync(fileURLToPath(new URL("../metadata.yaml", import.meta.url)), "utf-8");
    const m = meta.match(/^version:\s*"?([^"#\r\n]+?)"?\s*$/m);
    return m ? m[1].trim() : "dev";
  } catch {
    return "dev";
  }
}

export default defineConfig({
  plugins: [vue(), cssInjectedByJs()],
  base: "./",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  define: {
    __PLUGIN_VERSION__: JSON.stringify(pluginVersion()),
  },
  build: {
    outDir: "../pages/moe-console",
    emptyOutDir: true,
    chunkSizeWarningLimit: 4000,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
