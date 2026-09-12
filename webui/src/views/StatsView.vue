<script setup lang="ts">
/** 统计页：汇总卡片 + 趋势面积图 + 分布饼图 + 排行榜（范围：今天~近一年）。 */
import { computed, onMounted, ref, watch } from "vue";
import { NButton, NCard, NGrid, NGridItem, NSelect, NStatistic, NSpin, NTabPane, NTabs, useMessage } from "naive-ui";
import { apiGet } from "../bridge";
import Chart from "../components/Chart.vue";

const msg = useMessage();
const range = ref("7d");
const loading = ref(false);
const overview = ref<Record<string, any> | null>(null);
const trend = ref<{ bucket: string; dates: string[]; search: number[]; play: number[] } | null>(null);
const top = ref<{ users: any[]; groups: any[]; tracks: any[] } | null>(null);
const dist = ref<Record<string, any> | null>(null);

const rangeOptions = [
  { label: "今天（按小时）", value: "today" },
  { label: "近一天（按小时）", value: "24h" },
  { label: "近三天", value: "3d" },
  { label: "近一周", value: "7d" },
  { label: "近 14 天", value: "14d" },
  { label: "近一月", value: "30d" },
  { label: "近 90 天", value: "90d" },
  { label: "近一年（按月）", value: "1y" },
];

const PALETTE = ["#ff7eb9", "#7ec9ff", "#ffd166", "#a78bfa", "#5ad8a6", "#ff9d4d", "#269a99", "#ff99c5"];

const SOURCE_NAMES: Record<string, string> = {
  kw: "酷我",
  kg: "酷狗",
  tx: "QQ音乐",
  wy: "网易云",
  mg: "咪咕",
  xm: "虾米",
  bd: "百度",
};

function labelOf(k: string): string {
  return SOURCE_NAMES[k] ?? k;
}

async function load() {
  loading.value = true;
  try {
    const [o, t, tp, d] = await Promise.all([
      apiGet<any>("stats/overview", { range: range.value }),
      apiGet<any>("stats/trend", { range: range.value }),
      apiGet<any>("stats/top", { limit: 10 }),
      apiGet<any>("stats/dist", { range: range.value }),
    ]);
    overview.value = o;
    trend.value = t;
    top.value = tp;
    dist.value = d;
  } catch (e: any) {
    msg.error(`加载统计失败：${e.message ?? e}`);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(range, load);

const trendOption = computed(() => {
  const t = trend.value;
  if (!t) return {};
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["搜索", "点歌"] },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: t.dates, boundaryGap: false },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      {
        name: "搜索",
        type: "line",
        smooth: true,
        data: t.search,
        itemStyle: { color: PALETTE[0] },
        areaStyle: { opacity: 0.18 },
      },
      {
        name: "点歌",
        type: "line",
        smooth: true,
        data: t.play,
        itemStyle: { color: PALETTE[1] },
        areaStyle: { opacity: 0.18 },
      },
    ],
  };
});

function pieOption(rows: any[], translate = false) {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, type: "scroll" },
    series: [
      {
        type: "pie",
        radius: ["38%", "66%"],
        center: ["50%", "44%"],
        data: (rows || []).map((r) => ({ name: translate ? labelOf(r.k) : r.k, value: r.n })),
        color: PALETTE,
        label: { formatter: "{b}\n{d}%" },
      },
    ],
  };
}

function barOption(rows: any[], nameKey: string, subKey: string | null) {
  const data = (rows || []).slice(0, 8);
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", minInterval: 1 },
    yAxis: {
      type: "category",
      data: data.map((r) =>
        subKey && r[subKey] ? `${r[nameKey]}（${r[subKey]}）` : String(r[nameKey]),
      ),
    },
    series: [{ type: "bar", data: data.map((r) => r.n), itemStyle: { color: PALETTE[0] }, barMaxWidth: 18 }],
  };
}
</script>

<template>
  <n-spin :show="loading">
    <div style="display: flex; justify-content: flex-end; gap: 10px; margin-bottom: 12px">
      <n-select v-model:value="range" :options="rangeOptions" style="width: 190px" />
      <n-button @click="load">🔄 刷新</n-button>
    </div>

    <n-grid :cols="24" :x-gap="12" :y-gap="12">
      <n-grid-item :span="6"><n-card size="small"><n-statistic label="累计搜索" :value="overview?.overall_search ?? 0" /></n-card></n-grid-item>
      <n-grid-item :span="6"><n-card size="small"><n-statistic label="累计点歌" :value="overview?.overall_play ?? 0" /></n-card></n-grid-item>
      <n-grid-item :span="6"><n-card size="small"><n-statistic :label="`本范围搜索`" :value="overview?.search_total ?? 0" /></n-card></n-grid-item>
      <n-grid-item :span="6"><n-card size="small"><n-statistic :label="`本范围点歌`" :value="overview?.play_total ?? 0" /></n-card></n-grid-item>

      <n-grid-item :span="24">
        <n-card title="趋势" size="small">
          <Chart :option="trendOption" height="300px" />
        </n-card>
      </n-grid-item>

      <n-grid-item :span="8"><n-card size="small"><n-statistic label="平均全流程耗时" :value="`${overview?.play_avg_total_ms ?? 0} ms`" /></n-card></n-grid-item>
      <n-grid-item :span="8"><n-card size="small"><n-statistic label="平均排队耗时" :value="`${overview?.play_avg_queue_ms ?? 0} ms`" /></n-card></n-grid-item>
      <n-grid-item :span="8"><n-card size="small"><n-statistic label="音质降级率">
        <template #default>
          {{ overview && overview.play_total ? Math.round((overview.quality_fallback / overview.play_total) * 100) : 0 }}%
        </template>
      </n-statistic></n-card></n-grid-item>

      <n-grid-item :span="12">
        <n-card title="音质分布" size="small"><Chart :option="pieOption(dist?.quality)" /></n-card>
      </n-grid-item>
      <n-grid-item :span="12">
        <n-card title="发送方式分布" size="small"><Chart :option="pieOption(dist?.send_mode)" /></n-card>
      </n-grid-item>
      <n-grid-item :span="12">
        <n-card title="选歌方式" size="small"><Chart :option="pieOption(dist?.selection)" /></n-card>
      </n-grid-item>
      <n-grid-item :span="12">
        <n-card title="平台分布" size="small"><Chart :option="pieOption(dist?.source, true)" /></n-card>
      </n-grid-item>

      <n-grid-item :span="24">
        <n-card size="small">
          <n-tabs type="line" animated>
            <n-tab-pane name="users" tab="用户排行">
              <Chart :option="barOption(top?.users, 'user_name', 'user_id')" height="300px" />
            </n-tab-pane>
            <n-tab-pane name="groups" tab="群排行">
              <Chart :option="barOption(top?.groups, 'group_name', 'group_id')" height="300px" />
            </n-tab-pane>
            <n-tab-pane name="tracks" tab="歌曲排行">
              <Chart :option="barOption(top?.tracks, 'track_name', 'singer')" height="300px" />
            </n-tab-pane>
          </n-tabs>
        </n-card>
      </n-grid-item>
    </n-grid>
  </n-spin>
</template>
