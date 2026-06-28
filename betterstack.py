"""Better Stack Telemetry emitter for the parallel controller.

Pushes the same signals we persist to SQLite (SoC, in/out/solar/grid watts,
RSSI) up to a Better Stack Telemetry source, plus BLE connection events
(connect / disconnect / reconnect_fail) so drop rate is chartable/alertable
there too. This is the metrics path; the per-unit heartbeat dead-man's switch
(controller.heartbeat_watchdog) is the separate paging path.

Config (env):
    BETTERSTACK_SOURCE_URL    ingest URL of a Better Stack Telemetry source
    BETTERSTACK_SOURCE_TOKEN  that source's token (sent as Bearer)
    BETTERSTACK_METRICS_INTERVAL_S  per-unit sample cadence (default 60s)

Design notes:
  * Sends are fire-and-forget tasks with their exceptions consumed, so a slow
    or down Better Stack never blocks (or crashes) the BLE control loop. We
    learned the hard way that orphaned tasks with unretrieved exceptions are a
    footgun — tasks are tracked and their results discarded explicitly.
  * Sample emits are rate-limited per unit to the DB sample cadence so the two
    stores stay roughly in lockstep; events are emitted immediately.
"""

import asyncio
from datetime import datetime, timezone

DEFAULT_INTERVAL_S = 60


def from_env(env):
    """Build a BetterStackEmitter from an env mapping, or None if unconfigured."""
    url = env.get("BETTERSTACK_SOURCE_URL")
    token = env.get("BETTERSTACK_SOURCE_TOKEN")
    if not (url and token):
        return None
    try:
        interval = int(env.get("BETTERSTACK_METRICS_INTERVAL_S", DEFAULT_INTERVAL_S))
    except ValueError:
        interval = DEFAULT_INTERVAL_S
    return BetterStackEmitter(url, token, interval_s=interval)


class BetterStackEmitter:
    def __init__(self, url, token, interval_s=DEFAULT_INTERVAL_S):
        self._url = url
        self._token = token
        self._interval = interval_s
        self._session = None
        self._last = {}        # unit -> ts of last sample emit (rate limiting)
        self._tasks = set()    # in-flight POST tasks (exceptions consumed)

    @staticmethod
    def _dt(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def maybe_emit_sample(self, unit, ts, fields):
        """Emit a metrics sample for a unit, rate-limited to the configured
        interval. fields: dict of numeric signals (soc, watts_in, ...)."""
        last = self._last.get(unit)
        if last is not None and (ts - last) < self._interval:
            return
        self._last[unit] = ts
        body = {"dt": self._dt(ts), "kind": "sample", "unit": unit,
                "message": "ecoflow sample %s" % unit}
        body.update({k: v for k, v in fields.items() if v is not None})
        self._spawn(body)

    def emit_event(self, unit, ts, event):
        """Emit a BLE lifecycle event immediately (not rate-limited)."""
        self._spawn({"dt": self._dt(ts), "kind": "event", "unit": unit,
                     "event": event, "message": "ecoflow %s %s" % (unit, event)})

    def _spawn(self, body):
        task = asyncio.create_task(self._post(body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, body):
        import aiohttp
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"Authorization": "Bearer %s" % self._token,
                             "Content-Type": "application/json"})
            async with self._session.post(self._url, json=body) as resp:
                await resp.read()
        except Exception as e:
            print("WARN: Better Stack emit failed: %s" % e)

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        if self._session is not None:
            await self._session.close()
