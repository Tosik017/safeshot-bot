# SafeShot Bot

Telegram bot for **safe link previews** in group chats. When someone posts a link, the bot opens it in an isolated headless Chromium (full mobile emulation), takes a screenshot, extracts metadata (OG / JSON-LD), and replies with **a single message per link**: an instant warning placeholder that transforms in place into the final preview card — so nobody has to click an unknown link to see what's behind it.

Bot UI language: Ukrainian. Built for anti-phishing protection of marketplace/community groups.

## How it responds (single-message flow)

1. A user posts a link → the bot instantly replies with **one photo message**: a warning placeholder image + a short anti-phishing notice (quote-styled caption). 
2. When the safe preview is ready, the bot **edits that same message** (`editMessageMedia`): the placeholder becomes a screenshot of the page's first screen, the caption becomes a card (site, title, brand, price, rating, description, sender attribution, anti-phishing disclaimer).
3. If a screenshot is impossible (anti-bot site, timeout), the caption is edited into a text card; the warning image stays. Failures edit the caption into a short warning.
4. Cached links get the final card immediately, no placeholder stage.
5. Nothing is ever deleted and no extra messages are posted — **one message per link**, the feed stays clean.

Why a photo placeholder: Telegram cannot edit a text message into a photo, so the status message starts as a photo and is morphed via `editMessageMedia`.

**Trusted domains (whitelist):** links to domains in `TRUSTED_DOMAINS` are not processed at all — the bot silently reacts with 👌. Matching is strict (exact hostname or dot-boundary subdomain; `youtube.com.evil.top` does not match; userinfo tricks and IDN homoglyphs don't pass). Note: ✅ cannot be used — the Bot API allows only a fixed reaction set; if reactions are restricted in your group, allow 👌.

**Mobile format:** Chromium runs full mobile emulation (390×844 viewport, DPR 2, `is_mobile`, `has_touch`, Chrome-for-Android UA), so sites serve their real mobile layout and the preview is readable on phones. Long pages are clamped; only the first screen is sent (marked in the card).

## Architecture

| File | Role |
|---|---|
| `main.py` | FastAPI (`/`, `/ping`, `/health`) + aiogram polling, graceful shutdown, logging |
| `bot.py` | Telegram handlers: group allow-list, topic denylist, trusted-domain whitelist, admin bypass, duplicate-spam escalation (up to a 5-min mute), rate limiting, cache, queue, single-message flow, card building, placeholder generation |
| `screenshot.py` | Playwright + Pillow: hardened Chromium launch flags, per-request SSRF filtering in a route handler, heavy-content blocking (media/ws/fonts/ads), capture-height clamp, slicing, periodic browser restart + crash self-recovery |
| `metadata.py` | httpx metadata fetch with body-size limit and manual redirect walking (SSRF check on every hop) |
| `security.py` | `is_safe()` SSRF filter — resolves all A/AAAA records, blocks private/loopback/link-local/metadata ranges, ports other than 80/443 |
| `queue_manager.py` | Bounded queue: depth + per-chat quota + RAM watchdog + dedup + timeout; supervised worker |
| `cache.py` | In-RAM cache (Telegram `file_id` + metadata), sha256 keys, per-kind TTL, negative cache |
| `config.py` | Env-based config, fail-closed defaults, resource limits |
| `placeholder.png` | Optional custom placeholder (see Customization) |

## Configuration (environment variables)

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token (fail-fast if missing) |
| `ALLOWED_GROUP_IDS` | yes* | Space/comma-separated group IDs. The bot leaves any other chat. *Empty list without `ALLOW_OPEN_MODE=true` aborts startup (secure by default) |
| `ALLOW_OPEN_MODE` | no | `true` to deliberately run without a group allow-list |
| `DISABLED_THREADS` | no | Topic denylist: `group:thread` pairs, `group:general` for the General topic |
| `TRUSTED_DOMAINS` | no | Whitelist, comma/space separated. Unset → default `youtube.com youtu.be wikipedia.org github.com`; set empty → whitelist disabled. Deliberately excludes `google.com` (Forms/Drive phishing), `t.me` (scam channels), social networks and marketplaces |
| `CHROMIUM_SANDBOX` | no | `on` to enable the real Chromium sandbox (needs userns/seccomp — works locally/VPS, not on Render Free) |
| `JITLESS` | no | `on` disables the V8 JIT (smaller RCE surface, slower heavy pages) |
| `LOG_LEVEL` | no | Default `INFO` |
| `PORT` | no | Default `8000` |

Key tunables live as constants in `config.py` / `screenshot.py` (queue depth, per-chat quota, RAM threshold, timeouts, capture clamp, `TRUSTED_REACTION` emoji, etc.) — each is commented with the reason for its value.

## Security model

- **SSRF / DNS rebinding / cloud metadata:** `is_safe()` is enforced at four layers — before enqueue, again before `goto`, on **every** Chromium request (route handler), and on **every** httpx redirect hop (manual redirect walking, no blind `follow_redirects`).
- **Container hardening:** fresh Chromium (Playwright pinned to the image version), non-root `pwuser`, background networking/telemetry disabled, WebRTC off, service workers blocked, downloads off.
- **DoS/OOM protection:** bounded queue with per-chat quota, RAM watchdog, single-screenshot semaphore, capture-height clamp at the browser level, response-body limit, differentiated cache TTLs, periodic browser restart.
- **Secure by default:** fail-closed group allow-list, secrets only via env, `/health` exposes booleans only.

Known limitations (honest list): `--no-sandbox` is unavoidable on Render Free (compensated by non-root); no egress filtering on Free (SSRF defense is app-level; full defense needs a VPS + nftables); a narrowed-but-open DNS-rebinding window remains without pin-to-IP; the free instance sleeps without external inbound HTTP — use an external pinger on `/ping` (~10 min interval).

## Customization

- **Placeholder image:** drop a `placeholder.png` (vertical, **780×1280** — the exact size of the first screenshot frame, so the message doesn't change shape when the image is swapped) into the repo root and push. Without it the bot generates a warning image at startup.
- **Whitelist reaction:** `TRUSTED_REACTION` constant in `bot.py` (must be from the Bot API reaction set).
- **Alert texts:** caption/disclaimer constants at the top of `bot.py`.

## Deploy

**Render (Blueprint):** `render.yaml` ships a free-plan web/docker service with `healthCheckPath: /ping` and `sync: false` secrets. Push to GitHub → connect the repo → set env vars. Free plan has no Shell; restart via an empty commit (`git commit --allow-empty -m "restart" && git push`) or Suspend/Resume. Add an external pinger (UptimeRobot / cron-job.org) hitting `/ping` to prevent sleep.

**Local:** `docker-compose.yml` runs with stronger isolation than Render Free allows (cap_drop ALL, read-only rootfs, memory/pids limits, optional real sandbox via `CHROMIUM_SANDBOX=on` + Playwright's seccomp profile).

The Docker image is `mcr.microsoft.com/playwright/python:v1.60.0-noble` — the pip `playwright` version in `requirements.txt` must match the image tag; browsers are preinstalled, no `playwright install` needed.

## Health

`GET /health` → `{"status":"ok","browser":true,"bot":true,"worker":true}` — browser connectivity, Telegram `getMe` (cached 20 s), and queue-worker liveness.

## Troubleshooting

- **`TelegramConflictError` after redeploy** — old and new instances poll for a few seconds; resolves itself. If it lasts >1–2 min, force-restart with an empty commit.
- **No 👌 reaction on trusted links** — the emoji is not allowed in the group's reaction settings; the bot logs `react skipped` and continues.
- **`Screenshot failed: TimeoutError` → text fallback** — heavy/anti-bot site (e.g. OLX); expected behavior, not a regression.
- **Page shows "Just a moment…"** — Cloudflare challenge got screenshotted; an anti-bot limit.
- **Endless `GET /ping 200` in logs** — Render's internal health check; it does not keep the free instance awake.

## Roadmap (growth track, deliberately not done on a single free instance)

Pin-to-IP for full DNS-rebinding closure, egress filtering (VPS/nftables), Redis-backed cache + stateless replicas, queue start guards, CI for the SSRF test suite, `/metrics`.

## Tests

`tests/test_security.py` — smoke tests for the SSRF filter (private ranges, metadata IPs, ports, IPv6-mapped tricks).
