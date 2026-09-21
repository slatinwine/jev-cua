#!/usr/bin/env python3
"""Computer control via cua-driver (trycua/cua) with a local Jev decision
service as the proposal brain. Stdlib only. UTF-8 safe.

Subcommands:
  daemon                       ensure the cua-driver serve daemon is running
  windows [--all]              list on-screen app windows
  state --pid P --window-id W [--out DIR] [--query SUB] [--max-elements N]
                               snapshot a window: element tree + screenshot
                               (writes state.json + shot.png, prints summary)
  decide --goal TXT --state-file FILE [--endpoint URL] [--api-key KEY]
                               two-stage Jev decision: action, then target element
  act click|type|key|hotkey|scroll ...   execute via cua-driver (element-first)

Click addressing prefers element_index + snapshot_id from the last `state`
call (cache lives in %TEMP%\\jev_cua_snap.json). The driver replaces the
index map on every snapshot of the same (pid, window_id) — re-run `state`
after any action before the next element-indexed action.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

def _find_cua_exe():
    exe = os.environ.get("CUA_DRIVER_EXE")
    if exe:
        return exe
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(base, "Programs", "Cua", "cua-driver", "bin", "cua-driver.exe")


CUA_EXE = _find_cua_exe()
SNAP_CACHE = os.path.join(tempfile.gettempdir(), "jev_cua_snap.json")
DEFAULT_ENDPOINT = "http://127.0.0.1:8767"
DECIDE_PATH = "/api/v1/decide"
PY = sys.executable
# port -> (env var holding the backend's service dir, argv relative to it);
# set the env var to enable auto-start for that backend
BACKENDS = {
    "8767": ("JEV_STUDENT_DIR", ["intent_server.py"]),
    "8766": ("JEV_LAYA_DIR", ["laya_server.py"]),
    "8765": ("JEV_NANOJEV_DIR", ["scripts/serve_decisions.py",
                                 "--checkpoint-dir", "checkpoints/NanoJev-unified",
                                 "--web-root", "web", "--port", "8765",
                                 "--disable-native-triton", "--precision", "fp32"]),
}
CONFIDENCE_FLOOR = 0.6
SKIP_TITLES = ("Cua.AgentCursorOverlay", "StatusBarWnd", "Program Manager")

ACTIONS = {
    "click": "点击某个元素（按钮/菜单/标签页等）",
    "type_text": "向获得焦点的输入区逐字输入文字",
    "press_key": "按一个单键（如 enter/esc/tab）",
    "hotkey": "按组合键（如 ctrl+s）",
    "scroll": "滚动内容",
    "done": "目标已完成，结束任务",
}
CLICK_FAMILY = {"click"}


def out(s):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(s)


def _reconfigure_stdio():
    # Git Bash pipes are UTF-8; the Windows default (GBK) mangles Chinese.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_reconfigure_stdio()


def die(msg, code=1):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    print(msg, file=sys.stderr)
    sys.exit(code)


def driver(*argv, timeout=180):
    p = subprocess.run([CUA_EXE, *argv], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        die(f"cua-driver {' '.join(argv[:2])} 失败: {p.stdout or p.stderr}")
    txt = p.stdout.strip()
    if not txt:
        die(f"cua-driver {' '.join(argv[:2])} 无输出")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        die(f"cua-driver 输出不是 JSON: {txt[:200]}")


def call_tool(tool, args=None, timeout=180):
    argv = ["call", tool]
    if args:
        argv += ["--args", json.dumps(args, ensure_ascii=False)]
    res = driver(*argv, timeout=timeout)
    if res.get("status") == "refused" or res.get("isError"):
        die(f"{tool} 被拒绝/出错: {json.dumps(res, ensure_ascii=False)[:400]}")
    return res


# ---------------------------------------------------------------- daemon

def daemon_running():
    try:
        p = subprocess.run([CUA_EXE, "status"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return "daemon is running" in (p.stdout or "")
    except Exception:
        return False


def cmd_daemon(args):
    if daemon_running():
        out(json.dumps({"daemon": "running"}))
        return
    flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    log = open(os.path.join(tempfile.gettempdir(), "cua_driver_serve.log"), "ab")
    kicked = subprocess.run([CUA_EXE, "autostart", "kick"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
    deadline = time.time() + 60
    while time.time() < deadline:
        if daemon_running():
            out(json.dumps({"daemon": "running", "via": "autostart kick"}))
            return
        time.sleep(2)
    subprocess.Popen([CUA_EXE, "serve"], stdout=log, stderr=log,
                     creationflags=flags)
    while time.time() < deadline:
        if daemon_running():
            out(json.dumps({"daemon": "running", "via": "detached serve"}))
            return
        time.sleep(2)
    die("cua-driver 守护进程启动失败；手动运行: cua-driver serve")


# ---------------------------------------------------------------- windows

def cmd_windows(args):
    res = call_tool("list_windows", {"only_on_screen": True} if not args.all else None)
    wins = res.get("windows") or res.get("_legacy_windows") or []
    rows = []
    for w in wins:
        title = w.get("title") or ""
        if not args.all and (not title or any(s in title for s in SKIP_TITLES)):
            continue
        rows.append({"pid": w.get("pid"), "window_id": w.get("window_id"),
                     "title": title, "minimized": w.get("minimized", False)})
    out(json.dumps({"windows": rows}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- state

def load_snap_cache():
    try:
        with open(SNAP_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_snap_cache(cache):
    with open(SNAP_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def cmd_state(args):
    tool_args = {"pid": args.pid, "window_id": args.window_id,
                 "include_screenshot": not args.no_shot}
    if args.query:
        tool_args["query"] = args.query
    if args.max_elements:
        tool_args["max_elements"] = args.max_elements
    d = call_tool("get_window_state", tool_args, timeout=240)
    outdir = args.out or os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    shot_path = None
    if d.get("screenshot_png_b64"):
        shot_path = os.path.abspath(os.path.join(outdir, f"shot_{args.pid}.png"))
        with open(shot_path, "wb") as f:
            f.write(base64.b64decode(d["screenshot_png_b64"]))
    state_path = os.path.abspath(os.path.join(outdir, f"state_{args.pid}.json"))
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    cache = load_snap_cache()
    cache[f"{args.pid}:{args.window_id}"] = {
        "snapshot_id": d.get("snapshot_id"), "ts": time.time()}
    save_snap_cache(cache)
    rows = [{"idx": e.get("element_index"), "role": e.get("role"),
             "label": (e.get("label") or "")[:60],
             "value": (e.get("value") or "")[:40] or None,
             "actions": e.get("actions", [])}
            for e in d.get("elements", [])]
    out(json.dumps({
        "pid": args.pid, "window_id": args.window_id,
        "window_title": d.get("window_title"), "snapshot_id": d.get("snapshot_id"),
        "app_name": d.get("app_name"), "element_count": d.get("total_element_count"),
        "screenshot": shot_path, "state_file": state_path,
        "elements": rows,
    }, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- decide

def health(endpoint, timeout=2.0):
    try:
        with urllib.request.urlopen(endpoint + "/api/health", timeout=timeout) as r:
            return json.loads(r.read()).get("ready") is True
    except Exception:
        return False


def ensure_server(endpoint, wait=240):
    if health(endpoint):
        return True
    port = endpoint.split("//127.0.0.1:", 1)[-1].split("/", 1)[0]
    backend = BACKENDS.get(port)
    if backend is None:
        return False
    env_var, argv = backend
    cwd = os.environ.get(env_var)
    if not cwd or not os.path.isdir(cwd):
        print(f"{endpoint} 未启动，且未设置 {env_var}（指向后端服务目录），无法自动拉起。",
              file=sys.stderr)
        return False
    print(f"本地决策服务 {endpoint} 未启动，正在拉起...", file=sys.stderr)
    log = open(os.path.join(cwd, "server.log"), "ab")
    flags = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen([PY, *argv], cwd=cwd, stdout=log, stderr=log,
                     creationflags=flags)
    deadline = time.time() + wait
    while time.time() < deadline:
        if health(endpoint):
            return True
        time.sleep(3)
    return False


def is_official(endpoint):
    return "typesafe.ai" in endpoint or endpoint.rstrip("/").endswith("/systemone")


def decide_local(endpoint, state, questions, timeout=120):
    body = json.dumps({"state": state, "questions": questions},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint + DECIDE_PATH, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read())
    decisions = {}
    for qid, q in result["decisions"].items():
        decisions[qid] = {"probabilities": q["probabilities"],
                          "choice": q.get("choice") or q.get("value")}
    return decisions, result.get("model")


def decide_official(endpoint, api_key, state, questions, timeout=120):
    """Official contract: one noul question per candidate, single request."""
    noul = {qid: {"type": "noul", "instructions": q["instructions"] +
                  " Candidates: " + "; ".join(f"{k}:{v}" for k, v in q["criteria"].items())}
            for qid, q in questions.items()}
    body = json.dumps({"state": state, "model": "jev-latest", "questions": noul},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or os.environ.get('TYPESAFE_API_KEY', '')}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read())
    answers = result.get("answers") or {}
    decisions = {}
    for qid, q in questions.items():
        scores = {}
        for cid in q["criteria"]:
            a = answers.get(f"{qid}_{cid}") or {}
            scores[cid] = float(a.get("noul", 0.0) or 0.0)
        # official answers are keyed by question then candidate; try nested too
        nested = answers.get(qid)
        if isinstance(nested, dict):
            for cid in q["criteria"]:
                a = nested.get(cid)
                if isinstance(a, dict):
                    scores[cid] = float(a.get("noul", 0.0) or 0.0)
        total = sum(scores.values()) or 1.0
        probs = {k: v / total for k, v in scores.items()}
        best = max(probs, key=probs.get)
        decisions[qid] = {"probabilities": probs, "choice": best}
    return decisions, f"{result.get('model', 'jev')} (official)"


def best_clickable(elements):
    rows = []
    for e in elements:
        acts = set(e.get("actions") or [])
        if acts & {"invoke", "expand", "select", "toggle", "press"}:
            rows.append(e)
    return rows[:40]


def resolve_goal(args):
    """Chinese via argv gets GBK-mangled on Windows — prefer a UTF-8 file/stdin."""
    if args.goal_file:
        with open(args.goal_file, encoding="utf-8") as f:
            return f.read().strip()
    if args.goal_stdin:
        return sys.stdin.read().strip()
    return args.goal


def cmd_decide(args):
    goal = resolve_goal(args)
    if not goal:
        die("目标为空：用 --goal-file FILE（UTF-8）或 --goal-stdin 传入")
    with open(args.state_file, encoding="utf-8") as f:
        snap = json.load(f)
    elements = snap.get("elements", [])
    win_title = snap.get("window_title", "")
    state_text = f"目标: {goal}\n当前窗口: {win_title}\n" + "\n".join(
        f"idx{e.get('element_index')} {e.get('role')} {e.get('label') or ''} [{','.join(e.get('actions') or [])}]"
        for e in best_clickable(elements))
    questions = {"action": {"type": "choice",
                            "instructions": "为达成目标，下一步执行哪个动作",
                            "criteria": ACTIONS}}
    if is_official(args.endpoint):
        if not (args.api_key or os.environ.get("TYPESAFE_API_KEY")):
            die("官方端点需要 --api-key 或 TYPESAFE_API_KEY")
        decisions, model = decide_official(args.endpoint.rstrip("/"), args.api_key,
                                           state_text, questions, timeout=args.timeout)
    else:
        if not ensure_server(args.endpoint):
            die("本地决策服务不可用；先启动 NanoJev/Laya 或换 --endpoint")
        decisions, model = decide_local(args.endpoint, state_text, questions,
                                        timeout=args.timeout)
    act = decisions["action"]
    result = {"goal": goal, "action": act["choice"],
              "confidence": round(act["probabilities"][act["choice"]], 4),
              "action_distribution": {k: round(v, 4) for k, v in
                                      sorted(act["probabilities"].items(), key=lambda kv: -kv[1])},
              "model": model}
    # target stage runs unconditionally: the action layer is often unsure,
    # but the element-label ranking is still useful to the verifying agent
    cands = best_clickable(elements)
    result["target_element"] = None
    result["target_confidence"] = None
    if len(cands) >= 2:
        criteria = {f"idx{e['element_index']}": f"{e.get('role')} {e.get('label') or ''}".strip()
                    for e in cands}
        q = {"target": {"type": "choice",
                        "instructions": f"要达成目标「{goal}」，应操作哪个元素",
                        "criteria": criteria}}
        if is_official(args.endpoint):
            decisions2, _ = decide_official(args.endpoint.rstrip("/"), args.api_key,
                                            state_text, q, timeout=args.timeout)
        else:
            decisions2, _ = decide_local(args.endpoint, state_text, q,
                                         timeout=args.timeout)
        tgt = decisions2["target"]
        result["target_element"] = tgt["choice"]
        result["target_confidence"] = round(tgt["probabilities"][tgt["choice"]], 4)
        result["target_distribution"] = {k: round(v, 4) for k, v in
                                         sorted(tgt["probabilities"].items(), key=lambda kv: -kv[1])}
    confs = [c for c in (result["confidence"], result["target_confidence"]) if c is not None]
    result["low_confidence"] = bool(confs) and min(confs) < CONFIDENCE_FLOOR
    out(json.dumps(result, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- act

def snap_for(pid, window_id):
    entry = load_snap_cache().get(f"{pid}:{window_id}")
    if not entry:
        die("没有可用的 snapshot：先对该窗口跑 state --pid ... --window-id ...")
    if time.time() - entry["ts"] > 300:
        die("snapshot 已过期（>5 分钟或动作后失效）；重新跑 state 后再执行元素动作")
    return entry["snapshot_id"]


def cmd_act(args):
    base = {"pid": args.pid, "window_id": args.window_id}
    if args.foreground:
        base["delivery_mode"] = "foreground"
    if args.action == "click":
        if args.element is not None:
            base["element_index"] = args.element
            base["snapshot_id"] = snap_for(args.pid, args.window_id)
        elif args.x is not None and args.y is not None:
            base["x"], base["y"] = args.x, args.y
        else:
            die("click 需要 --element IDX 或 --x X --y Y")
        if args.right:
            base["button"] = "right"
        if args.double:
            base["count"] = 2
        res = call_tool("click", base)
    elif args.action == "type":
        if not args.text:
            die("type 需要 --text")
        base["text"] = args.text
        if args.delay:
            base["delay_ms"] = args.delay
        res = call_tool("type_text", base)
    elif args.action == "key":
        if not args.key:
            die("key 需要 --key（如 enter/esc/tab）")
        base["key"] = args.key
        res = call_tool("press_key", base)
    elif args.action == "hotkey":
        if not args.keys:
            die("hotkey 需要 --keys（如 ctrl+s）")
        base["keys"] = args.keys
        res = call_tool("hotkey", base)
    elif args.action == "scroll":
        if not args.direction:
            die("scroll 需要 --direction up|down|left|right")
        base["direction"] = args.direction
        if args.amount:
            base["amount"] = args.amount
        res = call_tool("scroll", base)
    else:
        die(f"未知动作 {args.action}")
    res = dict(res)
    res["_hint"] = "动作可能异步生效：重新跑 state 验证效果，再决定下一步"
    out(json.dumps(res, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser(description="cua-driver + Jev 电脑控制桥")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="确保 cua-driver 守护进程在运行")

    p = sub.add_parser("windows")
    p.add_argument("--all", action="store_true", help="不过滤系统/覆盖窗口")

    p = sub.add_parser("state", help="窗口快照：元素树+截图")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--window-id", type=int, required=True)
    p.add_argument("--out", help="输出目录（默认当前目录）")
    p.add_argument("--query", help="只投影标签匹配该子串的元素")
    p.add_argument("--max-elements", type=int)
    p.add_argument("--no-shot", action="store_true", help="不截屏（更快）")

    p = sub.add_parser("decide", help="Jev 两级决策：动作→目标元素")
    p.add_argument("--goal", help="目标文本（仅 ASCII 安全；中文用 --goal-file/--goal-stdin）")
    p.add_argument("--goal-file", help="UTF-8 文件，内容为目标文本")
    p.add_argument("--goal-stdin", action="store_true", help="从 stdin 读目标（UTF-8）")
    p.add_argument("--state-file", required=True)
    p.add_argument("--endpoint", default=os.environ.get("JEV_ENDPOINT", DEFAULT_ENDPOINT))
    p.add_argument("--api-key", default=os.environ.get("JEV_API_KEY"))
    p.add_argument("--timeout", type=int, default=120)

    p = sub.add_parser("act")
    p.add_argument("action", choices=["click", "type", "key", "hotkey", "scroll"])
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--window-id", type=int, required=True)
    p.add_argument("--element", type=int)
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--text")
    p.add_argument("--key")
    p.add_argument("--keys")
    p.add_argument("--direction")
    p.add_argument("--amount", type=int)
    p.add_argument("--delay", type=int)
    p.add_argument("--double", action="store_true")
    p.add_argument("--right", action="store_true")
    p.add_argument("--foreground", action="store_true",
                   help="升级为前台送达（默认后台 UIA/PostMessage；仅在驱动报 background_unavailable 后使用）")

    args = ap.parse_args()
    {"daemon": cmd_daemon, "windows": cmd_windows, "state": cmd_state,
     "decide": cmd_decide, "act": cmd_act}[args.cmd](args)


if __name__ == "__main__":
    main()
