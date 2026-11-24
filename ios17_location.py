"""Synchronous bridge to the iOS 17+ DVT location service.

The tunnel and DVT channel live on one event loop for the whole route.
"""

import asyncio
import threading


class DvtPointStream:
    def __init__(self, udid=None):
        self.udid = udid
        self.dev = {"udid": udid or "iPhone"}
        self.reconnects = 0
        self.loop = None
        self.thread = None
        self.tunnel = None
        self.dvt = None
        self.location = None
        self.ready = threading.Event()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    def _call(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _open(self):
        from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

        self.tunnel = PreferredRsdTunnel(serial=self.udid)
        try:
            rsd = await self.tunnel.__aenter__()
            self.dev = {"udid": rsd.udid}
            self.dvt = DvtProvider(rsd)
            await self.dvt.__aenter__()
            self.location = LocationSimulation(self.dvt)
            await self.location.__aenter__()
            self.reconnects += 1
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        for name in ("location", "dvt", "tunnel"):
            obj = getattr(self, name)
            setattr(self, name, None)
            if obj is not None:
                try:
                    await obj.__aexit__(None, None, None)
                except Exception:
                    pass

    def ensure(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            self.ready.wait(5)
        if self.location is None:
            self._call(self._open())

    def point(self, lat, lon):
        self.ensure()
        self._call(self.location.set(lat, lon), timeout=10)

    def reset(self):
        self.ensure()
        self._call(self.location.clear(), timeout=10)

    def hard_reset(self):
        if self.loop and self.location:
            self._call(self._close(), timeout=10)

    def close(self, clear=True):
        if self.loop:
            try:
                if clear and self.location:
                    try:
                        self._call(self.location.clear(), timeout=10)
                    except Exception:
                        pass
                self._call(self._close(), timeout=10)
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=5)
                self.loop = None
                self.thread = None
