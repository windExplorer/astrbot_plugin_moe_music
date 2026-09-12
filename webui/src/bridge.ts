/**
 * AstrBot 插件 Page 桥接封装（官方 bridge：window.AstrBotPluginPage）。
 * - ready() 等待父页面初始上下文（含 isDark / locale）；
 * - apiGet / apiPost 直接 resolve 业务数据；
 * - subscribeSSE 支持（实时任务页）。
 */

interface BridgeContext {
  pluginName?: string;
  displayName?: string;
  pageName?: string;
  locale?: string;
  isDark?: boolean;
}

interface Bridge {
  ready(): Promise<BridgeContext>;
  getContext(): BridgeContext | null;
  onContext(handler: (ctx: BridgeContext) => void): () => void;
  apiGet(endpoint: string, params?: Record<string, unknown>): Promise<unknown>;
  apiPost(endpoint: string, body?: unknown): Promise<unknown>;
  subscribeSSE(
    endpoint: string,
    handlers: Record<string, (event: unknown) => void>,
    params?: Record<string, unknown>,
  ): Promise<number>;
  unsubscribeSSE(id: number): Promise<void>;
}

function getBridge(): Bridge | null {
  const w = window as unknown as { AstrBotPluginPage?: Bridge };
  if (w.AstrBotPluginPage) return w.AstrBotPluginPage;
  return null;
}

let ctx: BridgeContext | null = null;
const ctxListeners = new Set<(ctx: BridgeContext) => void>();

/** 初始化：等 bridge 就绪并缓存上下文。超时抛错（提示从后台打开）。 */
export async function initBridge(timeoutMs = 3000): Promise<Bridge> {
  const bridge = getBridge();
  if (!bridge) {
    throw new Error("未检测到 AstrBot 插件桥接，请从 AstrBot 后台的插件拓展页打开本页面");
  }
  ctx = await withTimeout(bridge.ready(), timeoutMs);
  bridge.onContext?.((c) => {
    ctx = c;
    ctxListeners.forEach((fn) => fn(c));
  });
  return bridge;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("插件桥接连接超时")), ms);
    p.then((v) => {
      clearTimeout(timer);
      resolve(v);
    }).catch((e) => {
      clearTimeout(timer);
      reject(e);
    });
  });
}

export function getCtx(): BridgeContext | null {
  return ctx;
}

export function isDark(): boolean {
  return Boolean(ctx?.isDark);
}

export function onCtxChange(fn: (ctx: BridgeContext) => void): () => void {
  ctxListeners.add(fn);
  return () => ctxListeners.delete(fn);
}

export async function apiGet<T>(endpoint: string, params?: Record<string, unknown>): Promise<T> {
  const bridge = getBridge();
  if (!bridge) throw new Error("桥接不可用");
  return (await bridge.apiGet(endpoint, params)) as T;
}

export async function apiPost<T>(endpoint: string, body?: unknown): Promise<T> {
  const bridge = getBridge();
  if (!bridge) throw new Error("桥接不可用");
  return (await bridge.apiPost(endpoint, body)) as T;
}

export async function subscribeSSE(
  endpoint: string,
  onMessage: (parsed: unknown) => void,
  params?: Record<string, unknown>,
): Promise<(() => void) | null> {
  const bridge = getBridge();
  if (!bridge?.subscribeSSE) return null;
  const id = await bridge.subscribeSSE(
    endpoint,
    {
      onMessage(event: unknown) {
        const e = event as { parsed?: unknown };
        onMessage(e?.parsed);
      },
      onError() {
        /* 断线由上层轮询兜底 */
      },
    },
    params,
  );
  return () => {
    bridge.unsubscribeSSE?.(id);
  };
}
