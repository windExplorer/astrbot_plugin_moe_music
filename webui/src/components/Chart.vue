<script setup lang="ts">
/** ECharts 封装：接收 option，容器自适应尺寸，主题色跟随暗色模式。 */
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { isDark } from "../bridge";

echarts.use([
  LineChart,
  PieChart,
  BarChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);

const props = defineProps<{ option: Record<string, unknown>; height?: string }>();
const el = ref<HTMLElement | null>(null);
let chart: echarts.ECharts | null = null;
let ro: ResizeObserver | null = null;

function textColor() {
  return isDark() ? "#d8d8d8" : "#333";
}
function splitLineColor() {
  return isDark() ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.09)";
}

function baseOption(): Record<string, unknown> {
  return {
    backgroundColor: "transparent",
    textStyle: { color: textColor() },
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: textColor() } },
  };
}

function render() {
  if (!el.value) return;
  if (!chart) {
    chart = echarts.init(el.value);
    ro = new ResizeObserver(() => chart?.resize());
    ro.observe(el.value);
  }
  const option = {
    ...baseOption(),
    ...props.option,
  };
  chart.setOption(option, true);
}

onMounted(render);
watch(
  () => props.option,
  () => render(),
  { deep: true },
);
watch(isDark, () => render());
onBeforeUnmount(() => {
  ro?.disconnect();
  chart?.dispose();
  chart = null;
});
</script>

<template>
  <div ref="el" :style="{ width: '100%', height: height || '320px' }" />
</template>
