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
import collections
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Missing dependency: python3 -m pip install websockets", file=sys.stderr)
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
SEEN_MAXLEN = 1000        # max entries in result listener seen-set (#6)

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
    # session_key: empty string means "use openclaw default" (#2)
    cfg.setdefault("session_key", "")
    cfg.setdefault("openclaw_bin", "openclaw")
    return cfg


# ---------------------------------------------------------------------------
# OpenClaw forwarding — invoke agent via CLI (#2 fix: session_key optional)
# ---------------------------------------------------------------------------

async def forward_to_agent(cfg: dict, message: str, dry_run: bool = False) -> None:
    """Trigger an OpenClaw agent session turn with `message` as input."""
    if dry_run:
        log.info("[DRY RUN] Would forward to OpenClaw:\n  %s", message[:200])
        return

    bin_ = cfg["openclaw_bin"]
    session_key = cfg.get("session_key", "").strip()

    # Build command — use `openclaw agent --session-id <id> -m <text>`
    # Session lookup is async to avoid blocking the WS event loop.
    cmd = [bin_, "agent", "-m", message]
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, "sessions", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            import json as _json
            data = _json.loads(stdout)
            sessions = data.get("sessions", [])
            if session_key:
                match = [s for s in sessions if session_key in s.get("key", "")]
            else:
                match = [s for s in sessions if s.get("kind") == "direct"]
            if match:
                session_id = match[0]["sessionId"]
                cmd = [bin_, "agent", "--session-id", session_id, "-m", message]
            else:
                log.warning("No matching session found, trying without session-id")
        else:
            log.warning("Could not list sessions (rc=%d)", proc.returncode)
    except asyncio.TimeoutError:
        log.warning("Session lookup timed out, using default agent command")
    except Exception as e:
        log.warning("Session lookup failed: %s", e)

    try:
        # Fire-and-forget — agent turns can take 30-120s; don't await
        await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        log.info("Forwarded to OpenClaw agent (background)")
    except FileNotFoundError:
        log.error("openclaw binary not found at: %s", bin_)
    except Exception as e:
        log.warning("sessions send failed: %s", e)


# ---------------------------------------------------------------------------
# Result queue — agent drops result files, daemon picks them up (#6 fix)
# ---------------------------------------------------------------------------

def result_listener_thread(result_dir: Path) -> None:
    """
    Watch a directory for result files dropped by the agent.
    Each file is JSON: {"task_id": "...", "status": "done"|"failed", "result": "..."}
    Also supports progress updates: {"task_id": "...", "status": "running", "message": "..."}
    """
    result_dir.mkdir(parents=True, exist_ok=True)
    seen: collections.deque = collections.deque(maxlen=SEEN_MAXLEN)  # bounded (#6)
    seen_set: set = set()

    while True:
        try:
            for f in sorted(result_dir.glob("*.json")):
                if f.name in seen_set:
                    continue
                seen.append(f.name)
                seen_set.add(f.name)
                # Evict oldest if deque wrapped
                while len(seen_set) > SEEN_MAXLEN:
                    oldest = seen.popleft()
                    seen_set.discard(oldest)
                try:
                    data = json.loads(f.read_text())
                    _result_queue.put(data)
                    log.info("📤 Result queued: task_id=%s status=%s",
                             data.get("task_id", "?")[:8], data.get("status", "?"))
                    f.unlink()
                except Exception as e:
                    log.warning("Failed to read result file %s: %s", f, e)
        except Exception as e:
            log.warning("Result listener error: %s", e)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Pincer WS protocol (#5 fix: include conversation_id and depth)
# ---------------------------------------------------------------------------

def make_envelope(msg_type: str, from_id: str, to: str, payload: dict,
                  conversation_id: str = "", depth: int = 0) -> str:
    env = {
        "id": str(uuid.uuid4()),
        "type": msg_type,
        "from": from_id,
        "to": to,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    }
    if conversation_id:
        env["conversation_id"] = conversation_id
    if depth > 0:
        env["depth"] = depth
    return json.dumps(env)


# ---------------------------------------------------------------------------
# Result send loop (#4: TASK_UPDATE for running status)
# ---------------------------------------------------------------------------

async def send_result_loop(ws, agent_id: str, dry_run: bool) -> None:
    """Drain _result_queue and send TASK_RESULT or TASK_UPDATE to Pincer."""
    while True:
        await asyncio.sleep(1)
        while not _result_queue.empty():
            try:
                res = _result_queue.get_nowait()
            except queue.Empty:
                break

            task_id = res.get("task_id", "")
            status = res.get("status", "done")

            if dry_run:
                log.info("[DRY RUN] Would send %s: task=%s status=%s",
                         "TASK_UPDATE" if status == "running" else "TASK_RESULT",
                         task_id[:8], status)
                continue

            try:
                if status == "running":
                    # Interim progress update (#4)
                    payload = {
                        "task_id": task_id,
                        "status": "running",
                        "message": res.get("message", ""),
                    }
                    await ws.send(make_envelope("TASK_UPDATE", agent_id, "hub", payload))
                    log.info("📊 TASK_UPDATE sent: task=%s", task_id[:8])
                else:
                    # Final result
                    payload = {"task_id": task_id, "status": status}
                    if status == "done":
                        payload["result"] = res.get("result", "")
                    else:
                        payload["error"] = res.get("error", "")
                    await ws.send(make_envelope("TASK_RESULT", agent_id, "hub", payload))
                    log.info("✅ TASK_RESULT sent: task=%s status=%s", task_id[:8], status)
            except Exception as e:
                log.warning("Failed to send result: %s", e)
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


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def handle_message(raw: str, cfg: dict, agent_id: str, ws, dry_run: bool,
                         _auth_done: list = None) -> None:
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
            # Only log "Authenticated" once on initial AUTH handshake, not on every heartbeat ACK
            if _auth_done is not None and not _auth_done:
                _auth_done.append(True)
                log.info("✓ Authenticated with Pincer hub.")

    elif msg_type in ("TASK_ASSIGN", "task.assigned"):
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
            f"For progress updates use status 'running' with a 'message' field.\n"
            f"The daemon will relay results back to Pincer automatically."
        )
        await forward_to_agent(cfg, context, dry_run)

    elif msg_type in ("MESSAGE", "agent.message"):
        # From field is set by hub from the sender's WS connection — no sender_agent_id in payload
        from_id = msg.get("from", "?")
        text = payload.get("text", "")
        pincer_url = cfg.get("pincer_url", "").replace("ws://", "http://").replace("wss://", "https://").removesuffix("/ws")
        log.info("💬 DM from %s: %s", from_id[:8], text[:80])
        reply_hint = (
            f"\nTo reply via Pincer, POST to {pincer_url}/api/v1/messages/send:\n"
            f'  {{"from_agent_id": "{agent_id}", "to_agent_id": "{from_id}", "payload": {{"text": "<reply>"}}}}\n'
            f"  Header: X-API-Key: {cfg.get('api_key', '')}"
        )
        await forward_to_agent(cfg, f"[Pincer DM from {from_id}]\n{text}{reply_hint}", dry_run)

    elif msg_type in ("broadcast", "BROADCAST"):
        from_id = msg.get("from", "hub")
        text = payload.get("text", str(payload))
        log.info("📢 Broadcast from %s: %s", from_id[:8], text[:80])

    elif msg_type == "inbox.delivery":
        items = payload if isinstance(payload, list) else [payload]
        for m in items:
            inner = (m.get("payload") or {})
            text = inner.get("text", json.dumps(inner))
            from_id = m.get("from", "?")
            log.info("📬 Inbox from %s", from_id[:8])
            await forward_to_agent(cfg, f"[Pincer Inbox from {from_id}]\n{text}", dry_run)

    elif msg_type in ("HEARTBEAT_ACK", "heartbeat.ack"):
        inbox = payload.get("inbox") or []
        if inbox:
            log.info("📬 %d inbox message(s) via heartbeat ACK", len(inbox))
            for m in inbox:
                inner = (m.get("payload") or {})
                text = inner.get("text", json.dumps(inner))
                await forward_to_agent(cfg, f"[Pincer Inbox]\n{text}", dry_run)

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

    t = threading.Thread(target=result_listener_thread, args=(result_dir,), daemon=True)
    t.start()

    # Run room WS loop concurrently (if room_id configured)
    room_task = asyncio.create_task(run_room_loop(cfg, dry_run))

    reconnect_delay = RECONNECT_DELAY_BASE
    while True:
        try:
            connect_url = pincer_url
            if "agent_id=" not in connect_url:
                sep = "&" if "?" in connect_url else "?"
                connect_url = f"{connect_url}{sep}agent_id={agent_id}"
            log.info("Connecting to %s ...", pincer_url)

            async with websockets.connect(connect_url, ping_interval=None, close_timeout=5) as ws:
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
                auth_done = []  # mutable flag: empty = not yet authed, [True] = authed
                try:
                    async for raw in ws:
                        await handle_message(raw, cfg, agent_id, ws, dry_run, auth_done)
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

    room_task.cancel()
    try:
        await room_task
    except asyncio.CancelledError:
        pass


async def run_room_loop(cfg: dict, dry_run: bool = False) -> None:
    """Subscribe to the Pincer room WebSocket and forward messages to agent session."""
    room_id = cfg.get("room_id", "").strip()
    if not room_id:
        return  # no room configured, skip

    agent_id = cfg["agent_id"]
    api_key = cfg["api_key"]
    context_window = cfg.get("room_context_window", 5)  # how many recent msgs to include as context

    # Convert WS URL back to HTTP base
    base_url = cfg["pincer_url"].removesuffix("/ws").replace("wss://", "https://").replace("ws://", "http://")
    room_ws_url = f"{base_url.replace('http://', 'ws://').replace('https://', 'wss://')}/api/v1/rooms/{room_id}/ws?api_key={api_key}"

    # Rolling context buffer: keeps last N room messages for context
    context_buf: collections.deque = collections.deque(maxlen=max(context_window, 1))

    reconnect_delay = RECONNECT_DELAY_BASE
    log.info("Room WS: subscribing to room %s", room_id)
    while True:
        try:
            async with websockets.connect(room_ws_url, ping_interval=None, close_timeout=5) as ws:
                reconnect_delay = RECONNECT_DELAY_BASE
                log.info("Room WS: connected to room %s", room_id)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type != "room.message":
                        continue
                    data = msg.get("data") or msg.get("payload") or {}
                    sender = data.get("sender_agent_id", "unknown")
                    content = data.get("content", "")
                    # Don't forward messages sent by this agent (would loop)
                    if sender == agent_id:
                        context_buf.append(f"[{sender[:8]}(me)]: {content}")
                        continue
                    # Add to context buffer before filtering
                    context_buf.append(f"[{sender[:8]}]: {content}")
                    # Forward rules (mention_only mode, default true):
                    # 1. @agent名字 → forward to this agent
                    # 2. @all or @所有人 → broadcast, forward to all agents
                    # 3. otherwise → discard (0 token cost)
                    agent_name = cfg.get("agent_name", "")
                    mention_only = cfg.get("room_mention_only", True)
                    is_mentioned = agent_name and (agent_name in content or f"@{agent_name}" in content)
                    is_broadcast = "@all" in content or "@所有人" in content
                    if mention_only and not is_mentioned and not is_broadcast:
                        log.debug("💬 Room msg from %s ignored (no mention): %s", sender[:8], content[:40])
                        continue
                    log.info("💬 Room msg from %s: %s", sender[:8], content[:60])
                    # Build context string from recent messages (excluding the current one)
                    ctx_msgs = list(context_buf)[:-1]  # all except the triggering message
                    if ctx_msgs and context_window > 0:
                        ctx_str = "\n".join(ctx_msgs)
                        forward_text = f"[Pincer Room context (last {len(ctx_msgs)} msgs)]\n{ctx_str}\n\n[Pincer Room msg from {sender}]\n{content}"
                    else:
                        forward_text = f"[Pincer Room msg from {sender}]\n{content}"
                    await forward_to_agent(cfg, forward_text, dry_run)
        except websockets.exceptions.ConnectionClosed as e:
            log.warning("Room WS disconnected: %s. Retry in %ds...", e, reconnect_delay)
        except OSError as e:
            log.warning("Room WS error: %s. Retry in %ds...", e, reconnect_delay)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Room WS unexpected: %s. Retry in %ds...", e, reconnect_delay)
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_DELAY_MAX)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pincer WebSocket Daemon")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true")
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
