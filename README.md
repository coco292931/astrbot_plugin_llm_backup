# 对话备份

按会话与逻辑日记录 AstrBot 传给 LLM 的每一轮往来：用户输入、思考内容、工具流水与模型回复，并把用户或模型经由 AstrBot 原生工具发出的媒体、当前人设一并归档。

- 只记录真正调用过 LLM 的轮次，插件直接调用工具不会产生记录
- 以 JSONL 落盘，一行一轮，便于检索与二次处理
- 私聊字段经过裁剪，体积更小
- 媒体与人设按内容哈希去重，只保留首次出现的那一份
- 换日时间、空闲阈值、备份内容与详细程度均可配置

## 工作原理

插件只读取 AstrBot 的钩子事件，不读取、不修改 AstrBot 的会话历史，因此不会因为框架对上下文的滚动压缩而写出重复或错位的内容。

| 钩子 | 用途 |
| --- | --- |
| `on_llm_request` | 开启一轮记录，抓取用户输入、模型、对话、系统提示词 |
| `on_agent_begin` | 记录 Agent 上下文的起始位置 |
| `on_using_llm_tool` / `on_llm_tool_respond` | 记录工具名、参数、结果、耗时，并从 `send_message_to_user` 提取媒体 |
| `on_llm_response` | 记录最终思考、回复与 token 用量 |
| `on_agent_done` | 只从本轮新增上下文中还原中间步骤，然后落盘 |

不记录的内容：

- 发送前被其他插件改写后的最终文案（`on_decorating_result` 之后的内容）
- 子代理（subagent）内部产生的临时对话
- 未经过 LLM 的纯工具调用
- 工具内部直接发送、无法从工具入参还原的媒体

## 目录结构

```
<备份根目录>/
├── sessions/
│   └── aiocqhttp_FriendMessage_10001/
│       ├── _session.json                 # 会话信息与对话索引
│       └── 2026-11-03/
│           └── 3f2a1c9d.jsonl
├── files/
│   ├── 图片/2026-11-03/cat__a1b2c3d4.png
│   ├── 音频/2026-11-03/voice__9f8e7d6c.amr
│   └── 文档/2026-11-03/report__11223344.pdf
├── personas/
│   └── 猫娘/2026-11-03__9f8e7d6c.md
└── state/
    ├── rotation.json                     # 各会话当前逻辑日
    ├── media_index.json                  # 媒体哈希索引
    ├── persona_index.json                # 人设哈希索引
    └── pending/                          # 未完成轮次，重启后自动补写
```

- `sessions/<会话>/<日期>/` 由逻辑日决定，同一天内同一对话写入同一个文件
- `files/<大类>/<首次出现日期>/` 与 `personas/<人设>/` 按内容哈希去重，同一内容只保存一次
- 同一会话切换对话后，会写入不同的 JSONL 文件

## 快速开始

1. 把插件目录放入 `data/plugins/`
2. 打开插件配置，按需修改 `backup_root`（留空则为 `data/plugin_data/astrbot_plugin_llm_backup/backup`）
3. 在 `umos` 里选择要备份的会话；留空表示白名单不生效，但私聊/群聊开关仍然生效
4. 用 `/backup_status` 确认备份目录与当前逻辑日

## 命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/backup_status` | 管理员 | 查看备份目录、逻辑日、活动轮次、写队列、媒体与人设数量 |
| `/backup_now` | 管理员 | 立即把内存中的轮次落盘 |
| `/backup_rotate` | 管理员 | 强制把当前会话切换到下一个逻辑日目录 |
| `/backup_doctor` | 管理员 | 检查写权限、剩余空间与关键配置 |
| `/backup_debug` | 管理员 | 查看当前会话是否会被记录、跳过原因，以及最近的钩子判定 |

## 配置说明

### 基础

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 总开关 |
| `backup_root` | 空 | 备份根目录，支持 `~`、环境变量；相对路径以插件数据目录为基准 |
| `session_dir_template` | `{platform}_{message_type}_{session_id}` | 会话目录名 |
| `conversation_file_template` | `{conv_short}` | 对话文件名。加入 `{conv_title}` 更易读，但对话改名后会拆成新文件 |

Windows 与 Linux 通用：路径分隔符、非法字符、保留名、长度限制都会自动处理。

### 过滤

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `filter_mode` | `whitelist` | `whitelist` / `blacklist` / `all` |
| `umos` | 空 | 会话来源。白名单为空表示不限制；黑名单表示排除。支持 `*` 通配符，如 `aiocqhttp:FriendMessage:*` |
| `senders` | 空 | 只备份这些用户，留空表示不限 |
| `include_private` | 开 | 备份私聊 |
| `include_groups` | 关 | 备份群聊 |
| `include_webchat` | 开 | 备份 WebChat |
| `include_cron` | 关 | 备份定时任务触发的对话 |
| `include_plugin_initiated` | 关 | 备份其他插件主动发起的请求 |

### 内容详细度

`detail_level` 提供三档预设，`capture.*` 可以逐项覆盖，默认值 `follow` 表示跟随预设。

| 项目 | 精简 | 标准 | 完整 |
| --- | --- | --- | --- |
| `user_input` | 摘要 | 完整 | 完整 |
| `reasoning` | 不记录 | 摘要 | 完整 |
| `reply` | 摘要 | 完整 | 完整 |
| `tool_calls` | 不记录 | 摘要 | 完整 |
| `tool_results` | 不记录 | 不记录 | 记录 |
| `system_prompt` | 不记录 | 哈希 | 完整 |

截断上限在 `limits.*` 中设置，默认摘要 200 字、用户输入 8000 字、思考 4000 字、回复 8000 字、工具参数与结果各 2000 字。

`privacy.mask_patterns` 可以填入正则，命中内容在落盘前替换为 `***`。

### 媒体

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `media.enabled` | 开 | 是否归档媒体 |
| `media.store_bytes` | 开 | 关闭后只记录来源，不复制文件 |
| `media.capture_quoted` | 关 | 是否同样归档被引用消息中的媒体 |
| `media.max_file_mb` | 50 | 单文件上限，0 为不限 |
| `media.download_timeout_seconds` | 20 | 下载超时 |
| `media.categories` | 空 | 覆盖内置的扩展名分类，格式 `{"分类": ["扩展名"]}` |

内置分类：图片、音频、视频、文档、压缩包、代码、字体、其他。未知扩展名会尝试按文件头识别。

### 人设

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `persona.enabled` | 开 | 是否备份人设 |
| `persona.daily` | 开 | 每个逻辑日检查一次 |
| `persona.on_change` | 开 | 每次请求时比对哈希，变化即写新快照 |
| `persona.poll_minutes` | 30 | 空闲会话的轮询间隔，0 为关闭 |

快照文件名形如 `2026-11-03__9f8e7d6c.md`，日期为首次出现时间，后缀为内容哈希前 8 位。内容相同的快照不会重复写入。

### 换日

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `rotation.day_boundary` | `02:00` | 换日时间点 |
| `rotation.idle_minutes` | 120 | 切换所需的空闲时长 |
| `rotation.mode` | `both` | `both` / `lazy` / `scheduled` |
| `rotation.check_interval_seconds` | 60 | 定时检查间隔 |
| `rotation.precreate_next_day_folder` | 关 | 提前创建目录 |
| `rotation.max_turns_per_file` | 0 | 单文件轮次上限，超出自动分卷 |

规则：越过换日点、并且距上一次记录超过空闲阈值后，才切换到新目录。空闲时间以触发 LLM 的轮次为准。

```
11-03 23:00 有消息        -> 2026-11-03
11-04 01:30 仍在聊天      -> 2026-11-03（未过换日点）
11-04 02:30 仍在聊天      -> 2026-11-03（已过换日点，但未空闲）
11-04 05:00 已空闲 2h     -> 2026-11-04
```

`lazy` 仅在下一次写入时判定，`scheduled` 仅在定时任务里判定，`both` 两者都生效。

### 存储

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `storage.compress_jsonl` | 关 | 使用 gzip 压缩会话文件 |
| `storage.retention_days` | 0 | 保留天数，0 为永久 |

### 运行时

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `performance.turn_timeout_seconds` | 600 | 单轮无进展多久后强制落盘 |
| `performance.media_wait_seconds` | 5 | 落盘前等待媒体复制的上限 |
| `performance.finalize_debounce_seconds` | 20 | 收到回复后仍未结束时的兜底落盘延时，0 为关闭 |

会话活动时间由真正触发 LLM 的消息更新，插件不会注册普通消息处理器，因此不影响 AstrBot 原有的唤醒判定。

## 数据格式

`sessions/<会话>/<日期>/*.jsonl` 每行一轮，字段采用短名以节省空间。

| 字段 | 含义 |
| --- | --- |
| `ts` / `end` | 开始与结束时间，ISO 8601 含时区 |
| `conv` | 对话短 ID，对应 `_session.json` 中的 `cid` |
| `src` | `cron` 或 `plugin`，用户触发的轮次省略 |
| `gid` | 群号，仅群聊 |
| `who` | 发送者 `{id, n}`，私聊只保留 `id` |
| `in` | 用户输入 `{t: 文本, p: 生效提示词, m: 媒体}` |
| `req` | 请求信息 `{model, ctx: 上下文条数, tools: 工具数或列表, sp: 系统提示词}` |
| `steps` | 中间步骤 `[{r: 思考, t: 文本, tools: [...]}]` |
| `out` | 最终输出 `{r: 思考, t: 回复, m: 媒体}` |
| `u` | token 用量 `{i: 输入, o: 输出}` |
| `st` | 状态，正常时省略 |
| `note` | 备注，例如 `media_error=1`、`steps_unavailable` |

工具条目：`{n: 名称, a: 参数, r: 结果, ms: 耗时, ok: false, m: 媒体}`。

媒体统一写为相对备份根目录的 POSIX 路径，例如 `files/图片/2026-11-03/cat__a1b2c3d4.png`。

| `st` 取值：`degraded`（上下文被压缩，中间步骤可能缺失）、`err`（模型返回错误）、`superseded`（同一事件发起了新的请求）、`fallback`（未收到结束钩子，按回复兜底落盘）、`timeout`（超时兜底）、`manual`（手动落盘）、`interrupted`（进程重启）。

`_session.json` 保存会话元信息：

```json
{
  "v": 1,
  "umo": "aiocqhttp:FriendMessage:10001",
  "platform": "aiocqhttp",
  "msg_type": "FriendMessage",
  "session_id": "10001",
  "private": true,
  "name": "alice",
  "created": "2026-11-03T14:00:00+08:00",
  "conversations": {
    "3f2a1c9d": {"cid": "…", "title": "默认对话", "persona": "猫娘"}
  }
}
```

## 存储与体积

- 默认不写完整上下文与系统提示词，只记录条数与哈希；需要完整提示词时把 `capture.system_prompt` 设为 `full`
- 媒体按 SHA-256 全局去重，同名不同内容、同内容不同名都能正确合并
- 需要更小的体积可以开启 `storage.compress_jsonl`，或把 `detail_level` 调低
- 单文件过大时用 `rotation.max_turns_per_file` 自动分卷
- `storage.retention_days` 会按日期目录清理过期数据

## 已知限制

- 工具内部直接 `send` 给用户的媒体无法还原，只能记录工具返回的文本
- 上下文被框架压缩时，早期中间步骤的思考可能丢失，该轮会标记为 `degraded`，工具流水与最终回复仍然完整
- 子代理对话不记录
- 人设改动在请求时或轮询时被发现，不是实时事件
- 平台提供的临时媒体链接可能过期，复制失败会记录为 `media_error`

## 排查：没有生成文件

先发一条会触发 LLM 的消息，再执行 `/backup_debug`。它会显示当前事件的 UMO、平台、是否私聊、本次判定结果，以及最近若干次钩子判定。

| 判定 | 含义 | 处理 |
| --- | --- | --- |
| `opened` | 已开启一轮记录 | 正常，回复结束后会写入 |
| `service_not_started` | 插件初始化未完成 | 查看日志中 `[llm_backup]` 开头的报错并修复配置后重载 |
| `plugin_disabled` | `enabled` 为关 | 打开总开关 |
| `umo_not_in_whitelist` | UMO 与白名单不匹配 | 复制 `/backup_debug` 显示的 UMO 到 `umos`，或改用通配符 |
| `umo_in_blacklist` | 命中黑名单 | 从 `umos` 中移除 |
| `group_disabled` | 群聊未开启 | 打开 `include_groups` |
| `private_disabled` | 私聊未开启 | 打开 `include_private` |
| `cron_filtered` | 定时任务触发的对话 | 需要时打开 `include_cron` |
| `plugin_initiated_filtered` | 其他插件主动发起的请求 | 需要时打开 `include_plugin_initiated` |
| `sender_not_in_list` | 发送者不在 `senders` 中 | 清空或补充该用户 |
| `no_active_turn` | 结束钩子没找到对应轮次 | 说明开启阶段被过滤，按上一条判定排查 |

日志中同一条 `[llm_backup] <钩子> 跳过: <原因>` 只会出现一次；每写入一条记录会输出 `[llm_backup] 写入 <路径>`。

如果 AstrBot 版本缺少 `on_agent_done`，插件会在回复后 `finalize_debounce_seconds` 秒兜底落盘，记录标记为 `st: fallback`；`/backup_doctor` 会显示缺失的钩子。

## 测试

```bash
python tests/run_all.py
```

测试覆盖路径与文件名处理、换日规则、各类消息组件兼容性、媒体分类与去重、会话命名、记录构建、写入队列与端到端流程。
