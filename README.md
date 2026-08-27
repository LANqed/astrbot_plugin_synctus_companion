# 它也在 Synctus 里（astrbot_plugin_synctus_companion）

把 Bot 变成 Synctus 房间里的一台设备。

它的一天来自 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)
每天生成的日程；这个插件把日程翻译成 Synctus 的状态字段，于是你在电脑悬浮窗
或手机通知栏里看到的它，和看一个真人搭档没有区别：

```
小澈 · 手机   在忙
备忘录：赶稿子
🔋68%  🍅▶专注 41:20  ☑4/10
```

它在专注时你可以点「一起专注」，它在摸鱼时「别摸鱼了」按钮会亮起来，
点了它会在 QQ 私聊里回你一句。日程切换的时候（早安、去赶稿、干饭、晚安），
它也会顺手来 QQ 说一声。

## 它怎么变成一个"人"

| 你看到的 | 从哪来 |
| --- | --- |
| **在忙 / 休息中 / 离开 / 免打扰** | 日程段的性质：睡觉→休息中，吃饭通勤→离开，专注→在忙；精力低于 25 时专注段降为免打扰 |
| **正在做什么** | 日程活动映射成前台应用与窗口标题：赶稿子→「备忘录：赶稿子」，刷视频→「bilibili」，睡觉→「锁屏」 |
| **手机电量** | 按日程积分出的曲线：睡觉插着充电器，专注时掉得慢，摸鱼刷视频掉得快，低于 18% 会去充。同一天多次读取结果相同，不会上下乱跳 |
| **番茄钟** | 专注段就是一轮进行中的番茄钟，截止时间是这一段的结束时间。只同步截止时间戳，对端本地插值 |
| **今日专注 / 目标** | 已经走过的专注段时长 / 今天全部专注段总时长，于是一天结束时进度条正好走满 |
| **待办清单** | 重要日期变成带 deadline 的任务（「交稿（还剩 2 天）· 专栏」），日程段变成一件件事并随时间推进被勾掉，`can_do` 补齐零碎小事 |
| **敲一敲的回应** | 对端的互动转达到 QQ 私聊，并带上「我正在赶稿子」。「别摸鱼了」允许穿透静音 |

## 数据从哪来

```
astrbot_plugin_private_companion（可选依赖）
    │  运行实例的 data 字典 ─┐
    │  companions.json    ─┴─► 今日日程 / 拟人状态 / 重要日期 / 可做事项
    ▼
companion_bridge.py   归一化成首尾相接、覆盖 1440 分钟的日程段
    ├─ presence.py    presence + 前台应用 + 番茄钟 + 专注分钟
    ├─ battery.py     电量曲线
    └─ tasks.py       待办清单
    ▼
synctus/（纯 Python 客户端，端到端加密）→ 中继 → 你的电脑 / 手机
```

优先读**运行中的实例**（内存里就是最新的，不受落盘时机影响），失败时读
**数据文件**兜底。两条路都读不到时退回内置默认作息，插件仍然可用——
依赖插件是可选的，不是必需的。

全程只读，不修改依赖插件的任何数据。

## 安装

先装两个加密依赖，装在 **AstrBot 所用的那个 Python 环境**里：

```sh
pip install argon2-cffi pynacl
# Docker: docker exec -it astrbot pip install argon2-cffi pynacl && 重启容器
```

### 从 Release 下载 ZIP（推荐）

[Releases](https://github.com/LANqed/Synctus/releases) 里找 `AstrBot 插件` 开头
的发布，下载 `astrbot_plugin_synctus_companion-v*.zip`，然后
AstrBot WebUI → 插件管理 → **从本地上传**。

Release 里的 ZIP 由 CI 打包并逐项校验过：顶层目录名与 `metadata.yaml` 一致、
不含测试与字节码缓存、解开后能被真实导入并渲染出状态文本。

### 自己打包

改过代码、或想装未发布的版本：

```sh
python scripts/pack_astrbot_plugin.py
# → dist/astrbot_plugin_synctus_companion-v1.0.0.zip（约 42 KB）

# 可选：跑一遍 CI 用的同一份校验
python scripts/verify_astrbot_plugin_zip.py dist/astrbot_plugin_synctus_companion-v1.0.0.zip
```

手工压缩也行，但要注意压的是**目录本身而不是目录里的文件**——解开后必须能看到
`astrbot_plugin_synctus_companion/main.py` 这样的路径，否则 AstrBot 识别不了：

```sh
zip -r plugin.zip astrbot_plugin_synctus_companion \
    -x '*/tests/*' -x '*/__pycache__/*'
```

### 直接复制目录

不想经过 ZIP 的话，把 `astrbot_plugin_synctus_companion` 整个目录放进插件目录，
目录名不能改：

```
Windows: C:\Users\<用户名>\.astrbot\data\plugins\astrbot_plugin_synctus_companion
Linux:   ~/.astrbot/data/plugins/astrbot_plugin_synctus_companion
Docker:  <挂载出来的 data>/plugins/astrbot_plugin_synctus_companion
```

然后在 WebUI 里重载插件。`tests/` 不必复制。

## 配置

在 AstrBot WebUI 的插件配置里：

- **target_users**：QQ 号或完整会话标识。不填则不发 QQ 消息，只做 Synctus 上报。
- **synctus.server** 与 **synctus.invite_code**：与你自己设备上填的**完全一致**，
  否则会进到不同的房间。配对码是密钥材料，不要外传。
- **synctus.synctus_user**：留空使用 Bot 名字。与你自己设备的昵称不同即可，
  这样管理面板与对端界面会把你们显示为两个人。

其余都有合理默认值。**bot_name** 留空会自动读依赖插件的配置。

装完后私聊发 `陪伴 状态`：能看到活动、电量、待办和「Synctus 上报：已连接」
就说明整条链路通了。若显示「未连接」，`陪伴 连接` 会给出具体原因。

## 命令

```
陪伴 状态     现在在做什么、手机电量、今日专注、待办进度、上报状态
陪伴 日程     今天的完整日程（标出当前段与日程来源）
陪伴 待办     待办清单与临近的重要日期
陪伴 电量     手机电量与预计可用时间
陪伴 连接     Synctus 上报状态与房间标识前缀
陪伴 静音     暂停主动消息（只影响发命令的人）
陪伴 恢复     恢复主动消息
日程 / 待办    等价于对应子命令
```

## 主动消息的边界

- 日程切换汇报：随机延迟 10~180 秒，优先用日程里的 `message_seed`
- 随机搭话：只在可打扰的段（非睡眠、非专注），默认每分钟 0.6% 概率
- 每人每天上限 12 条，随机搭话最小间隔 45 分钟
- 免打扰时段默认 23:00-08:00，期间不随机搭话（日程的晚安/早安不受影响）
- 插件重启后第一次观察不会立刻播报当前段，避免每次重载都打扰

## 安全性

载荷用 XChaCha20-Poly1305 端到端加密，中继读不到 Bot 的任何状态。
房间密钥由配对码经 Argon2id + HKDF-SHA256 派生，参数与 Rust 客户端逐位一致，
由 `tests/test_crypto_vectors.py` 里的跨语言测试向量固定。

`tls_verify` 默认开启。关掉它只应发生在可信网络里连自签中继的场景：
载荷仍然加密，但会失去对中继身份的验证。

## 测试

```sh
# 逻辑与加密向量（无需 Rust）
python -m pytest astrbot_plugin_synctus_companion/tests -q

# 想跑与真实中继联通的集成测试，先构建中继
cargo build -p synctus-server --bin synctus-server
```

未构建中继时相关测试自动跳过。测试用 AstrBot 的最小替身驱动插件，
不需要真的跑一个 AstrBot。

## 发布

插件版本在 `metadata.yaml`，与 Synctus 本体的 `Cargo.toml` 版本无关，
所以它有独立的发布流程（`.github/workflows/astrbot-plugin.yml`）：

```sh
# 方式一：GitHub Actions → AstrBot plugin → Run workflow
# 方式二：推标签，标签必须与 metadata.yaml 的 version 一致
git tag astrbot-plugin-v1.0.0 && git push origin astrbot-plugin-v1.0.0
```

两种方式都会先跑 lint 与全部测试（含与真实中继联通的集成测试），
打包、校验归档可导入，最后把 ZIP 与 `SHA256SUMS.txt` 附到 Release。
