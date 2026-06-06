"""aiohttp /healthz on 127.0.0.1.

Bound to localhost only — see standing principle "Workstation lockout
protected: no inbound from the network." External monitoring queries
this from on-host or via SSH tunnel, never directly.
"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from harness.db import Ledger
from harness.selftest import StateMachine

log = logging.getLogger(__name__)


def build_app(*, state: StateMachine, ledger: Ledger) -> web.Application:
    app = web.Application()

    async def healthz(request: web.Request) -> web.Response:
        hb = await ledger.latest_heartbeat()
        body: dict[str, Any] = {
            "state": state.state.value,
            "reason": state.reason,
            "last_heartbeat": (
                {
                    "ts": hb.ts,
                    "in_flight": hb.in_flight_count,
                    "pid": hb.pid,
                }
                if hb is not None
                else None
            ),
        }
        # state HEALTHY → 200; anything else → 503 so load balancers / oncall
        # treat it as "do not route" if we ever exposed it externally
        status = 200 if state.can_spawn else 503
        return web.json_response(body, status=status)

    app.router.add_get("/healthz", healthz)
    return app


async def start_healthz(
    *,
    state: StateMachine,
    ledger: Ledger,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> tuple[web.AppRunner, web.TCPSite]:
    app = build_app(state=state, ledger=ledger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("healthz bound on http://%s:%d/healthz", host, port)
    return runner, site
