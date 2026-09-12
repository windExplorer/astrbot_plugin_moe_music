<script setup lang="ts">
/** 控制台外壳：固定导航（页签 / 版本号→更新日志）+ 萌系粉色主题 + 三页面切换。 */
import { computed, inject, onMounted, ref, watch } from "vue";
import {
  NButton,
  NConfigProvider,
  NMessageProvider,
  NModal,
  NTag,
  darkTheme,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from "naive-ui";
import { apiGet, isDark, onCtxChange } from "./bridge";
import StatsView from "./views/StatsView.vue";
import TasksView from "./views/TasksView.vue";
import SettingsView from "./views/SettingsView.vue";

const TABS = [
  { key: "stats", label: "📊 统计", comp: StatsView },
  { key: "tasks", label: "⚡ 实时任务", comp: TasksView },
  { key: "settings", label: "⚙️ 配置", comp: SettingsView },
];

const dark = ref(isDark());
onCtxChange(() => {
  dark.value = isDark();
});
const theme = computed(() => (dark.value ? darkTheme : null));

// 萌系粉色主题
const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    primaryColor: "#ff7eb9",
    primaryColorHover: "#ff9dcb",
    primaryColorPressed: "#f25d9c",
    primaryColorSuppl: "#ff7eb9",
    borderRadius: "10px",
    borderRadiusSmall: "7px",
    successColor: "#7ecfa8",
    warningColor: "#ffc069",
    errorColor: "#ff8f9c",
    infoColor: "#82b8ff",
  },
  Card: { borderRadius: "14px" },
}));

const bridgeError = inject<string | null>("bridgeError", null);

// 页签记忆（sessionStorage：iframe 刷新/重建后恢复）
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
  /* ignore */
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

// 版本号 + 更新日志
const version = __PLUGIN_VERSION__ as unknown as string;
const showChangelog = ref(false);
const changelog = ref("");
const changelogLoading = ref(false);
async function openChangelog() {
  showChangelog.value = true;
  if (changelog.value) return;
  changelogLoading.value = true;
  try {
    const res = await apiGet<any>("changelog");
    changelog.value = res.content ?? "";
  } catch {
    changelog.value = "更新日志加载失败";
  } finally {
    changelogLoading.value = false;
  }
}
onMounted(() => void version);
</script>

<template>
  <n-config-provider :theme="theme" :theme-overrides="themeOverrides" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <div class="shell" :data-theme="dark ? 'dark' : 'light'">
        <header class="topbar">
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
            <span class="logo">🎵 萌音控制台</span>
            <n-button
              size="tiny"
              quaternary
              round
              type="primary"
              :loading="changelogLoading"
              @click="openChangelog"
            >
              {{ version }}
            </n-button>
          </div>
          <n-tabs
            type="segment"
            :value="active.key"
            style="width: 360px"
            @update:value="(k: string) => (hash = k)"
          >
            <n-tab v-for="t in TABS" :key="t.key" :name="t.key">{{ t.label }}</n-tab>
          </n-tabs>
        </header>
        <main class="content">
          <n-alert v-if="bridgeError" type="error" title="桥接不可用" style="margin-bottom: 12px">
            {{ bridgeError }}
          </n-alert>
          <component :is="active.comp" />
        </main>
      </div>

      <n-modal v-model:show="showChangelog" preset="card" title="📋 更新日志" style="max-width: 640px">
        <pre class="changelog">{{ changelog || "加载中…" }}</pre>
      </n-modal>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
.shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: linear-gradient(180deg, #fff0f7 0%, #f5f8ff 100%);
}
[data-theme="dark"] .shell {
  background: linear-gradient(180deg, #221a26 0%, #191c2a 100%);
}
.topbar {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding: 12px 20px;
  border-bottom: 1px solid rgba(255, 126, 185, 0.25);
  background: rgba(255, 255, 255, 0.75);
  backdrop-filter: blur(8px);
  z-index: 10;
}
[data-theme="dark"] .topbar {
  background: rgba(30, 26, 38, 0.75);
  border-bottom-color: rgba(255, 126, 185, 0.2);
}
.logo {
  font-size: 17px;
  font-weight: 700;
  background: linear-gradient(90deg, #ff7eb9, #a78bfa);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  white-space: nowrap;
}
.content {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 32px;
}
.changelog {
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
  max-height: 60vh;
  overflow-y: auto;
}
</style>
