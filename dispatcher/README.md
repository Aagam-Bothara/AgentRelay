# AgentRelay dispatcher

A small Cloudflare Worker that routes Slack interactivity callbacks to local AgentRelay CLIs over websocket. **Stateless wrt commands** — never sees what Claude is trying to run, only opaque approval IDs and button clicks.

## What it does

1. Hosts the Slack OAuth flow (`/oauth/start`, `/oauth/callback`, `/oauth/poll`).
2. Receives Slack interactivity webhooks (`/slack/interactive`) — i.e. button clicks.
3. Holds a Durable Object per install with the user's install_secret (for websocket auth) and an open websocket from their local CLI.
4. Forwards button clicks to the right local CLI via websocket.

Total: ~400 lines of TypeScript across `src/index.ts` and `src/install.ts`.

## What it does NOT do

- **Never sees commands.** Button payloads carry only `<install_id>:<approval_id>:<decision>`. The actual command lives in the Slack message body, which is sent directly from the user's laptop to Slack — never through us.
- **Never sees project data, file paths, secrets, agent output.**
- **Never holds a long-term database.** Per-install state (bot token, install_secret) lives in the Durable Object; OAuth sessions are 10-min KV entries.

## Deploy

You need:
- A Cloudflare account (free tier works — Workers + KV + Durable Objects are all free at small scale)
- A Slack app registered for OAuth distribution (one-time, see below)
- The `wrangler` CLI: `npm install -g wrangler`

### 1. Create the Slack app

Go to https://api.slack.com/apps → **Create New App** → **From scratch**. Name it whatever (e.g. "AgentRelay"). Then:

- **OAuth & Permissions** → set **Redirect URL** to `https://YOUR-WORKER-URL/oauth/callback`
- **OAuth & Permissions** → **Bot Token Scopes**: `chat:write`, `commands`, `im:write`
- **Slash Commands** → create `/relay` → request URL `https://YOUR-WORKER-URL/slack/slash-proxy` *(if you want a hosted slash; otherwise users use a self-hosted slash. The dispatcher does not currently proxy slash commands — see TODO below.)*
- **Interactivity & Shortcuts** → enable, request URL `https://YOUR-WORKER-URL/slack/interactive`
- **Manage Distribution** → enable public distribution if you want anyone to install your app

Note your **Client ID**, **Client Secret**, and **Signing Secret**.

### 2. Create the Cloudflare resources

```bash
cd dispatcher
npm install

# KV namespace for OAuth sessions and routing pointers:
wrangler kv:namespace create STATE
wrangler kv:namespace create STATE --preview
# Paste the IDs into wrangler.toml.

# Set Slack secrets (do not commit these):
wrangler secret put SLACK_CLIENT_ID
wrangler secret put SLACK_CLIENT_SECRET
wrangler secret put SLACK_SIGNING_SECRET
```

Set `PUBLIC_URL` in `wrangler.toml` `[vars]` to your final Worker URL (e.g. `https://agentrelay-dispatcher.your-subdomain.workers.dev`, or your custom domain).

### 3. Deploy

```bash
wrangler deploy
```

You'll get a URL. Update `agentrelay.auth.DEFAULT_DISPATCHER_URL` in the local CLI to point at it (or have users pass `--dispatcher` to `agentrelay login`).

### 4. Verify

```bash
curl https://YOUR-WORKER-URL/healthz
# {"ok":true,"service":"agentrelay-dispatcher"}
```

Then run `agentrelay login --dispatcher https://YOUR-WORKER-URL` from a local install and walk through OAuth.

## Local development

```bash
wrangler dev
# Worker is on http://127.0.0.1:8787
```

Tunnel that for Slack to reach it:
```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

Update Slack app's redirect URL + interactivity URL to the tunnel's `https://*.trycloudflare.com` URL while developing.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check |
| GET | `/oauth/start?session=<token>` | Begins Slack OAuth; redirects to Slack |
| GET | `/oauth/callback?code=...&state=...` | Slack returns here; exchanges code → token, allocates install_id |
| GET | `/oauth/poll?session=<token>` | Local CLI polls until OAuth completes |
| POST | `/slack/interactive` | Slack interactivity webhook (button clicks) |
| GET | `/ws/<install_id>` | Local CLI websocket connection (`Authorization: Bearer <install_secret>`) |

## Security notes

- **Slack signing secret verification** is enforced on `/slack/interactive`. Replays older than 5 min are rejected.
- **Websocket auth** is a per-install bearer token (`install_secret`) generated during OAuth. Stored only in the Durable Object and the user's OS keychain.
- **OAuth sessions** are short-lived (10 min) KV entries; deleted on first successful poll.
- **No PII at rest** beyond what Slack OAuth provides (bot_token, team_id, user_id). No commands, no project contents.

## TODOs

- [ ] Hosted slash command proxy (currently slash routes to user's local server in self-hosted mode; for dispatcher mode the slash command should hit the dispatcher and be forwarded to the right install). Workaround: users can directly DM the bot or use a personal-shortcut workflow until this lands.
- [ ] Rate limiting per install_id
- [ ] Admin endpoint to list active installs (for ops debugging only — no payload data)
