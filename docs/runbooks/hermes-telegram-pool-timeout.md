# Hermes Telegram Pool Timeout Runbook

Date: 2026-05-18

## Current incident

`Hermes_Viewport_Bot` receives Telegram updates but stops replying.

Verified live evidence:

- Container: `docker-viewport` daemon, container `hermes`, image `viewport-corp/hermes-agent:v0.12.0`.
- Telegram queue: `getWebhookInfo.pending_update_count = 0`; `getUpdates = []`.
- Direct Bot API `sendMessage` from inside the same container succeeds.
- Hermes logs repeat:
  `telegram.error.TimedOut: Pool timeout: All connections in the connection pool are occupied. Request was not sent to Telegram.`
- Container healthcheck is failing because it curls `localhost:8642/health`, but no listener is present there.
- Visible status cron is paused in `/opt/data/cron/jobs.json`.

Conclusion: Telegram and the bot token are alive. Hermes consumes inbound updates, then its internal outbound `python-telegram-bot` / `httpx` send client pool wedges or exhausts.

## Official documentation basis

Primary references:

- PTB `HTTPXRequest`: https://docs.python-telegram-bot.org/en/latest/telegram.request.httpxrequest.html
- PTB `ApplicationBuilder`: https://docs.python-telegram-bot.org/en/v22.6/telegram.ext.applicationbuilder.html
- PTB errors: https://docs.python-telegram-bot.org/en/stable/telegram.error.html
- HTTPX timeouts: https://www.python-httpx.org/advanced/timeouts/
- HTTPX exceptions: https://www.python-httpx.org/exceptions/
- HTTPX resource limits: https://www.python-httpx.org/advanced/resource-limits/

Official behavior that matters:

- PTB uses `HTTPXRequest` for Bot API calls.
- `pool_timeout` is the time spent waiting for a free HTTP connection from the pool.
- If more Bot API requests run concurrently than the pool can serve before `pool_timeout`, PTB raises `telegram.error.TimedOut`.
- PTB separates normal outbound Bot API requests from `getUpdates` polling requests. A clean polling queue does not prove outbound sends are healthy.
- PTB warns that raising `pool_timeout` alone may not be enough when `concurrent_updates`, `Application.create_task`, non-blocking handlers, or job queues create parallel Bot API calls.
- HTTPX defines `PoolTimeout` as timeout while acquiring a connection from the pool.

## Live Hermes-specific findings

Live container dependency versions:

- `python-telegram-bot 22.7`
- `httpx 0.28.1`
- `httpcore 1.0.9`

Hermes already creates separate `HTTPXRequest` objects for outbound requests and polling:

- outbound default `HERMES_TELEGRAM_HTTP_POOL_SIZE = 512`
- outbound default `HERMES_TELEGRAM_HTTP_POOL_TIMEOUT = 8.0`
- polling has a separate request object

Therefore the permanent fix is not only "increase pool size". The system also needs bounded outbound concurrency, pool-wedge recovery, and health monitoring.

## Permanent fix

Implement all of these together:

1. Add outbound backpressure.
   - Wrap every Telegram outbound send/edit path behind one shared `asyncio.Semaphore`.
   - Start with `HERMES_TELEGRAM_SEND_CONCURRENCY=32`.
   - Keep concurrency far below `HERMES_TELEGRAM_HTTP_POOL_SIZE`.
   - Include all direct send paths: normal replies, fallbacks, edits, cron delivery, and relay/status messages.

2. Add Telegram rate limiting.
   - Use PTB `AIORateLimiter` or a Hermes-owned per-chat/global limiter.
   - Prevent large agent outputs, fallback retries, and cron/status sends from creating Bot API bursts.

3. Stop retry storms.
   - On PTB `TimedOut` caused by pool acquire timeout, do not immediately try Markdown fallback through the same wedged client.
   - Count consecutive pool timeouts.
   - After a small threshold, mark the Telegram adapter degraded and trigger recovery.

4. Reset or recreate the outbound request pool.
   - Hermes currently has logic to drain polling connections, but the failing path is outbound `send_message`.
   - Add a safe outbound request reset/recreate path, or restart the Telegram adapter/gateway process when outbound pool timeouts repeat.
   - Do not rely on manual container restarts.

5. Make healthcheck reflect real bot health.
   - Either start a real HTTP health server on `8642`, or change the healthcheck.
   - Health must fail when outbound sends are wedged, not only when the process exits.
   - Include recent successful send timestamp, consecutive pool-timeout count, gateway state freshness, and Telegram adapter state.

6. Add a watchdog.
   - If outbound sends fail with pool timeout N times in M minutes, restart the Telegram adapter/gateway.
   - Emit one alert after recovery, not an infinite failure loop.

7. Resume visible status only after the send path is protected.
   - The paused Viewport-Ops cron should not be resumed until the outbound send path has backpressure and health recovery.

## Verification gate

Do not call this fixed until all pass:

- `Hermes_Viewport_Bot` replies to private messages after heavy work.
- `getWebhookInfo.pending_update_count` stays near zero.
- Logs show no repeated pool-timeout loop.
- Direct Bot API send and Hermes internal send both work.
- Docker health is `healthy`.
- A forced burst test does not silence the bot.
- Visible status cron runs once and posts to Viewport-Ops.

## What this is not

- Not a Telegram group permission issue.
- Not the old `hermes-bccl` / `BuddhaGroup_Bot` container.
- Not a dead token, because direct Bot API sending works from the Hermes container.
- Not solved permanently by restarting the container.
