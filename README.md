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

通过 macOS `log stream` 实时监控系统级事件，包括认证（密码/Touch ID）、屏幕解锁、休眠与唤醒，可选在认证失败时拍照，支持 Telegram 实时通知。

```bash
sudo mt monitor start                    # 前台启动
sudo mt monitor start --daemon           # 后台守护进程
sudo mt monitor start --daemon --capture # 后台守护 + 失败拍照
sudo mt monitor stop                     # 停止守护进程
mt monitor report                        # 今日报告
mt monitor report --days 7               # 最近 7 天
mt monitor report --all                  # 全部记录
```

#### 监控的系统事件

| 事件 | 来源进程 | 说明 |
|------|----------|------|
| 屏幕锁定 | `loginwindow` | 手动锁屏或合盖触发 |
| 屏幕解锁 | `loginwindow` | 密码或 Touch ID 解锁成功 |
| 密码认证成功/失败 | `authd` / `opendirectoryd` | 锁屏界面输入密码 |
| Touch ID 认证成功/失败 | `coreauthd` | 锁屏界面触摸 Touch ID |
| 进入休眠 | `powerd` | 合盖或系统主动休眠 |
| 从休眠恢复 | `powerd` | 开盖或按键唤醒 |

> 底层使用 `log stream --predicate ... --level debug` 订阅内核级日志，需要 `sudo` 权限。

#### Telegram 通知

配置后，以下事件会实时推送到 Telegram：

- 🔓 **屏幕解锁** — 1 分钟内最多通知一次
- 💻 **从休眠恢复** — 1 分钟内最多通知一次

**配置步骤：**

1. 在 Telegram 通过 [@BotFather](https://t.me/BotFather) 创建 Bot，获取 Token
2. 给 Bot 发一条消息，然后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取 Chat ID
3. 写入配置文件：

```bash
sudo tee ~/.mac-toolbox/config.json > /dev/null << 'EOF'
{
  "telegram": {
    "token": "YOUR_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  }
}
EOF
```

> 配置文件路径 `~/.mac-toolbox/config.json`。若目录由 `sudo` 创建，写入时同样需要 `sudo`。

#### 数据存储

```
~/.mac-toolbox/
├── config.json          # Telegram 等配置
└── monitor/
    ├── auth_events.log  # 所有事件 JSON 日志
    ├── monitor.pid      # 守护进程 PID
    ├── monitor.out      # 守护进程输出日志
    └── captures/        # 认证失败拍照（--capture 模式）
```

## 添加新工具

1. 在 `mac_toolbox/tools/` 下新建模块
2. 实现 `register(subparsers)` 函数注册子命令
3. 在 `mac_toolbox/cli.py` 的 `TOOLS` 字典中添加导入
