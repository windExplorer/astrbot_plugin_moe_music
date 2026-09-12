"""点歌任务队列：多用户同时点歌时的并发控制与排队统计。

- 固定数量 worker 协程消费任务，限制同时执行的后端请求 / 下载 / 发送数量；
- 等待队列有上限，队满时直接拒绝（用户侧提示「点歌的人太多」），不无限堆积；
- 每个任务记录排队耗时（入队 → 开始执行），供 play/search 记录的 queue_wait_ms 使用；
- QPS 节流仍由 MusicApiClient._throttle 统一负责，本队列只控制并发度。

用法：
    result = await queue.submit(fn, arg1, arg2)   # fn(*args) 执行于 worker 中
    队列满时抛 asyncio.QueueFull。
"""

import asyncio
import time

from astrbot.api import logger


class SongTaskQueue:
    """固定并发度的异步点歌任务队列。"""

    def __init__(self, concurrency: int = 2, max_pending: int = 20):
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, max_pending))
        self._workers: list[asyncio.Task] = []
        self._running = False
        self.active = 0  # 正在执行的任务数（含排队结束进入执行的）
        # 统计（自检展示用）
        self.submitted = 0
        self.rejected = 0

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def snapshot(self) -> dict:
        """队列当前状态快照（WebUI / 自检展示用）。"""
        return {
            "running": self._running,
            "concurrency": self._concurrency,
            "max_pending": self._queue.maxsize,
            "pending": self.pending,
            "active": self.active,
            "submitted": self.submitted,
            "rejected": self.rejected,
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"moe-music-worker-{i}"))
        logger.info(f"[萌音点歌] 任务队列已启动：并发 {self._concurrency}，等待上限 {self._queue.maxsize}")

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def submit(self, fn, *args, **kwargs):
        """提交任务并等待其完成，返回 fn 的返回值；队列满抛 QueueFull。

        worker 未启动时自动启动（防调用方漏调 start 导致任务无人消费而挂起）。
        """
        if not self._running:
            await self.start()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        try:
            self._queue.put_nowait((fn, args, kwargs, fut, time.monotonic()))
        except asyncio.QueueFull:
            self.rejected += 1
            logger.warning(f"[萌音点歌] 任务队列已满（{self._queue.maxsize}），拒绝新任务")
            raise
        self.submitted += 1
        return await fut

    async def _worker(self, index: int) -> None:
        while True:
            fn, args, kwargs, fut, enqueued_at = await self._queue.get()
            try:
                if fut.done():  # 提交方已取消（理论上不会发生，防御）
                    continue
                queue_wait_ms = int((time.monotonic() - enqueued_at) * 1000)
                self.active += 1
                try:
                    result = await fn(*args, queue_wait_ms=queue_wait_ms, **kwargs)
                finally:
                    self.active -= 1
                if not fut.done():
                    fut.set_result(result)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.cancel()
                raise
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
            finally:
                self._queue.task_done()
