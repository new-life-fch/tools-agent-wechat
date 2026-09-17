#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vscode_notify.py —— 把文本推送到 VSCode 系编辑器（VSCode / Qoder / Cursor / Trae …）
底部「答题板」面板。

设计原则
  * 只用标准库，Python 3.8+，macOS / Windows / Linux 通吃。
  * 永不阻塞：默认 2 秒超时，失败快速返回，由调用方决定要不要忽略。
  * 面板没开 = 退出码 2，主流程据此静默跳过。

用法
  python vscode_notify.py push --title "第3题" --text "答案…"
  python vscode_notify.py push --title "第3题" --file answer.md
  cat answer.md | python vscode_notify.py push --title "第3题"
  python vscode_notify.py push --id q3 --text "先给结论…"     # 同 id 再推 = 就地更新那条
  python vscode_notify.py clear
  python vscode_notify.py status
  python vscode_notify.py doctor

退出码
  0 成功    2 没有可用的面板（编辑器没开/扩展没装）    3 通道错误（连上但被拒）    4 用法错误
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

APP_TAG = "answer-panel"
DEFAULT_REGISTRY = os.path.join(os.path.expanduser("~"), ".answer-panel", "bridge.json")
DEFAULT_TIMEOUT = 2.0

EXIT_OK = 0
EXIT_NO_PANEL = 2
EXIT_CHANNEL = 3
EXIT_USAGE = 4


# --------------------------------------------------------------------------- 输出

def _console_safe(text: str) -> str:
    """Windows 老控制台（GBK）下也别炸。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, "replace").decode(enc, "replace")


def say(msg: str = "", quiet: bool = False) -> None:
    if quiet:
        return
    try:
        print(_console_safe(msg))
    except Exception:
        pass


def warn(msg: str) -> None:
    try:
        print(_console_safe(msg), file=sys.stderr)
    except Exception:
        pass


# --------------------------------------------------------------------------- 注册文件

def registry_path(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("ANSWER_PANEL_REGISTRY")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return DEFAULT_REGISTRY


def load_registry(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []
    items = data.get("instances") if isinstance(data, dict) else None
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


# --------------------------------------------------------------------------- 探测

def http_json(url: str, payload=None, token: str | None = None, timeout: float = DEFAULT_TIMEOUT,
              method: str | None = None):
    """返回 (ok, status, data_or_error)。任何异常都收敛成 (False, code, 说明)。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Answer-Panel-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, resp.status, json.loads(raw or "{}")
            except ValueError:
                return True, resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = {"raw": body}
        return False, exc.code, parsed
    except urllib.error.URLError as exc:
        return False, 0, str(getattr(exc, "reason", exc))
    except (OSError, ValueError) as exc:
        return False, 0, str(exc)


def probe(entry: dict, timeout: float = DEFAULT_TIMEOUT):
    """向候选实例的 /health 打招呼；通了返回合并后的实例信息，否则 None。"""
    port = entry.get("port")
    if not port:
        return None
    ok, status, data = http_json("http://127.0.0.1:%d/health" % int(port), timeout=timeout)
    if not ok or status != 200 or not isinstance(data, dict) or not data.get("ok"):
        return None
    if data.get("app") != APP_TAG:
        return None
    merged = dict(entry)
    merged["port"] = int(data.get("port") or port)
    if data.get("pid") is not None:
        merged["health_pid"] = data.get("pid")
    # token 只在与健康检查报出的 pid 一致时才可信（防端口被别的实例接管后用了旧令牌）
    if data.get("pid") is not None and entry.get("pid") is not None and int(data["pid"]) != int(entry["pid"]):
        merged["token"] = None
        merged["token_stale"] = True
    merged["_health"] = data
    return merged


def live_instances(registry: str, timeout: float = DEFAULT_TIMEOUT) -> list:
    out = []
    seen = set()
    for entry in load_registry(registry):
        probe_result = probe(entry, timeout=timeout)
        if not probe_result:
            continue
        key = (probe_result.get("port"), probe_result.get("health_pid") or probe_result.get("pid"))
        if key in seen:
            continue
        seen.add(key)
        out.append(probe_result)
    out.sort(key=lambda it: int(it.get("port") or 0))
    return out


def _ws_list(entry: dict) -> list:
    raw = entry.get("workspace") or ""
    if not isinstance(raw, str) or not raw:
        return []
    return [p for p in raw.split(os.pathsep) if p]


def pick(instances: list, cwd: str, broadcast: bool = False) -> list:
    """按工作区匹配挑窗口；匹配不上就广播给全部（多开窗口时不漏）。"""
    if broadcast or len(instances) <= 1:
        return list(instances)
    cwd = os.path.abspath(cwd)
    best, best_len = None, -1
    for inst in instances:
        for ws in _ws_list(inst):
            ws_abs = os.path.abspath(ws)
            if cwd == ws_abs or cwd.startswith(ws_abs.rstrip(os.sep) + os.sep):
                if len(ws_abs) > best_len:
                    best, best_len = inst, len(ws_abs)
    return [best] if best else list(instances)


# --------------------------------------------------------------------------- 动作

def do_push(args) -> int:
    text = args.text
    if args.file:
        try:
            with open(os.path.expanduser(args.file), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            warn("读取文件失败：%s" % exc)
            return EXIT_USAGE
    if text is None and not sys.stdin.isatty():
        text = sys.stdin.read()
    if text is None:
        warn("没有内容可推：用 --text / --file / 标准输入 提供正文")
        return EXIT_USAGE
    if args.strip:
        text = text.strip("\n")

    registry = registry_path(args.registry)
    instances = live_instances(registry, timeout=args.timeout)
    if not instances:
        say("NO-PANEL  没有正在运行的答题板（%s）" % registry, quiet=args.quiet)
        return EXIT_NO_PANEL

    targets = pick(instances, os.getcwd(), broadcast=args.broadcast)
    payload = {"text": text}
    if args.title:
        payload["title"] = args.title
    if args.id:
        payload["id"] = args.id
    if args.source:
        payload["source"] = args.source

    results, ok_count = [], 0
    for inst in targets:
        url = "http://127.0.0.1:%d/push" % int(inst["port"])
        ok, status, data = http_json(url, payload=payload, token=inst.get("token"), timeout=args.timeout)
        results.append({"port": inst["port"], "ok": bool(ok and status == 200), "status": status, "data": data})
        if ok and status == 200:
            ok_count += 1
        elif not args.quiet:
            warn("推送失败 127.0.0.1:%s → HTTP %s %s" % (inst["port"], status, data))

    if args.json:
        say(json.dumps({"ok": ok_count > 0, "pushed": ok_count, "results": results}, ensure_ascii=False))
    elif ok_count:
        first = results[[r["ok"] for r in results].index(True)]
        seq = (first.get("data") or {}).get("seq")
        say("OK  %d 个面板  seq=%s  %d 字" % (ok_count, seq, len(text)), quiet=args.quiet)
    return EXIT_OK if ok_count else EXIT_CHANNEL


def do_clear(args) -> int:
    registry = registry_path(args.registry)
    instances = live_instances(registry, timeout=args.timeout)
    if not instances:
        say("NO-PANEL  没有正在运行的答题板", quiet=args.quiet)
        return EXIT_NO_PANEL
    ok_count = 0
    for inst in pick(instances, os.getcwd(), broadcast=args.broadcast):
        url = "http://127.0.0.1:%d/clear" % int(inst["port"])
        ok, status, _ = http_json(url, payload={}, token=inst.get("token"), timeout=args.timeout)
        if ok and status == 200:
            ok_count += 1
    say("OK  已清空 %d 个面板" % ok_count, quiet=args.quiet)
    return EXIT_OK if ok_count else EXIT_CHANNEL


def _fmt_instance(inst: dict, verbose: bool = False) -> str:
    health = inst.get("_health") or {}
    bits = [
        "127.0.0.1:%s" % inst.get("port"),
        "pid=%s" % (inst.get("health_pid") or inst.get("pid")),
        health.get("editor") or inst.get("app") or "?",
        "vscode=%s" % (inst.get("vscode") or "?"),
    ]
    ws = _ws_list(inst)
    if ws:
        bits.append("ws=%s" % ws[0])
    if inst.get("token_stale"):
        bits.append("TOKEN-STALE")
    if verbose:
        health = inst.get("_health") or {}
        bits.append("items=%s" % health.get("items"))
        bits.append("token=%s" % ("yes" if inst.get("token") else "no"))
    return "  ".join(str(b) for b in bits)


def do_status(args) -> int:
    registry = registry_path(args.registry)
    instances = live_instances(registry, timeout=args.timeout)
    if args.json:
        say(json.dumps({"registry": registry, "count": len(instances),
                        "instances": [{k: v for k, v in it.items() if k != "_health"} for it in instances]},
                       ensure_ascii=False))
        return EXIT_OK if instances else EXIT_NO_PANEL
    if not instances:
        say("NO-PANEL  注册文件 %s 里没有活着的面板" % registry)
        return EXIT_NO_PANEL
    say("注册文件：%s" % registry)
    for inst in instances:
        say("  " + _fmt_instance(inst, verbose=True))
    return EXIT_OK


def do_doctor(args) -> int:
    registry = registry_path(args.registry)
    say("答题板诊断")
    say("  注册文件      ：%s" % registry)
    say("  文件存在      ：%s" % os.path.exists(registry))
    raw = load_registry(registry)
    say("  登记条目      ：%d" % len(raw))
    for it in raw:
        say("    - pid=%s port=%s %s" % (it.get("pid"), it.get("port"), it.get("app") or "?"))
    instances = live_instances(registry, timeout=args.timeout)
    say("  存活面板      ：%d" % len(instances))
    for inst in instances:
        say("    - " + _fmt_instance(inst, verbose=True))
    if not raw:
        say("")
        say("  提示：注册文件不存在通常意味着扩展没激活。")
        say("        确认扩展已安装并重启过编辑器；或在命令面板运行「答题板: 显示服务信息」。")
    elif not instances:
        say("")
        say("  提示：有条目但探不通 → 编辑器可能已关闭（陈旧条目会被自动忽略）。")
    return EXIT_OK if instances else EXIT_NO_PANEL


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    # 注意：这里绝不能调用 p.set_defaults(...)。
    # argparse 的 parents= 是**共享同一批 action 对象**，而 set_defaults() 会顺手把
    # 匹配 action 的 default 改掉 —— 于是子解析器又拿到了普通默认值，在解析子命令时
    # 反向覆盖主命名空间，`--registry X push …` 里的 X 就被冲成 None。
    # 因此统一用 SUPPRESS，真正的默认值由 _apply_defaults() 在解析完成后补。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", default=argparse.SUPPRESS, help="端口注册文件路径（默认 ~/.answer-panel/bridge.json）")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="单次请求超时秒数（默认 2）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出结果")
    common.add_argument("--quiet", "-q", action="store_true", default=argparse.SUPPRESS, help="成功时不打印任何东西")
    common.add_argument("--broadcast", action="store_true", default=argparse.SUPPRESS, help="推给所有活着的窗口，而不是只推给当前工作区")

    p = argparse.ArgumentParser(
        prog="vscode_notify.py",
        description="把文本推送到 VSCode 系编辑器底部的「答题板」面板。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = p.add_subparsers(dest="cmd")

    # 子命令也带上同一组参数，这样 --json/--registry 放在子命令前后都能用
    sp = sub.add_parser("push", help="推送一段文本", parents=[common])
    sp.add_argument("--title", "-t", default="", help="标题（显示在正文上方）")
    sp.add_argument("--text", default=None, help="正文；不给则读 --file 或标准输入")
    sp.add_argument("--file", "-f", default=None, help="从文件读正文（UTF-8）")
    sp.add_argument("--id", default=None, help="可选标识；同 id 再推会就地更新原来那条")
    sp.add_argument("--source", default="cli", help="来源标记，默认 cli")
    sp.add_argument("--strip", action="store_true", help="去掉正文首尾空行")
    sp.set_defaults(func=do_push)

    sp = sub.add_parser("clear", help="清空面板", parents=[common])
    sp.set_defaults(func=do_clear)

    sp = sub.add_parser("status", help="列出活着的面板", parents=[common])
    sp.set_defaults(func=do_status)

    sp = sub.add_parser("doctor", help="诊断注册文件与面板连通性", parents=[common])
    sp.set_defaults(func=do_doctor)
    return p


def _apply_defaults(args):
    """补上 SUPPRESS 掉的全局参数默认值（见 build_parser 里的说明）。"""
    args.registry = getattr(args, "registry", None)
    args.timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    args.json = getattr(args, "json", False)
    args.quiet = getattr(args, "quiet", False)
    args.broadcast = getattr(args, "broadcast", False)
    return args


def main(argv=None) -> int:
    parser = build_parser()
    args = _apply_defaults(parser.parse_args(argv))
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
