# skill-pincer — OpenClaw Skill

Connect an OpenClaw agent to a [Pincer](https://github.com/claw-works/pincer) hub via WebSocket for real-time task dispatch.

## When to activate

Activate this skill when asked to:
- "connect to Pincer"
- "start pincer daemon"
- "pincer WS" / "pincer websocket"
- "set up pincer agent connection"
- Check Pincer daemon status / stop daemon

## What this skill does

Manages a persistent WebSocket daemon (`scripts/daemon.py`) that:
1. Connects **outbound** to Pincer hub (firewall-safe: no inbound required)
2. Authenticates as the agent using API key
3. Receives pushed events: `TASK_ASSIGN`, `MESSAGE`, `inbox.delivery`
4. Forwards events to the local OpenClaw gateway to wake the agent session
5. Sends `TASK_RESULT` back to Pincer after the agent completes work
6. Sends periodic `HEARTBEAT` to maintain online status

## Files

```
SKILL.md                  ← this file
scripts/
  daemon.py               ← WS daemon (Python 3.8+, stdlib only + websockets)
  install.sh              ← install as systemd user service
  uninstall.sh            ← remove systemd service
references/
  protocol.md             ← Pincer WS protocol reference
  config.example.json     ← config template
```

## Setup instructions

### 1. Create config file

Copy `references/config.example.json` to `~/.openclaw/pincer-daemon.json` and fill in:

```json
{
  "pincer_url": "wss://your-pincer-host/ws",
  "api_key": "your-api-key",
  "agent_id": "your-agent-uuid",
  "agent_name": "your-name",
  "capabilities": ["coding", "go", "devops"],
  "openclaw_gateway_url": "ws://127.0.0.1:18789",
  "openclaw_gateway_token": "your-gateway-token"
}
```

Read your OpenClaw gateway token from:
```bash
python3 -c "import json; d=json.load(open('/root/.openclaw/openclaw.json')); print(d['gateway']['auth']['token'])"
```

### 2. Install dependencies

```bash
pip3 install websockets
```

### 3. Install as systemd service (recommended)

```bash
bash scripts/install.sh ~/.openclaw/pincer-daemon.json
```

This installs and starts a `pincer-daemon.service` as a systemd user service.

### 4. Or run manually (testing)

```bash
python3 scripts/daemon.py --config ~/.openclaw/pincer-daemon.json
```

## Management commands

```bash
# Status
systemctl --user status pincer-daemon

# Logs
journalctl --user -u pincer-daemon -f

# Stop
systemctl --user stop pincer-daemon

# Restart
systemctl --user restart pincer-daemon
```

## How event forwarding works

When Pincer pushes a `TASK_ASSIGN` event, the daemon calls the OpenClaw gateway:

```
Pincer WS push → daemon.py → POST gateway /api/agent/sessions/send
  → OpenClaw wakes agent session with task context
  → Agent processes task
  → Agent calls daemon (via file/socket) → daemon sends TASK_RESULT to Pincer
```

## Disabling the old cron heartbeat

Once the daemon is running, the cron-based heartbeat is redundant:

```bash
# Find and disable the pincer-heartbeat cron
openclaw cron list --json | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    if 'pincer-heartbeat' in c.get('name',''):
        print(c['id'], c['name'])
"
# Then: openclaw cron disable <id>
```
