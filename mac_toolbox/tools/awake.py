"""awake - 阻止 Mac 在锁屏状态下进入休眠。"""

import subprocess
import signal
import sys
import time
import re


def get_battery_percent() -> int | None:
    """获取当前电池电量百分比，台式机返回 None。"""
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True)
        m = re.search(r"(\d+)%", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def is_on_ac_power() -> bool:
    """检查是否正在充电。"""
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True)
        return "AC Power" in out
    except Exception:
        return False


def register(subparsers):
    """注册 awake 子命令。"""
    p = subparsers.add_parser("awake", help="阻止 Mac 在锁屏时休眠")
    p.add_argument("-t", "--time", type=int, default=0,
                   help="阻止休眠的时长（分钟），0 表示无限期")
    p.add_argument("-b", "--battery", type=int, default=30,
                   help="电量低于此百分比时停止阻止休眠（默认 30）")
    p.add_argument("--no-battery", action="store_true",
                   help="不监控电池电量")
    p.set_defaults(func=run)


def run(args):
    """执行 awake 工具。"""
    duration_min = args.time
    battery_threshold = args.battery
    check_battery = not args.no_battery

    # caffeinate -s: 阻止系统休眠（即使屏幕关闭/锁定）
    # caffeinate -i: 阻止空闲休眠
    caffeinate_cmd = ["caffeinate", "-s", "-i"]
    if duration_min > 0:
        caffeinate_cmd += ["-t", str(duration_min * 60)]

    print("已启动，阻止系统休眠")
    if duration_min > 0:
        print(f"  时长限制: {duration_min} 分钟")
    else:
        print(f"  时长限制: 无（手动 Ctrl+C 停止）")
    if check_battery:
        print(f"  电量阈值: {battery_threshold}%")
    else:
        print(f"  电量监控: 已禁用")

    proc = subprocess.Popen(caffeinate_cmd)

    def cleanup(signum=None, frame=None):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("\n已停止，系统恢复正常休眠策略")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    start_time = time.time()
    check_interval = 60  # 每 60 秒检查一次

    try:
        while True:
            # 检查 caffeinate 是否仍在运行（时间到了会自行退出）
            ret = proc.poll()
            if ret is not None:
                print("指定时间已到，系统恢复正常休眠策略")
                break

            # 检查电池电量
            if check_battery:
                pct = get_battery_percent()
                if pct is not None:
                    elapsed = int((time.time() - start_time) / 60)
                    on_ac = is_on_ac_power()
                    power_src = "充电中" if on_ac else "电池"
                    print(f"  [{elapsed}分钟] 电量: {pct}% ({power_src})",
                          end="\r", flush=True)

                    if not on_ac and pct < battery_threshold:
                        print(f"\n电量 {pct}% 低于阈值 {battery_threshold}%，停止阻止休眠")
                        proc.terminate()
                        proc.wait(timeout=5)
                        break

            time.sleep(check_interval)
    except Exception as e:
        print(f"\n异常: {e}")
        cleanup()

    print("已退出")
