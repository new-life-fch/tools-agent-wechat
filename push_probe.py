#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""push_probe.py — 推送模式（wait_events.py --push）的时序自测

用一个**本地桩服务器**替掉 relay-wake 插件，验证四件事（不打扰任何真实会话）：

    1. 静默去抖：图分散到达时，只在最后一个事件之后约 debounce 秒推一次，且合并成一批
    2. 持续有新事件 → 期间一次都不推；停手 debounce 秒后才推
    3. 推送失败（桩返回 500）→ 事件保留、稍后重试，成功后才确认（不丢、不重复）
    4. 常驻不退出（推送完还在跑）

用法::

    python push_probe.py                 # 全部跑一遍（约 30 秒）
    python push_probe.py --debounce 2    # 换去抖时长

跨平台：临时目录用 tempfile，端口让 OS 分配，Windows/macOS 都能跑。
退出码：0=全部通过；1=有失败。
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("✓" if ok else "✗", name, ("  — %s" % detail) if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> int:
    ap = argparse.ArgumentParser(description="推送模式时序自测（本地桩，不碰真实会话）")
    ap.add_argument("--debounce", type=float, default=3.0, help="去抖秒数（默认 3）")
    args = ap.parse_args()
    debounce = args.debounce

    root = tempfile.mkdtemp(prefix="push_probe_")
    shots = os.path.join(root, "shots")
    os.makedirs(shots, exist_ok=True)
    state = os.path.join(root, "state.json")
    cfg_path = os.path.join(root, "cfg.json")
    log_path = os.path.join(root, "run.log")
    port = free_port()

    posts: list = []
    fail_until = [0.0]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n).decode("utf-8")
            if time.time() < fail_until[0]:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"probe-injected-failure"}')
                return
            posts.append((time.time(), json.loads(raw)))
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"status":"idle"}')

        def log_message(self, *a):                            # 静音
            pass

    json.dump({
        "sources": ["shots"],
        "shot_dirs": [shots],
        "poll_interval_sec": 0.4,
        "stability_check": False,
        "state_file": state,
        "exclusive_owner": False,
        "shots_cold_start_lookback_sec": 0,
        "output": {"format": "lines", "banner": False},
        "push": {"enabled": True, "url": "http://127.0.0.1:%d/relay/wake" % port,
                 "token": "probe", "session": "session-probe",
                 "debounce_sec": debounce, "retry_log_sec": 1.0,
                 "request_timeout_sec": 5.0, "fail_limit": 0},
    }, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def unacked() -> int:
        try:
            return len(json.load(open(state, encoding="utf-8")).get("unacked_shots") or [])
        except (OSError, ValueError):
            return -1

    def drop(name: str) -> None:
        with open(os.path.join(shots, name), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 400)

    print("=== push_probe：桩端口 %d，去抖 %.1fs ===" % (port, debounce), flush=True)
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(HERE, "wait_events.py"), "--config", cfg_path, "--quiet"],
                            cwd=HERE, stdout=logf, stderr=subprocess.STDOUT)
    try:
        # 1) 静默去抖
        time.sleep(1.5)
        for name in ("a.png", "b.png", "c.png"):
            drop(name)
            time.sleep(1.0)
        time.sleep(debounce + 1.5)
        check("静默去抖后推送一次", len(posts) == 1, "实际 %d 次" % len(posts))
        if posts:
            body = posts[0][1]
            check("三张图合并成一批", len(body["text"].splitlines()) - 1 == 3,
                  "实际 %d 条" % (len(body["text"].splitlines()) - 1))
            check("会话与鉴权头都有", body.get("session") == "session-probe")
            check("正文首行是事件摘要", body["text"].splitlines()[0].startswith("[relay] "),
                  body["text"].splitlines()[0])

        # 2) 持续有新事件 → 不推
        base = len(posts)
        for name in ("d.png", "e.png", "f.png", "g.png"):
            drop(name)
            time.sleep(0.8)
        check("持续有新事件期间不推送", len(posts) == base, "期间推了 %d 次" % (len(posts) - base))
        time.sleep(debounce + 1.5)
        check("停手后推一次", len(posts) == base + 1, "实际 %d 次" % (len(posts) - base))

        # 3) 失败重试
        fail_until[0] = time.time() + 4.0
        drop("h.png")
        time.sleep(2.5)
        check("失败期间事件保留在账本里", unacked() >= 1, "unacked=%d" % unacked())
        time.sleep(9.0)
        check("重试成功后账本清空", unacked() == 0, "unacked=%d" % unacked())
        check("最后一批只含失败那一张（不重复推前面的）",
              len(posts[-1][1]["text"].splitlines()) - 1 == 1 if posts else False,
              "实际 %d 条" % (len(posts[-1][1]["text"].splitlines()) - 1) if posts else "-")

        # 4) 常驻
        check("推送之后脚本仍然常驻", proc.poll() is None, "已退出 rc=%s" % proc.poll())
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT if os.name != "nt" else signal.CTRL_BREAK_EVENT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        logf.close()
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    print("\n=== 结果：%s ===" % ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), "；".join(FAILS))),
          flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
