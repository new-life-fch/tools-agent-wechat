#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_gateway.py — 微信收发服务（iLink Bot API）

一个进程搞定三件事：
  1. **发**：把文本 / 图片 / 文件发到指定微信会话（内建切片、限流退避、图片优先）；
  2. **收**：后台长轮询 iLink，把收到的消息落到本地收件箱 JSONL（含图片/文件下载），
     供 ``wait_events.py`` 阻塞读取——这样 Agent 永远不会漏消息；
  3. **HTTP API**：``serve`` 模式下额外暴露本地 HTTP 接口，Agent 用 curl 也能发。

常用命令::

    python wechat_gateway.py login                     # 扫码登录（只需一次）
    python wechat_gateway.py doctor                    # 自检：配置/凭据/网络/收件箱
    python wechat_gateway.py send --text "第 3 题：B"  # 发文本（给 default_chat_id）
    python wechat_gateway.py send --image code.png --file solution.py --text "思路…"
    python wechat_gateway.py serve                     # 常驻：长轮询 + HTTP API
    python wechat_gateway.py inbox --since 0           # 查看收件箱
    python wechat_gateway.py chats                     # 列出已知会话
    python wechat_gateway.py typing --status start     # 显示“正在输入”
    python wechat_gateway.py logout                    # 清除登录态

所有路径都相对本文件所在目录解析（配置文件里也可写绝对路径），Windows 同样适用。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import stat
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ilink_client import (  # noqa: E402
    ILINK_BASE_URL, WEIXIN_CDN_BASE_URL, ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO,
    TYPING_START, TYPING_STOP, IlinkClient, IlinkError, format_text, qr_login, render_qr, split_text,
)

def _console_safe() -> None:
    """Windows 控制台默认是 GBK(cp936)：打印 ✓ ✗ ⚠ 这类符号会直接 UnicodeEncodeError 崩掉。
    这里只把错误策略放宽成 replace —— 保留控制台原生编码（中文照常显示），个别符号降级成 ?。
    想全 UTF-8：set PYTHONUTF8=1 或 chcp 65001。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_console_safe()


VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "wechat_gateway.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "_readme": "微信网关配置。login 会自动填 account_id/token/user_id/default_chat_id；其余项按需改。",
    "base_url": ILINK_BASE_URL,
    "cdn_base_url": WEIXIN_CDN_BASE_URL,
    "bot_type": "3",
    "account_id": "",
    "token": "",
    "user_id": "",
    "default_chat_id": "",
    "inbox_file": "wechat_inbox.jsonl",
    "state_dir": ".state/wechat",
    "media_dir": "wechat_media",
    "http": {"host": "127.0.0.1", "port": 8799, "api_token": ""},
    "send": {"chunk_size": 1800, "chunk_delay_sec": 1.2, "retries": 4, "retry_delay_sec": 1.0,
             "typing": True, "max_file_mb": 30},
    "inbox": {"download_media": True, "max_media_mb": 50, "poll_timeout_ms": 35000,
              "dedup_window": 2000, "keep_media_days": 0, "heartbeat_interval_sec": 15},
    "log": {"level": "info"},
}


# ----------------------------- 配置 -----------------------------

def _strip_jsonc(text: str) -> str:
    """允许配置文件里写 // 与 /* */ 注释（JSONC-lite）"""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_path(cfg: Dict[str, Any], value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(HERE, value)


def load_config(path: str, create: bool = True) -> Dict[str, Any]:
    if not os.path.exists(path):
        if not create:
            raise SystemExit("配置文件不存在: %s" % path)
        save_config(path, DEFAULT_CONFIG)
        print("[配置] 已生成默认配置: %s" % path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.loads(_strip_jsonc(f.read()) or "{}")
    return deep_merge(DEFAULT_CONFIG, raw)


_JSONC_SCALAR = r'(?:"(?:[^"\\]|\\.)*"|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'


def patch_jsonc_top_level(text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """把 cfg 的顶层标量键就地写回 text，**保留注释与排版**（login 只改凭据几个键，
    以前整份重写会把配置里的说明注释全抹掉）。补丁后解析不过就返回 None 让调用方回退。"""
    out = text
    for key, value in cfg.items():
        if isinstance(value, (dict, list)):
            continue
        pattern = re.compile(r'(^[ \t]*"%s"[ \t]*:[ \t]*)%s' % (re.escape(key), _JSONC_SCALAR), re.M)
        if not pattern.search(out):
            continue
        out = pattern.sub(lambda m: m.group(1) + json.dumps(value, ensure_ascii=False), out, count=1)
    try:
        json.loads(_strip_jsonc(out))
    except ValueError:
        return None
    return out


def save_config(path: str, cfg: Dict[str, Any]) -> None:
    body = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if existing and ("//" in existing or "/*" in existing):     # 带注释的配置：原地打补丁
        patched = patch_jsonc_top_level(existing, cfg)
        if patched is not None:
            body = patched
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)          # token 落盘，收紧权限
    except OSError:
        pass


# ----------------------------- 本地状态 -----------------------------

class State:
    """网关的磁盘状态：同步游标 / context_token / 会话表 / 心跳 / 收件箱序号"""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.dir = resolve_path(cfg, cfg["state_dir"])
        self.inbox_path = resolve_path(cfg, cfg["inbox_file"])
        self.media_dir = resolve_path(cfg, cfg["media_dir"])
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._write_warned: set = set()

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def read_json(self, name: str, default: Any) -> Any:
        try:
            with open(self._path(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def write_json(self, name: str, data: Any) -> None:
        """原子写。两点 Windows 适配：
        * 临时文件名带 pid —— 固定名字在多进程/杀软扫描下更容易撞车；
        * Windows 上若目标文件正被别的进程打开，os.replace 会抛 PermissionError
          （POSIX 不会），而心跳/游标这类状态不是关键路径，绝不能因此把网关带崩。
        """
        with self._lock:
            target = self._path(name)
            tmp = "%s.%d.tmp" % (target, os.getpid())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, target)
            except OSError as exc:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                key = "%s:%s" % (name, exc.__class__.__name__)
                if key not in self._write_warned:          # 同类错只提醒一次，别刷屏
                    self._write_warned.add(key)
                    print("[警告] 状态写入失败（下一轮会重试）: %s -> %s" % (name, exc), file=sys.stderr)

    # -- 长轮询游标 --
    @property
    def sync_buf(self) -> str:
        return str(self.read_json("sync.json", {}).get("get_updates_buf") or "")

    @sync_buf.setter
    def sync_buf(self, value: str) -> None:
        self.write_json("sync.json", {"get_updates_buf": value, "ts": time.time()})

    # -- context_token（回复时要原样回带；丢了会自动降级为无 token 发送） --
    def get_context_token(self, chat_id: str) -> Optional[str]:
        return (self.read_json("context_tokens.json", {}) or {}).get(chat_id)

    def set_context_token(self, chat_id: str, token: str) -> None:
        if not token:
            return
        data = self.read_json("context_tokens.json", {}) or {}
        if data.get(chat_id) != token:
            data[chat_id] = token
            self.write_json("context_tokens.json", data)

    # -- 微信侧已知会话 --
    def chats(self) -> Dict[str, Any]:
        return self.read_json("chats.json", {}) or {}

    def remember_chat(self, chat_id: str, chat_type: str, text: str) -> None:
        data = self.chats()
        item = data.get(chat_id) or {"chat_id": chat_id, "chat_type": chat_type, "count": 0}
        item["chat_type"] = chat_type
        item["count"] = int(item.get("count", 0)) + 1
        item["last_ts"] = time.time()
        item["last_text"] = (text or "")[:200]
        data[chat_id] = item
        self.write_json("chats.json", data)

    # -- 心跳（wait_events.py 靠它判断网关是否在跑） --
    def heartbeat(self, **fields: Any) -> None:
        data = self.read_json("heartbeat.json", {}) or {}
        data.update({"pid": os.getpid(), "ts": time.time(), "version": VERSION})
        data.update(fields)
        self.write_json("heartbeat.json", data)

    def read_heartbeat(self) -> Dict[str, Any]:
        return self.read_json("heartbeat.json", {}) or {}

    # -- 收件箱 --
    def next_seq(self) -> int:
        with self._lock:
            seq_file = self._path("seq.txt")
            try:
                with open(seq_file, "r", encoding="utf-8") as f:
                    n = int(f.read().strip() or "0") + 1
            except (OSError, ValueError):
                n = int(self.read_json("seq.json", {}).get("seq", 0)) + 1
            with open(seq_file, "w", encoding="utf-8") as f:
                f.write(str(n))
            return n

    def append_inbox(self, record: Dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.inbox_path), exist_ok=True)
            with open(self.inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def read_inbox(self, since_seq: int = 0, limit: int = 0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not os.path.exists(self.inbox_path):
            return out
        with open(self.inbox_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if int(rec.get("seq", 0)) > since_seq:
                    out.append(rec)
                    if limit and len(out) >= limit:
                        break
        return out


# ----------------------------- 入站解析 -----------------------------

def sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def extract_text(item_list: List[Dict[str, Any]]) -> str:
    """从 item_list 里取正文：优先文本项，其次语音转写，并把被引用的消息带出来"""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if ref_item.get("type") in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE):
                title = ref.get("title") or ""
                return ("[引用媒体: %s]\n%s" % (title, text)).strip() if title else "[引用媒体]\n%s" % text
            if ref_item:
                inner = extract_text([ref_item])
                parts = [p for p in ((str(ref["title"]) if ref.get("title") else ""), inner) if p]
                if parts:
                    return "[引用: %s]\n%s" % (" | ".join(parts), text)
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            if not (voice.get("media") or {}) and voice.get("text"):
                return "[语音] %s" % voice["text"]
    return ""


def chat_identity(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    """判断这条消息来自私聊还是群聊 -> (chat_type, chat_id)"""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    if room_id or (to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1):
        return "group", (room_id or to_user_id or str(message.get("from_user_id") or ""))
    return "dm", str(message.get("from_user_id") or "")


MEDIA_ITEM_KEYS = {"image_item": "image", "file_item": "file", "voice_item": "voice", "video_item": "video"}


# ----------------------------- 收件箱轮询线程 -----------------------------

class InboxPoller(threading.Thread):
    """后台长轮询：消息 -> 收件箱 JSONL（图片/文件顺带落盘）"""

    def __init__(self, client: IlinkClient, state: State, stop_event: threading.Event, echo: bool = True) -> None:
        super().__init__(name="wechat-inbox-poller", daemon=True)
        self.client = client
        self.state = state
        self.stop_event = stop_event
        self.echo = echo
        self.seen: "queue.Queue[str]" = queue.Queue()
        self._seen_ids: List[str] = []
        self.last_poll_ts = 0.0
        self.last_error = ""
        self.inbound_count = 0
        self.soft_reconnects = 0        # 服务端关闭空闲长轮询连接的次数（正常现象，不算错误）

    def _dedup(self, key: str) -> bool:
        if not key:
            return False
        if key in self._seen_ids:
            return True
        self._seen_ids.append(key)
        limit = int(self.state.cfg["inbox"].get("dedup_window", 2000))
        if len(self._seen_ids) > limit:
            self._seen_ids = self._seen_ids[-limit:]
        return False

    def _save_media(self, item: Dict[str, Any], seq: int, index: int) -> Optional[Dict[str, str]]:
        payload_key = next((k for k in MEDIA_ITEM_KEYS if item.get(k)), None)
        if payload_key is None:
            return None
        payload = item[payload_key] or {}
        kind = MEDIA_ITEM_KEYS[payload_key]
        max_mb = float(self.state.cfg["inbox"].get("max_media_mb", 50))
        try:
            data = self.client.download_media(item)
        except IlinkError as exc:
            print("[收] 媒体下载失败(%s): %s" % (kind, exc), flush=True)
            return {"kind": kind, "path": "", "name": "", "error": str(exc)}
        if len(data) > max_mb * 1024 * 1024:
            return {"kind": kind, "path": "", "name": "", "error": "超过 %sMB 限制" % max_mb}
        name = str(payload.get("file_name") or "")
        ext = os.path.splitext(name)[1] or sniff_ext(data)
        os.makedirs(self.state.media_dir, exist_ok=True)
        fname = "%s_%04d_%d_%s%s" % (time.strftime("%Y%m%d_%H%M%S"), seq, index, kind, ext)
        path = os.path.join(self.state.media_dir, fname)
        with open(path, "wb") as f:
            f.write(data)
        return {"kind": kind, "path": path, "name": name or fname}

    def handle_message(self, message: Dict[str, Any]) -> None:
        account_id = str(self.state.cfg.get("account_id") or "")
        sender = str(message.get("from_user_id") or "").strip()
        msg_id = str(message.get("message_id") or "").strip()
        if not sender or sender == account_id:
            return
        if self._dedup("id:%s" % msg_id if msg_id else ""):
            return
        item_list = message.get("item_list") or []
        text = extract_text(item_list)
        if text and self._dedup("text:%s:%s" % (sender, text[:200])):
            return
        chat_type, chat_id = chat_identity(message, account_id)
        if not chat_id:
            return
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self.state.set_context_token(chat_id, context_token)
            if sender != chat_id:
                self.state.set_context_token(sender, context_token)

        media: List[Dict[str, str]] = []
        if bool(self.state.cfg["inbox"].get("download_media", True)):
            seq_preview = self.state.next_seq()
            for idx, item in enumerate(item_list):
                if item.get("type") in (ITEM_IMAGE, ITEM_FILE, ITEM_VOICE, ITEM_VIDEO):
                    saved = self._save_media(item, seq_preview, idx)
                    if saved:
                        media.append(saved)
            seq = seq_preview
        else:
            seq = self.state.next_seq()

        if not text and not media:
            return
        record = {
            "seq": seq,
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "msg_id": msg_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "sender": sender,
            "text": text,
            "media": media,
            "gateway": account_id,
        }
        self.state.append_inbox(record)
        self.state.remember_chat(chat_id, chat_type, text or ("[%s]" % (media[0]["kind"] if media else "媒体")))
        self.inbound_count += 1
        if self.echo:
            summary = text.replace("\n", " ")[:120] if text else "[%s]" % (media[0]["kind"] if media else "媒体")
            print("[收] seq=%d %s %s: %s" % (seq, chat_type, sender[:12], summary), flush=True)

    def run(self) -> None:
        backoff = 0
        while not self.stop_event.is_set():
            try:
                started = time.monotonic()
                resp = self.client.get_updates(
                    self.state.sync_buf, timeout_ms=int(self.state.cfg["inbox"].get("poll_timeout_ms", 35000)))
                elapsed = time.monotonic() - started
                self.last_poll_ts = time.time()
                self.last_error = ""
                backoff = 0
                if resp.get("reconnect"):
                    self.soft_reconnects += 1
                    if self.soft_reconnects == 1 or self.soft_reconnects % 20 == 0:
                        print("[收] 服务端关闭了空闲的长轮询连接，已自动重连（第 %d 次；正常现象，不影响收信）"
                              % self.soft_reconnects, flush=True)
                    self.stop_event.wait(0.5)      # 稍等一下再连，别空转
                new_buf = str(resp.get("get_updates_buf") or "")
                for message in (resp.get("msgs") or []):
                    try:
                        self.handle_message(message)
                    except Exception as exc:                     # 单条坏消息不能拖垮轮询
                        print("[收] 处理消息异常: %r" % exc, flush=True)
                if new_buf and new_buf != self.state.sync_buf:
                    self.state.sync_buf = new_buf
                if not (resp.get("msgs") or []) and elapsed < 1.0:
                    # 正常情况下服务端会把请求挂到 35s 才返回；秒回空结果说明长轮询被中间设备
                    # （代理/VPN/防火墙）截断了，这里垫一下，避免空转把接口打爆触发限流(-2)
                    self.stop_event.wait(1.0 - elapsed)
                self.state.heartbeat(ok=True, mode="serve", last_poll_ts=self.last_poll_ts,
                                     inbox_count=self.inbound_count,
                                     account_id=self.state.cfg.get("account_id", ""))
            except IlinkError as exc:
                self.last_error = str(exc)
                backoff = min(backoff + 1, 6) if backoff else 1
                wait = min(30.0, 2.0 * backoff)
                print("[收] 轮询失败(%s)，%.0fs 后重试" % (exc, wait), flush=True)
                self.state.heartbeat(ok=False, mode="serve", error=self.last_error,
                                     last_poll_ts=self.last_poll_ts, inbox_count=self.inbound_count)
                self.stop_event.wait(wait)
            except Exception as exc:
                self.last_error = repr(exc)
                print("[收] 轮询异常: %r" % exc, flush=True)
                self.stop_event.wait(5)


# ----------------------------- 发送 -----------------------------

def make_client(cfg: Dict[str, Any], verbose: bool = False) -> IlinkClient:
    send = cfg.get("send") or {}
    return IlinkClient(
        token=str(cfg.get("token") or ""),
        base_url=str(cfg.get("base_url") or ILINK_BASE_URL),
        cdn_base_url=str(cfg.get("cdn_base_url") or WEIXIN_CDN_BASE_URL),
        retries=int(send.get("retries", 4)),
        retry_delay=float(send.get("retry_delay_sec", 1.0)),
        verbose=verbose,
    )


def resolve_target(cfg: Dict[str, Any], to: str = "") -> str:
    target = (to or cfg.get("default_chat_id") or cfg.get("user_id") or "").strip()
    if not target:
        raise IlinkError("没有发送目标：请先 login，或用 --to 指定，或在配置里填 default_chat_id")
    return target


def send_bundle(
    cfg: Dict[str, Any],
    state: State,
    to: str,
    text: str = "",
    images: Optional[List[str]] = None,
    files: Optional[List[str]] = None,
    typing: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """发一条「文本 + 图片 + 文件」的组合消息，返回结构化结果（供 CLI / HTTP / Skill 复用）"""
    target = resolve_target(cfg, to)
    client = make_client(cfg, verbose=bool(dry_run))
    send_cfg = cfg.get("send") or {}
    chunk_size = int(send_cfg.get("chunk_size", 1800))
    chunk_delay = float(send_cfg.get("chunk_delay_sec", 1.2))
    max_bytes = float(send_cfg.get("max_file_mb", 30)) * 1024 * 1024
    images = [p for p in (images or []) if p]
    files = [p for p in (files or []) if p]
    report: Dict[str, Any] = {"ok": True, "to": target, "sent": [], "errors": [], "chunks": 0,
                              "images": [], "files": [], "dry_run": dry_run}

    for label, paths in (("image", images), ("file", files)):
        for path in paths:
            real = path if os.path.isabs(path) else os.path.join(HERE, path)
            if not os.path.isfile(real):
                report["ok"] = False
                report["errors"].append("附件不存在: %s" % path)
            elif os.path.getsize(real) > max_bytes:
                report["ok"] = False
                report["errors"].append("附件超过 %.0fMB: %s" % (max_bytes / 1024 / 1024, path))
    if report["errors"]:
        return report

    do_typing = bool(send_cfg.get("typing", True)) if typing is None else bool(typing)
    ticket = ""
    context_token = state.get_context_token(target) if state else None
    if dry_run:
        print("[dry-run] 目标=%s context_token=%s" % (target, "有" if context_token else "无"))

    if do_typing and not dry_run:
        ticket = client.typing_ticket(target, context_token)
        if ticket:
            client.send_typing(target, ticket, TYPING_START)

    try:
        if (text or "").strip():
            payload = format_text(text)
            for chunk in split_text(payload, chunk_size):
                report["chunks"] += 1
                if dry_run:
                    print("[dry-run] 文本 #%d (%d 字):\n%s\n" % (report["chunks"], len(chunk), chunk))
                    continue
                client.send_text(target, chunk, context_token=context_token)
                report["sent"].append({"type": "text", "chars": len(chunk)})
                print("[发] 文本 %d 字 -> %s" % (len(chunk), target), flush=True)
                if chunk_delay > 0:
                    time.sleep(chunk_delay)

        for path in images:
            real = path if os.path.isabs(path) else os.path.join(HERE, path)
            if dry_run:
                print("[dry-run] 图片: %s (%d KB)" % (real, os.path.getsize(real) // 1024))
                continue
            client.send_media(target, real, context_token=context_token)
            report["images"].append(real)
            report["sent"].append({"type": "image", "path": real})
            print("[发] 图片 %s (%d KB)" % (os.path.basename(real), os.path.getsize(real) // 1024), flush=True)
            time.sleep(0.6)

        for path in files:
            real = path if os.path.isabs(path) else os.path.join(HERE, path)
            if dry_run:
                print("[dry-run] 文件: %s (%d KB)" % (real, os.path.getsize(real) // 1024))
                continue
            client.send_media(target, real, context_token=context_token)
            report["files"].append(real)
            report["sent"].append({"type": "file", "path": real})
            print("[发] 文件 %s (%d KB)" % (os.path.basename(real), os.path.getsize(real) // 1024), flush=True)
            time.sleep(0.6)
    except IlinkError as exc:
        report["ok"] = False
        report["errors"].append(str(exc))
    finally:
        if do_typing and ticket and not dry_run:
            client.send_typing(target, ticket, TYPING_STOP)

    if state is not None and not dry_run:
        state.heartbeat(ok=report["ok"], mode="send", last_send_ts=time.time(),
                        target=target, account_id=cfg.get("account_id", ""))
    return report


# ----------------------------- HTTP API -----------------------------

def make_handler(cfg: Dict[str, Any], state: State, poller: Optional[InboxPoller], started_at: float):
    api_token = str((cfg.get("http") or {}).get("api_token") or "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "wechat-gateway/%s" % VERSION

        def log_message(self, fmt: str, *args: Any) -> None:      # 静音默认日志，避免刷屏
            if str(cfg.get("log", {}).get("level", "info")) == "debug":
                sys.stderr.write("[http] %s\n" % (fmt % args))

        # -- helpers --
        def _send_json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not api_token:
                return True
            return self.headers.get("X-Api-Token", "") == api_token

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        # -- routes --
        def do_GET(self) -> None:                                  # noqa: N802
            if not self._authorized():
                return self._send_json({"ok": False, "error": "未授权：缺少正确的 X-Api-Token"}, 401)
            path, _, query = self.path.partition("?")
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            if path == "/health":
                hb = state.read_heartbeat()
                return self._send_json({
                    "ok": True,
                    "version": VERSION,
                    "account_id": cfg.get("account_id", ""),
                    "default_chat_id": cfg.get("default_chat_id", ""),
                    "uptime_sec": round(time.time() - started_at, 1),
                    "inbox": {"last_poll_ts": getattr(poller, "last_poll_ts", 0.0),
                              "last_error": getattr(poller, "last_error", ""),
                              "count": getattr(poller, "inbound_count", 0),
                              "reconnects": getattr(poller, "soft_reconnects", 0)},
                    "heartbeat": hb,
                    "chats": list(state.chats().values()),
                })
            if path == "/inbox":
                since = int(params.get("since", "0") or 0)
                limit = int(params.get("limit", "50") or 50)
                return self._send_json({"ok": True, "messages": state.read_inbox(since, limit)})
            if path == "/chats":
                return self._send_json({"ok": True, "chats": list(state.chats().values())})
            return self._send_json({"ok": False, "error": "未知路径 %s" % path}, 404)

        def do_POST(self) -> None:                                 # noqa: N802
            if not self._authorized():
                return self._send_json({"ok": False, "error": "未授权：缺少正确的 X-Api-Token"}, 401)
            path = self.path.partition("?")[0]
            body = self._read_json()
            if path == "/send":
                try:
                    report = send_bundle(
                        cfg, state,
                        to=str(body.get("to") or ""),
                        text=str(body.get("text") or ""),
                        images=list(body.get("images") or []),
                        files=list(body.get("files") or []),
                        typing=body.get("typing"),
                        dry_run=bool(body.get("dry_run")),
                    )
                except IlinkError as exc:
                    return self._send_json({"ok": False, "error": str(exc)}, 500)
                return self._send_json(report, 200 if report.get("ok") else 500)
            if path == "/typing":
                target = resolve_target(cfg, str(body.get("to") or ""))
                status = TYPING_STOP if str(body.get("status") or "start").lower() in ("stop", "0", "end") else TYPING_START
                client = make_client(cfg)
                ticket = client.typing_ticket(target, state.get_context_token(target))
                ok = client.send_typing(target, ticket, status) if ticket else False
                return self._send_json({"ok": ok, "to": target, "status": status})
            return self._send_json({"ok": False, "error": "未知路径 %s" % path}, 404)

    return Handler


def preflight_session(cfg: Dict[str, Any], state: State) -> Optional[str]:
    """启动自检：与默认收件人的**会话是否已建立**。

    没建立时 iLink 会拒绝主动推送（ret=-2 prepare failed），而这不是 token 失效、
    网络也正常 —— 唯一解法是让用户先用微信给机器人发一条消息。
    返回：None=已建立（或无法判断）；字符串=需要提醒用户的说明。
    """
    target = (cfg.get("default_chat_id") or cfg.get("user_id") or "").strip()
    if not target or not cfg.get("token"):
        return None
    try:
        resp = make_client(cfg).get_config(target, state.get_context_token(target))
    except IlinkError as exc:
        if "-4" in str(exc) or "TypingTicket" in str(exc):
            return ("与 %s 的会话还没建立：请先用微信给机器人发一条消息（随便一个字），"
                    "否则主动推送会被 iLink 拒绝（ret=-2 prepare failed）" % target)
        return None                     # 网络类问题交给轮询循环去报
    return None if resp.get("ret") in (None, 0) else None


def run_server(cfg: Dict[str, Any], state: State, with_poll: bool = True) -> int:
    stop_event = threading.Event()
    poller: Optional[InboxPoller] = None
    started_at = time.time()
    client = make_client(cfg)

    if with_poll:
        if not str(cfg.get("token") or ""):
            print("[服务] 未登录（缺少 token）：只启动 HTTP 接口，不接收消息。先跑 login。")
        else:
            poller = InboxPoller(client, state, stop_event)
            poller.start()
            print("[服务] 收件轮询已启动（长轮询 35s/次，消息落盘: %s）" % state.inbox_path)
            notice = preflight_session(cfg, state)
            if notice:
                print("[服务] ⚠ %s" % notice)
            elif cfg.get("default_chat_id"):
                print("[服务] 会话已建立，可随时主动推送（无需先发消息激活）")

    host = str((cfg.get("http") or {}).get("host") or "127.0.0.1")
    port = int((cfg.get("http") or {}).get("port") or 8799)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(cfg, state, poller, started_at))
    except OSError as exc:
        print("[服务] HTTP 端口 %s:%d 绑定失败: %s" % (host, port, exc))
        stop_event.set()
        return 1
    print("[服务] HTTP API: http://%s:%d  (GET /health /inbox /chats, POST /send /typing)" % (host, port))
    print("[服务] account_id=%s  默认目标=%s" % (cfg.get("account_id") or "-", resolve_target(cfg) if (cfg.get("default_chat_id") or cfg.get("user_id")) else "-"))
    print("[服务] Ctrl+C 退出")

    thread = threading.Thread(target=server.serve_forever, name="wechat-http", daemon=True)
    thread.start()
    # 心跳只用来让 wait_events.py / doctor 判断「网关还活着」，没必要高频写盘：
    # 原来每 0.5s 写一次 ≈ 17 万次/天，现在按 heartbeat_interval_sec（默认 15s）写，少 30 倍。
    hb_interval = float((cfg.get("inbox") or {}).get("heartbeat_interval_sec", 15) or 15)
    last_hb = 0.0
    try:
        while True:
            time.sleep(0.5)
            now = time.time()
            if poller is not None and now - last_hb >= hb_interval:
                last_hb = now
                state.heartbeat(ok=not poller.last_error, mode="serve", last_poll_ts=poller.last_poll_ts,
                                error=poller.last_error, inbox_count=poller.inbound_count,
                                account_id=cfg.get("account_id", ""))
    except KeyboardInterrupt:
        print("\n[服务] 收到中断，正在安全退出…")
    finally:
        stop_event.set()
        server.shutdown()
        if poller is not None:
            poller.join(timeout=3)
        state.heartbeat(ok=False, mode="stopped", ts=time.time())
    return 0


# ----------------------------- 子命令 -----------------------------

def cmd_login(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    if args.reset:
        cfg["account_id"] = cfg["token"] = cfg["user_id"] = cfg["default_chat_id"] = ""
        save_config(path, cfg)
    if cfg.get("token") and not args.force:
        print("已登录，沿用现有机器人身份：account_id=%s" % (cfg.get("account_id") or "?"))
        print("  · 直接收发：python wechat_gateway.py serve / send")
        print("  · 要换一个全新机器人（例如把工具发给别人、或换微信号）：加 --new 重新扫码")
        return 0
    if args.force and cfg.get("token"):
        print("[登录] --new：忽略现有身份，向 iLink 申请一张全新二维码")
        print("       对方扫完拿到的是他自己的机器人，与 %s 无关；旧身份不会自动解绑"
              "（可在微信里删除该机器人会话）。" % (cfg.get("account_id") or "旧身份"))
    client = IlinkClient(base_url=str(cfg.get("base_url") or ILINK_BASE_URL))
    png = os.path.join(HERE, args.qr_png)
    print("=== 微信扫码登录 ===")
    print("请用微信扫下方二维码（终端字符画或 %s），手机上点确认。" % png)
    try:
        creds = qr_login(
            client,
            bot_type=str(cfg.get("bot_type") or "3"),
            timeout_seconds=args.timeout,
            on_qr=lambda value, url: render_qr(url, png),
            on_status=lambda msg: print("[登录] %s" % msg, flush=True),
        )
    except IlinkError as exc:
        print("[登录] 失败: %s" % exc)
        return 1
    cfg.update({"account_id": creds["account_id"], "token": creds["token"],
                "base_url": creds["base_url"], "user_id": creds["user_id"]})
    if not cfg.get("default_chat_id"):
        cfg["default_chat_id"] = creds["user_id"] or creds["account_id"]
    save_config(path, cfg)
    if os.path.exists(png):
        try:
            os.remove(png)
        except OSError:
            pass
    print("[登录] 成功！account_id=%s" % creds["account_id"])
    print("[登录] 机器人身份=%s  默认发送目标=%s" % (creds["account_id"], cfg["default_chat_id"]))
    print("[登录] 凭据已写入 %s（权限 600）" % path)
    print("[登录] 下一步：python wechat_gateway.py send --text \"测试消息\"")
    return 0


def cmd_send(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    text = args.text or ""
    if args.text_file:
        with open(resolve_path(cfg, args.text_file), "r", encoding="utf-8") as f:
            text = f.read()
    if not (text.strip() or args.image or args.file):
        print("没有内容可发：用 --text / --text-file / --image / --file")
        return 2
    try:
        report = send_bundle(cfg, state, to=args.to or "", text=text,
                             images=args.image or [], files=args.file or [],
                             typing=(None if args.typing is None else args.typing), dry_run=args.dry_run)
    except IlinkError as exc:
        report = {"ok": False, "errors": [str(exc)]}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print("[结果] %s  文本块=%d 图片=%d 文件=%d%s"
              % ("成功" if report.get("ok") else "失败", report.get("chunks", 0),
                 len(report.get("images", [])), len(report.get("files", [])),
                 ("  错误: " + "; ".join(report.get("errors", []))) if report.get("errors") else ""))
    return 0 if report.get("ok") else 1


def cmd_typing(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    try:
        target = resolve_target(cfg, args.to or "")
    except IlinkError as exc:
        print("[typing] %s" % exc)
        return 2
    client = make_client(cfg)
    ticket = client.typing_ticket(target, state.get_context_token(target))
    if not ticket:
        print("[typing] 拿不到 typing_ticket（可能未登录或会话未激活），跳过")
        return 1
    status = TYPING_START if args.status == "start" else TYPING_STOP
    ok = client.send_typing(target, ticket, status)
    print("[typing] %s -> %s" % ("已显示" if ok and status == TYPING_START else "已停止" if ok else "失败", target))
    return 0 if ok else 1


def cmd_inbox(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    msgs = state.read_inbox(since_seq=args.since, limit=args.limit)
    if args.json:
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return 0
    if not msgs:
        print("（收件箱没有新消息；文件: %s）" % state.inbox_path)
        return 0
    for rec in msgs:
        when = time.strftime("%H:%M:%S", time.localtime(rec.get("ts", 0)))
        who = "群" if rec.get("chat_type") == "group" else "私聊"
        print("#%-4d %s %s %s" % (rec.get("seq", 0), when, who, rec.get("sender", "")[:20]))
        for line in str(rec.get("text") or "").splitlines():
            print("      %s" % line)
        for media in rec.get("media") or []:
            print("      [%s] %s" % (media.get("kind"), media.get("path") or media.get("error", "")))
    return 0


def cmd_chats(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    chats = sorted(state.chats().values(), key=lambda c: c.get("last_ts", 0), reverse=True)
    if not chats:
        print("（还没有收到过任何消息。让对方先给机器人发一条微信，或直接看 default_chat_id=%s）"
              % (cfg.get("default_chat_id") or "-"))
        return 0
    for chat in chats:
        when = time.strftime("%m-%d %H:%M", time.localtime(chat.get("last_ts", 0)))
        print("%-28s %-6s %3d 条  %s  %s" % (chat.get("chat_id", ""), chat.get("chat_type", ""),
                                             chat.get("count", 0), when, (chat.get("last_text") or "")[:40]))
    print("\n默认目标: %s" % (cfg.get("default_chat_id") or "-"))
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    ok = True
    print("=== wechat_gateway 自检 ===")
    print("[1] 配置文件: %s" % path)
    print("    account_id=%s  user_id=%s  默认目标=%s"
          % (cfg.get("account_id") or "(空)", cfg.get("user_id") or "(空)", cfg.get("default_chat_id") or "(空)"))
    mode = oct(os.stat(path).st_mode & 0o777) if os.path.exists(path) else "-"
    print("    权限=%s %s" % (mode, "" if mode in ("0o600",) else "（建议 600：文件里有 token）"))
    if not cfg.get("token"):
        print("[2] 凭据: 缺失 —— 请先运行 python wechat_gateway.py login")
        ok = False
    else:
        print("[2] 凭据: token=%s…%s  account_id=%s"
              % (str(cfg["token"])[:6], str(cfg["token"])[-4:], cfg.get("account_id")))
        client = make_client(cfg)
        try:
            client._get("ilink/bot/get_bot_qrcode?bot_type=%s" % (cfg.get("bot_type") or "3"), timeout=15.0)
            print("[3] 网络: iLink 可达 ✓")
        except IlinkError as exc:
            print("[3] 网络: iLink 不可达 ✗ %s" % exc)
            ok = False
        if cfg.get("user_id"):
            try:
                resp = client.get_config(str(cfg["user_id"]), state.get_context_token(str(cfg["user_id"])))
                print("[4] 凭据有效性: getconfig ✓ (typing_ticket=%s)"
                      % ("有" if resp.get("typing_ticket") else "无"))
            except IlinkError as exc:
                # ret=-4/GetTypingTicket 只是「还没有活跃会话」（对方还没给机器人发过消息），不是 token 失效
                if "-4" in str(exc) or "TypingTicket" in str(exc):
                    print("[4] 凭据有效性: 会话尚未建立（正常）—— 让对方先在微信里给机器人发一条消息，"
                          "之后即可双向收发")
                else:
                    print("[4] 凭据有效性: getconfig 失败 ✗ %s（token 可能已失效，需要重新 login）" % exc)
                    ok = False
    print("[5] 收件箱: %s (%s)" % (state.inbox_path, "存在" if os.path.exists(state.inbox_path) else "尚未创建"))
    hb = state.read_heartbeat()
    age = time.time() - float(hb.get("ts") or 0) if hb else -1
    if hb:
        print("[6] 网关心跳: mode=%s ok=%s %.1fs 前  pid=%s" % (hb.get("mode"), hb.get("ok"), age, hb.get("pid")))
        if hb.get("mode") == "serve" and age > 90:
            print("    ⚠️ 心跳偏旧：serve 可能已退出（wait_events 的微信源会读不到新消息）")
    else:
        print("[6] 网关心跳: 无（还没跑过 serve）")
    chats = state.chats()
    print("[7] 已知会话: %d 个%s" % (len(chats), ("  例: " + ", ".join(list(chats)[:3])) if chats else ""))
    print("[8] 结果: %s" % ("一切正常 ✓" if ok else "存在问题 ✗（见上）"))
    return 0 if ok else 1


def cmd_logout(args: argparse.Namespace, cfg: Dict[str, Any], path: str, state: State) -> int:
    cfg.update({"account_id": "", "token": "", "user_id": "", "default_chat_id": ""})
    save_config(path, cfg)
    removed = []
    for name in ("sync.json", "context_tokens.json", "heartbeat.json", "chats.json", "seq.txt", "seq.json"):
        try:
            os.remove(os.path.join(state.dir, name))
            removed.append(name)
        except OSError:
            pass
    if args.purge:
        # 准备把整个目录发给别人：连收到的消息、媒体、聊天记录一起清掉，不留隐私残留
        try:
            os.remove(state.inbox_path)
            removed.append(os.path.basename(state.inbox_path))
        except OSError:
            pass
        if os.path.isdir(state.media_dir):
            shutil.rmtree(state.media_dir, ignore_errors=True)
            removed.append(os.path.basename(state.media_dir) + "/")
    print("[登出] 已清除本地凭据（微信侧如需解绑，可在微信里删除该机器人会话）")
    if removed:
        print("[登出] 同时移除: %s" % ", ".join(removed))
    if args.purge:
        print("[登出] --purge 已清空收件箱/媒体/状态：这一包现在可以安全发给别人。")
        print("       对方拿到后跑 `python wechat_gateway.py login --new` 扫他自己的码即可。")
        print("       别忘了再删掉自己的截图与产物：shots/* 、work/* 、.state/wait_events_state.json")
    return 0


# ----------------------------- 入口 -----------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="微信网关：发消息 / 收消息 / HTTP API（iLink Bot API）")
    ap.add_argument("--config", default=os.path.join(HERE, CONFIG_NAME), help="配置文件路径")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印协议层细节")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="扫码登录（终端/PNG 二维码）")
    p.add_argument("--timeout", type=int, default=480, help="等待扫码秒数（默认 480）")
    p.add_argument("--new", "--force", dest="force", action="store_true",
                   help="生成全新二维码/全新机器人身份（不沿用旧的）；给别人用时必加")
    p.add_argument("--reset", action="store_true", help="先清空旧凭据再扫码")
    p.add_argument("--qr-png", default="wechat_login_qr.png", help="二维码 PNG 落盘路径")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("send", help="发送文本/图片/文件")
    p.add_argument("--to", default="", help="目标 chat_id（默认用配置里的 default_chat_id）")
    p.add_argument("--text", default="", help="文本内容")
    p.add_argument("--text-file", default="", help="从文件读文本（与 --text 二选一）")
    p.add_argument("--image", action="append", default=[], help="图片路径，可多次指定")
    p.add_argument("--file", action="append", default=[], help="文件路径，可多次指定")
    p.add_argument("--typing", dest="typing", action="store_true", default=None, help="强制显示“正在输入”")
    p.add_argument("--no-typing", dest="typing", action="store_false", help="不显示“正在输入”")
    p.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真的发")
    p.add_argument("--json", action="store_true", help="输出 JSON 结果")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("serve", help="常驻服务：长轮询收信 + HTTP API")
    p.add_argument("--no-poll", action="store_true", help="只起 HTTP，不接收消息")
    p.set_defaults(func=lambda a, c, p_, s: run_server(c, s, with_poll=not a.no_poll))

    p = sub.add_parser("inbox", help="查看收件箱")
    p.add_argument("--since", type=int, default=0, help="只看 seq 大于该值的消息")
    p.add_argument("--limit", type=int, default=20, help="最多显示条数（0=不限）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("chats", help="列出已知会话")
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("typing", help="显示/停止“正在输入”")
    p.add_argument("--to", default="", help="目标 chat_id")
    p.add_argument("--status", choices=["start", "stop"], default="start")
    p.set_defaults(func=cmd_typing)

    p = sub.add_parser("doctor", help="自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("logout", help="清除本地登录态")
    p.add_argument("--purge", action="store_true",
                   help="连收件箱/媒体/状态一起清空（准备把整个目录发给别人时用）")
    p.set_defaults(func=cmd_logout)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config(os.path.abspath(args.config))
    state = State(cfg)
    if args.verbose:
        print("[配置] %s" % json.dumps({k: v for k, v in cfg.items() if k not in ("token",)}, ensure_ascii=False))
    try:
        return int(args.func(args, cfg, os.path.abspath(args.config), state) or 0)
    except IlinkError as exc:
        print("[错误] %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 已停止")
        return 130
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
