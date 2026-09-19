#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_events.py — 阻塞式「等新截图 / 等新微信消息」轮询脚本

学习场景的中枢：用户在前台按下截图热键（screenpeer.py 把整屏存进指定文件夹），
或用户在微信里给机器人发消息（wechat_gateway.py 把消息写进收件箱），本脚本都会
**阻塞**在那里等，一旦有新东西就把名字打印出来；等到「空闲超过 N 秒」再安全退出。

设计要点（都是硬要求，别改语义）::

    1. 打印过的名字永不重复打印 —— 状态文件持久化，跨进程、跨重启、跨会话都算数，
       这样 Agent 的上下文不会被同一张图/同一条消息污染。
    2. 双源一次阻塞 —— 截图目录 + 微信收件箱一起等，谁先来打印谁，
       空闲计时器在任意一路有新事件时重置。Agent 只调一个脚本就能同时监听两路。
    3. 干净退出才「确认」输出；被 kill 掉的那一轮，下次运行会以 REPLAY 重放，
       既不丢事件，也不会把正常情况搞乱。
    4. 没有超时 —— 没内容就永远等（可用 --max-wait 设一个硬上限，默认 0=不限）。

输出格式（stdout，逐行，立即 flush）::

    IMG /abs/path/shots/20260916_120001_123.png
    IMG /abs/path/shots/20260916_115900_777.png REPLAY
    WX {"seq":3,"ts":...,"chat_id":"...","chat_type":"dm","sender":"...","text":"...","media":[...]}

用法::

    python wait_events.py                       # 阻塞等待（默认配置）
    python wait_events.py --once                # 只扫一遍就退出（调试/自测）
    python wait_events.py --interval 2 --idle-timeout 10
    python wait_events.py --sources shots       # 只等截图，不等微信
    python wait_events.py --status              # 看状态，不阻塞
    python wait_events.py --reset               # 清空状态（下次会重放未确认项）
    python wait_events.py --json                # 输出 JSON 行（机器解析）

推送模式（--push，推荐跑法，见下）::

    python wait_events.py --push                # 常驻：有新事件且静默 N 秒后主动唤醒 Agent

两种模式的区别::

    阻塞模式（默认）  打印事件 → 空闲 N 秒 → 自己退出；靠「job 结算通知」去叫醒 Agent，
                      而那条通知受 tool-jobs 的 maxConsecutiveWakes 预算限制（默认 3 次），
                      预算用完后只进收件箱不唤醒 —— 会出现长时间没人接的空窗。
    推送模式（--push）常驻不退出；事件先攒着，**最后一个事件之后安静 N 秒**才 POST 给
                      DSH 的 relay-wake 插件，由它调 Agent.followup() 直接开一个新 turn。
                      持续有新事件就一直不推（攒成一批），没有新事件就什么都不做。
                      推送成功才推进账本；失败保留、稍后重试，最多重复投递，绝不丢。

退出码：0=正常（空闲超时安全退出，或 --once 完成）；2=配置/用法错误；4=被接管/会话结束（静默退出）；
        5=推送连续失败（见 push.fail_limit，主动退出让 job 通知兜底）；130=被中断。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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
CONFIG_NAME = "wait_events.json"
STATE_VERSION = 1

DEFAULT_CONFIG: Dict[str, Any] = {
    "_readme": "wait_events.py 的配置；相对路径都相对于本文件所在目录；支持 // 注释",
    "sources": ["shots", "wechat"],
    "shot_dirs": ["shots"],
    "globs": ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"],
    "poll_interval_sec": 2.0,
    "idle_timeout_sec": 10.0,
    "max_wait_sec": 0,
    "stability_check": True,
    "ignore_suffixes": [".part", ".tmp", ".crdownload", ".download", ".partial", ".!ut"],
    "ignore_prefixes": [".", "~$", "._"],
    "state_file": ".state/wait_events_state.json",
    "exclusive_owner": True,       # 同一时刻只允许一个等待脚本（新会话接管旧的，防串会话/防抢事件）
    "owner_file": "",              # 独占登记表；留空 = 与 state_file 同目录同名 + .owner.json
    "owner_memory_sec": 604800,    # 「谁接管过谁」的记忆保留多久（7 天）；真正生效的门槛是
                                   # 「接管者会话是否还在线」，不是时间
    "replay_unacked": True,
    "max_seen": 4000,
    "shots_cold_start_lookback_sec": 0,
    "wechat": {
        "inbox_file": "wechat_inbox.jsonl",
        "gateway_heartbeat": ".state/wechat/heartbeat.json",
        "require_gateway_fresh_sec": 0,
        "cold_start_lookback_sec": 300,
        "max_batch": 20,
    },
    "output": {"format": "lines", "wechat_show_meta": False, "banner": True},
    "push": {
        "_readme": "推送模式：把新事件主动推给 DSH 的 relay-wake 插件，由它唤醒空闲会话",
        "enabled": False,                  # true = 常驻推送模式（不再空闲退出）；也可用 --push
        "url": "http://127.0.0.1:3080/relay/wake",
        "token": "",                       # 与插件行里的 config.token 一致
        "session": "",                     # 唤醒目标会话；留空 = --owner-session / DSH_SESSION_ID
        "debounce_sec": 8.0,               # 最后一个事件之后安静这么久才推送（用户语义：静默去抖）
        "max_hold_sec": 0,                 # >0 = 即使持续有新事件，攒够这么久也推一次（0=一直不推）
        "max_text_chars": 8000,            # 单条推送正文上限，超出截断并标注
        "request_timeout_sec": 10.0,
        "retry_log_sec": 30.0,             # 连续失败时的日志限频
        "fail_limit": 6,                   # 连续推送失败这么多次就主动退出（0=永不死等），
                                           # 让 job 结算通知去叫醒 Agent —— 否则插件挂了就是黑洞
    },
}


# ----------------------------- 配置 -----------------------------

def strip_jsonc(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
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


def load_config(path: str, create: bool = True) -> Dict[str, Any]:
    if not os.path.exists(path):
        if not create:
            raise SystemExit("配置不存在: %s" % path)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        print("[配置] 已生成默认配置: %s" % path, file=sys.stderr)
    with open(path, "r", encoding="utf-8") as f:
        return deep_merge(DEFAULT_CONFIG, json.loads(strip_jsonc(f.read()) or "{}"))


def abspath(value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(HERE, value)


# ----------------------------- 状态 -----------------------------

class State:
    """跨运行持久化的「已打印/已确认」账本

    shots:  seen      {文件名: mtime}         —— 见过就不再打印
            unacked   [文件名]                —— 打印过但那一轮没干净退出
    wechat: acked_offset / printed_offset / read_offset —— 字节游标
            acked   = 已确认送达 Agent；printed = 至少打印过一次；
            kill 掉的那一轮里 acked < printed，于是重启后 [acked, printed) 这段以 REPLAY 补打。
            seen_ids  []                      —— 去重（iLink 会重复投递同一条）
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Any] = {
            "version": STATE_VERSION,
            "seen": {},
            "unacked_shots": [],
            "wechat_acked_offset": 0,      # 已确认送达 Agent 的位置
            "wechat_printed_offset": 0,    # 至少打印过一次的位置（被 kill 时用它判定 REPLAY）
            "wechat_seen_ids": [],
            "runs": 0,
            "last_run": {},
        }
        self.fresh = True
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and int(data.get("version", 0)) == STATE_VERSION:
                self.data.update(data)
                self.fresh = False
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)      # 原子替换：kill 也不会写坏账本
        except OSError as exc:              # Windows 上目标被占用会失败；账本丢一轮不致命
            try:
                os.remove(tmp)
            except OSError:
                pass
            print("[警告] 账本写入失败: %s" % exc, file=sys.stderr)

    # -- shots --
    def is_seen(self, name: str) -> bool:
        return name in self.data["seen"]

    def mark_seen(self, name: str, mtime: float) -> None:
        self.data["seen"][name] = round(float(mtime), 3)
        limit = int(self.data.get("_max_seen") or 4000)
        seen = self.data["seen"]
        if len(seen) > limit:                # 按时间淘汰最老的，防止无限膨胀
            for key, _ in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - limit]:
                seen.pop(key, None)

    def add_unacked(self, name: str) -> None:
        if name not in self.data["unacked_shots"]:
            self.data["unacked_shots"].append(name)

    def clear_unacked(self) -> None:
        self.data["unacked_shots"] = []

    # -- wechat --
    def seen_id(self, msg_id: str) -> bool:
        return bool(msg_id) and msg_id in self.data["wechat_seen_ids"]

    def mark_id(self, msg_id: str) -> None:
        if msg_id and msg_id not in self.data["wechat_seen_ids"]:
            self.data["wechat_seen_ids"].append(msg_id)
            if len(self.data["wechat_seen_ids"]) > 2000:
                self.data["wechat_seen_ids"] = self.data["wechat_seen_ids"][-2000:]


# ----------------------------- 独占登记（防串会话） -----------------------------
# 背景：DSH 里「终结一个对话」不会杀掉它的后台任务——会话仍驻留、任务仍在跑。
# 于是旧会话的等待脚本收到截图后会打印，把旧会话重新唤醒，还会把该给新会话的事件
# 抢走（打印即入账）：新会话永远看不到那张图。这里用一张独占登记表解决：
#   * 新会话启动即接管；被接管的旧脚本在下一轮**静默退出**（不打印事件、不改账本）；
#   * 只要接管者还在任，被接管者就不能再抢回来（避免两个会话来回抢）。

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=5, errors="replace").stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError, TypeError):
            return True                      # 查不到就当活着，避免误抢
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def find_session_lock(session_id: str) -> Optional[str]:
    """定位 ~/.dsh/sessions/<项目目录>/<session_id>/session.lock；找不到返回 None"""
    if not session_id:
        return None
    root = os.path.expanduser(os.environ.get("DSH_HOME") or "~/.dsh")
    sessions = os.path.join(root, "sessions")
    try:
        for entry in os.listdir(sessions):
            candidate = os.path.join(sessions, entry, session_id, "session.lock")
            if os.path.exists(candidate):
                return candidate
    except OSError:
        return None
    return None


_ARCHIVE_CACHE: Dict[str, Any] = {}


def session_archived(session_id: str) -> Optional[bool]:
    """本会话是否已在 DSH 里被「归档」（= GUI 里终结那个对话）。

    True=已归档（该静默退出了）；False=没归档；None=判断不了（非 DSH 环境/读不到）。
    只读 $DSH_HOME/storages/workspace.json 的 global.archivedSessionIds，不干扰 DSH。
    有了这个，即使新会话还没起等待脚本，旧脚本也会在归档后 ≤1 个轮询周期内自己退场。
    """
    if not session_id:
        return None
    home = os.path.expanduser(os.environ.get("DSH_HOME") or "~/.dsh")
    path = os.path.join(home, "storages", "workspace.json")
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if _ARCHIVE_CACHE.get("key") == key:
        ids = _ARCHIVE_CACHE.get("ids") or set()
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        ids = set((data.get("global") or {}).get("archivedSessionIds") or [])
        _ARCHIVE_CACHE["key"] = key
        _ARCHIVE_CACHE["ids"] = ids
    return session_id in ids


def session_ended(lock_path: Optional[str]) -> Optional[bool]:
    """会话是否已经真正结束。True=结束（锁空闲），False=还活着，None=判断不了。

    用 flock 探针：活着的会话会持有 session.lock，句柄一释放锁就空了。
    探针只短暂加锁后立刻放开，不会影响 DSH 自己加锁。
    """
    if not lock_path:
        return None
    try:
        import fcntl
    except ImportError:                      # Windows 没有 flock → 不做这项判断
        return None
    if not os.path.exists(lock_path):
        return True
    try:
        # 必须是 O_RDONLY：flock(2) 不要求写权限，而 O_RDWR 在「文件自身只读」或
        # 「进程被文件沙箱限制写工作区外路径」时会直接 EPERM —— 探针于是永远返回 None，
        # 等于把独占判定废掉：被接管过的旧会话能把事件流抢回去。
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)       # 立刻释放，绝不占用
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class OwnerGuard:
    def __init__(self, cfg: Dict[str, Any], session_id: str, enabled: bool = True) -> None:
        self.cfg = cfg
        self.enabled = bool(enabled)
        self.session = session_id or ""
        self.pid = os.getpid()
        base = str(cfg.get("owner_file") or "")
        if base:
            self.path = abspath(base)
        else:
            stem, _ext = os.path.splitext(abspath(str(cfg.get("state_file") or ".state/wait_events_state.json")))
            self.path = stem + ".owner.json"
        self.lock_path = find_session_lock(self.session)
        self.reason = ""

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _evictor_alive(self, session_id: str) -> bool:
        """接管者会话是否还在线（只有明确「在线」才算；已归档/已释放/判断不了都算不在线）"""
        if not session_id:
            return False
        if session_archived(session_id) is True:
            return False
        return session_ended(find_session_lock(session_id)) is False

    def _mine(self, owner: Dict[str, Any]) -> bool:
        return bool(owner) and int(owner.get("pid") or 0) == self.pid

    def claim(self, force: bool = False) -> int:
        """尝试成为唯一 owner：0=可以跑；3=不该跑（已归档 / 同会话重复 / 已被接管过 / 还在交棒保留期）"""
        if not self.enabled:
            return 0
        if session_archived(self.session) is True:
            self.reason = "本会话已被归档（在 GUI 里终结的对话），不再监听"
            return 3
        reg = self._read()
        cur = reg.get("owner") or {}
        evicted = dict(reg.get("evicted") or {})
        now = time.time()
        if not cur and not force:
            # 没有活着的 owner（上一任已退出）：检查我是否曾被某个**仍然在线**的会话接管过。
            # 是的话就不许抢回来 —— 与时间无关，只看接管者还在不在。
            ev = evicted.get(self.session) or {}
            by = str(ev.get("by") or "")
            if by and self._evictor_alive(by):
                self.reason = ("本会话此前已被 %s 接管，而它仍然在线；"
                               "确需本会话接管请加 --force-owner 重跑" % by)
                return 3
        if cur and not self._mine(cur):
            cur_pid = int(cur.get("pid") or 0)
            same_session = bool(self.session) and cur.get("session_id") == self.session
            if pid_alive(cur_pid):
                if same_session:
                    self.reason = "本会话已经有一个等待脚本在跑（pid %d）" % cur_pid
                    return 3
                ev = evicted.get(self.session) or {}
                if self.session and ev.get("by") == cur.get("session_id"):
                    self.reason = ("本会话已被 %s（pid %d）接管，不再监听"
                                   % (cur.get("session_id") or "另一个会话", cur_pid))
                    return 3
                if self.session:
                    evicted[cur.get("session_id") or ("pid:%d" % cur_pid)] = {
                        "by": self.session, "ts": round(now, 3)}
        memory = float(self.cfg.get("owner_memory_sec", 604800) or 0)
        evicted = {k: v for k, v in evicted.items()
                   if memory <= 0 or now - float((v or {}).get("ts") or 0) < memory}
        reg["owner"] = {"session_id": self.session, "pid": self.pid, "started_ts": round(now, 3)}
        reg["evicted"] = evicted
        self._write(reg)
        return 0

    def still_owner(self) -> bool:
        """每轮打印前调用。False = 该静默退出了（别的会话接管了，或本会话已结束）"""
        if not self.enabled:
            return True
        if session_archived(self.session) is True:
            self.reason = "本会话已被归档（在 GUI 里终结的对话）"
            return False
        if session_ended(self.lock_path) is True:
            self.reason = "本会话已结束（session.lock 已释放）"
            return False
        reg = self._read()
        cur = reg.get("owner") or {}
        if self._mine(cur):
            return True
        if cur:
            self.reason = "事件流已被 %s（pid %s）接管" % (cur.get("session_id") or "另一个会话", cur.get("pid"))
            return False
        # 槽位是空的：可能只是接管者来了又走（例如对方跑了一次 --once）。
        # 用「驱逐记忆」判断：确实有在线的会话接管过我 → 退场；否则我仍是 owner，把登记补回去。
        ev = (reg.get("evicted") or {}).get(self.session) or {}
        by = str(ev.get("by") or "")
        if by and self._evictor_alive(by):
            self.reason = "事件流已被 %s 接管" % by
            return False
        if self.enabled:
            reg["owner"] = {"session_id": self.session, "pid": self.pid, "started_ts": round(time.time(), 3)}
            self._write(reg)
        return True

    def release(self) -> None:
        """干净退出：释放所有权（但「谁接管过谁」的记忆留在 evicted 里，
        所以被接管过的旧会话即使趁空窗重启脚本也抢不回来）。"""
        if not self.enabled:
            return
        reg = self._read()
        if self._mine(reg.get("owner") or {}):
            reg["owner"] = {}
            self._write(reg)


# ----------------------------- 事件 -----------------------------

class Event:
    __slots__ = ("kind", "path", "name", "record", "replay", "hint")

    def __init__(self, kind: str, path: str = "", name: str = "", record: Optional[Dict[str, Any]] = None,
                 replay: bool = False, hint: str = "") -> None:
        self.kind = kind          # "IMG" | "WX"
        self.path = path
        self.name = name
        self.record = record
        self.replay = replay
        self.hint = hint          # 仅非默认会话时填 "[chat=…]"，其余情况为空

    def to_line(self, wechat_meta: bool = False) -> str:
        suffix = " REPLAY" if self.replay else ""
        if self.kind == "IMG":
            return "IMG %s%s" % (self.path, suffix)
        rec = self.record or {}
        if wechat_meta:                        # 调试用：完整 JSON
            return "WX %s%s" % (json.dumps(rec, ensure_ascii=False, separators=(",", ":")), suffix)
        # 默认：只给「用户说了什么」+ 附件本地路径；不带时间/msg_id/sender 等噪音
        line = " ".join(str(rec.get("text") or "").split())
        atts = []
        for media in rec.get("media") or []:
            kind = {"image": "图片", "file": "文件", "voice": "语音", "video": "视频"}.get(media.get("kind"), "附件")
            loc = media.get("path") or media.get("error") or ""
            atts.append("[%s] %s" % (kind, loc))
        if atts:
            line = (line + " " if line else "") + " ".join(atts)
        if self.hint:
            line = (line + " " if line else "") + self.hint
        return "WX %s%s" % (line.strip(), suffix)

    def to_json(self) -> str:
        obj: Dict[str, Any] = {"kind": self.kind, "replay": self.replay}
        if self.kind == "IMG":
            obj.update({"path": self.path, "name": self.name})
        else:
            obj.update(self.record or {})
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ----------------------------- 截图源 -----------------------------

class ShotSource:
    def __init__(self, cfg: Dict[str, Any], state: State) -> None:
        self.cfg = cfg
        self.state = state
        self.dirs = [abspath(d) for d in cfg.get("shot_dirs") or []]
        self.globs = list(cfg.get("globs") or ["*.png"])
        self.ignore_suffixes = tuple(str(s).lower() for s in (cfg.get("ignore_suffixes") or []))
        self.ignore_prefixes = tuple(str(s) for s in (cfg.get("ignore_prefixes") or []))
        self.stability = bool(cfg.get("stability_check", True))
        self.lookback = float(cfg.get("shots_cold_start_lookback_sec", 0) or 0)
        self._prev: Dict[str, Tuple[int, int]] = {}
        for directory in self.dirs:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                print("[警告] 截图目录不可用: %s (%s)" % (directory, exc), file=sys.stderr)

    def baseline(self) -> int:
        """冷启动：目录里已有的文件记为「已见过」，避免把历史截图灌进 Agent 上下文。

        lookback > 0 时，最近 lookback 秒内落盘的图不算旧图（留给本轮上报），
        用于「先按了截图、再让 Agent 起脚本」的场景。
        """
        if not self.state.fresh:
            return 0
        cutoff = time.time() - self.lookback
        kept = 0
        for directory in self.dirs:
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not self._match(name):
                    continue
                path = os.path.join(directory, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                if self.lookback > 0 and st.st_mtime >= cutoff:
                    kept += 1                      # 新鲜：交给主循环上报
                    continue
                self.state.mark_seen(name, st.st_mtime)
        return kept

    def _match(self, name: str) -> bool:
        if name.startswith(self.ignore_prefixes):
            return False
        low = name.lower()
        if low.endswith(self.ignore_suffixes):
            return False
        return any(fnmatch.fnmatch(low, g.lower()) for g in self.globs)

    def scan(self) -> List[Tuple[str, str, float]]:
        """返回本次可上报的 (目录, 文件名, mtime)；顺带维护稳定性检查的上一轮快照"""
        ready: List[Tuple[str, str, float]] = []
        current: Dict[str, Tuple[int, int]] = {}
        for directory in self.dirs:
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for name in entries:
                path = os.path.join(directory, name)
                if not self._match(name) or not os.path.isfile(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = path
                sig = (st.st_size, st.st_mtime_ns)
                current[key] = sig
                if st.st_size <= 0 or self.state.is_seen(name):
                    continue
                if self.stability and self._prev.get(key) != sig:
                    continue                     # 下一轮再看：避免读到半截文件
                ready.append((directory, name, st.st_mtime))
        self._prev = current
        ready.sort(key=lambda item: item[2])     # 按修改时间，先来的先处理
        return ready


# ----------------------------- 微信源 -----------------------------

class WechatSource:
    def __init__(self, cfg: Dict[str, Any], state: State) -> None:
        wcfg = cfg.get("wechat") or {}
        self.cfg = wcfg                       # 注意：self.cfg 只是 wechat 子配置
        self.config_dir = str(cfg.get("_config_dir") or os.path.dirname(os.path.abspath(__file__)))
        self.state = state
        self.inbox = abspath(str(wcfg.get("inbox_file") or "wechat_inbox.jsonl"))
        self.heartbeat_path = abspath(str(wcfg.get("gateway_heartbeat") or ".state/wechat/heartbeat.json"))
        self.max_batch = int(wcfg.get("max_batch", 20))
        self.default_chat = self._default_chat()
        self.require_fresh = float(wcfg.get("require_gateway_fresh_sec", 0) or 0)
        self.read_offset = 0
        self.printed_offset = 0
        self.available = os.path.exists(self.inbox)
        self.warning = ""
        self._prepare()

    def _default_chat(self) -> str:
        """读 wechat_gateway.json 的默认收件人：只有「非默认会话」的消息才需要提示 chat_id"""
        try:
            gw = os.path.join(self.config_dir, "wechat_gateway.json")
            with open(gw, "r", encoding="utf-8") as f:
                data = json.loads(strip_jsonc(f.read()) or "{}")
            return str(data.get("default_chat_id") or data.get("user_id") or "")
        except (OSError, ValueError):
            return ""

    # -- 启动时定位游标 --
    def _prepare(self) -> None:
        acked = int(self.state.data.get("wechat_acked_offset", 0) or 0)
        try:
            size = os.path.getsize(self.inbox) if self.available else 0
        except OSError:
            size = 0
        printed = int(self.state.data.get("wechat_printed_offset", acked) or 0)
        if size < acked:                          # 收件箱被轮转/清空过
            acked = 0
        if size < printed:
            printed = 0
        if self.state.fresh:
            lookback = float(self.cfg.get("cold_start_lookback_sec", 300) or 0)
            acked = printed = self._offset_for_lookback(size, lookback)
        self.read_offset = acked                   # 从「上次确认的位置」开始
        self.printed_offset = max(printed, acked)  # 到「至少打印过」的位置为止都算重放
        self.state.data["wechat_acked_offset"] = acked
        self.state.data["wechat_printed_offset"] = self.printed_offset
        self.state.data["wechat_read_offset"] = self.read_offset
        age = self.heartbeat_age()
        if self.available and age is not None:
            self.available = True
            if age > 90 and self.require_fresh:
                self.available = False
                self.warning = "网关心跳已 %.0fs 没更新，微信源暂停（换个 --sources shots 或重启 serve）" % age
        elif not self.available:
            self.warning = "收件箱文件还不存在（wechat_gateway.py serve 跑起来后才有）"

    def _offset_for_lookback(self, size: int, lookback_sec: float) -> int:
        """冷启动：只有最近 lookback 秒内的消息才算「新」，更早的历史消息静默跳过"""
        if lookback_sec <= 0 or size <= 0:
            return size
        cutoff = time.time() - lookback_sec
        offset = size
        try:
            with open(self.inbox, "r", encoding="utf-8") as f:
                pos = 0
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        pos += len(line.encode("utf-8"))
                        continue
                    if float(rec.get("ts") or 0) >= cutoff:
                        offset = pos
                        break
                    pos += len(line.encode("utf-8"))
        except OSError:
            return size
        return offset

    def heartbeat_age(self) -> Optional[float]:
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                hb = json.load(f)
            return max(0.0, time.time() - float(hb.get("ts") or 0))
        except (OSError, ValueError):
            return None

    def gateway_running(self) -> bool:
        age = self.heartbeat_age()
        return age is not None and age <= 90

    # -- 读取新消息 --
    def poll(self) -> List[Tuple[Dict[str, Any], bool]]:
        """返回 [(记录, 是否重放)]；同时把 read_offset 推进到已打印的位置（内存）"""
        if not self.available:
            return []
        try:
            size = os.path.getsize(self.inbox)
        except OSError:
            return []
        if size < self.read_offset:
            self.read_offset = 0
        if size == self.read_offset:
            return []
        out: List[Tuple[Dict[str, Any], bool]] = []
        try:
            with open(self.inbox, "r", encoding="utf-8") as f:
                f.seek(self.read_offset)
                pos = self.read_offset
                for line in f:
                    pos += len(line.encode("utf-8"))
                    raw = line.strip()
                    if not raw:
                        self.read_offset = pos
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        self.read_offset = pos
                        continue
                    # 这一行在「至少打印过一次」的位置之前 = 上一轮打印了但没确认 → 重放
                    replay = pos <= self.printed_offset
                    msg_id = str(rec.get("msg_id") or "")
                    if not replay and self.state.seen_id(msg_id):
                        self.read_offset = pos
                        continue
                    out.append((rec, replay))
                    self.read_offset = pos
                    if pos > self.printed_offset:            # 打印过就记账（被 kill 也不丢）
                        self.printed_offset = pos
                        self.state.data["wechat_printed_offset"] = pos
                    if len(out) >= self.max_batch:
                        break
        except OSError:
            return []
        return out


# ----------------------------- 推送唤醒 -----------------------------

class Waker:
    """把一批事件主动推给 DSH 的 relay-wake 插件，由它唤醒空闲会话。

    语义（用户定的，别改）：**静默去抖** —— 事件先攒着，最后一个事件之后安静
    `debounce_sec` 秒才推送；期间持续有新事件就一直不推（继续攒成一批）。
    推送成功才推进账本；失败保留待重试 —— 最多重复投递，绝不丢。
    """

    def __init__(self, cfg: Dict[str, Any], session_id: str, wechat_meta: bool = False) -> None:
        self.requested = bool(cfg.get("enabled"))
        self.url = str(cfg.get("url") or "").strip()
        self.token = str(cfg.get("token") or "")
        self.session = str(cfg.get("session") or session_id or "").strip()
        self.debounce = max(0.0, float(cfg.get("debounce_sec", 8.0) or 0.0))
        self.max_hold = max(0.0, float(cfg.get("max_hold_sec", 0) or 0.0))
        self.max_text = max(200, int(cfg.get("max_text_chars", 8000) or 8000))
        self.timeout = max(1.0, float(cfg.get("request_timeout_sec", 10.0) or 10.0))
        self.retry_log_sec = max(1.0, float(cfg.get("retry_log_sec", 30.0) or 30.0))
        self.fail_limit = max(0, int(cfg.get("fail_limit", 6) or 0))
        self.wechat_meta = bool(wechat_meta)
        self.blocker = ""
        if self.requested and not self.url:
            self.blocker = "缺 push.url"
        elif self.requested and not self.session:
            self.blocker = "缺会话 id（push.session / --owner-session / DSH_SESSION_ID 都没有）"

    @property
    def enabled(self) -> bool:
        return self.requested and not self.blocker

    def render(self, events: List[Event]) -> str:
        img = sum(1 for e in events if e.kind == "IMG")
        wx = sum(1 for e in events if e.kind == "WX")
        lines = ["[relay] %d 个新事件（截图 %d / 微信 %d）" % (len(events), img, wx)]
        lines.extend(event.to_line(self.wechat_meta) for event in events)
        text = "\n".join(lines)
        if len(text) > self.max_text:
            text = text[: self.max_text] + "\n…[清单过长已截断，完整内容见本脚本日志]"
        return text

    def push(self, events: List[Event]) -> Tuple[bool, str]:
        """推送一批事件；返回 (是否成功, 说明)。"""
        if not self.enabled:
            return False, self.blocker or "推送未启用"
        body = json.dumps({
            "session": self.session,
            "text": self.render(events),
            "summary": "relay：%d 个新事件" % len(events),
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json; charset=utf-8")
        if self.token:
            req.add_header("x-relay-token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            return False, "HTTP %s %s" % (exc.code, detail.strip())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, "连接失败 %r" % (exc,)
        try:
            data = json.loads(payload) if payload.strip() else {}
        except ValueError:
            return False, "响应不是合法 JSON: %s" % payload.strip()[:200]
        if not data.get("ok"):
            return False, "插件拒绝: %s" % payload.strip()[:200]
        return True, str(data.get("status") or "")


# ----------------------------- 主循环 -----------------------------

def render(cfg: Dict[str, Any], shots: ShotSource, wechat: WechatSource, state: State,
           interval: float, idle: float, max_wait: float, sources: List[str],
           guard: "OwnerGuard", waker: Optional[Waker] = None) -> None:
    if not (cfg.get("output") or {}).get("banner", True):
        return
    push = waker is not None and waker.enabled
    print("=== wait_events v%s ===" % VERSION)
    if "shots" in sources:
        print("截图源: %s" % ", ".join(shots.dirs))
    if "wechat" in sources:
        age = wechat.heartbeat_age()
        status = "网关未运行" if age is None else ("网关心跳 %.0fs 前 ✓" % age if age <= 90 else "网关心跳 %.0fs 前 ⚠" % age)
        print("微信源: %s  (%s)" % (wechat.inbox, status))
        if wechat.warning:
            print("        ⚠ %s" % wechat.warning)
    if push:
        print("模式: 常驻推送")
        hold = "攒够 %.0fs 也推一次" % waker.max_hold if waker.max_hold > 0 else "持续有新事件就一直不推"
        print("推送: %s → 会话 %s" % (waker.url, waker.session))
        print("      静默去抖 %.1fs（最后一个事件之后安静这么久才推；%s）" % (waker.debounce, hold))
    elif waker is not None and waker.requested:
        print("模式: 阻塞（推送未启用：%s）" % (waker.blocker or "未知原因"))
    tail = "  最长等待 %.0fs" % max_wait if max_wait > 0 else ""     # 默认不限，不占行
    if push:
        # 推送模式没有「空闲退出」这回事，别再打印它
        print("轮询 %.1fs  状态文件 %s%s" % (interval, state.path, tail))
    else:
        print("轮询 %.1fs  空闲退出 %s  状态文件 %s%s"
              % (interval, ("%.1fs" % idle) if idle > 0 else "关闭（零事件时一直等下去）", state.path, tail))
    print("独占: %s pid %d" % (guard.session or "非 DSH 环境", guard.pid))
    if push:
        print("等待新事件…（本脚本常驻：有新事件会主动唤醒你；不用守着它，也不要重启）", flush=True)
    else:
        print("等待新事件…（Agent 请保持阻塞，不要设超时）", flush=True)


def run(args: argparse.Namespace) -> int:
    cfg = load_config(os.path.abspath(args.config))
    cfg["_config_dir"] = os.path.dirname(os.path.abspath(args.config))
    for key, value in (("poll_interval_sec", args.interval), ("idle_timeout_sec", args.idle_timeout),
                       ("max_wait_sec", args.max_wait), ("stability_check", args.stability)):
        if value is not None:
            cfg[key] = value
    if args.dir:
        cfg["shot_dirs"] = list(args.dir)
    if args.replay is not None:
        cfg["replay_unacked"] = args.replay
    if args.format:
        cfg.setdefault("output", {})["format"] = args.format
    if args.wechat_meta is not None:
        cfg.setdefault("output", {})["wechat_show_meta"] = args.wechat_meta
    if args.exclusive is not None:
        cfg["exclusive_owner"] = args.exclusive
    push_cfg = dict(cfg.get("push") or {})
    for key, value in (("enabled", args.push), ("url", args.push_url), ("token", args.push_token),
                       ("session", args.push_session), ("debounce_sec", args.debounce),
                       ("max_hold_sec", args.push_max_hold)):
        if value is not None:
            push_cfg[key] = value
    cfg["push"] = push_cfg
    sources = [s for s in (args.sources.split(",") if args.sources else cfg.get("sources") or []) if s]
    if not sources:
        print("[错误] 没有可用的监听源（sources 为空）", file=sys.stderr)
        return 2

    session_id = args.owner_session or os.environ.get("DSH_SESSION_ID") or ""
    state = State(abspath(str(cfg.get("state_file") or ".state/wait_events_state.json")))
    state.data["_max_seen"] = int(cfg.get("max_seen", 4000))

    guard = OwnerGuard(cfg, session_id=session_id,
                       enabled=bool(cfg.get("exclusive_owner", True)))
    claim_rc = guard.claim(force=bool(getattr(args, "force_owner", False)))
    if claim_rc:
        print("--- 不启动：%s —— 本会话不再监听（别重启本脚本，事件不会丢，交给当前会话即可）---"
              % guard.reason, flush=True)
        return claim_rc

    if args.reset:
        state.data["seen"] = {}
        state.data["unacked_shots"] = []
        state.data["wechat_seen_ids"] = []
        state.save()
        print("[状态] 已清空 %s（已打印过的名字会被重新视为新事件）" % state.path)
        return 0

    shots = ShotSource(cfg, state) if "shots" in sources else None
    wechat = WechatSource(cfg, state) if "wechat" in sources else None
    if shots is not None and not args.status:
        kept = shots.baseline()                    # 冷启动基线（旧图静默忽略）
        if kept and not args.quiet:
            print("[基线] 目录里有 %d 个最近落盘的图，算新事件上报" % kept, file=sys.stderr)

    fmt = (cfg.get("output") or {}).get("format", "lines")
    wmeta = bool((cfg.get("output") or {}).get("wechat_show_meta", False))
    emit = (lambda ev: print(ev.to_json() if fmt == "json" else ev.to_line(wmeta), flush=True))
    waker = Waker(push_cfg, session_id, wmeta)
    push_mode = waker.enabled
    if push_mode and args.once:
        waker.debounce = 0.0                       # --once 是自测：扫完就推，不等去抖

    if args.status:
        print("=== wait_events 状态 ===")
        print("状态文件: %s（%s）" % (state.path, "首次运行前" if state.fresh else "已存在"))
        print("记录过 %d 个文件名，未确认 %d 个" % (len(state.data["seen"]), len(state.data["unacked_shots"])))
        if shots:
            pending = [n for n in sorted(os.listdir(shots.dirs[0]))] if os.path.isdir(shots.dirs[0]) else []
            print("截图目录: %s（现有 %d 个文件）" % (", ".join(shots.dirs), len(pending)))
        if wechat:
            print("微信收件箱: %s（游标 acked=%s read=%s）"
                  % (wechat.inbox, state.data.get("wechat_acked_offset"), state.data.get("wechat_read_offset")))
        print("推送: %s  会话 %s  静默 %.1fs  已推 %s 批  最近一次 %s"
              % (("启用 " + waker.url) if waker.enabled else ("未启用（%s）" % (waker.blocker or "配置里 enabled=false")),
                 waker.session or "-", waker.debounce, state.data.get("pushes", 0),
                 json.dumps(state.data.get("last_push") or {}, ensure_ascii=False)))
        print("最近一次运行: %s" % json.dumps(state.data.get("last_run") or {}, ensure_ascii=False))
        return 0

    interval = max(0.2, float(cfg["poll_interval_sec"]))
    idle = float(cfg["idle_timeout_sec"])
    if push_mode:
        idle = 0.0                                 # 常驻：永不因空闲退出（推送才是「结束」）
    max_wait = float(cfg["max_wait_sec"])
    started_at = time.monotonic()
    last_event = started_at
    # 关键语义：**一个事件都没打印过时，永不因空闲退出**（一直阻塞等第一个事件）；
    # 打印过之后，才开始「空闲 N 秒 → 安全退出」的计时。
    armed = False
    printed_this_run: List[Event] = []
    stop = {"flag": False, "reason": "signal"}

    def on_signal(signum, _frame):
        stop["flag"] = True
        stop["reason"] = "signal"

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError, AttributeError):
            pass

    if not args.once:
        render(cfg, shots, wechat, state, interval, idle, max_wait, sources, guard, waker)

    # ---- 推送批次（推送成功才清空；push_mode 之外一直是空的） ----
    pending: List[Event] = []
    pending_since: Optional[float] = None
    next_retry_at = 0.0
    last_push_error_at = 0.0
    fail_streak = 0
    pushes = 0

    # ---- 启动时重放：上一轮打印了但没干净退出的事件 ----
    # 截图源在这里补打；微信源不在这里轮询（否则会把「新消息」误当已消费吞掉），
    # 它的未确认项由主循环第一轮的 poll() 自动带上 replay=True 打印出来。
    if shots and cfg.get("replay_unacked"):
        for name in list(state.data["unacked_shots"]):
            path = next((os.path.join(d, name) for d in shots.dirs if os.path.exists(os.path.join(d, name))), "")
            event = Event("IMG", path=path or name, name=name, replay=True)
            print(event.to_line(), flush=True)
            printed_this_run.append(event)
            if push_mode:
                pending.append(event)
    if printed_this_run:
        last_event = time.monotonic()
        armed = True                           # 重放也是「打印过」，同样启动空闲计时
    if pending:
        pending_since = last_event

    # ---- 主循环 ----
    while True:
        if stop["flag"]:
            print("--- 收到中断信号：本轮输出未确认，下次运行会以 REPLAY 重放 ---", flush=True)
            return 130
        if not guard.still_owner():                    # 必须在扫描/打印之前判断
            print("--- %s：静默退出（本轮不打印事件、不改账本）---" % guard.reason, flush=True)
            return 4
        new_count = 0
        before = len(printed_this_run)                 # 这一轮新打印的事件从 before 开始
        if shots:
            for _directory, name, mtime in shots.scan():
                event = Event("IMG", path=os.path.join(_directory, name), name=name)
                emit(event)
                printed_this_run.append(event)
                state.mark_seen(name, mtime)
                state.add_unacked(name)
                new_count += 1
        if wechat:
            for rec, replay in wechat.poll():
                cid = str(rec.get("chat_id") or "")
                hint = "[chat=%s]" % cid if (wechat.default_chat and cid and cid != wechat.default_chat) else ""
                event = Event("WX", record=rec, replay=replay, hint=hint)
                emit(event)
                printed_this_run.append(event)
                state.mark_id(str(rec.get("msg_id") or ""))
                new_count += 1
        if new_count:
            last_event = time.monotonic()
            armed = True                       # 有输出 → 从此开始算空闲
            state.save()                       # 打印即落盘：被 kill 也不会重复打印
            if push_mode:                      # 攒进批次：等「静默 N 秒」再推
                if not pending:
                    pending_since = last_event
                pending.extend(printed_this_run[before:])

        # ---- 推送：静默去抖（最后一个事件之后安静 debounce 秒才推）----
        # 持续有新事件 → last_event 一直被刷新 → 永远不满足 quiet，于是「一直不推」。
        if push_mode and pending:
            now = time.monotonic()
            quiet = (now - last_event) >= waker.debounce
            held = (now - pending_since) if pending_since is not None else 0.0
            due = quiet and (waker.max_hold <= 0 or held >= waker.max_hold)
            if due and now >= next_retry_at:
                batch = list(pending)
                ok, info = waker.push(batch)
                if ok:
                    pending = []
                    pending_since = None
                    next_retry_at = 0.0
                    fail_streak = 0
                    pushes += 1
                    state.clear_unacked()                      # 送达才确认
                    if wechat:
                        state.data["wechat_acked_offset"] = wechat.printed_offset
                    state.data["pushes"] = int(state.data.get("pushes", 0)) + 1
                    state.data["last_push"] = {
                        "ts": round(time.time(), 3),
                        "count": len(batch),
                        "img": sum(1 for e in batch if e.kind == "IMG"),
                        "wx": sum(1 for e in batch if e.kind == "WX"),
                        "status": info,
                    }
                    state.save()
                    print("--- 已推送 %d 条事件 → 会话 %s（agent status=%s）---"
                          % (len(batch), waker.session, info or "?"), flush=True)
                else:
                    fail_streak += 1
                    if now - last_push_error_at >= waker.retry_log_sec:   # 失败限频播报
                        last_push_error_at = now
                        print("--- 推送失败（%s）：%d 条事件保留在批次里，稍后重试 ---"
                              % (info, len(pending)), file=sys.stderr, flush=True)
                    next_retry_at = now + (60.0 if "429" in info else 5.0)
                    if waker.fail_limit > 0 and fail_streak >= waker.fail_limit:
                        # 插件挂了/配置错了：一直常驻就等于「谁也不叫醒 Agent」的黑洞。
                        # 主动退出让 job 结算通知顶上（预算够的话会把 Agent 叫醒来看这条错误）。
                        print("--- 推送连续失败 %d 次（最后一次：%s）：本脚本主动退出，"
                              "改由 job 结算通知叫醒 Agent 处理；%d 条事件留在账本里，下次运行补推 ---"
                              % (fail_streak, info, len(pending)), file=sys.stderr, flush=True)
                        return 5

        if args.once:                          # 自测：扫完（并推完）就退
            break

        if max_wait > 0 and (time.monotonic() - started_at) >= max_wait:
            print("--- 到达最长等待 %.0fs（硬上限），退出 ---" % max_wait, flush=True)
            break
        # armed 之前不判空闲：没等到任何事件就一直等下去（这是设计，不是卡死）
        if armed and idle > 0 and (time.monotonic() - last_event) >= idle:
            break
        slept = 0.0
        while slept < interval and not stop["flag"]:
            step = min(0.2, interval - slept)
            time.sleep(step)
            slept += step

    # ---- 干净退出：确认本轮所有输出 ----
    # 推送模式例外：只有「推送成功」的批次才算确认过，这里绝不能顺手把没推出去的事件清掉
    # （否则 --max-wait / --once / Ctrl+C 一退，事件就凭空消失了）。
    guard.release()
    if not (push_mode and pending):
        state.clear_unacked()
        if wechat:
            state.data["wechat_acked_offset"] = wechat.read_offset
    elif not args.quiet:
        print("--- 还有 %d 条事件没推出去：保留在账本里，下次运行会自动补推 ---" % len(pending),
              file=sys.stderr, flush=True)
    state.data["runs"] = int(state.data.get("runs", 0)) + 1
    state.data["last_run"] = {
        "ts": round(time.time(), 3),
        "printed": len(printed_this_run),
        "img": sum(1 for e in printed_this_run if e.kind == "IMG"),
        "wx": sum(1 for e in printed_this_run if e.kind == "WX"),
        "replay": sum(1 for e in printed_this_run if e.replay),
        "idle_timeout": idle,
        "pushes": pushes,
        "pending": len(pending),
    }
    state.save()
    if not args.quiet:
        if push_mode:
            print("--- 常驻推送结束：本轮推送 %d 批，%d 条未推送（留在账本里，下次运行会补推）---"
                  % (pushes, len(pending)), flush=True)
        elif printed_this_run:
            print("--- 空闲 %.1fs 无新事件，安全退出；本轮输出 %d 条（IMG %d / WX %d，重放 %d）---"
                  % (idle, len(printed_this_run),
                     sum(1 for e in printed_this_run if e.kind == "IMG"),
                     sum(1 for e in printed_this_run if e.kind == "WX"),
                     sum(1 for e in printed_this_run if e.replay)), flush=True)
        else:
            print("--- 本轮 0 条输出（零事件时不会因空闲退出，只可能是 --once / --max-wait / 信号 结束）---",
                  flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="阻塞等待新截图 / 微信新消息，把名字打印出来")
    ap.add_argument("--config", default=os.path.join(HERE, CONFIG_NAME), help="配置文件路径")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔秒（默认取配置 2.0）")
    ap.add_argument("--idle-timeout", type=float, default=None, help="空闲多久算结束（默认取配置 10.0，0=永不退出）")
    ap.add_argument("--max-wait", type=float, default=None, help="最长等待秒（0=不限，默认取配置）")
    ap.add_argument("--dir", action="append", default=None, help="截图目录，可多次指定（覆盖配置）")
    ap.add_argument("--sources", default=None, help="监听源，逗号分隔: shots,wechat")
    ap.add_argument("--stability", dest="stability", action="store_true", default=None,
                    help="强制开启文件稳定性检查（连续两轮大小/mtime 一致才算就绪）")
    ap.add_argument("--no-stability", dest="stability", action="store_false", help="关闭稳定性检查（快一轮）")
    ap.add_argument("--replay", dest="replay", action="store_true", default=None, help="重放上一轮未确认的输出")
    ap.add_argument("--no-replay", dest="replay", action="store_false", help="不重放未确认输出")
    ap.add_argument("--format", choices=["lines", "json"], default=None, help="输出格式")
    ap.add_argument("--wechat-meta", dest="wechat_meta", action="store_true", default=None,
                    help="WX 行打印完整 JSON（默认只打印消息正文+附件路径）")
    ap.add_argument("--no-wechat-meta", dest="wechat_meta", action="store_false",
                    help="WX 行只打印消息正文（默认行为）")
    ap.add_argument("--once", action="store_true", help="只扫一遍就退出（调试用）")
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    ap.add_argument("--reset", action="store_true", help="清空状态文件后退出")
    ap.add_argument("--quiet", action="store_true", help="不打印横幅与收尾说明")
    ap.add_argument("--owner-session", default="", help="会话 id（默认取环境变量 DSH_SESSION_ID）")
    ap.add_argument("--no-exclusive", dest="exclusive", action="store_false", default=None,
                    help="关闭独占登记（允许多个等待脚本并存，一般不用）")
    ap.add_argument("--force-owner", dest="force_owner", action="store_true",
                    help="强制接管事件流（忽略交棒保留期；确认要在本会话监听时才用）")
    ap.add_argument("--push", dest="push", action="store_true", default=None,
                    help="常驻推送模式：新事件静默 N 秒后主动推给 DSH 的 relay-wake 插件，唤醒空闲会话")
    ap.add_argument("--no-push", dest="push", action="store_false",
                    help="关闭推送模式（回到「打印 + 空闲退出」的阻塞模式）")
    ap.add_argument("--push-url", default=None, help="推送地址（默认取配置 push.url）")
    ap.add_argument("--push-token", default=None, help="鉴权头 x-relay-token（默认取配置 push.token）")
    ap.add_argument("--push-session", default=None,
                    help="唤醒目标会话 id（默认取 --owner-session / DSH_SESSION_ID）")
    ap.add_argument("--debounce", type=float, default=None,
                    help="静默去抖秒数：最后一个事件之后安静这么久才推送（默认取配置 8.0）")
    ap.add_argument("--push-max-hold", type=float, default=None,
                    help="兜底：持续有新事件时，攒够这么久也推一次（0=一直不推，默认）")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n--- 被 Ctrl+C 中断 ---", flush=True)
        return 130
    except Exception as exc:                       # 配置/环境问题：给出可读错误
        print("[错误] %r" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
