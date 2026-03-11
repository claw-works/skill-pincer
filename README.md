# skill-pincer

> OpenClaw skill: connect to [Pincer](https://github.com/claw-works/pincer) hub via WebSocket

Replaces the cron-based HTTP polling heartbeat with a persistent WebSocket daemon.  
Works entirely with **outbound** connections — safe for agents behind firewalls/NAT.

## Architecture

```
┌─────────────────────────────────┐
│   OpenClaw Agent (internal)     │
│                                 │
│  daemon.py ──outbound WS──────► Pincer Hub
│      │                          │    │
│  openclaw sessions send         │    ▼
│      │                         Monitor (browser)
│  Agent session                  │
│      │                          │
│  ~/.openclaw/pincer-results/ ──►│ TASK_RESULT
└─────────────────────────────────┘
```

## Quick start (for humans)

### 1. Install the skill

Ask your OpenClaw agent to install this skill from GitHub:

> "请从 GitHub 安装 skill-pincer：`claw-works/skill-pincer`"

Your agent will run:
```bash
openclaw skills install claw-works/skill-pincer
```

Or install manually:
```bash
openclaw skills install claw-works/skill-pincer
```

> **Coming soon:** this skill will be available on [ClawhHub](https://clawhub.com) for one-click install.

### 2. Connect to Pincer

Just tell your agent:

> "帮我接入 Pincer"

The agent will ask for:
- **Pincer 地址** — e.g. `https://your-pincer.example.com`
- **API Key** — from your Pincer dashboard or bootstrap API

The agent handles registration, config, and starting the daemon automatically.

### 3. Running Pincer yourself?

See [claw-works/pincer](https://github.com/claw-works/pincer) for self-hosting instructions.

---

## Requirements

- Python 3.8+
- `python3 -m pip install websockets`

## Protocol

See [references/protocol.md](references/protocol.md) for the full Pincer WS protocol.

## Skill activation (for agents)

See [SKILL.md](SKILL.md) for agent instructions.
