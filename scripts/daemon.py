#!/usr/bin/env python3
"""
Pincer WebSocket Daemon
Connects an OpenClaw agent to a Pincer hub via WebSocket.
Receives pushed events and triggers the local OpenClaw agent session via CLI.

Usage:
    python3 daemon.py --config ~/.openclaw/pincer-daemon.json
    python3 daemon.py --config ~/.openclaw/pincer-daemon.json --dry-run
"""

import argparse
import asyncio
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Missing dependency: pip3 install websockets", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pincer-daemon")

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.openclaw/pincer-daemon.json")
HEARTBEAT_INTERVAL = 30   # seconds
RECONNECT_DELAY_BASE = 5
RECONNECT_DELAY_MAX = 60

# Result queue: agent writes results here, send loop picks them up
_result_queue: queue.Queue = queue.Queue()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    for key in ["pincer_url", "api_key", "agent_id", "agent_name"]:
        if not cfg.get(key):
            raise ValueError(f"Missing required config key: {key}")
    cfg.setdefault("capabilities", [])
    cfg.setdefault("session_key", "agent:main:pincer:tasks")
    cfg.setdefault("openclaw_bin", "openclaw")
    return cfg


# ---------------------------------------------------------------------------
# OpenClaw forwarding — invoke agent via CLI
# ---------------------------------------------------------------------------

def forward_to_agent(cfg: dict, message: str, dry_run: bool = False) -> None:
    """Trigger an OpenClaw agent session turn with `message` as input."""
    if dry_run:
        log.info("[DRY RUN] Would forward to OpenClaw:\n  %s", message[:200])
        return

    session_key = cfg["session_key"]
    bin_ = cfg["openclaw_bin"]

    cmd = [bin_, "sessions", "send", "--session-key", session_key, "--message", message]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.warning("sessions send failed (rc=%d): %s", result.returncode, result.stderr[:200])
        else:
            log.info("Forwarded to OpenClaw agent (session=%s)", session_key)
    except FileNotFoundError:
        log.error("openclaw binary not found at: %s", bin_)
    except subprocess.TimeoutExpired:
        log.warning("sessions send timed out")


# ---------------------------------------------------------------------------
# Result queue — agent posts results back to this queue
# ---------------------------------------------------------------------------

def result_listener_thread(result_dir: Path) -> None:
    """
    Watch a directory for result files dropped by the agent.
    Each file is a JSON: {"task_id": "...", "status": "done"|"failed", "result": "..."}
    """
    result_dir.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    while True:
        try:
            for f in sorted(result_dir.glob("*.json")):
                if f.name in seen:
                    continue
                seen.add(f.name)
                try:
                    data = json.loads(f.read_text())
                    _result_queue.put(data)
                    log.info("📤 Result queued: task_id=%s status=%s",
                             data.get("task_id", "?")[:8], data.get("status", "?"))
                    f.unlink()  # consume
                except Exception as e:
                    log.warning("Failed to read result file %s: %s", f, e)
        except Exception as e:
            log.warning("Result listener error: %s", e)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Pincer WS protocol
# ---------------------------------------------------------------------------

def make_envelope(msg_type: str, from_id: str, to: str, payload: dict) -> str:
    return json.dumps({
        "id": str(uuid.uuid4()),
        "type": msg_type,
        "from": from_id,
        "to": to,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    })


async def send_result_loop(ws, agent_id: str, dry_run: bool) -> None:
    """Drain _result_queue and send TASK_RESULT to Pincer."""
    while True:
        await asyncio.sleep(1)
        while not _result_queue.empty():
            try:
                res = _result_queue.get_nowait()
            except queue.Empty:
                break
            task_id = res.get("task_id", "")
            status = res.get("status", "done")
            result_text = res.get("result", "")
            error_text = res.get("error", "")

            if dry_run:
                log.info("[DRY RUN] Would send TASK_RESULT: task=%s status=%s",
                         task_id[:8], status)
                continue

            payload = {"task_id": task_id, "status": status}
            if status == "done":
                payload["result"] = result_text
            else:
                payload["error"] = error_text

            try:
                await ws.send(make_envelope("TASK_RESULT", agent_id, "hub", payload))
                log.info("✅ TASK_RESULT sent: task=%s status=%s", task_id[:8], status)
            except Exception as e:
                log.warning("Failed to send TASK_RESULT: %s", e)
                _result_queue.put(res)  # re-queue on failure


async def heartbeat_loop(ws, agent_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await ws.send(make_envelope("HEARTBEAT", agent_id, "hub", {"agent_id": agent_id}))
            log.debug("Heartbeat sent.")
        except Exception as e:
            log.warning("Heartbeat failed: %s", e)
            break


async def handle_message(raw: str, cfg: dict, agent_id: str, ws, dry_run: bool) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Non-JSON message: %s", raw[:80])
        return

    msg_type = msg.get("type", "")
    payload = msg.get("payload") or {}
    log.debug("← %s from=%s", msg_type, msg.get("from", "?"))

    if msg_type == "ACK":
        if payload.get("status") != "ok":
            log.error("ACK error: %s", payload.get("error", "unknown"))
        else:
            log.info("✓ Authenticated with Pincer hub.")

    elif msg_type == "TASK_ASSIGN":
        task_id = payload.get("task_id", "?")
        title = payload.get("title", "")
        description = payload.get("description", "")
        report_ch = payload.get("report_channel")
        log.info("📋 Task assigned: [%s] %s", task_id[:8], title)

        context = (
            f"[Pincer Task]\n"
            f"task_id: {task_id}\n"
            f"title: {title}\n"
            f"description:\n{description}\n"
        )
        if report_ch:
            context += f"report_channel: {json.dumps(report_ch)}\n"
        context += (
            f"\nWhen done, write result to ~/.openclaw/pincer-results/<timestamp>.json:\n"
            f'  {{"task_id": "{task_id}", "status": "done", "result": "<summary>"}}\n'
            f"The daemon will pick it up and send it back to Pincer automatically."
        )
        forward_to_agent(cfg, context, dry_run)

    elif msg_type == "MESSAGE":
        from_id = msg.get("from", "?")
        text = payload.get("text", "")
        log.info("💬 DM from %s: %s", from_id[:8], text[:80])
        forward_to_agent(cfg, f"[Pincer DM from {from_id}]\n{text}", dry_run)

    elif msg_type in ("broadcast", "BROADCAST"):
        text = payload.get("text", str(payload))
        log.info("📢 Broadcast: %s", text[:80])

    elif msg_type == "inbox.delivery":
        items = payload if isinstance(payload, list) else [payload]
        for m in items:
            inner = (m.get("payload") or {})
            text = inner.get("text", json.dumps(inner))
            from_id = m.get("from", "?")
            log.info("📬 Inbox from %s", from_id[:8])
            forward_to_agent(cfg, f"[Pincer Inbox from {from_id}]\n{text}", dry_run)

    elif msg_type == "HEARTBEAT_ACK":
        inbox = payload.get("inbox") or []
        if inbox:
            log.info("📬 %d inbox message(s) via heartbeat ACK", len(inbox))
            for m in inbox:
                inner = (m.get("payload") or {})
                text = inner.get("text", json.dumps(inner))
                forward_to_agent(cfg, f"[Pincer Inbox]\n{text}", dry_run)

    elif msg_type == "PING":
        await ws.send(make_envelope("PONG", agent_id, "hub", {}))

    elif msg_type == "ERROR":
        log.error("Pincer error: code=%s msg=%s",
                  payload.get("code"), payload.get("message"))
    else:
        log.debug("Unhandled type: %s", msg_type)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_daemon(cfg: dict, dry_run: bool = False) -> None:
    agent_id = cfg["agent_id"]
    pincer_url = cfg["pincer_url"]
    result_dir = Path.home() / ".openclaw" / "pincer-results"

    # Start result listener in background thread
    t = threading.Thread(
        target=result_listener_thread, args=(result_dir,), daemon=True
    )
    t.start()

    reconnect_delay = RECONNECT_DELAY_BASE
    while True:
        try:
            log.info("Connecting to %s ...", pincer_url)
            async with websockets.connect(pincer_url, ping_interval=None, close_timeout=5) as ws:
                reconnect_delay = RECONNECT_DELAY_BASE

                await ws.send(make_envelope("REGISTER", agent_id, "hub", {
                    "name": cfg["agent_name"],
                    "capabilities": cfg["capabilities"],
                    "runtime_version": "openclaw/skill-pincer/1.0",
                    "messaging_mode": "ws",
                }))
                await ws.send(make_envelope("AUTH", agent_id, "hub", {
                    "api_key": cfg["api_key"],
                }))
                log.info("Registered as %s (%s)", cfg["agent_name"], agent_id[:8])

                hb_task = asyncio.create_task(heartbeat_loop(ws, agent_id))
                result_task = asyncio.create_task(send_result_loop(ws, agent_id, dry_run))
                try:
                    async for raw in ws:
                        await handle_message(raw, cfg, agent_id, ws, dry_run)
                finally:
                    hb_task.cancel()
                    result_task.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            log.warning("Disconnected: %s. Retry in %ds...", e, reconnect_delay)
        except OSError as e:
            log.error("Connection error: %s. Retry in %ds...", e, reconnect_delay)
        except asyncio.CancelledError:
            log.info("Daemon shutting down.")
            break
        except Exception as e:
            log.exception("Unexpected: %s. Retry in %ds...", e, reconnect_delay)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_DELAY_MAX)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pincer WebSocket Daemon")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="Log events but don't forward to OpenClaw or Pincer")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        log.error("Config error: %s", e)
        sys.exit(1)

    log.info("Starting pincer-daemon (agent=%s)", cfg["agent_id"][:8])

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)]
        )

    try:
        loop.run_until_complete(run_daemon(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        log.info("Pincer daemon stopped.")


if __name__ == "__main__":
    main()
