# AgentRelay deployment templates

Reference configs for running AgentRelay on an always-on machine — Raspberry Pi, home server, mini-PC, VPS. Use these when you want supervision to keep working overnight, while traveling, or any time your laptop isn't reliably awake.

## Files

- [`agentrelay.service`](agentrelay.service) — systemd unit. Works on most Linux distros (Ubuntu, Debian, Raspberry Pi OS, Fedora). Boots AgentRelay at every system start and restarts on crash.

## Quick start (Linux/systemd box)

```bash
# Install AgentRelay + Claude Code on the always-on box.
pipx install agentrelay
npm install -g @anthropic-ai/claude-code

# As the user that'll run the agent:
agentrelay login           # interactive OAuth — only needs to happen once
claude                     # authenticate Claude Code once interactively

# Drop in the systemd unit:
sudo cp deploy/agentrelay.service /etc/systemd/system/
sudo nano /etc/systemd/system/agentrelay.service   # replace REPLACE_WITH_* placeholders
sudo systemctl daemon-reload
sudo systemctl enable --now agentrelay.service

# Verify:
systemctl status agentrelay.service
journalctl -u agentrelay.service -f
```

After that, the service runs forever and survives reboots. From any device, DM the AgentRelay bot in Slack — sessions execute on the always-on box, not on your laptop.

## Code sync — where the agent actually edits files

The agent operates on whatever files live on the always-on box. Three common patterns:

| Pattern | Best for | Setup |
|---|---|---|
| **git remote** | Long-running tasks, PR-style work | Agent clones repo on session start, commits + pushes when done; you pull from your laptop |
| **syncthing** | Bidirectional sync of in-progress work | Install Syncthing on laptop + box, share the projects folder, instant 2-way sync |
| **on-demand rsync** | Quick one-offs | `rsync -a ~/projects/foo box:~/projects/foo && trigger-session && rsync -a box:~/projects/foo ~/projects/foo` |

Pick whichever fits how you work. Syncthing is the most "set and forget."

## VPS hosting

Any small VPS works ($5/mo Hetzner / Linode / Vultr is plenty). Pick a region near you to minimize latency between Slack callback → dispatcher → your VPS websocket.

## Raspberry Pi notes

- A Pi 4 with 4GB RAM is plenty; Pi Zero 2 W can manage but Claude Code's memory footprint is the bottleneck
- Use a quality SD card (or USB SSD) — Claude Code does a lot of small disk reads
- If the Pi runs on Wi-Fi, the dispatcher websocket will reconnect through brief drops automatically
