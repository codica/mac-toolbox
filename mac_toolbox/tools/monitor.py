"""monitor - macOS 锁屏登录监控。

子命令:
    mt monitor start [--capture] [--daemon]   启动监控
    mt monitor stop                           停止守护进程
    mt monitor report [--days N | --all]       查看报告
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".mac-toolbox" / "monitor"
CONFIG_FILE = Path.home() / ".mac-toolbox" / "config.json"
LOG_FILE = DATA_DIR / "auth_events.log"
CAPTURE_DIR = DATA_DIR / "captures"
PID_FILE = DATA_DIR / "monitor.pid"
DAEMON_LOG = DATA_DIR / "monitor.out"

# ── log stream 配置 ──────────────────────────────────────────

LOG_PREDICATE = (
    '(process == "loginwindow" AND eventMessage CONTAINS[c] "lock") '
    'OR (process == "authd" AND subsystem == "com.apple.Authorization") '
    'OR (process == "opendirectoryd" AND eventMessage CONTAINS "authentication") '
    'OR (process == "coreauthd" AND subsystem == "com.apple.localauthentication") '
    'OR (process == "powerd" AND (eventMessage CONTAINS "kIOMessageSystemHasPoweredOn" OR eventMessage CONTAINS "kIOMessageSystemWillSleep"))'
)

# Pattern matching rules: (process, keyword, event_type)
# Order matters — more specific patterns first
PATTERNS = [
    ("opendirectoryd", "Failed SecureToken authentication", "auth_fail_password"),
    ("opendirectoryd", "failed to authenticate", "auth_fail_password"),
    ("authd", "Succeeded authorizing right 'system.login.screensaver'", "auth_success_password"),
    ("authd", "Succeeded authorizing right", "auth_success_password"),
    ("coreauthd", "unlocked:1", "auth_success_touchid"),
    ("coreauthd", "biometric lockout", "auth_fail_touchid"),
    ("coreauthd", "evaluation error", "auth_fail_touchid"),
    ("loginwindow", "Screen saver unlocked", "screen_unlocked"),
    ("loginwindow", "sendScreenUnlockedNotification", "screen_unlocked"),
    ("loginwindow", "enqueueScreenLockRequest", "screen_locked"),
    ("loginwindow", "CGSSessionScreenIsLocked: isLocked", "screen_locked"),
    ("powerd", "kIOMessageSystemHasPoweredOn", "system_wake"),
    ("powerd", "kIOMessageSystemWillSleep", "system_sleep"),
]

FAIL_EVENTS = {"auth_fail_password", "auth_fail_touchid"}
SUCCESS_EVENTS = {"auth_success_password", "auth_success_touchid", "screen_unlocked"}
PHOTO_DELAY = 5  # 失败后等几秒，确认没有成功事件再拍照

# ── ANSI colors ──────────────────────────────────────────────

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

EVENT_LABELS = {
    "screen_locked": ("锁屏", DIM),
    "screen_unlocked": ("解锁", GREEN),
    "auth_success_password": ("密码成功", GREEN),
    "auth_fail_password": ("密码失败", RED),
    "auth_success_touchid": ("Touch ID 成功", GREEN),
    "auth_fail_touchid": ("Touch ID 失败", RED),
    "system_sleep": ("进入休眠", DIM),
    "system_wake": ("从休眠恢复", CYAN),
}

# ── Telegram ─────────────────────────────────────────────────

def _load_telegram_config() -> tuple[str, str] | tuple[None, None]:
    """从 ~/.mac-toolbox/config.json 读取 telegram.token 和 telegram.chat_id。"""
    if not CONFIG_FILE.exists():
        return None, None
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        tg = cfg.get("telegram", {})
        return tg.get("token"), tg.get("chat_id")
    except Exception:
        return None, None


def _send_telegram(message: str):
    """非阻塞地发送 Telegram 消息，失败只打印警告，不影响主流程。"""
    token, chat_id = _load_telegram_config()
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"  [Telegram] 发送失败: {e}")


_last_unlock_notify: float = 0.0     # 上次发送解锁通知的时间
_last_wake_notify: float = 0.0       # 上次发送唤醒通知的时间
EVENT_NOTIFY_COOLDOWN = 60           # 解锁/唤醒通知最短间隔（秒）

def _notify_telegram(event_type: str, detail: str):
    """根据事件类型决定是否发送 Telegram 通知。"""
    global _last_unlock_notify, _last_wake_notify
    now_ts = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = os.uname().nodename

    if event_type in FAIL_EVENTS:
        pass  # 认证失败事件不发通知（噪音过多）

    elif event_type == "screen_unlocked":
        if now_ts - _last_unlock_notify >= EVENT_NOTIFY_COOLDOWN:
            _last_unlock_notify = now_ts
            _send_telegram(f"🔓 <b>[{hostname}] 屏幕已解锁</b>\n🕐 {now_str}")

    elif event_type == "system_wake":
        if now_ts - _last_wake_notify >= EVENT_NOTIFY_COOLDOWN:
            _last_wake_notify = now_ts
            _send_telegram(f"💻 <b>[{hostname}] 从休眠恢复</b>\n🕐 {now_str}")


# ── 全局状态 ─────────────────────────────────────────────────

_camera_enabled = False
_running = True


def _signal_handler(signum, frame):
    global _running
    _running = False


# ── 工具函数 ─────────────────────────────────────────────────

def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _capture_photo() -> str | None:
    if not _camera_enabled:
        return None

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    photo_path = CAPTURE_DIR / f"fail_{timestamp}.jpg"

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-f", "avfoundation",
                "-video_size", "1280x720", "-framerate", "30",
                "-i", "0", "-frames:v", "1", "-update", "1", "-y",
                str(photo_path),
            ],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 and photo_path.exists():
            print(f"  Photo captured: {photo_path}")
            return str(photo_path)
        else:
            print(f"  Camera capture failed (exit code {result.returncode})")
            return None
    except FileNotFoundError:
        print("  ffmpeg not found, skipping photo capture")
        return None
    except subprocess.TimeoutExpired:
        print("  Camera capture timed out")
        return None
    except Exception as e:
        print(f"  Camera capture error: {e}")
        return None


def _write_event(event_type: str, detail: str, photo: str | None = None):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event_type,
        "detail": detail,
    }
    if photo:
        entry["photo"] = photo
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['timestamp']}] {event_type}: {detail}")


def _parse_log_line(line: str) -> tuple[str, str] | None:
    """Parse a log stream line and return (process, message) or None."""
    line = line.strip()
    if not line or line.startswith("Filtering") or line.startswith("Timestamp"):
        return None

    process = None
    for proc_name in ("loginwindow", "authd", "opendirectoryd", "coreauthd", "powerd"):
        if proc_name in line:
            process = proc_name
            break
    if process is None:
        return None
    return (process, line)


def _classify_event(process: str, message: str) -> tuple[str, str] | None:
    """Match a log line against known patterns. Returns (event_type, detail) or None."""
    for pat_process, keyword, event_type in PATTERNS:
        if process == pat_process and keyword.lower() in message.lower():
            return (event_type, keyword)
    return None


def _find_monitor_pids() -> list[int]:
    """Find all running monitor and its child log stream PIDs via pgrep."""
    pids = []
    my_pid = os.getpid()
    for pattern in ("mac_toolbox.*monitor", "log stream.*loginwindow"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True,
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid != my_pid:
                    pids.append(pid)
        except (subprocess.SubprocessError, ValueError):
            pass
    return sorted(set(pids))


# ── start ────────────────────────────────────────────────────

def _start_monitor():
    """Start the log stream monitor loop."""
    global _running
    _running = True
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    _ensure_data_dir()
    PID_FILE.write_text(str(os.getpid()))

    print(f"Login monitor started (PID: {os.getpid()})")
    print(f"Log file: {LOG_FILE}")
    print("Listening for authentication events... (Ctrl+C to stop)")

    cmd = ["log", "stream", "--predicate", LOG_PREDICATE, "--style", "compact", "--level", "debug"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except PermissionError:
        print("Error: Permission denied. Try running with sudo.", file=sys.stderr)
        sys.exit(1)

    import select

    seen_events = set()  # simple dedup
    screen_locked = True  # 默认 True，启动时可能已在锁屏；后续由事件驱动
    pending_capture = False  # 是否有待拍照的失败事件
    pending_capture_time = 0.0  # 首次失败的时间
    auth_succeeded = False  # 延迟窗口内是否出现过成功事件

    try:
        while _running and proc.poll() is None:
            # 检查延迟拍照：到时间了就决定拍不拍
            if pending_capture and time.time() - pending_capture_time >= PHOTO_DELAY:
                if not auth_succeeded:
                    photo_path = _capture_photo()
                    if photo_path:
                        _write_event("auth_fail_password", "Failed authentication (photo)", photo_path)
                else:
                    print(f"  [skip photo] auth succeeded within {PHOTO_DELAY}s, not a real failure")
                pending_capture = False
                auth_succeeded = False

            # select: 有待拍照时最多等到 deadline，否则无限等待新数据
            if pending_capture:
                timeout = max(0, pending_capture_time + PHOTO_DELAY - time.time())
            else:
                timeout = None  # 无限等待，完全不耗 CPU

            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if not ready:
                continue

            line = proc.stdout.readline()
            if not line:
                continue

            parsed = _parse_log_line(line)
            if parsed is None:
                continue

            process, message = parsed
            result = _classify_event(process, message)
            if result is None:
                continue

            event_type, detail = result

            # Simple dedup: skip identical events within same second
            dedup_key = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}:{event_type}"
            if dedup_key in seen_events:
                continue
            seen_events.add(dedup_key)
            if len(seen_events) > 1000:
                seen_events.clear()

            # 跟踪锁屏状态
            if event_type in ("screen_locked", "system_wake"):
                screen_locked = True
            elif event_type == "screen_unlocked":
                screen_locked = False

            # 成功事件：标记，取消待拍照
            if event_type in SUCCESS_EVENTS:
                auth_succeeded = True

            # 非锁屏期间的认证失败 = 系统内部行为（Dark Wake 等），跳过
            if event_type in FAIL_EVENTS and not screen_locked:
                continue

            # 失败事件：标记待拍照（延迟执行）
            if event_type in FAIL_EVENTS and not pending_capture:
                pending_capture = True
                pending_capture_time = time.time()
                auth_succeeded = False

            _write_event(event_type, detail)
            _notify_telegram(event_type, detail)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if PID_FILE.exists():
            PID_FILE.unlink()
        print("\nMonitor stopped.")


def _daemonize():
    """Fork into background using nohup-style double fork."""
    _ensure_data_dir()

    old_pids = _find_monitor_pids()
    for pid in old_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if old_pids:
        time.sleep(0.5)
        print(f"Cleaned up {len(old_pids)} old process(es).")

    pid = os.fork()
    if pid > 0:
        print(f"Monitor started in background (PID: {pid}).")
        sys.exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    sys.stdout.flush()
    sys.stderr.flush()

    devnull = open(os.devnull, "r")
    log_out = open(DAEMON_LOG, "a")

    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(log_out.fileno(), sys.stdout.fileno())
    os.dup2(log_out.fileno(), sys.stderr.fileno())

    _start_monitor()


# ── stop ─────────────────────────────────────────────────────

def _stop_monitor():
    """Stop all running monitor daemons and their child log stream processes."""
    pids = _find_monitor_pids()

    if PID_FILE.exists():
        try:
            file_pid = int(PID_FILE.read_text().strip())
            if file_pid not in pids:
                pids.append(file_pid)
        except ValueError:
            pass
        PID_FILE.unlink()

    if not pids:
        print("No running monitor found.")
        return

    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            print(f"  Stopped PID {pid}")
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"  Permission denied for PID {pid}. Try sudo.", file=sys.stderr)
    print(f"Stopped {killed} process(es).")


# ── report ───────────────────────────────────────────────────

def _load_events(since: datetime | None = None) -> list[dict]:
    if not LOG_FILE.exists():
        return []

    events = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
            if since and ts < since:
                continue
            entry["_ts"] = ts
            events.append(entry)
    return events


def _time_period(hour: int) -> str:
    if 6 <= hour < 12:
        return "上午 (06-12)"
    elif 12 <= hour < 14:
        return "中午 (12-14)"
    elif 14 <= hour < 18:
        return "下午 (14-18)"
    elif 18 <= hour < 22:
        return "晚上 (18-22)"
    else:
        return "深夜 (22-06)"


def _detect_consecutive_failures(events: list[dict]) -> list[tuple[str, int]]:
    alerts = []
    streak = 0
    streak_start = ""
    for e in events:
        if e["event"] in FAIL_EVENTS:
            if streak == 0:
                streak_start = e["timestamp"]
            streak += 1
        else:
            if streak >= 3:
                alerts.append((streak_start, streak))
            streak = 0
    if streak >= 3:
        alerts.append((streak_start, streak))
    return alerts


def _print_report(events: list[dict], title: str):
    if not events:
        print(f"\n{DIM}暂无认证事件记录。{RESET}")
        print(f"{DIM}请先运行: sudo mt monitor start{RESET}")
        return

    total_success = sum(1 for e in events if e["event"] in SUCCESS_EVENTS)
    total_fail = sum(1 for e in events if e["event"] in FAIL_EVENTS)
    total_lock = sum(1 for e in events if e["event"] == "screen_locked")
    total_unlock = sum(1 for e in events if e["event"] == "screen_unlocked")

    print(f"\n{BOLD}{'=' * 56}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'=' * 56}{RESET}")

    print(f"\n{BOLD}总览{RESET}")
    print(f"  锁屏次数:     {total_lock}")
    print(f"  解锁次数:     {total_unlock}")
    print(f"  认证成功:     {GREEN}{total_success}{RESET}")
    if total_fail > 0:
        print(f"  认证失败:     {RED}{BOLD}{total_fail}{RESET}")
    else:
        print(f"  认证失败:     {total_fail}")
    total_photos = sum(1 for e in events if e.get("photo"))
    if total_photos > 0:
        print(f"  拍照记录:     {YELLOW}{total_photos}{RESET} 张 (captures/ 目录)")

    by_day = defaultdict(list)
    for e in events:
        day = e["_ts"].strftime("%Y-%m-%d")
        by_day[day].append(e)

    print(f"\n{BOLD}按日期统计{RESET}")
    print(f"  {'日期':<14} {'成功':>6} {'失败':>6} {'锁屏':>6} {'解锁':>6}")
    print(f"  {'-' * 44}")

    for day in sorted(by_day.keys()):
        day_events = by_day[day]
        s = sum(1 for e in day_events if e["event"] in SUCCESS_EVENTS)
        f = sum(1 for e in day_events if e["event"] in FAIL_EVENTS)
        lk = sum(1 for e in day_events if e["event"] == "screen_locked")
        ul = sum(1 for e in day_events if e["event"] == "screen_unlocked")
        fail_str = f"{RED}{f}{RESET}" if f > 0 else str(f)
        print(f"  {day:<14} {s:>6} {fail_str:>{'16' if f > 0 else '6'}} {lk:>6} {ul:>6}")

    by_period = defaultdict(lambda: {"success": 0, "fail": 0})
    for e in events:
        period = _time_period(e["_ts"].hour)
        if e["event"] in SUCCESS_EVENTS:
            by_period[period]["success"] += 1
        elif e["event"] in FAIL_EVENTS:
            by_period[period]["fail"] += 1

    if by_period:
        print(f"\n{BOLD}按时段统计{RESET}")
        print(f"  {'时段':<18} {'成功':>6} {'失败':>6}")
        print(f"  {'-' * 32}")
        for period in ["上午 (06-12)", "中午 (12-14)", "下午 (14-18)", "晚上 (18-22)", "深夜 (22-06)"]:
            if period in by_period:
                s = by_period[period]["success"]
                f = by_period[period]["fail"]
                fail_str = f"{RED}{f}{RESET}" if f > 0 else str(f)
                print(f"  {period:<18} {s:>6} {fail_str:>{'16' if f > 0 else '6'}}")

    alerts = _detect_consecutive_failures(events)
    if alerts:
        print(f"\n{RED}{BOLD}! 连续失败告警{RESET}")
        for ts, count in alerts:
            print(f"  {RED}* {ts} 起连续 {count} 次认证失败{RESET}")

    recent = events[-15:]
    print(f"\n{BOLD}最近事件{RESET}")
    for e in recent:
        label, color = EVENT_LABELS.get(e["event"], (e["event"], ""))
        detail = f" -- {e['detail']}" if e.get("detail") else ""
        photo = f"  {e['photo']}" if e.get("photo") else ""
        print(f"  {DIM}{e['timestamp']}{RESET}  {color}{label}{RESET}{DIM}{detail}{RESET}{photo}")

    print(f"\n{DIM}日志文件: {LOG_FILE}{RESET}\n")


def _run_report(args):
    if args.report_all:
        since = None
        title = "全部认证事件报告"
    elif args.days:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=args.days - 1)
        title = f"最近 {args.days} 天认证事件报告"
    else:
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"今日认证事件报告 ({datetime.now().strftime('%Y-%m-%d')})"

    events = _load_events(since)
    _print_report(events, title)


# ── LaunchDaemon install/uninstall ───────────────────────────

PLIST_LABEL = "com.mac-toolbox.monitor"
PLIST_PATH = Path(f"/Library/LaunchDaemons/{PLIST_LABEL}.plist")


def _mt_executable() -> str:
    """返回当前 mt 可执行文件的绝对路径。"""
    import shutil
    path = shutil.which("mt")
    if path:
        return path
    # fallback: 与当前 Python 同目录
    return str(Path(sys.executable).parent / "mt")


def _install_launchdaemon():
    mt = _mt_executable()
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{mt}</string>
        <string>monitor</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{DATA_DIR}/launchd.out</string>
    <key>StandardErrorPath</key>
    <string>{DATA_DIR}/launchd.err</string>
</dict>
</plist>
"""
    if os.geteuid() != 0:
        print("Error: install 需要 sudo 权限。", file=sys.stderr)
        sys.exit(1)

    _ensure_data_dir()
    PLIST_PATH.write_text(plist)
    PLIST_PATH.chmod(0o644)

    subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)], check=True)
    print(f"已安装并启动 LaunchDaemon: {PLIST_PATH}")
    print(f"日志: {DATA_DIR}/launchd.out")
    print("开机将自动运行，无需 sudo mt monitor start。")


def _uninstall_launchdaemon():
    if os.geteuid() != 0:
        print("Error: uninstall 需要 sudo 权限。", file=sys.stderr)
        sys.exit(1)

    if PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH)], check=False)
        PLIST_PATH.unlink()
        print(f"已移除 LaunchDaemon: {PLIST_PATH}")
    else:
        print("未找到已安装的 LaunchDaemon。")


# ── CLI 注册 ─────────────────────────────────────────────────

def register(subparsers):
    """注册 monitor 子命令。"""
    p = subparsers.add_parser("monitor", help="macOS 锁屏登录监控")
    sp = p.add_subparsers(dest="action", help="操作")

    # mt monitor start
    start_p = sp.add_parser("start", help="启动监控")
    start_p.add_argument("--daemon", action="store_true", help="以守护进程方式后台运行")
    start_p.add_argument("--capture", action="store_true", help="认证失败时用前置摄像头拍照")
    start_p.set_defaults(func=_run_start)

    # mt monitor stop
    stop_p = sp.add_parser("stop", help="停止守护进程")
    stop_p.set_defaults(func=_run_stop)

    # mt monitor install
    install_p = sp.add_parser("install", help="安装为开机自启 LaunchDaemon（需要 sudo）")
    install_p.set_defaults(func=lambda args: _install_launchdaemon())

    # mt monitor uninstall
    uninstall_p = sp.add_parser("uninstall", help="移除开机自启 LaunchDaemon（需要 sudo）")
    uninstall_p.set_defaults(func=lambda args: _uninstall_launchdaemon())

    # mt monitor report
    report_p = sp.add_parser("report", help="查看认证事件报告")
    rg = report_p.add_mutually_exclusive_group()
    rg.add_argument("--days", type=int, metavar="N", help="显示最近 N 天的事件")
    rg.add_argument("--all", dest="report_all", action="store_true", help="显示所有事件")
    report_p.set_defaults(func=_run_report)

    p.set_defaults(func=lambda args: p.print_help())


def _run_start(args):
    global _camera_enabled
    if args.capture:
        _camera_enabled = True
    if args.daemon:
        _daemonize()
    else:
        _start_monitor()


def _run_stop(args):
    _stop_monitor()
