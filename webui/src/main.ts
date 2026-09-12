import { createApp, ref } from "vue";
import naive from "naive-ui";
import App from "./App.vue";
import { initBridge } from "./bridge";

// 初始化桥接后挂载应用（失败也挂载，页面内展示错误提示）
const bridgeError = ref<string | null>(null);

initBridge()
  .catch((e: Error) => {
    bridgeError.value = e.message;
  })
  .finally(() => {
    const app = createApp(App);
    app.use(naive);
    app.provide("bridgeError", bridgeError);
    app.mount("#app");
  });
