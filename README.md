# overwatch-harness

Channel-multiplexer + classifier + router daemon for the Overwatch Platform.

**Status: v0.1 scaffold (OPS-197). Business logic not yet implemented.**

## What this is

A workstation-resident Python systemd-user daemon that:

1. Receives operator messages from Telegram (Discord deferred to v0.2).
2. Per message, spawns a fresh `claude -p --output-format stream-json` subprocess against the operator's Claude Max OAuth credentials. Zero per-token API cost beyond the flat-rate Max plan.
3. Streams stdout back to the operator via the same channel, chunked.
4. Records every routing decision in a sqlite ledger (`~/.local/state/overwatch-harness/harness.db`).
5. Hooks fire automatically inside the spawned session, so Langfuse OTEL traces land for every LLM call without daemon-side telemetry code.

## What this is NOT

- Not a replacement for Claude Code itself. Claude Code stays the operator's interactive design tool.
- Not an autonomous agent. The daemon doesn't reason about messages — it routes them and lets a Claude session do the work.
- Not a Gemini or other-model client. Claude only.
- Not a deployment automation tool. It can hand work off to sentinel-agent or to a Plane issue, but it doesn't act on infra directly.

## Architecture

See the canonical plan at `~/.claude/plans/im-concerned-we-aren-t-structured-tower.md` (operator-local). Tracked as OPS-197.

## v0.1 scope (planned, not yet implemented)

- Telegram long-poll ingress with allowlist gate
- Per-message rule-based router into one of four destinations: `status_query`, `routine_action`, `spec_request`, `security_event`
- Engagement-based session resumption (30-min idle window auto-resume; outside window, fresh session)
- Slash commands: `/help`, `/status`, `/sessions`, `/end`, `/new`
- Startup self-test: refuses to accept Telegram traffic if `claude -p` is not working
- Bounded concurrency (`max_concurrent_sessions = 4`)
- sqlite ledger with idempotency on Telegram `update_id`
- Plane integration: one issue per engagement (not per message)
- Vault-backed secrets via hvac

## Deploy

(After implementation lands in a separate PR.)

```bash
cd ~/repos/overwatch-harness
./deploy/install.sh
systemctl --user daemon-reload
systemctl --user enable --now overwatch-harness.service
```

Replaces the (currently crash-looping) `claude-channels.service`. See OPS-197 for the cutover plan; Telegram allows only one `getUpdates` consumer per token, so this is a cutover not a parallel run.

## Ledger location

Runtime state lives at `~/.local/state/overwatch-harness/harness.db` (not in this repo). The schema is in `migrations/0001_init.sql`.

## Reference

- Approved plan: OPS-197 in Plane (haists-it-consulting workspace)
- Independent design review: in-session Judge verdict PROCEED-WITH-CHANGES (10 changes incorporated)
- Companion: sentinel-agent (overwatch repo) — separate daemon, not affected by this one
