<script setup lang="ts">
import { computed, inject, ref, watch } from "vue";
import { NConfigProvider, NMessageProvider, darkTheme, zhCN, dateZhCN } from "naive-ui";
import { isDark, onCtxChange } from "./bridge";
import StatsView from "./views/StatsView.vue";
import TasksView from "./views/TasksView.vue";
import SettingsView from "./views/SettingsView.vue";

const TABS = [
  { key: "stats", label: "统计", comp: StatsView },
  { key: "tasks", label: "实时任务", comp: TasksView },
  { key: "settings", label: "配置", comp: SettingsView },
];

const dark = ref(isDark());
onCtxChange(() => {
  dark.value = isDark();
});
const theme = computed(() => (dark.value ? darkTheme : null));

const bridgeError = inject<string | null>("bridgeError", null);

// 页签记忆：iframe 重建/刷新后恢复（hash 会随 iframe src 重置，故用 sessionStorage）
const TAB_KEYS = TABS.map((t) => t.key);
function savedTab(): string {
  try {
    const v = sessionStorage.getItem("moe_console_tab");
    return v && TAB_KEYS.includes(v) ? v : "stats";
  } catch {
    return "stats";
  }
}
const hash = ref(savedTab());
try {
  sessionStorage.setItem("moe_console_tab", hash.value);
} catch {
  /* 隐私模式等场景忽略 */
}
window.addEventListener("hashchange", () => {
  hash.value = (location.hash || `#${savedTab()}`).replace("#", "");
});
watch(hash, (v) => {
  try {
    sessionStorage.setItem("moe_console_tab", v);
  } catch {
    /* ignore */
  }
  location.hash = v;
});
const active = computed(() => TABS.find((t) => t.key === hash.value) ?? TABS[0]);
</script>

<template>
  <n-config-provider :theme="theme" :locale="zhCN" :date-locale="dateZhCN" style="min-height: 100vh">
    <n-message-provider>
      <n-layout style="min-height: 100vh; background: transparent">
        <n-layout-header bordered style="padding: 12px 20px">
          <div style="display: flex; align-items: center; gap: 16px; flex-wrap: wrap">
            <span style="font-size: 17px; font-weight: 600">🎵 萌音控制台</span>
            <span style="opacity: 0.55; font-size: 12px">{{ __PLUGIN_VERSION__ }}</span>
            <n-tabs
              type="segment"
              :value="active.key"
              style="width: 340px"
              @update:value="(k: string) => (hash = k)"
            >
              <n-tab v-for="t in TABS" :key="t.key" :name="t.key">{{ t.label }}</n-tab>
            </n-tabs>
          </div>
        </n-layout-header>
        <n-layout-content content-style="padding: 16px 20px 32px;">
          <n-alert v-if="bridgeError" type="error" title="桥接不可用" style="margin-bottom: 12px">
            {{ bridgeError }}
          </n-alert>
          <component :is="active.comp" />
        </n-layout-content>
      </n-layout>
    </n-message-provider>
  </n-config-provider>
</template>
