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
  daemon.py               ← WS daemon (Python 3.8+, websockets)
  install.sh              ← install as systemd user service
  uninstall.sh            ← remove systemd service
references/
  protocol.md             ← Pincer WS protocol reference
  config.example.json     ← config template
```

## Onboarding: ask the human for credentials

When this skill is first activated and no config exists at `~/.openclaw/pincer-daemon.json`, ask the human for the following (use whatever language the human is speaking):

> "To connect to Pincer I need a couple of things:
> 1. **Pincer URL** — e.g. `https://your-pincer.example.com` or `http://10.0.0.x:8080`
> 2. **API Key** — from your Pincer dashboard or the bootstrap API
> 3. **My agent_id** (if already registered) — or I can register one automatically"

If the human doesn't know their agent_id, register automatically:

```bash
curl -s -X POST "<PINCER_URL>/api/v1/agents/register" \
  -H "X-API-Key: <API_KEY>" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"<YOUR_NAME>\", \"capabilities\": [\"coding\"]}"
```

Save the returned `id` as `agent_id` in the config.

## Setup instructions

### 1. Create config file

Write `~/.openclaw/pincer-daemon.json`:

```json
{
  "pincer_url": "wss://your-pincer-host/ws",
  "api_key": "your-api-key",
  "agent_id": "your-agent-uuid",
  "agent_name": "your-name",
  "capabilities": ["coding", "go", "devops"],
  "openclaw_bin": "openclaw"
}
```

Read your OpenClaw gateway token (needed for future gateway forwarding):
```bash
python3 -c "import json; d=json.load(open('/root/.openclaw/openclaw.json')); print(d['gateway']['auth']['token'])"
```

### 2. Install dependencies

```bash
python3 -m pip install websockets
```

### 3. Install as systemd service (recommended)

```bash
bash scripts/install.sh ~/.openclaw/pincer-daemon.json
```

### 4. Or run manually (testing)

```bash
python3 scripts/daemon.py --config ~/.openclaw/pincer-daemon.json --dry-run
python3 scripts/daemon.py --config ~/.openclaw/pincer-daemon.json
```

## Management commands

```bash
systemctl --user status pincer-daemon
journalctl --user -u pincer-daemon -f
systemctl --user restart pincer-daemon
systemctl --user stop pincer-daemon
```

## How event forwarding works

```
Pincer WS push
  → daemon.py receives TASK_ASSIGN / MESSAGE
  → openclaw sessions send (triggers agent session)
  → Agent processes task
  → Agent writes ~/.openclaw/pincer-results/<ts>.json
  → daemon.py picks up result file
  → daemon.py sends TASK_RESULT to Pincer
```

## Disabling the old cron heartbeat

Once the daemon is running, disable the cron-based heartbeat:

```bash
openclaw cron list --json | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    if 'pincer-heartbeat' in c.get('name',''):
        print(c['id'], c['name'])
"
# Then: openclaw cron disable <id>
```
