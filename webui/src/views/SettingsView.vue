<script setup lang="ts">
/** 配置页：读后端 schema 结构化渲染表单，保存回插件配置（热应用）。 */
import { onMounted, ref } from "vue";
import {
  NButton,
  NCard,
  NEl,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSelect,
  NSlider,
  NSpin,
  NSwitch,
  useMessage,
} from "naive-ui";
import { apiGet, apiPost } from "../bridge";

const msg = useMessage();
const loading = ref(false);
const saving = ref(false);
const schema = ref<Record<string, any>>({});
const values = ref<Record<string, any>>({});

// 表单分组顺序与标题（新增配置项必须加入对应组才会渲染）
const GROUPS: Array<{ title: string; keys: string[] }> = [
  { title: "音乐服务", keys: ["api_base_url", "api_key", "public_base_url", "proxy", "request_timeout"] },
  {
    title: "点歌行为",
    keys: [
      "default_source",
      "default_quality",
      "song_limit",
      "selection_display",
      "send_modes",
      "record_via_onebot",
      "timeout",
      "enable_lyrics",
      "embed_metadata",
      "recall_candidate",
    ],
  },
  { title: "文件下载（点歌文件 指令）", keys: ["file_quality", "file_embed_metadata"] },
  {
    title: "分享识别（QQ音乐 / 网易云 / 酷狗 / 酷我）",
    keys: ["share_auto_play", "share_send_modes", "share_quality"],
  },
  { title: "队列", keys: ["queue_concurrency", "queue_max_pending"] },
  { title: "访问控制（白/黑名单，白名单优先）", keys: ["whitelist_groups", "whitelist_users", "blacklist_groups", "blacklist_users"] },
  { title: "其他", keys: ["enable_self_test"] },
];

/** 名单类配置：schema 里的 list 且无 options —— 用多行文本域，一行一个。 */
function isLineList(item: any): boolean {
  return item?.type === "list" && !item.options;
}

/** 文本 -> 名单数组：按换行 / 逗号 / 顿号 / 空格切分并去空（粘贴格式容错）。 */
function splitList(text: string): string[] {
  return text
    .split(/[\s,，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 配置值 -> 文本域内容（兼容数组与历史遗留的逗号分隔字符串）。 */
function toLines(value: any): string {
  if (Array.isArray(value)) return value.join("\n");
  if (typeof value === "string") return splitList(value).join("\n");
  return "";
}

// 名单文本域的「原始输入」：编辑过程中必须原样保留（含空格 / 换行 / 逗号）。
// 若把解析后的数组 join 回去当受控值渲染，用户敲下的分隔符会被立刻吃掉——
// 表现为「怎么都无法输入空格和换行」，连第二个号码都填不进去。
const listText = ref<Record<string, string>>({});

/** 文本域当前内容：优先用正在编辑的原文，否则由配置值渲染。 */
function textOf(key: string, value: any): string {
  return key in listText.value ? listText.value[key] : toLines(value);
}

/** 文本域输入：保留原文，同时同步解析结果（供保存与「当前 N 项」统计）。 */
function onListInput(key: string, text: string) {
  listText.value[key] = text;
  values.value[key] = splitList(text);
}

onMounted(load);

async function load() {
  loading.value = true;
  try {
    const [s, c] = await Promise.all([apiGet<any>("schema"), apiGet<any>("config")]);
    schema.value = s;
    values.value = { ...c };
    listText.value = {}; // 丢弃编辑缓存，按最新配置重新渲染
  } catch (e: any) {
    msg.error(`加载配置失败：${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
}

function optValues(item: any): { label: string; value: string }[] {
  return (item.options || []).map((o: string) => ({ label: o, value: o }));
}

function optionsPlain(item: any): { label: string; value: string }[] {
  // list 无选项 → 动态标签输入（白名单等），此处用于带 options 的 list（send_modes）
  return optValues(item);
}

async function save() {
  saving.value = true;
  try {
    // 关键：转纯 JSON 对象再交给 bridge —— Vue 的 reactive 代理对象无法被
    // postMessage 结构化克隆（报 "could not be cloned"）
    const payload = JSON.parse(JSON.stringify(values.value));
    const res = await apiPost<any>("config", payload);
    msg.success(`已保存并生效：${(res.applied ?? []).length} 项（队列并发等结构性配置重启后生效）`);
  } catch (e: any) {
    msg.error(`保存失败：${e.message ?? e}`);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <n-spin :show="loading">
    <!-- margin-bottom 给右下角固定操作条让位，避免遮住最后一个表单控件 -->
    <n-card size="small" style="margin-bottom: 64px">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">
          <span>插件配置</span>
          <n-button size="small" @click="load">重载</n-button>
          <n-button size="small" type="primary" :loading="saving" @click="save">保存并生效</n-button>
        </div>
      </template>
      <n-form label-placement="top" style="max-width: 860px">
        <n-card v-for="g in GROUPS" :key="g.title" :title="g.title" size="small" style="margin-bottom: 12px">
          <n-grid :cols="24" :x-gap="16" :y-gap="4">
            <template v-for="key in g.keys" :key="key">
              <n-grid-item v-if="schema[key]" :span="24">
                <n-form-item :label="schema[key].description">
                  <template v-if="(schema[key].type === 'string' || schema[key].type === 'text') && !schema[key].options">
                    <n-input
                      v-if="schema[key].type === 'text'"
                      type="textarea"
                      :rows="3"
                      v-model:value="values[key]"
                      :placeholder="schema[key].hint || ''"
                    />
                    <n-input v-else v-model:value="values[key]" :placeholder="schema[key].hint || ''" />
                  </template>
                  <!-- 名单类（list 且无 options）：多行文本域，一行一个；粘贴逗号/空格分隔也能识别 -->
                  <n-input
                    v-else-if="isLineList(schema[key])"
                    type="textarea"
                    :rows="4"
                    :value="textOf(key, values[key])"
                    placeholder="一行一个（也支持逗号或空格分隔），支持 * 通配；留空表示不启用"
                    @update:value="(v: string) => onListInput(key, v)"
                  />
                  <n-select
                    v-else-if="schema[key].type === 'string' && schema[key].options"
                    v-model:value="values[key]"
                    :options="optValues(schema[key])"
                  />
                  <n-select
                    v-else-if="schema[key].type === 'list' && schema[key].options"
                    v-model:value="values[key]"
                    multiple
                    :options="optionsPlain(schema[key])"
                  />
                  <n-switch
                    v-else-if="schema[key].type === 'bool'"
                    v-model:value="values[key]"
                  />
                  <div v-else-if="schema[key].type === 'int'" style="display: flex; align-items: center; gap: 12px; width: 100%">
                    <template v-if="schema[key].slider">
                      <n-slider
                        style="flex: 1"
                        :value="values[key] ?? schema[key].default"
                        :min="schema[key].slider.min"
                        :max="schema[key].slider.max"
                        :step="schema[key].slider.step"
                        @update:value="(v: number) => (values[key] = v)"
                      />
                      <n-input-number
                        :value="values[key] ?? schema[key].default"
                        style="width: 110px"
                        :min="schema[key].slider.min"
                        :max="schema[key].slider.max"
                        :step="schema[key].slider.step"
                        @update:value="(v: number | null) => (values[key] = v ?? schema[key].default)"
                      />
                    </template>
                    <n-input-number v-else v-model:value="values[key]" style="width: 200px" />
                  </div>
                  <n-input v-else v-model:value="values[key]" />
                </n-form-item>
                <div v-if="schema[key].hint" style="margin: -12px 0 8px; font-size: 12px; opacity: 0.55; line-height: 1.5">
                  <span v-if="isLineList(schema[key])">当前 {{ (values[key] || []).length }} 项 · </span>
                  {{ schema[key].hint }}
                </div>
              </n-grid-item>
            </template>
          </n-grid>
        </n-card>
      </n-form>
    </n-card>

    <!-- 配置页很长：把「保存」固定到右下角，改完即点，无需滚回顶部 -->
    <n-el tag="div" class="settings-actions">
      <n-button size="small" @click="load">重载</n-button>
      <n-button size="small" type="primary" :loading="saving" @click="save">保存并生效</n-button>
    </n-el>
  </n-spin>
</template>

<style scoped>
/* 固定操作条：滚动容器是控制台的 .content（不是 window），用 fixed 才能稳定贴住视口；
   sticky 会被卡片自身的 overflow 裁掉。n-el 用于取得 naive-ui 主题变量，自动适配暗色主题。 */
.settings-actions {
  position: fixed;
  right: 20px;
  bottom: 20px;
  z-index: 100;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 12px;
  background: var(--n-color, rgba(255, 255, 255, 0.92));
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, 0.25));
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.18);
}
</style>
