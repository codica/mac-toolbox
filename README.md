# mac-toolbox

Mac 个人工具箱，统一命令行入口 `mt`。

## 安装

```bash
pip install -e .
```

## 工具

### awake — 阻止锁屏休眠

阻止 Mac 在锁屏/合盖时进入休眠，支持定时和电量监控。

```bash
mt awake                        # 无限期，电量<30%时停止
mt awake -t 60                  # 60 分钟
mt awake -t 120 -b 20           # 120 分钟，电量<20%时停止
mt awake --no-battery           # 不监控电量
```

### monitor — 锁屏登录监控

通过 macOS `log stream` 实时监控认证事件（密码/Touch ID），记录成功和失败的登录尝试，可选在认证失败时拍照。

```bash
sudo mt monitor start                    # 前台启动
sudo mt monitor start --daemon --capture # 后台守护 + 失败拍照
sudo mt monitor stop                     # 停止守护进程
mt monitor report                        # 今日报告
mt monitor report --days 7               # 最近 7 天
mt monitor report --all                  # 全部记录
```

数据存储在 `~/.mac-toolbox/monitor/`。

## 添加新工具

1. 在 `mac_toolbox/tools/` 下新建模块
2. 实现 `register(subparsers)` 函数注册子命令
3. 在 `mac_toolbox/cli.py` 的 `TOOLS` 字典中添加导入
