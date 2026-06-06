"""Telegram inbound — python-telegram-bot Application + allowlist gate.

The ingress is intentionally thin: it converts an inbound Update into a
ledger row, then hands off to a dispatch coroutine the daemon supplies.
The dispatch coroutine handles routing + spawning + reply.

Allowlist policy: fail-closed. Empty allowlist = no chats accepted.
Unknown chat_id is acked with a flat refusal so the operator notices in
their Telegram client (rather than silent drop).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from harness.config import HarnessSettings
from harness.db import Ledger
from harness.models import Message

log = logging.getLogger(__name__)

# Signature: (Message, Update) → coroutine that returns nothing
DispatchCallable = Callable[[Message, Update], Awaitable[None]]


class TelegramIngress:
    """Long-poll Telegram, persist messages, hand off to dispatch.

    Use as `await ingress.start()` from the daemon's main task. Stop with
    `await ingress.stop()` on shutdown.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        ledger: Ledger,
        settings: HarnessSettings,
        dispatch: DispatchCallable,
    ):
        self._token = bot_token
        self._ledger = ledger
        self._settings = settings
        self._dispatch = dispatch
        self._app: Application | None = None
        self._allowlist = settings.allowed_chat_ids()

    async def start(self) -> None:
        if not self._allowlist:
            log.warning(
                "telegram allowlist empty — daemon will refuse ALL inbound. "
                "Set HARNESS_TELEGRAM_ALLOWLIST in env file before traffic."
            )
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.ALL, self._on_message))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=False)
        log.info("telegram ingress started; allowlist=%s", sorted(self._allowlist))

    async def stop(self) -> None:
        if self._app is None:
            return
        try:
            await self._app.updater.stop()
        finally:
            await self._app.stop()
            await self._app.shutdown()
        self._app = None

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or chat is None:
            return
        body = msg.text or msg.caption or ""
        chat_id = chat.id

        if chat_id not in self._allowlist:
            log.warning("telegram: rejecting unallowlisted chat_id=%d", chat_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="overwatch-harness: this chat is not on the operator allowlist.",
                )
            except Exception:
                pass
            return

        operator = await self._ledger.upsert_operator(
            channel_kind="telegram",
            channel_user_id=str(chat_id),
            display_name=(msg.from_user.full_name if msg.from_user else None),
        )
        m = await self._ledger.insert_message(
            operator_id=operator.operator_id,
            channel_kind="telegram",
            channel_msg_id=str(msg.message_id),
            body=body,
            reply_to_msg_id=str(msg.reply_to_message.message_id) if msg.reply_to_message else None,
        )
        if m is None:
            log.info("telegram: replay update_id=%s; skipping", msg.message_id)
            return
        try:
            await self._dispatch(m, update)
        except Exception:
            log.exception("dispatch failed for message_id=%d", m.message_id)
            await self._ledger.update_message_status(
                m.message_id, status="failed",
                error_summary="dispatch raised; see daemon logs",
            )
