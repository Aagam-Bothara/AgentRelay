# AgentRelay

**Supervise your coding agent from your phone, not your terminal.**

---

You ask Claude Code to "fix the auth tests and open a PR," then walk away to grab coffee. Three minutes later it wants to run `git push origin main` and pauses, waiting for you. You're not there. The agent sits idle. Your terminal does nothing for fifteen minutes.

AgentRelay fixes that.

It's a small relay server plus a Claude Code hook that sends risky actions to your phone — Slack with Approve / Reject buttons, or SMS as a fallback. You tap a button. The agent continues. You never had to be at your laptop.

That's the whole pitch.

---

## How a session looks

You DM the relay's Slack app from your phone:

```
/relay fix the failing auth tests and open a PR
```

Claude Code spins up in your project. While it's reading files, running tests, and editing code — all *safe* stuff — you hear nothing. Then it tries `npm install jsonwebtoken`. Your phone buzzes:

> ⚠️ **MEDIUM risk — approve?**
> *Task:* fix the failing auth tests and open a PR
> *Session:* `a3f7c2b1` · cs494updated
> ```
> npm install jsonwebtoken
> ```
> [ ✅ Approve ]   [ ❌ Reject ]

You tap Approve while standing in line at the deli. The agent continues. Ten minutes later: a clean PR, all tests passing, summary in Slack.

If something goes wrong — five failed test runs in a row, a hang, anything weird — AgentRelay notices and pings you. Stuck sessions don't sit silently.

---

## How it works

```
   Slack / SMS  ←── webhooks ──┐
                               │
   You ─/relay ...─→  AgentRelay (FastAPI)
                               │  ▲
                               │  │ POST /v1/approval  (blocks)
                               ▼  │
                        claude -p "..."  ─PreToolUse hook→  hook.py
```

The whole trick: **the hook makes a long-poll HTTP call that doesn't return until you tap a button on your phone.** That single blocking call is what lets a synchronous subprocess wait for an async human three miles away. Everything else — risk classification, message routing, stall detection — is normal web-app code wrapped around that one mechanic.

No bot daemon. No orchestrator. No event store. One process, one hook script, two adapters.

---

## Layout

```
agentrelay/
  server.py          FastAPI — endpoints, stall watcher, session spawn
  risk.py            command classifier (SAFE / MEDIUM / HIGH / BLOCKED)
  sessions.py        in-memory session + approval state
  adapters/
    slack.py         Slack Web API + threaded messages
    sms.py           Twilio SMS + reply parsing
hook.py              Claude Code PreToolUse hook
config.example.toml
```

Concurrent sessions stay legible because every Slack message threads under its session's "Started" post, and every SMS includes the task name and session ID.

---

## Quick start

```bash
git clone <this repo>
cd agentrelay
pip install -e .
cp config.example.toml config.toml
# fill in slack + twilio creds, then:
agentrelay
```

You'll need a public URL for Slack and Twilio webhooks to reach you:

- **Production:** deploy to Fly.io / Railway / Render — anywhere that runs a Python web app.
- **Development:** `ngrok http 8000` is the fastest path to a working URL.

### Slack (~5 min)

Create a Slack app. Give it two scopes: `chat:write`, `commands`. Then:

- Slash command `/relay` → `https://YOUR-APP/v1/slack/slash`
- Interactivity URL → `https://YOUR-APP/v1/slack/interactive`

Drop the bot token and channel ID into `config.toml` under `[slack]`.

### Twilio (~10–30 min)

Get a Twilio number. Set its inbound messaging webhook to `https://YOUR-APP/v1/sms/incoming` (POST). Drop your account SID, auth token, and phone numbers into `config.toml` under `[sms]`.

> **Heads up about US SMS.** Twilio in the US requires A2P 10DLC brand + campaign registration before carriers will reliably deliver bot SMS. Plan for ~$15–50 in fees and a few days of approval. SMS is best treated as a *fallback* to Slack, not the primary channel.

---

## What it isn't (yet)

I'd rather tell you upfront than have you find out later:

- **No persistence.** Restart the server, you lose in-flight approvals. v0.1 cut.
- **Single-user.** One shared auth token, one `to_number`. Multi-user comes later.
- **No webhook signature verification.** Add it before exposing the server publicly.
- **No mid-flight instructions.** Approve, reject, or kill — but you can't say *"actually, regenerate the fixtures first"* to a running session. Kill and restart is the v0.1 escape hatch.
- **Claude Code only.** No Codex CLI, no Aider. The architecture is mostly runtime-agnostic, but the hook script and spawn logic are Claude-specific.

---

## Roadmap

Honest priority order, not a wishlist:

1. Slack + Twilio webhook signature verification
2. SQLite persistence (survive restarts)
3. Multi-user (per-user tokens and routing)
4. Codex CLI support — when someone asks for it
5. Mid-flight instructions (would need cooperation from the agent runtime)

---

## License

MIT. Use it, fork it, ship it.
