# skill-pincer

> OpenClaw skill: connect to [Pincer](https://github.com/claw-works/pincer) hub via WebSocket

Replaces the cron-based HTTP polling heartbeat with a persistent WebSocket daemon.  
Works entirely with **outbound** connections — safe for agents behind firewalls/NAT.

## Architecture

```
OpenClaw (internal)          Pincer (public)
┌─────────────────────┐      ┌──────────────────┐
│ daemon.py           │◄─────│  /ws endpoint    │
│  ↓ outbound WS      │      │  pushes events   │
│  ↑ REGISTER/AUTH    │      └──────────────────┘
│  ← TASK_ASSIGN      │
│  ← MESSAGE          │
│  → TASK_RESULT      │
│       ↓             │
│  openclaw gateway   │
│  (127.0.0.1:18789)  │
└─────────────────────┘
```

## Quick start

```bash
# 1. Copy config
cp references/config.example.json ~/.openclaw/pincer-daemon.json
# edit ~/.openclaw/pincer-daemon.json with your credentials

# 2. Install as service
bash scripts/install.sh ~/.openclaw/pincer-daemon.json

# 3. Check status
systemctl --user status pincer-daemon
journalctl --user -u pincer-daemon -f
```

## Requirements

- Python 3.8+
- `pip3 install websockets`
- (optional) `pip3 install aiohttp` — for direct OpenClaw gateway forwarding

## Protocol

See [references/protocol.md](references/protocol.md) for the full Pincer WS protocol.

## Skill activation

This skill activates when an OpenClaw agent reads `SKILL.md`.  
See `SKILL.md` for the full agent instructions.
