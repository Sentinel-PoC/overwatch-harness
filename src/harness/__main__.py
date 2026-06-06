"""overwatch-harness daemon entry point.

Boot order (dep-strict):
  1. logging
  2. settings (env file + os.environ)
  3. secrets (Vault)
  4. ledger (sqlite + apply migrations + replay pending queue)
  5. self-test (claude --version then claude -p "ok")
       fail → state=DEGRADED, still ack telegram, refuse spawns
  6. /healthz aiohttp on 127.0.0.1:8787
  7. systemd notify READY=1
  8. heartbeat task (60s)
  9. telegram long-poll (foreground until SIGTERM)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from contextlib import suppress

from harness.audit.plane import PlaneClient
from harness.config import HarnessSettings, load_settings
from harness.db import Ledger
from harness.egress.telegram import send_reply
from harness.http import start_healthz
from harness.ingress.telegram import TelegramIngress
from harness.models import Message
from harness.router import Intent, decide
from harness.secrets import HarnessSecrets, SecretsError, load_secrets
from harness.selftest import DaemonState, StateMachine, run_selftest
from harness.slash import dispatch as slash_dispatch
from harness.spawner import Spawner

log = logging.getLogger("harness")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # SEC-47: httpx logs every request URL at INFO, and python-telegram-bot
    # uses httpx; the Telegram bot token is part of the URL path
    # (https://api.telegram.org/bot<TOKEN>/...). At INFO that token lands
    # in journalctl. Bumping httpx + httpcore to WARNING drops the
    # per-request visibility from the journal entirely while leaving
    # connection-error and 4xx/5xx logging intact.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _sd_notify(msg: str) -> None:
    """Best-effort sd_notify implementation — no python-systemd dependency.

    NOTIFY_SOCKET unset → no-op (development / tests).
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        # Abstract namespace on linux uses leading null byte in path
        addr = sock_path
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode("utf-8"))
    except Exception as exc:
        log.warning("sd_notify(%r) failed: %s", msg, exc)


async def _heartbeat_loop(ledger: Ledger, state: StateMachine, spawner: Spawner) -> None:
    pid = os.getpid()
    while True:
        try:
            await ledger.write_heartbeat(
                pid=pid, state=state.state.value, in_flight_count=spawner.in_flight,
                notes=state.reason,
            )
            # Prune occasionally — cheap; ledger handles the lock
            await ledger.prune_heartbeats(keep_rows=1440)
        except Exception:
            log.exception("heartbeat write failed")
        await asyncio.sleep(60)


def build_dispatch(
    *,
    ledger: Ledger,
    settings: HarnessSettings,
    state: StateMachine,
    spawner: Spawner,
    bot_provider,
    plane: PlaneClient | None,
):
    """Returns the async callback ingress hands new messages to."""

    async def _dispatch(msg: Message, update) -> None:
        chat = update.effective_chat
        chat_id = chat.id
        reply_to = update.effective_message.message_id

        # Mark classifying
        await ledger.update_message_status(msg.message_id, status="classifying")
        decision = await decide(msg, ledger=ledger, settings=settings)
        # Map v0.1 router intents → schema-vocabulary classifier_route.
        # The CHECK constraint in 0001_init.sql was authored for the v0.2
        # LLM-based richer classifier; v0.1 has 3 coarse intents and
        # collapses NEW/RESUME → routine_action.
        _SCHEMA_ROUTE = {
            Intent.SLASH_COMMAND: "slash_command",
            Intent.RESUME_SESSION: "routine_action",
            Intent.NEW_SESSION: "routine_action",
        }
        await ledger.update_message_status(
            msg.message_id, status="routed",
            engagement_id=decision.engagement.engagement_id,
            classifier_route=_SCHEMA_ROUTE[decision.intent],
        )

        bot = bot_provider()
        # ---- slash command path ------------------------------------------
        if decision.intent == Intent.SLASH_COMMAND:
            reply = await slash_dispatch(
                decision.slash_name, decision.slash_args,
                ledger=ledger, settings=settings, engagement=decision.engagement,
            )
            await send_reply(
                bot, chat_id=chat_id, text=reply, reply_to_message_id=reply_to,
                engagement_id=decision.engagement.engagement_id,
                session_id=decision.engagement.current_session_id,
                mode=f"slash:/{decision.slash_name}",
            )
            await ledger.update_message_status(msg.message_id, status="done", reply_text=reply[:1000])
            return

        # ---- spawn path (NEW or RESUME) ----------------------------------
        if not state.can_spawn:
            note = f"daemon-{state.state.value}: {state.reason or 'spawns refused'}"
            await send_reply(
                bot, chat_id=chat_id, text=note, reply_to_message_id=reply_to,
                engagement_id=decision.engagement.engagement_id,
                session_id=None, mode=state.state.value,
            )
            await ledger.update_message_status(msg.message_id, status="failed",
                                                error_summary=note, reply_text=note[:1000])
            return

        sess = await ledger.create_session(
            decision.engagement.engagement_id, prompt_excerpt=msg.body,
        )
        await ledger.update_message_status(msg.message_id, session_id=sess.session_id, status="routed")

        async def _on_sid(sid: str) -> None:
            await ledger.attach_claude_session_id(sess.session_id, sid)

        result = await spawner.spawn(
            msg.body,
            resume_session_id=decision.resume_claude_session_id,
            on_session_id=_on_sid,
        )

        # Quota detection → throttle
        if result.quota_exhausted:
            await state.transition(DaemonState.QUOTA_THROTTLED,
                                    reason=f"quota; reset_window={result.quota_reset_window}")

        status = "completed" if result.exit_code == 0 else "failed"
        await ledger.finalize_session(
            sess.session_id, status=status, exit_code=result.exit_code,
            response_excerpt=result.full_text, duration_ms=result.duration_ms,
            max_quota_reset_window=result.quota_reset_window,
        )
        mode = "resume" if decision.intent == Intent.RESUME_SESSION else "new"
        await send_reply(
            bot, chat_id=chat_id, text=result.full_text or "(no output)",
            reply_to_message_id=reply_to,
            engagement_id=decision.engagement.engagement_id,
            session_id=sess.session_id, mode=mode,
        )
        await ledger.update_message_status(msg.message_id, status="done",
                                            reply_text=(result.full_text or "")[:1000])

        # Best-effort plane audit
        if plane is not None:
            await plane.comment(
                decision.engagement.plane_issue_id or "",
                f"<p>session s{sess.session_id} {status} in {result.duration_ms}ms</p>",
            ) if decision.engagement.plane_issue_id else None

    return _dispatch


async def _run() -> int:
    settings = load_settings()
    _setup_logging(settings.log_level)
    log.info("overwatch-harness starting")

    # Vault-backed secrets
    try:
        secrets: HarnessSecrets = load_secrets(settings)
    except SecretsError as exc:
        log.error("secrets load failed: %s", exc)
        return 78  # EX_CONFIG

    # Ledger
    ledger = await Ledger.open(settings.db_path)
    log.info("ledger opened at %s", settings.db_path)

    # Replay pending
    pending = await ledger.list_pending_on_startup()
    if pending:
        log.warning("%d pending messages from prior run; will reprocess as new arrivals reach them",
                    len(pending))

    state = StateMachine()

    # Build Langfuse env block from resolved Vault secrets.  Only populated
    # when all three keys are present; otherwise empty dict (no injection).
    _lf_env: dict[str, str] = {}
    if (
        secrets.langfuse_public_key
        and secrets.langfuse_secret_key
        and secrets.langfuse_host
    ):
        _lf_env = {
            "LANGFUSE_PUBLIC_KEY": secrets.langfuse_public_key,
            "LANGFUSE_SECRET_KEY": secrets.langfuse_secret_key,
            "LANGFUSE_HOST": secrets.langfuse_host,
        }
        log.info("langfuse env ready; will inject into all spawned claude processes")
    else:
        log.warning(
            "langfuse keys incomplete or absent in Vault; "
            "spawned claude processes will not have Langfuse env"
        )

    spawner = Spawner(
        max_concurrent=settings.max_concurrent_sessions,
        binary=settings.selftest_command,
        timeout_seconds=600.0,
        langfuse_env=_lf_env or None,
    )

    # Self-test
    log.info("running selftest…")
    selftest = await run_selftest(
        binary=settings.selftest_command,
        timeout_seconds=settings.selftest_timeout_seconds,
    )
    if selftest.healthy:
        await state.transition(DaemonState.HEALTHY, reason="selftest passed")
    else:
        await state.transition(DaemonState.DEGRADED, reason=selftest.notes)
        log.warning("selftest FAILED — entering DEGRADED. notes=%s", selftest.notes)

    # /healthz
    runner, site = await start_healthz(
        state=state, ledger=ledger,
        host=settings.healthz_host, port=settings.healthz_port,
    )

    # Plane audit (optional — requires project_id)
    plane: PlaneClient | None = None
    if settings.plane_project_id:
        plane = PlaneClient(
            base_url=settings.plane_base_url,
            workspace=settings.plane_workspace,
            project_id=settings.plane_project_id,
            api_key=secrets.plane_api_key,
            verify_tls=False,
        )

    # Telegram ingress (deferred: bot constructed inside Application)
    bot_holder: list = []

    def _bot():
        return bot_holder[0]

    dispatch = build_dispatch(
        ledger=ledger, settings=settings, state=state,
        spawner=spawner, bot_provider=_bot, plane=plane,
    )
    ingress = TelegramIngress(
        bot_token=secrets.telegram_bot_token, ledger=ledger,
        settings=settings, dispatch=dispatch,
    )
    await ingress.start()
    bot_holder.append(ingress._app.bot)  # internals; acceptable inside daemon

    # Heartbeat task
    hb_task = asyncio.create_task(_heartbeat_loop(ledger, state, spawner))

    # Tell systemd we're ready
    _sd_notify("READY=1")
    log.info("overwatch-harness ready (state=%s)", state.state.value)

    # Wait for SIGTERM/SIGINT
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutdown signal received")
    _sd_notify("STOPPING=1")
    await state.transition(DaemonState.SHUTTING_DOWN, reason="signal")

    hb_task.cancel()
    with suppress(asyncio.CancelledError):
        await hb_task
    await ingress.stop()
    await site.stop()
    await runner.cleanup()
    await ledger.close()
    log.info("overwatch-harness stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
