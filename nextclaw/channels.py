"""Messaging channels: modular interface + Telegram integration (opt-in).

The Telegram channel is a separate, optional module. It does not touch any
existing bot (e.g. an OpenClaw-driven bot): it uses its own token from
NEXTCLAW_TELEGRAM_BOT_TOKEN and only talks to user IDs listed in
NEXTCLAW_TELEGRAM_ALLOWED_IDS.

Commands (from authorized users only):
  /task <goal>      create + run an agent task, reply with the verified result
  /status [task-id] show task status (default: latest task)
  /tasks            list recent tasks
  /cancel <task-id> request cancellation
  /help             this help

Safety: update offsets persist in the data dir (no duplicate task creation
on restart); long tasks are acknowledged immediately and the result is sent
when done; the same permission/approval system applies — approvals must be
granted via the dashboard or CLI, the bot never auto-approves.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

API_BASE = "https://api.telegram.org"


@dataclass
class IncomingMessage:
    update_id: int
    chat_id: int
    user_id: int
    text: str


class ChannelError(Exception):
    pass


class HttpTransport:
    """Real urllib transport. Swap for a fake in tests."""

    def __init__(self, timeout_s: int = 40):
        self.timeout_s = timeout_s

    def get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(url + "?" + qs, headers={"User-Agent": "nextclaw/0.2"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "nextclaw/0.2"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))


class TelegramChannel:
    """Minimal Telegram Bot API client (getUpdates long-poll + sendMessage)."""

    def __init__(self, token: str, transport: Optional[HttpTransport] = None):
        if not token:
            raise ChannelError("Telegram bot token is required (NEXTCLAW_TELEGRAM_BOT_TOKEN)")
        self.token = token
        self.transport = transport or HttpTransport()
        self._base = f"{API_BASE}/bot{token}"

    def _api(self, method: str, params: Dict[str, Any], post: bool = False) -> Dict[str, Any]:
        try:
            if post:
                data = self.transport.post(f"{self._base}/{method}", params)
            else:
                data = self.transport.get(f"{self._base}/{method}", params)
        except Exception as exc:
            raise ChannelError(f"Telegram API call failed: {exc}")
        if not data.get("ok"):
            raise ChannelError(f"Telegram API error: {data}")
        return data

    def get_updates(self, offset: int = 0, timeout: int = 25) -> List[IncomingMessage]:
        data = self._api("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]})
        out = []
        for upd in data.get("result", []):
            msg = upd.get("message") or {}
            text = msg.get("text", "")
            if not text:
                continue
            out.append(IncomingMessage(
                update_id=upd["update_id"],
                chat_id=msg["chat"]["id"],
                user_id=msg["from"]["id"],
                text=text.strip(),
            ))
        return out

    def send(self, chat_id: int, text: str) -> None:
        # Telegram caps messages at 4096 chars; split on boundaries.
        while text:
            chunk, text = text[:4000], text[4000:]
            self._api("sendMessage", {"chat_id": chat_id, "text": chunk,
                                      "disable_web_page_preview": True}, post=True)


class ChannelRunner:
    """Poll loop: Telegram messages -> NextClaw tasks -> replies."""

    def __init__(self, cfg, channel: Optional[TelegramChannel] = None,
                 on_task_done: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        from .approvals import ApprovalStore
        from .tasks import TaskStore

        token = os.environ.get("NEXTCLAW_TELEGRAM_BOT_TOKEN", "")
        allowed = {int(x) for x in os.environ.get("NEXTCLAW_TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()}
        self.channel = channel or TelegramChannel(token)
        self.allowed = allowed
        self.cfg = cfg
        self.tasks = TaskStore(cfg.data_dir)
        self.approvals = ApprovalStore(cfg.data_dir)
        self.on_task_done = on_task_done
        self._offset_path = os.path.join(cfg.data_dir, "telegram_offset")
        self._stop = threading.Event()

    # -- offset persistence (no duplicate tasks on restart) ----------------
    def _load_offset(self) -> int:
        try:
            return int(open(self._offset_path).read().strip())
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        with open(self._offset_path, "w") as f:
            f.write(str(offset))

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, poll_timeout: int = 25) -> None:
        offset = self._load_offset()
        while not self._stop.is_set():
            try:
                updates = self.channel.get_updates(offset=offset, timeout=poll_timeout)
            except ChannelError:
                time.sleep(5)
                continue
            for msg in updates:
                offset = max(offset, msg.update_id + 1)
                self._save_offset(offset)
                try:
                    self.handle_message(msg)
                except Exception as exc:
                    try:
                        self.channel.send(msg.chat_id, f"Error: {exc}")
                    except Exception:
                        pass

    # -- command handling --------------------------------------------------
    def handle_message(self, msg: IncomingMessage) -> Optional[str]:
        """Handle one message; returns the reply text (also sent)."""
        if self.allowed and msg.user_id not in self.allowed:
            self.approvals.audit("telegram", "unauthorized", "",
                                 {"user_id": msg.user_id})
            return None  # silently ignore strangers
        text = msg.text
        if text.startswith("/task "):
            return self._cmd_task(msg, text[len("/task "):].strip())
        if text.startswith("/cancel "):
            return self._cmd_cancel(msg, text[len("/cancel "):].strip())
        if text.startswith("/status"):
            return self._cmd_status(msg, text[len("/status"):].strip())
        if text == "/tasks":
            return self._cmd_tasks(msg)
        reply = ("I understand: /task <goal>, /status [id], /tasks, /cancel <id>.\n"
                 "Approvals are granted in the dashboard, never here.")
        self.channel.send(msg.chat_id, reply)
        return reply

    def _send(self, msg: IncomingMessage, text: str) -> str:
        self.channel.send(msg.chat_id, text)
        return text

    def _cmd_task(self, msg: IncomingMessage, goal: str) -> str:
        from . import agent_llm
        if not goal:
            return self._send(msg, "Usage: /task <goal>")
        task_id = agent_llm.create_agent_task(goal, self.cfg)
        self.approvals.audit("telegram", "task.created", task_id, {"goal": goal[:200]})
        self._send(msg, f"Task {task_id[:8]} started. I'll reply when it's done.")
        threading.Thread(target=self._run_and_reply,
                         args=(msg.chat_id, task_id), daemon=True).start()
        return task_id

    def _run_and_reply(self, chat_id: int, task_id: str) -> None:
        from . import agent_llm
        try:
            task = agent_llm.run_llm_task(task_id, self.cfg)
        except Exception as exc:
            self.channel.send(chat_id, f"Task {task_id[:8]} crashed: {exc}")
            return
        status = task["status"]
        if status == "completed":
            result = task.get("result") or {}
            reply = f"Task {task_id[:8]} completed and verified.\n{json.dumps(result, indent=2)[:3000]}"
        elif status == "awaiting_approval":
            reply = (f"Task {task_id[:8]} needs approval in the dashboard "
                     f"(I never auto-approve).")
        else:
            reply = f"Task {task_id[:8]} ended: {status}. {task.get('error', '')[:500]}"
        try:
            self.channel.send(chat_id, reply)
        except Exception:
            pass
        if self.on_task_done:
            self.on_task_done(task_id, task)

    def _cmd_status(self, msg: IncomingMessage, arg: str) -> str:
        task = self.tasks.get_task(arg) if arg else None
        if task is None:
            recent = self.tasks.list_tasks(limit=1)
            task = recent[0] if recent else None
        if task is None:
            return self._send(msg, "No tasks yet.")
        result = task.get("result")
        verified = (result or {}).get("verified") if isinstance(result, dict) else None
        return self._send(
            msg,
            f"Task {task['id'][:8]}: {task['status']}\n"
            f"Goal: {task['goal'][:200]}\n"
            f"Verified: {verified}\n"
            f"{('Error: ' + task['error'][:300]) if task.get('error') else ''}".rstrip())

    def _cmd_tasks(self, msg: IncomingMessage) -> str:
        tasks = self.tasks.list_tasks(limit=8)
        if not tasks:
            return self._send(msg, "No tasks yet.")
        lines = [f"{t['id'][:8]} {t['status']} — {t['goal'][:60]}" for t in tasks]
        return self._send(msg, "\n".join(lines))

    def _cmd_cancel(self, msg: IncomingMessage, task_id: str) -> str:
        task = self.tasks.get_task(task_id)
        if not task:
            # allow prefix match
            matches = [t for t in self.tasks.list_tasks(limit=50) if t["id"].startswith(task_id)]
            task = matches[0] if matches else None
        if not task:
            return self._send(msg, f"Unknown task {task_id}.")
        self.tasks.request_cancel(task["id"])
        for rec in self.approvals.pending_for_task(task["id"]):
            self.approvals.decide(rec["id"], "rejected", decided_by="telegram-cancel")
        self.approvals.audit("telegram", "task.cancel", task["id"], {})
        return self._send(msg, f"Cancel requested for task {task['id'][:8]}.")


def run_telegram(cfg) -> int:
    runner = ChannelRunner(cfg)
    if not runner.allowed:
        print("WARNING: NEXTCLAW_TELEGRAM_ALLOWED_IDS is empty — the bot will ignore everyone.")
    print("Telegram channel running. Ctrl-C to stop.")
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        pass
    return 0
