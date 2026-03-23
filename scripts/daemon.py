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
import urllib.request as _urllib_req
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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            import json as _json
            raw = stdout.decode("utf-8", errors="replace").strip()
            data = {}
            try:
                data = _json.loads(raw)
            except _json.JSONDecodeError as _je:
                # Output may have trailing non-JSON content; truncate at error pos
                try:
                    data = _json.loads(raw[:_je.pos])
                except Exception:
                    pass
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
        _raw_url = cfg.get("pincer_url", "")
        pincer_url = _raw_url.replace("ws://", "http://").replace("wss://", "https://")
        if pincer_url.endswith("/ws"):
            pincer_url = pincer_url[:-3]
        log.info("💬 DM from %s: %s", from_id[:8], text[:80])
        # Structured route header so agent can reliably parse routing info
        route_header = (
            f"[Pincer Route]\n"
            f"type: dm\n"
            f"from_agent_id: {from_id}\n"
            f"my_agent_id: {agent_id}\n"
        )
        reply_hint = (
            f"\nTo reply via Pincer DM, POST to {pincer_url}/api/v1/messages/send:\n"
            f'  {{"from_agent_id": "{agent_id}", "to_agent_id": "{from_id}", "payload": {{"text": "<reply>"}}}}\n'
            f"  Header: X-API-Key: {cfg.get('api_key', '')}"
        )
        await forward_to_agent(cfg, f"{route_header}\n[Pincer DM from {from_id}]\n{text}{reply_hint}", dry_run)

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

    # Auto-discover room_id if not configured: GET /api/v1/rooms → first result
    if not cfg.get("room_id", "").strip():
        _raw = pincer_url.replace("wss://", "https://").replace("ws://", "http://")
        _base = _raw[:-3] if _raw.endswith("/ws") else _raw
        try:
            req = _urllib_req.Request(
                f"{_base}/api/v1/rooms",
                headers={"X-API-Key": cfg["api_key"]},
            )
            with _urllib_req.urlopen(req, timeout=10) as resp:
                rooms = json.loads(resp.read())
            if rooms:
                cfg["room_id"] = rooms[0]["id"]
                log.info("Auto-discovered room_id: %s", cfg["room_id"])
            else:
                log.warning("No rooms found for this API key, room subscription skipped.")
        except Exception as e:
            log.warning("Could not auto-discover room_id: %s", e)

    t = threading.Thread(target=result_listener_thread, args=(result_dir,), daemon=True)
    t.start()

    # Run room WS loop concurrently (if room_id configured)
    room_task = asyncio.create_task(run_room_loop(cfg, dry_run))

    # Run DM inbox poll loop (HTTP polling for reliable DM delivery)
    inbox_task = asyncio.create_task(run_inbox_poll_loop(cfg, dry_run))

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
    inbox_task.cancel()
    try:
        await inbox_task
    except asyncio.CancelledError:
        pass


async def run_inbox_poll_loop(cfg: dict, dry_run: bool = False) -> None:
    """
    HTTP inbox polling loop for reliable DM delivery.

    Polls GET /api/v1/agents/{id}/inbox every inbox_poll_interval seconds.
    Uses message IDs for deduplication to avoid double-forwarding.
    This is the primary DM delivery path; WS MESSAGE events are supplementary.
    """
    agent_id = cfg["agent_id"]
    api_key = cfg["api_key"]
    base_url = cfg["pincer_url"].removesuffix("/ws").replace("wss://", "https://").replace("ws://", "http://")
    poll_interval = cfg.get("inbox_poll_interval", 10)  # seconds, default 10s

    seen_ids: collections.deque = collections.deque(maxlen=SEEN_MAXLEN)
    seen_set: set = set()



    log.info("DM inbox poll: starting (interval=%ds)", poll_interval)
    while True:
        try:
            await asyncio.sleep(poll_interval)
            loop = asyncio.get_event_loop()

            def _fetch_inbox():
                req = _urllib_req.Request(
                    f"{base_url}/api/v1/agents/{agent_id}/inbox",
                    headers={"X-API-Key": api_key, "User-Agent": "pincer-daemon/1.0"}
                )
                try:
                    with _urllib_req.urlopen(req, timeout=10) as r:
                        raw = r.read()
                        if not raw or raw == b"null":
                            return []
                        data = json.loads(raw)
                        return data if isinstance(data, list) else []
                except Exception as e:
                    log.warning("DM inbox poll error: %s", e)
                    return []

            msgs = await loop.run_in_executor(None, _fetch_inbox)
            if not msgs:
                continue

            log.info("DM inbox: %d message(s)", len(msgs))
            for m in msgs:
                msg_id = m.get("id") or m.get("ID") or ""
                if msg_id and msg_id in seen_set:
                    log.debug("DM inbox: skipping duplicate %s", msg_id[:8])
                    continue
                if msg_id:
                    seen_ids.append(msg_id)
                    seen_set.add(msg_id)
                    while len(seen_set) > SEEN_MAXLEN:
                        oldest = seen_ids.popleft()
                        seen_set.discard(oldest)

                # Extract message content
                from_id = m.get("from") or m.get("From") or m.get("sender_agent_id") or "?"
                msg_type = (m.get("type") or m.get("Type") or "").lower()
                payload_data = m.get("payload") or m.get("Payload") or {}
                text = payload_data.get("text") or payload_data.get("content") or json.dumps(payload_data)

                # Skip messages sent by self
                if from_id == agent_id:
                    continue

                log.info("💬 DM inbox from %s (id=%s): %s", from_id[:8], msg_id[:8] if msg_id else "?", text[:80])

                route_header = (
                    f"[Pincer Route]\n"
                    f"type: dm\n"
                    f"from_agent_id: {from_id}\n"
                    f"my_agent_id: {agent_id}\n"
                )
                reply_hint = (
                    f"\nTo reply via Pincer DM, POST to {base_url}/api/v1/messages/send:\n"
                    f'  {{"from_agent_id": "{agent_id}", "to_agent_id": "{from_id}", "payload": {{"text": "<reply>"}}}}\n'
                    f"  Header: X-API-Key: {api_key}"
                )
                await forward_to_agent(cfg, f"{route_header}\n[Pincer DM from {from_id}]\n{text}{reply_hint}", dry_run)

        except asyncio.CancelledError:
            log.info("DM inbox poll loop cancelled.")
            return
        except Exception as e:
            log.warning("DM inbox poll loop error: %s", e)


async def run_room_loop(cfg: dict, dry_run: bool = False) -> None:
    """Subscribe to all Pincer project room WebSockets and forward messages to agent session.

    Discovers rooms from:
    1. cfg["room_id"]    — static room (e.g. user default room)
    2. GET /projects     — one room per project (room_id field)

    Refreshes project list every PROJECT_REFRESH_INTERVAL seconds so newly
    created projects are picked up automatically.
    """
    PROJECT_REFRESH_INTERVAL = 60  # seconds

    agent_id = cfg["agent_id"]
    api_key = cfg["api_key"]
    agent_name = cfg.get("agent_name", "")
    mention_only = cfg.get("room_mention_only", True)
    context_window = cfg.get("room_context_window", 5)

    _raw_base = cfg["pincer_url"].replace("wss://", "https://").replace("ws://", "http://")
    base_url = _raw_base[:-3] if _raw_base.endswith("/ws") else _raw_base

    def _fetch_project_rooms() -> list[str]:
        """Return list of room_ids from /projects endpoint."""
    
        try:
            req = _urllib_req.Request(f"{base_url}/api/v1/projects",
                headers={"X-API-Key": api_key, "User-Agent": "pincer-daemon/1.0"})
            with _urllib_req.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            rooms = [p["room_id"] for p in (data if isinstance(data, list) else []) if p.get("room_id")]
            return rooms
        except Exception as e:
            log.warning("Failed to fetch project rooms: %s", e)
            return []

    def _fetch_default_rooms() -> list[str]:
        """Return room_ids from /rooms (legacy auto-discover)."""
    
        try:
            req = _urllib_req.Request(f"{base_url}/api/v1/rooms",
                headers={"X-API-Key": api_key, "User-Agent": "pincer-daemon/1.0"})
            with _urllib_req.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            return [r["id"] for r in (data if isinstance(data, list) else []) if r.get("id")]
        except Exception as e:
            log.warning("Failed to fetch default rooms: %s", e)
            return []

    # Collect initial room set
    static_room = cfg.get("room_id", "").strip()
    active_rooms: dict[str, asyncio.Task] = {}  # room_id → subscriber task
    context_bufs: dict[str, collections.deque] = {}  # room_id → rolling context

    async def subscribe_room(room_id: str) -> None:
        """Long-running coroutine: subscribe to one room WS and forward mentions."""
        ws_url = f"{base_url.replace('http://', 'ws://').replace('https://', 'wss://')}/api/v1/rooms/{room_id}/ws?api_key={api_key}"
        buf = context_bufs.setdefault(room_id, collections.deque(maxlen=max(context_window, 1)))
        reconnect_delay = RECONNECT_DELAY_BASE
        log.info("Room WS: subscribing to room %s", room_id)
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=None, close_timeout=5) as ws:
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
                        if sender == agent_id:
                            buf.append(f"[{sender[:8]}(me)]: {content}")
                            continue
                        buf.append(f"[{sender[:8]}]: {content}")
                        is_mentioned = agent_name and f"@{agent_name}" in content
                        is_broadcast = "@all" in content or "@所有人" in content
                        if mention_only and not is_mentioned and not is_broadcast:
                            log.debug("💬 Room %s: ignored (no mention): %s", room_id[:8], content[:40])
                            continue
                        log.info("💬 Room %s msg from %s: %s", room_id[:8], sender[:8], content[:60])
                        # Immediately push agent_replying so the sender sees the read receipt / typing indicator
                        import aiohttp as _aiohttp  # noqa: PLC0415 – local import to avoid hard dep at module level
                        _typing_url = f"{base_url}/api/v1/rooms/{room_id}/typing"
                        _typing_headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

                        async def _push_typing(event: str) -> None:
                            try:
                                _p = json.dumps({"agent_id": agent_id, "event": event})
                                async with _aiohttp.ClientSession() as _s:
                                    await _s.post(_typing_url, data=_p, headers=_typing_headers,
                                                  timeout=_aiohttp.ClientTimeout(total=5))
                            except Exception as _te:
                                log.debug("%s push failed (non-fatal): %s", event, _te)

                        await _push_typing("agent_replying")
                        ctx_msgs = list(buf)[:-1]
                        # Structured route header for reliable agent-side parsing
                        route_header = (
                            f"[Pincer Route]\n"
                            f"type: room\n"
                            f"room_id: {room_id}\n"
                            f"from_agent_id: {sender}\n"
                            f"my_agent_id: {agent_id}\n"
                        )
                        if ctx_msgs and context_window > 0:
                            ctx_str = "\n".join(ctx_msgs)
                            forward_text = (
                                f"{route_header}\n"
                                f"[Pincer Room context (last {len(ctx_msgs)} msgs)]\n{ctx_str}\n\n"
                                f"[Pincer Room msg from {sender}]\n{content}"
                            )
                        else:
                            forward_text = f"{route_header}\n[Pincer Room msg from {sender}]\n{content}"
                        reply_hint = (
                            f"\nTo reply in this room, POST to {base_url}/api/v1/rooms/{room_id}/messages:\n"
                            f'  {{"sender_agent_id": "{agent_id}", "content": "<reply>"}}\n'
                            f"  Header: X-API-Key: {api_key}\n"
                            f"Do NOT reply via Feishu or other messaging channels."
                        )
                        await forward_to_agent(cfg, forward_text + reply_hint, dry_run)
                        # Push agent_replying_done after the agent has been handed the message
                        await _push_typing("agent_replying_done")
            except websockets.exceptions.ConnectionClosed as e:
                log.warning("Room WS %s disconnected: %s. Retry in %ds...", room_id[:8], e, reconnect_delay)
            except OSError as e:
                log.warning("Room WS %s error: %s. Retry in %ds...", room_id[:8], e, reconnect_delay)
            except asyncio.CancelledError:
                log.info("Room WS %s cancelled.", room_id[:8])
                return
            except Exception as e:
                log.warning("Room WS %s unexpected: %s. Retry in %ds...", room_id[:8], e, reconnect_delay)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_DELAY_MAX)

    def ensure_rooms(room_ids: list[str]) -> None:
        """Start subscriber tasks for any new room_ids not yet tracked."""
        for rid in room_ids:
            if rid and rid not in active_rooms:
                log.info("Room WS: adding new room %s", rid)
                active_rooms[rid] = asyncio.create_task(subscribe_room(rid))

    # Initial discovery
    loop = asyncio.get_event_loop()
    initial_rooms = []
    if static_room:
        initial_rooms.append(static_room)
    project_rooms = await loop.run_in_executor(None, _fetch_project_rooms)
    if project_rooms:
        initial_rooms.extend(project_rooms)
    elif not static_room:
        # Fallback: legacy /rooms endpoint
        default_rooms = await loop.run_in_executor(None, _fetch_default_rooms)
        initial_rooms.extend(default_rooms)

    if not initial_rooms:
        log.info("No rooms found at startup, will retry on next refresh.")
    ensure_rooms(initial_rooms)

    # Refresh loop: periodically re-fetch projects and subscribe to new rooms
    try:
        while True:
            await asyncio.sleep(PROJECT_REFRESH_INTERVAL)
            new_project_rooms = await loop.run_in_executor(None, _fetch_project_rooms)
            all_rooms = ([static_room] if static_room else []) + new_project_rooms
            ensure_rooms(all_rooms)
            # Clean up finished tasks
            for rid in list(active_rooms):
                if active_rooms[rid].done():
                    log.warning("Room WS task for %s ended unexpectedly", rid)
                    del active_rooms[rid]
    except asyncio.CancelledError:
        log.info("Room refresh loop cancelled, shutting down all room WS tasks.")
        for task in active_rooms.values():
            task.cancel()
        await asyncio.gather(*active_rooms.values(), return_exceptions=True)


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
