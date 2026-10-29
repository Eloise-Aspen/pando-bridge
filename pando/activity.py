"""进程内客户端活动门槛；不探测网络，也不持久化连接状态。"""

import asyncio


class ClientActivity:
    def __init__(self, require_client=False):
        self.require_client = require_client
        self._clients = set()
        self.revision = 0
        self._changed = asyncio.Event()

    @property
    def allowed(self):
        return not self.require_client or bool(self._clients)

    def connected(self, client):
        if client in self._clients:
            return
        self._clients.add(client)
        self._notify()

    def disconnected(self, client):
        if client not in self._clients:
            return
        self._clients.discard(client)
        self._notify()

    def _notify(self):
        # 每次替换事件；所有旧等待者都能醒来，不会彼此 clear 丢掉通知。
        self.revision += 1
        old, self._changed = self._changed, asyncio.Event()
        old.set()

    async def wait_until_allowed(self):
        while not self.allowed:
            await self._changed.wait()

    async def wait_for_check(self, seconds, since=None):
        if not self.require_client:
            await asyncio.sleep(seconds)
            return
        if since is not None and since != self.revision:
            await self.wait_until_allowed()
            return
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        await self.wait_until_allowed()
