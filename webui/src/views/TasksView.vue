<script setup lang="ts">
/** 实时任务页：队列状态 + 最近点歌/搜索记录（SSE 实时，断线自动轮询兜底）。 */
import { h, onBeforeUnmount, onMounted, ref } from "vue";
import { NButton, NCard, NDataTable, NSwitch, NTag, useMessage } from "naive-ui";
import { apiGet, subscribeSSE } from "../bridge";

const msg = useMessage();
const live = ref(true);
const queue = ref<Record<string, any> | null>(null);
const plays = ref<any[]>([]);
const searches = ref<any[]>([]);
let unsub: (() => void) | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

async function fetchOnce() {
  try {
    const [q, r] = await Promise.all([apiGet<any>("tasks/queue"), apiGet<any>("tasks/recent", { limit: 20 })]);
    queue.value = q.queue;
    plays.value = r.plays ?? [];
    searches.value = r.searches ?? [];
  } catch {
    // 静默（轮询兜底场景），不打断
  }
}

function applySnapshot(s: any) {
  if (!s) return;
  queue.value = s.queue;
  if (s.plays) plays.value = s.plays;
  if (s.searches) searches.value = s.searches;
}

async function startLive() {
  stopLive();
  const ok = await subscribeSSE("tasks/stream", (parsed) => applySnapshot(parsed));
  if (ok) {
    unsub = ok;
  } else {
    // bridge 不支持 SSE：降级轮询
    pollTimer = setInterval(fetchOnce, 2000);
    msg.warning("当前环境不支持实时流，已切换为 2 秒轮询");
  }
}

function stopLive() {
  unsub?.();
  unsub = null;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

onMounted(async () => {
  await fetchOnce();
  if (live.value) await startLive();
});
onBeforeUnmount(stopLive);

function toggleLive(v: boolean) {
  live.value = v;
  if (v) startLive();
  else stopLive();
}

function ms(v: unknown): string {
  return v === null || v === undefined ? "-" : `${v} ms`;
}

function statusTag(row: any) {
  return row.success
    ? h(NTag, { size: "small", type: "success", bordered: false }, { default: () => "成功" })
    : h(NTag, { size: "small", type: "error", bordered: false }, { default: () => `失败 ${row.error_code ?? ""}` });
}

const playColumns = [
  { title: "时间", key: "created_at", width: 160 },
  { title: "用户", key: "user_name", width: 110, ellipsis: { tooltip: true } },
  {
    title: "群",
    key: "group_name",
    width: 140,
    ellipsis: { tooltip: true },
    render: (row: any) => row.group_name || "私聊",
  },
  {
    title: "歌曲",
    key: "track_name",
    ellipsis: { tooltip: true },
    render: (row: any) => `${row.track_name} - ${row.singer}`,
  },
  { title: "音源", key: "source", width: 70 },
  { title: "音质", key: "quality", width: 80 },
  { title: "方式", key: "send_mode", width: 110 },
  {
    title: "来源",
    key: "trigger_type",
    width: 80,
    render: (row: any) => (row.trigger_type === "llm_tool" ? "AI" : "命令"),
  },
  { title: "排队", key: "queue_wait_ms", width: 90, render: (row: any) => ms(row.queue_wait_ms) },
  { title: "总耗时", key: "total_ms", width: 100, render: (row: any) => ms(row.total_ms) },
];

const searchColumns = [
  { title: "时间", key: "created_at", width: 160 },
  { title: "用户", key: "user_name", width: 110, ellipsis: { tooltip: true } },
  { title: "关键词", key: "keyword", ellipsis: { tooltip: true } },
  { title: "结果数", key: "result_count", width: 80 },
  { title: "状态", key: "success", width: 110, render: statusTag },
  { title: "耗时", key: "duration_ms", width: 100, render: (row: any) => ms(row.duration_ms) },
  { title: "排队", key: "queue_wait_ms", width: 90, render: (row: any) => ms(row.queue_wait_ms) },
];
</script>

<template>
  <div>
    <n-card size="small" style="margin-bottom: 12px">
      <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px">
        <div style="display: flex; gap: 20px; flex-wrap: wrap; align-items: center">
          <span style="font-weight: 600">队列状态</span>
          <n-tag :bordered="false" type="info">并发 {{ queue?.concurrency ?? "-" }}</n-tag>
          <n-badge :value="queue?.active ?? 0" type="success" show-zero>执行中</n-badge>
          <n-badge :value="queue?.pending ?? 0" type="warning" show-zero>等待中</n-badge>
          <n-tag :bordered="false">等待上限 {{ queue?.max_pending ?? "-" }}</n-tag>
          <n-tag :bordered="false">累计提交 {{ queue?.submitted ?? 0 }} / 拒绝 {{ queue?.rejected ?? 0 }}</n-tag>
        </div>
        <div style="display: flex; align-items: center; gap: 8px">
          <span style="font-size: 13px; opacity: 0.7">实时推送</span>
          <n-switch :value="live" size="small" @update:value="toggleLive" />
          <n-button size="small" @click="fetchOnce">手动刷新</n-button>
        </div>
      </div>
    </n-card>

    <n-card title="最近点歌" size="small" style="margin-bottom: 12px">
      <n-data-table :columns="playColumns" :data="plays" size="small" :bordered="false" :bottom-bordered="false" />
    </n-card>

    <n-card title="最近搜索" size="small">
      <n-data-table :columns="searchColumns" :data="searches" size="small" :bordered="false" :bottom-bordered="false" />
    </n-card>
  </div>
</template>
