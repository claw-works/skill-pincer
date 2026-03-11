#!/usr/bin/env python3
"""
Pincer WebSocket Daemon
Connects an OpenClaw agent to a Pincer hub via WebSocket.
Receives pushed events and forwards them to the local OpenClaw gateway.

Usage:
    python3 daemon.py --config ~/.openclaw/pincer-daemon.json
    python3 daemon.py --config ~/.openclaw/pincer-daemon.json --dry-run
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Missing dependency: pip3 install websockets", file=sys.stderr)
    sys.exit(1)

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pincer-daemon")

# --- Config ---

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.openclaw/pincer-daemon.json")
HEARTBEAT_INTERVAL = 30  # seconds
RECONNECT_DELAY_BASE = 5
RECONNECT_DELAY_MAX = 60


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    required = ["pincer_url", "api_key", "agent_id", "agent_name"]
    for key in required:
        if not cfg.get(key):
            raise ValueError(f"Missing required config key: {key}")
    cfg.setdefault("capabilities", [])
    cfg.setdefault("openclaw_gateway_url", "ws://127.0.0.1:18789")
    cfg.setdefault("openclaw_gateway_token", "")
    return cfg


# --- OpenClaw gateway forwarding ---

async def forward_to_openclaw(cfg: dict, message: str, dry_run: bool = False):
    """Forward a message to the local OpenClaw gateway to wake the agent."""
    if dry_run:
        log.info("[DRY RUN] Would forward to OpenClaw: %s", message[:120])
        return

    gw_url = cfg["openclaw_gateway_url"]
    token = cfg.get("openclaw_gateway_token", "")

    # Use HTTP REST endpoint on gateway (ws port serves HTTP too)
    http_url = gw_url.replace("ws://", "http://").replace("wss://", "https://")
    api_url = f"{http_url}/api/sessions/send"

    payload = {
        "sessionKey": f"agent:main:pincer:daemon",
        "message": message,
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if HAS_AIOHTTP:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=payload, headers=headers) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    log.warning("OpenClaw gateway returned %d: %s", resp.status, body[:200])
    else:
        # Fallback: write event to a local file that a cron can pick up
        event_dir = Path.home() / ".openclaw" / "pincer-events"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_file = event_dir / f"{int(time.time()*1000)}.json"
        event_file.write_text(json.dumps({"message": message, "ts": time.time()}))
        log.info("Event written to %s (install aiohttp for direct gateway forwarding)", event_file)


# --- Pincer WS protocol helpers ---

def make_envelope(msg_type: str, from_id: str, to: str, payload: dict) -> str:
    return json.dumps({
        "id": str(uuid.uuid4()),
        "type": msg_type,
        "from": from_id,
        "to": to,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    })


# --- Main daemon loop ---

async def run_daemon(cfg: dict, dry_run: bool = False):
    agent_id = cfg["agent_id"]
    agent_name = cfg["agent_name"]
    pincer_url = cfg["pincer_url"]

    reconnect_delay = RECONNECT_DELAY_BASE

    while True:
        try:
            log.info("Connecting to Pincer: %s", pincer_url)
            async with websockets.connect(
                pincer_url,
                ping_interval=None,  # we handle ping/heartbeat ourselves
                close_timeout=5,
            ) as ws:
                reconnect_delay = RECONNECT_DELAY_BASE  # reset on success
                log.info("Connected. Registering as %s (%s)...", agent_name, agent_id)

                # 1. REGISTER
                await ws.send(make_envelope("REGISTER", agent_id, "hub", {
                    "name": agent_name,
                    "capabilities": cfg["capabilities"],
                    "runtime_version": "openclaw/skill-pincer/1.0",
                    "messaging_mode": "ws",
                }))

                # 2. AUTH
                await ws.send(make_envelope("AUTH", agent_id, "hub", {
                    "api_key": cfg["api_key"],
                }))

                log.info("Registered and authenticated. Listening for events...")

                # Start heartbeat task
                heartbeat_task = asyncio.create_task(
                    heartbeat_loop(ws, agent_id)
                )

                try:
                    async for raw in ws:
                        await handle_message(raw, cfg, agent_id, ws, dry_run)
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

        except websockets.exceptions.ConnectionClosed as e:
            log.warning("Connection closed: %s. Reconnecting in %ds...", e, reconnect_delay)
        except OSError as e:
            log.error("Connection error: %s. Reconnecting in %ds...", e, reconnect_delay)
        except asyncio.CancelledError:
            log.info("Daemon shutting down.")
            break
        except Exception as e:
            log.exception("Unexpected error: %s. Reconnecting in %ds...", e, reconnect_delay)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_DELAY_MAX)


async def heartbeat_loop(ws, agent_id: str):
    """Send periodic HEARTBEAT to Pincer."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await ws.send(make_envelope("HEARTBEAT", agent_id, "hub", {
                "agent_id": agent_id,
            }))
            log.debug("Heartbeat sent.")
        except Exception as e:
            log.warning("Heartbeat send failed: %s", e)
            break


async def handle_message(raw: str, cfg: dict, agent_id: str, ws, dry_run: bool):
    """Dispatch an incoming Pincer WS message."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Received non-JSON message: %s", raw[:100])
        return

    msg_type = msg.get("type", "")
    payload = msg.get("payload") or {}

    log.debug("← %s from=%s", msg_type, msg.get("from", "?"))

    if msg_type == "ACK":
        status = payload.get("status", "?")
        if status != "ok":
            log.error("AUTH/REGISTER failed: %s", payload.get("error", "unknown"))
        else:
            log.info("ACK ok (trace=%s)", payload.get("trace_id", ""))

    elif msg_type == "TASK_ASSIGN":
        task_id = payload.get("task_id", "?")
        title = payload.get("title", "")
        description = payload.get("description", "")
        log.info("📋 Task assigned: [%s] %s", task_id[:8], title)

        # Build context message for OpenClaw agent
        context = (
            f"[Pincer Task Assigned]\n"
            f"task_id: {task_id}\n"
            f"title: {title}\n"
            f"description: {description}\n\n"
            f"Process this task. When complete, the result will be sent back to Pincer automatically."
        )
        await forward_to_openclaw(cfg, context, dry_run)

    elif msg_type == "MESSAGE":
        from_id = msg.get("from", "?")
        text = payload.get("text", "")
        log.info("💬 DM from %s: %s", from_id[:8], text[:60])

        context = f"[Pincer DM from {from_id}]\n{text}"
        await forward_to_openclaw(cfg, context, dry_run)

    elif msg_type in ("broadcast", "BROADCAST"):
        text = payload.get("text", str(payload))
        log.info("📢 Broadcast: %s", text[:80])

    elif msg_type == "inbox.delivery":
        # Offline messages delivered on reconnect
        messages = payload if isinstance(payload, list) else [payload]
        for m in messages:
            log.info("📬 Inbox message from %s", m.get("from", "?"))
            inner_payload = m.get("payload") or {}
            text = inner_payload.get("text", str(inner_payload))
            context = f"[Pincer Inbox from {m.get('from','?')}]\n{text}"
            await forward_to_openclaw(cfg, context, dry_run)

    elif msg_type == "PING":
        await ws.send(make_envelope("PONG", agent_id, "hub", {}))

    elif msg_type == "HEARTBEAT_ACK":
        inbox = payload.get("inbox") or []
        if inbox:
            log.info("📬 %d inbox message(s) in heartbeat ACK", len(inbox))
            for m in inbox:
                inner_payload = m.get("payload") or {}
                text = inner_payload.get("text", json.dumps(inner_payload))
                context = f"[Pincer Inbox]\n{text}"
                await forward_to_openclaw(cfg, context, dry_run)

    elif msg_type == "ERROR":
        log.error("Pincer error: code=%s msg=%s", payload.get("code"), payload.get("message"))

    else:
        log.debug("Unhandled message type: %s", msg_type)


# --- Entry point ---

def main():
    parser = argparse.ArgumentParser(description="Pincer WebSocket Daemon")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Don't forward to OpenClaw, just log")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        log.error("Config error: %s", e)
        sys.exit(1)

    log.info("Pincer daemon starting (agent=%s name=%s)", cfg["agent_id"][:8], cfg["agent_name"])
    if args.dry_run:
        log.info("DRY RUN mode — events will be logged but not forwarded to OpenClaw")

    loop = asyncio.new_event_loop()

    def shutdown(sig):
        log.info("Received signal %s, shutting down...", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: shutdown(s))

    try:
        loop.run_until_complete(run_daemon(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        log.info("Pincer daemon stopped.")


if __name__ == "__main__":
    main()
