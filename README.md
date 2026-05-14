# AgentRelay

**Supervise your coding agent from your phone, not your terminal.**

---

You ask Claude Code to "fix the auth tests and open a PR," then walk away to grab coffee. Three minutes later it wants to run `git push origin main` and pauses, waiting for you. You're not there. The agent sits idle. Your terminal does nothing for fifteen minutes.

AgentRelay fixes that.

It's a Claude Code hook plus a tiny relay that sends risky actions to your phone via Slack — with Approve / Reject buttons. You tap a button. The agent continues. You never had to be at your laptop.

---

## Install — fully seamless

```bash
pipx install agentrelay
agentrelay login              # browser opens → "Add to Slack" → done
agentrelay wire-hook --global # one-time: enables AgentRelay for every Claude Code session on this machine
agentrelay install-service    # one-time: runs `agentrelay run` automatically at every login
agentrelay run                # start it once now; from next reboot it auto-starts
```

That's it. No tunnel. No Slack app to create. No `config.toml`. No per-project setup. Every Claude Code session — CLI, VS Code extension, JetBrains plugin — goes through AgentRelay supervision until you uninstall.

Anytime you want to verify it's actually working:
```bash
agentrelay status
```
Shows login state, server reachability, dispatcher health, hook wiring, auto-startup status, and recent hook activity — all in one table.

> **What `login` does:** opens your browser to AgentRelay's hosted dispatcher, walks you through Slack's "Add to Workspace" page, comes back with a bot token + install secret stored in your OS keychain. Your laptop is now connected to the dispatcher via an outbound websocket — no public URL on your side needed.

---

## How a session looks

In Slack, DM the AgentRelay bot from your phone:

```
/relay fix the failing auth tests and open a PR
```

Claude Code spins up in your project. While it's reading files and running tests — all *safe* stuff — you hear nothing. Then it tries `npm install jsonwebtoken`. Your phone buzzes:

> ⚠️ **MEDIUM risk — approve?**
> *Task:* fix the failing auth tests and open a PR
> *Session:* `a3f7c2b1` · cs494updated
> ```
> npm install jsonwebtoken
> ```
> [ ✅ Approve ]   [ ❌ Reject ]

You tap Approve while standing in line at the deli. The agent continues. Ten minutes later: a clean PR, all tests passing, summary in Slack.

If something goes wrong — five failed tests in a row, a hang — AgentRelay notices and pings you.

---

## How it works

```
                                ┌────────────────────┐
                                │ Hosted dispatcher  │
                                │ (Cloudflare Worker)│
                                │                    │
   Slack ──webhooks────────────►│  routes button     │
                                │  clicks via        │
                                │  websocket         │
                                └─────────┬──────────┘
                                          │ websocket
                                          ▼
   Claude Code ─PreToolUse hook→  agentrelay (your laptop)
                                          │
                                          ▼
                                   chat.postMessage
                                   (your bot token,
                                   direct to Slack)
```

**The privacy property:** the dispatcher never sees commands, file paths, or project data. It only sees opaque approval IDs and routes button clicks. Your bot token lives on your laptop. Slack messages go directly from your laptop to Slack — never through the dispatcher.

The whole trick: **the hook makes a long-poll HTTP call that doesn't return until you tap a button on your phone.** That single blocking call is what lets a synchronous subprocess wait for an async human three miles away.

---

## CLI reference

```bash
agentrelay login                 # Slack OAuth → store creds in OS keychain
agentrelay logout                # clear stored creds
agentrelay run                   # default: dispatcher mode (background-friendly)
agentrelay run --self-hosted     # v0.2-style: config.toml + cloudflared
agentrelay status                # diagnose what's wired up, what isn't, and why
agentrelay wire-hook <project>   # add the PreToolUse hook to a single project
agentrelay wire-hook --global    # add the hook to ~/.claude/settings.json — applies everywhere
agentrelay install-service       # auto-start `agentrelay run` at every login
agentrelay uninstall-service     # undo the above
agentrelay init                  # [self-hosted] interactive setup wizard
agentrelay rewire-slack          # [self-hosted] regenerate Slack manifest after tunnel restart
```

### Keeping Claude Code running when your laptop sleeps

By default, when your laptop closes/sleeps the OS suspends every process — including AgentRelay and any active Claude Code session. Two ways around it depending on how long you want supervision to last:

**Short away-from-keyboard (minutes to hours):** start the server with `--keep-awake`:

```bash
agentrelay run --keep-awake
```

While that's running, the OS won't idle-sleep your machine (the display still sleeps to save power; only the system stays awake). Stop the server → normal sleep behavior returns. Backends:

- Windows: `SetThreadExecutionState(ES_SYSTEM_REQUIRED)`
- macOS: `caffeinate -i`
- Linux: `systemd-inhibit`

**Long away-from-keyboard / overnight / production:** run AgentRelay on a machine that's always on — a home server, Raspberry Pi, mini-PC, or a cheap VPS. Your laptop becomes one of many devices that can *see* the supervised agent via Slack, but isn't the thing running it.

Pattern:

1. On the always-on box: install AgentRelay + Claude Code, run `agentrelay login` + `agentrelay install-service`. Server boots at every reboot.
2. Sync your code to that box — git remote, syncthing, or rsync on demand. (The agent operates on whatever files live on that machine.)
3. Trigger sessions from your phone via Slack DM, just like before.
4. Your laptop can be off, in your bag, or in another country.

A `deploy/agentrelay.service` template is included for systemd-based Linux servers (most VPS hosts, Raspberry Pi OS, Ubuntu). Drop it into `/etc/systemd/system/`, edit the user/paths, and `systemctl enable --now agentrelay.service`.

### Debugging the hook

If something isn't supervising and you suspect the hook isn't firing:

```bash
$env:AGENTRELAY_DEBUG = "1"     # PowerShell
export AGENTRELAY_DEBUG=1       # bash/zsh
```

The hook will append a line to `~/.agentrelay/hook.log` on every invocation, including which Claude Code surface fired it and the resolved session_id. `agentrelay status` shows the most recent entries.

---

## Self-hosting

Don't trust a hosted dispatcher? Want to run everything on your own infrastructure? Both work.

```bash
agentrelay init                  # wizard creates Slack app + tunnel + config.toml
agentrelay run --self-hosted     # uses cloudflared instead of dispatcher
```

In self-hosted mode, you own the Slack app and the tunnel. Nothing leaves your machine except direct Slack API calls. See [config.example.toml](config.example.toml) and [Dockerfile](Dockerfile) / [fly.toml](fly.toml) for production deployment.

---

## Layout

```
agentrelay/
  cli.py               typer CLI: login, logout, run, init, wire-hook, rewire-slack
  auth.py              Slack OAuth client (browser + poll)
  keychain.py          OS keychain wrapper (with file fallback)
  dispatcher_client.py websocket client to the hosted dispatcher
  wizard.py            self-hosted setup wizard
  tunnel.py            Cloudflare quick-tunnel wrapper
  manifest.py          Slack App Manifest generator
  server.py            FastAPI app: /v1/approval, /v1/start, /v1/slack/*, /healthz
  risk.py              command classifier (SAFE / MEDIUM / HIGH / BLOCKED)
  sessions.py          in-memory session + approval state
  adapters/
    slack.py           Slack Web API + threaded messages
hook.py                Claude Code PreToolUse hook
dispatcher/            Cloudflare Worker source — see dispatcher/README.md
```

Concurrent sessions stay legible because every Slack message threads under its session's "Started" post.

---

## Privacy & trust model

**What the hosted dispatcher sees:** Slack workspace IDs, opaque approval IDs (random hex), button click events, your install_id.

**What the hosted dispatcher does NOT see:** the command being approved, file paths, project contents, secrets, agent output, your code.

Your laptop holds the Slack bot token, generates the approval messages, and posts them directly to Slack. The dispatcher only routes Slack's interactivity webhook callbacks back to you — it's a pure button-click relay.

If you don't want to take this on trust, the dispatcher is OSS ([dispatcher/](dispatcher/)) and self-hostable, or skip it entirely with `--self-hosted` mode.

---

## What it isn't (yet)

- **No persistence.** Server restart loses in-flight approvals.
- **Single-user-per-install.** Multi-user routing comes later.
- **Slack only.** No Discord, no SMS, no Teams — yet.
- **Claude Code only.** No Codex, no Aider — yet.

---

## Roadmap

Honest priority order:

1. SQLite persistence (survive restarts cleanly)
2. MCP layer — second install path: paste a URL into Claude Code config, advisory-mode approvals (loses enforcement, gains zero-friction trial)
3. Multi-user (per-user routing within one install)
4. More agent runtimes (Codex, Aider) — when asked
5. More messaging adapters (Discord, Teams) — when asked

---

## License

MIT. Use it, fork it, ship it.
