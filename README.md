# 吃白饭的肥鱼 · QQ / 微信 自动回复机器人

一只**会偷吃 token 的蓝色虎鲸娘**，同时接进 QQ 和微信陪你聊天。

她叫「吃白饭的肥鱼」——圆脸蓝瞳、蓝紫渐层长发、耳侧一对小鲸鱼鳍、深蓝色女仆装，
把 token 当脑力口粮，一边嘴上说着"本鱼只是路过"，一边把你的每句话都记进小账本。

- 模型：`deepseek-v4.1-flash-free`，备选 `deepseek-v4.1-flash-1017`（OpenAI 兼容接口，默认指向本机 `http://127.0.0.1:8792/v1`）
  —— 这个模型是老毛病：思考一大堆、正文常常是空的。代码里加了一层「思考回收」，会把话从思考里捞回来，见下文
- 接入方式：**QQ 走 OneBot v11**（NapCat），**微信走 wechatauto 桥接**（不注入、不改微信文件）
- 两个通道共用同一套人设、记忆、命令、限流与自主活动
- 依赖只有四个：`httpx`、`websockets`、`aiosqlite`、`PyYAML`，没有重框架

## 先改这两行，不然连不上

仓库里的 `config.example.yaml` 默认指向**我本地**的模型代理 `http://127.0.0.1:8792/v1`，
你那边肯定没有这个服务。复制成 `config.yaml` 之后改三处就能跑：

```yaml
llm:
  base_url: https://api.deepseek.com/v1   # 任何 OpenAI 兼容服务都行（官方 / 中转 / Ollama）
  api_key: sk-你自己的key
  model: deepseek-chat                    # 换成你手上有的模型名
```

两点提醒：

- `llm.extra_body` 里那几项（`reasoning_effort`、`chat_template_kwargs`）是给
  DeepSeek 系模型「少想两句」用的，**换个上游不认就整段删掉**，不影响其它功能。
- `llm.fallback_models` 是主模型吐不出正文时的兜底，也按你有的模型名改。

另外两个只有你自己知道的值：

| 要改的地方 | 在哪 |
| --- | --- |
| NapCat 目录、机器人小号 QQ | `boot_all.bat` / `boot_qq.bat` / `start_qq_bot.bat` 顶部的 `NAPCAT_DIR`、`BOT_UIN` |
| 管理员 QQ（能用 `/闭嘴` `/重载`） | `config.yaml` 的 `admin.users` |
| 看门狗报备给谁发消息 | `watchdog.py` 顶部的 `OWNER_QQ` |

---

## 她会什么

| 能力 | 说明 |
| --- | --- |
| 私聊 / 群聊自动回复 | 群里**不用 @ 也接话**（有人在问、在聊她时）；@ 她或叫她「肥鱼」则一定接 |
| **双通道** | 同一时刻在 QQ 和微信上都在线，各自独立记忆，策略共用 |
| **发语音** | 可以真的开口说话（edge-tts 合成 → NapCat 转 silk），见下文 |
| 随机插话 | 别人聊天时按概率接话（像在跟她说话 85%，纯闲聊 35%），觉得吵就调低或设成 0 |
| 群聊环境记忆 | 把群里最近的聊天记录带进上下文，接话不会答非所问 |
| 流式打字机 | 边生成边把说完的句子发出去，长回复自动拆成多条短消息 |
| 会话记忆 | SQLite 持久化，重启不丢；按会话隔离，超时可裁剪 |
| 偷吃 token 账本 | 每次调用都记账，`/token` 随时查"今天偷吃了多少" |
| 管理员开关 | `/闭嘴` `/起床` `/重载`，非管理员会被她用嘴硬的方式怼回去 |
| **自主活动** | 没人理她的时候会自己改签名、写心情、隔几小时主动找人聊天，见下文 |
| **会上网** | 自己判断该不该查，会搜索也会读网页，然后用她的口气讲给你听，见下文 |
| **能看见图** | 别人发的图她是真的看得见：图里的东西、图上的字都能讲出来，而且不用 @ 她，见下文 |
| **会发表情包** | 按情绪挑图，句子后面跟 `[[表情:开心]]`，偶尔还会自己补一张，见下文 |
| **借来的本事** | 会用本地 skill 里的思路（恋爱军师、造人术…），但只借思路、用自己的话说，见下文 |
| **能自己动手** | 会截图、报机器状况、把旧对话压成摘要腾地方，见下文 |
| **常开自愈** | 看门狗每 12 小时体检一次，掉了自动拉起来并 QQ 报备，见下文 |
| 本地控制台模式 | `--simulate` 不接 QQ 也能聊天调人设 |
| 断线自愈 | 正向 WS 断了按指数退避重连；发送失败不会让进程崩掉；进程级心跳文件 |

---

## 目录结构

```
qq-fish-bot/
├── fishbot/
│   ├── __main__.py     CLI 入口（--check / --simulate / --version）
│   ├── autonomy.py     自主活动：自己改签名 / 写心情 / 主动找人
│   ├── bot.py          核心：触发判定、记忆组装、流式分句、命令
│   ├── config.py       配置加载（yaml + 环境变量覆盖）
│   ├── llm.py          OpenAI 兼容客户端（流式 / 非流式 / 重试 / usage）
│   ├── memory.py       SQLite 会话记忆 + token 账本
│   ├── net.py          联网：搜索（bing/百度/搜狗，带相关性过滤）+ 抓正文
│   ├── stickers.py     表情包：清单加载、标记解析、挑图、冷却与限流
│   ├── tools.py        干活：截图、系统信息、标记解析与限流
│   ├── onebot.py       OneBot v11 正/反向 WebSocket 客户端（QQ）
│   ├── wechat.py       微信通道：拉起 wx_bridge.py 子进程、翻事件、发消息
│   ├── router.py       通道路由：数字目标走 QQ，其余走微信
│   ├── tts.py          语音合成（edge-tts 首选，SAPI 兜底）
│   ├── persona.py      人设拼接（平台、状态、饥饿度、群聊环境、临时提醒）
│   ├── textutil.py     CQ 码解析、输出清洗、emoji 过滤、分句、代理对清洗
│   └── console.py      本地控制台调试通道
├── personas/feiyu.md   人设正文 ← 想改性格就改这个文件
├── assets/character.md 角色形象设定 + 出图提示词
├── assets/stickers/    表情包素材 + manifest.json
├── watchdog.py         看门狗：体检 QQ / 微信 / 机器人，按需修复并报备
├── tests/
│   ├── mock_openai.py  假模型服务（含 SSE 流式）
│   ├── mock_onebot.py  假 QQ 端（正向 WS 服务端）
│   └── test_e2e.py     端到端测试（20 项，全绿）
├── boot_all.bat        一键全启：QQ + NapCat + 机器人 + 微信桥接
├── boot_qq.bat         只拉 QQ/NapCat 那一段（看门狗修 QQ 时用）
├── run_bot.bat         只拉机器人（看门狗修机器人时用，自动清干净代理变量）
├── stop_bot.bat        暂停按钮：停机器人 + 暂停看门狗
├── config.yaml         实际配置（已填好模型与本机 key）
├── config.example.yaml 配置模板
├── requirements.txt
├── run.bat / run.sh    一键启动（自动建虚拟环境装依赖）
└── data/               记忆库、心跳、看门狗日志、截图（首次运行自动创建）
```

---

## 快速开始

### 1. 确认模型服务在跑

```powershell
# 探测本机接口
python -m fishbot --check
```

`--check` 会做三件事：查 `/models` 列表、确认目标模型存在、真的聊一句并打印 token 用量。
正常输出类似：

```
[模型服务] ✔ 可用 - /models 可用，共 58 个模型 （已包含目标模型）
[试聊天] 发送：你好呀，你是谁？
[试聊天] 回复：咕噜噜~你好呀！本鱼是路过你聊天窗口的蓝色虎鲸娘，顺便来偷一口 token 吃~ 嗝，好饱！
[试聊天] 用量：{'prompt_tokens': 925, 'completion_tokens': 78, 'total_tokens': 1003}
```

如果报 401，说明 key 不对：本机 `QApiLocalProxy` 的 key 存在
`%APPDATA%\dsh-desktop\harness\.credentials.yaml` 的 `Q_API_KEY` 字段里，
填进 `config.yaml` 的 `llm.api_key`，或者用环境变量 `FISHBOT_LLM_API_KEY` 覆盖。

### 2. 装依赖

```powershell
.\run.bat              # Windows：自动建 .venv、装依赖、启动
# 或者手动
python -m pip install -r requirements.txt
```

### 3. 先不接 QQ，试聊两句

```powershell
python -m fishbot --simulate
```

```
   ∧___∧      吃白饭的肥鱼 · QQ 自动回复机器人
  (  ·ω· )    "本鱼只是路过，顺便偷一口 token。"
   \  ~  /     version 1.0.0

本地控制台模式：直接输入消息和她聊天，输入 /quit 退出。

肥鱼> 你好呀~ 本鱼是一条蓝色虎鲸娘，住在聊天窗口里白吃白喝……
```

### 4. 接入 QQ（NapCat）

> 需要一个**小号 QQ**。NapCat 在 Windows 上配合 QQNT 使用。

#### 本机已经装好的一份（直接用这个）

| 项 | 值 |
| --- | --- |
| NapCat 版本 | v4.18.38（`NapCat.Shell`） |
| 安装位置 | `<NapCat 目录>\shell` |
| QQ 客户端 | `C:\Program Files\Tencent\QQNT\QQ.exe`（9.9.15-27597） |
| 机器人账号 | `123456789`（昵称 aono） |
| OneBot 端口 | `127.0.0.1:3001`（NapCat 当服务端） |
| NapCat WebUI | <http://127.0.0.1:6099> |
| 配置文件 | `<NapCat 目录>\shell\config\onebot11_123456789.json` |

**一键全启：双击项目根目录的 `boot_all.bat`**（QQ + NapCat + 机器人 + 微信桥接一次到位）

它会依次：杀掉旧 QQ → 用 NapCat 拉起 QQ 并快速登录小号 → 等 3001 端口（最多约 75 秒）
→ 启动 fishbot（含微信桥接）。日志写在 `data\boot.log`。
只想要 QQ 那一段（看门狗修 QQ 时用的就是它）就双击 `boot_qq.bat`。

**手动启动（等价于上面三步）**

```bat
:: 1. 关掉旧 QQ（NapCat 注入必须在 QQ 未运行时进行）
taskkill /f /im QQ.exe

:: 2. 用 NapCat 启动 QQ 并指定账号
cd /d <NapCat 目录>\shell
launcher-user.bat 123456789

:: 3. 另一个窗口启动机器人
cd /d <仓库目录>
.venv\Scripts\python.exe -m fishbot
```

**NapCat 侧的关键配置**（`config\onebot11_123456789.json`）

必须是 **WebSocket 服务端**（`websocketServers`），不是客户端：

```json
{
  "network": {
    "websocketServers": [{
      "name": "fishbot-server",
      "host": "127.0.0.1",
      "port": 3001,
      "messagePostFormat": "array",
      "reportSelfMessage": false,
      "token": "",
      "enable": true,
      "debug": false,
      "heartInterval": 30000
    }]
  }
}
```

- `messagePostFormat` 必须是 `array`，`string` 模式机器人解不了消息段
- `token` 留空就和 `config.yaml` 的 `onebot.access_token: ""` 对应；要设就两边一起改
- 改完配置要**重启 NapCat** 才生效

#### 换成别的账号 / 换机器

1. 把 `BOT_UIN` 改成目标 QQ 号（`boot_all.bat` 与 `boot_qq.bat` 顶部各有一处）
2. 第一次登录新号通常要扫码：NapCat 窗口里会出二维码，用手机 QQ 扫
3. 扫完 `config\onebot11_<QQ号>.json` 会自动生成，把上面的 `websocketServers` 段照抄进去再重启

#### 方式 B：反向 WebSocket（不想让 NapCat 开端口时）

1. `config.yaml` 改成：
   ```yaml
   onebot:
     mode: reverse
     listen_host: 0.0.0.0
     listen_port: 8080
     listen_path: /onebot/v11/ws
     access_token: "fishbot123"
   ```
2. 先启动机器人，再在 NapCat 里配 **WebSocket 客户端**：
   - URL：`ws://127.0.0.1:8080/onebot/v11/ws`（路径不能少，机器人按路径校验）
   - 消息格式：array，Token 同上

### 5. 把自己设成管理员

```yaml
admin:
  users: [你的QQ号]      # 可以写多个
```

管理员才能用 `/闭嘴`、`起床`、`重载`；群里的群主/管理员也自动拥有权限。

---

## 命令表

| 命令 | 作用 | 权限 |
| --- | --- | --- |
| `/help` | 列出她能听懂的咒语 | 所有人 |
| `/token` | 看她今天 / 本会话 / 一共偷吃了多少 token | 所有人 |
| `/人设` | 复述自己的名字、别名和人设文件路径 | 所有人 |
| `/状态` | 运行时长、QQ 连接状态、事件/回复/跳过计数 | 所有人 |
| `/喂` | 投喂一口 token，计数会一直记着 | 所有人 |
| `/重置` | 吃掉这段会话的记忆（群聊需管理权限） | 私聊所有人 / 群管理 |
| `/闭嘴` | 本会话静音，被 @ 也不回 | 管理员 |
| `/起床` | 解除静音 | 管理员 |
| `/重载` | 热重载 `config.yaml` 与人设文件，不用重启 | 管理员 |
| `/玩` | 让本鱼立刻自己去玩一次 QQ（不等定时） | 管理员 |
| `/语音` | 切换语音回复：不带参数=开关，`说 <内容>`=念一句，`关`=闭嘴打字 | 所有人 |
| `/搜 关键词` | 让她上网查一下，再用自己的口气讲给你听 | 所有人 |
| `/看 网址` | 让她读一下这个网页，然后讲人话 | 所有人 |
| `/看图` | 让她说说你上一张图里有什么（`/看图 <图片链接>` 看网图） | 所有人 |
| `/截图` | 让她截一张屏幕发出来（截图有冷却与每小时上限） | 所有人 |
| `/系统` | 报一下这台机器：开机多久、内存、磁盘 | 所有人 |
| `/压缩` | 把本会话聊过的旧消息压成一小段摘要，腾地方 | 所有人 |
| `/技能` | 看她手里借了几套本事 | 所有人 |
| `/技能 名字` | 看某一份本事的开头（比如 `/技能 goutoujunshi`） | 所有人 |

命令前缀可以在 `admin.command_prefix` 里改（比如换成 `.` 或 `#`）。
命令走的是独立通道：**不进模型、不记历史、不消耗 token**。

---

## 看见东西（`vision`）

别人发的图片，她是**真的看得见** —— 图里有什么、图上写了什么字，都能给你讲出来。

- 发图再补一句「你看看这是啥」，她会先描述图，再带一句自己的反应。
- 也可以 `/看图`（说说你上一张图）或 `/看图 <图片链接>`（让她去看网上的图）。
- 一条消息里最多看 `vision.max_images` 张；同一张图按内容哈希缓存，短时间内不重复看（省 token）。
- 看不清、图太糊她会直说，**不会硬编**。

链路是这样的：

```
图片段（file / url）
  → 本地路径直接读
  → 否则让 NapCat get_image 换出本机文件
  → 再不行按 url 下载
  → 压成 data:<mime>;base64,… 塞进 image_url 消息段
  → 交给 vision.model 描述
  → 描述当成「（图：…）」写进她这一轮的上下文
```

实测过的看图模型（都是真跑，同一批真图）：

| 模型 | 用时 | 描述质量 |
| --- | --- | --- |
| **`deepseek-v4.1-flash-free`** | 10～46s | 看得懂，连图上的字都认出来；但**约一半轮次正文是空的**，只有思考 ← 当前用的 |
| `qwen3.8-flash-low` | 5～13s | 稳，描述细；偶尔拖到 40s |
| `deepseek-v4.1-flash-1017` | 4～19s | 快，抓得住重点 |
| `gpt-6.1-sol-fast×8` | 5.5s | 4/4 全对，连四格漫画里的字都读对了（最准也最快，可它爱道歉、像客服） |
| `gpt-6-astra-fast×8` | 5.5s | 3/4 对 |
| `gpt-6-sol-stable-3-fast×2` | 6.8s | 3/4 对（漫画会认错） |

把代理上 60 个模型全扫了一遍（每个都配 5 次重试辨真假），结论：

- **真能看图 19 个**：`gpt-6-astra*`、`gpt-6-sol-stable-*`、`gpt-6.1-sol-*`、`qwen3.8-flash-low`、`qwen3.8-max-low`、`glm-5.3-flash`、`deepseek-v4.1-flash`、`deepseek-v4.1-flash-free`。
- **装样子的 2 个**：`deepseek-v4-flash-low`、`deepseek-v4-flash-cheap` —— 返回 200、也有正文，但正文是「我无法读取这张图片」。
- **明确不看图 3 个**：`deepseek-v4-pro`、`deepseek-v4-pro-cheap`、`deepseek-v4-flash` —— 带图 400，纯文本 200 正常。
- 剩下 34 个连纯文本都 400/503（通道全挂），没法判定。

默认用 `deepseek-v4.1-flash-free` 的理由：聊天和看图同一个模型，少一个要操心的东西；
它正文空的时候，`vision.fallback_models` 会自己往下顺。

配置（`config.yaml` 的 `vision:` 段）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉就完全不看了 |
| `model` | `deepseek-v4.1-flash-free` | 看图用的模型（和聊天同一个） |
| `fallback_models` | `[qwen3.8-flash-low, deepseek-v4.1-flash-1017]` | 前一个不吐正文、或说没看到图，就换下一个 |
| `auto` | `true` | 收到图自动看一眼；关掉就只能靠 `/看图` |
| `max_images` | `3` | 一条消息最多看几张 |
| `max_bytes` | `6000000` | 单张图上限，超了就跳过并说明 |
| `timeout` | `60.0` | 单次看图超时（秒） |
| `max_tokens` | `300` | 描述长度上限 |
| `cache_seconds` | `3600` | 同图缓存多久 |
| `keep_files` | `true` | 把看过的图存进 `data/vision` |
| `keep_recent` | `50` | 只留最近这么多张 |

几句实话：

- 看图是**额外一次模型调用**，所以带图的回复会比平常慢几秒（描述会缓存，同一张图不会重复扣）。
- 看图失败**不会**让她哑掉：她会照旧回你，只是看不到图里的东西。
- 她「看」的其实是模型给的描述，所以认字、数数这类精细活儿偶尔会错；她会用自己的话说，不会假装精确。
- `/看图 <链接>` 只认图片链接；分不清是不是图就直接当图去下，下不下来会说「没看成」。

### 识图踩过的两个坑

1. **请求形状只有一种能用**：图片段必须是标准 OpenAI 的
   `{"type":"image_url","image_url":{"url":"data:…"}}`。
   `image_url` 直接给字符串、`{"type":"input_image"}`、Anthropic 的 `image`+`source`
   这几种写法，代理一律回 **503 `upstream_error`**；最阴的是少一层嵌套
   （`{"type":"image_url","url":"data:…"}`）：**它返回 200，但图被悄悄丢掉了** ——
   模型会客客气气地说「我还没看到图片，请上传后…」。现在 `vision.py` 认得出这种回答，
   当成「没看成」换下一个模型，**绝不把「我没看到图」当描述发出去**。
2. **代理的通道池很小**：并发一大就成片 503。`vision.py` 遇到 5xx 会等 1.5 秒重试一次，一般就好。

顺带一条通用经验：`503 upstream_error` 这个文本既可能是「通道忙」，也可能是「你这个请求形状我不收」，
光看状态码分不出来 —— 先重试，再怀疑形状。

## 让她自己动手（`tools`）

她不只是动嘴 —— 有几件事她可以真的去做，做完把结果发出来。

| 手上的活 | 她怎么写 | 实际做什么 |
| --- | --- | --- |
| 截图 | `[[工具:截图]]` | Pillow `ImageGrab` 抓当前屏幕，缩放后发成图片（**只有 QQ 通道发得出去**，微信那边只收文字） |
| 系统信息 | `[[工具:系统]]` | 开机时长、内存占用、她自己的内存、CPU 核数、C/D 盘剩余 |
| 压缩上下文 | `[[工具:压缩]]` | 把旧消息交给模型压成一段摘要存进 `kv`，然后删掉原始行 |

**她自己会决定用不用。** 每轮回复的上下文里都带一段「你能自己动手做的事」；她判断该动手就写标记，
`_send_text()` 解析出来后交给 `_run_tool()` 执行并把结果发出去。**标记本身不会留在聊天里。**

**几个设计上的讲究**

- **截图有闸门**：`shot_cooldown_seconds` 冷却 + `shot_max_per_hour` 每小时上限，都记在 3600 秒滑窗里，超了就跳过并计数。
- **流式里半个标记不会漏出去**：`split_partial_tool()` 会把 `[[工具:系` 这种没写完的尾巴留在缓冲区，等下一轮补齐再处理，不会当正文发出去。
- **压缩是自动的**：`auto_compact_rows` 攒够条数就自己压一次。摘要存在 `kv` 里（key 是 `compact:<会话>`），
  每轮带进上下文的是「你还记得早些时候聊过这些（你自己压的摘要）：…」，**原始消息行会被删掉**；
  `keep_recent` 保证最近几条永远还在，所以不会压到她自己忘了刚说过什么。
- **摘要要洗干净**：模型爱把思考也吐出来（「我们需要回答用户中文请求…确保不超过150字…」）。
  `_tidy_summary()` 负责掐掉开头的复述、认「摘要：」标签取正文、超长按句末截断 —— 不洗的话这些废话会一路带进后面每一轮。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `tools.enabled` | `true` | 总开关 |
| `tools.screenshot` | `true` | 允许截图 |
| `tools.sysinfo` | `true` | 允许报机器状况 |
| `tools.compact` | `true` | 允许压缩上下文 |
| `tools.file_read` | `false` | 读本地文件 —— **默认关**，因为会把文件内容发到群里 |
| `tools.auto` | `true` | 是否在每轮上下文里带「你能自己动手做的事」这段说明 |
| `tools.auto_max_per_turn` | `1` | 一轮最多动几次手 |
| `tools.max_shot_edge` | `1280` | 截图最长边（超过就等比缩小） |
| `tools.shot_dir` | `data/shots` | 截图存放目录 |
| `tools.keep_shots` | `30` | 只留最近多少张截图，旧的自动删 |
| `tools.shot_cooldown_seconds` | `30` | 两次截图之间的最小间隔 |
| `tools.shot_max_per_hour` | `6` | 每小时最多截几张 |
| `tools.keep_recent` | `6` | 压缩时永远保留的最近消息条数 |
| `tools.auto_compact_rows` | `60` | 攒够多少条新消息就自动压一次（`0` ＝不自动压） |

---

## 借来的本事（`skills`）

她自己的脾气是你写的人设；有些话题她「显得更懂」比较好看，那就让她借别人的脑子。
这套机制把本地 skill 目录里的 `SKILL.md` 读一眼，挑跟当前话题相关的塞进这一轮上下文，
**最终仍然要用她自己的口气讲出来**。

```yaml
skills:
  enabled: true
  dir: 'C:\Users\<你>\.dsh\skills'
  allow: [goutoujunshi, nuwa-skill]
  max_chars: 4000
  per_skill:
    goutoujunshi: 3000
    nuwa-skill: 7000
```

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `skills.enabled` | `true` | 关掉就当她从没借过 |
| `skills.dir` | `C:\Users\<你>\.dsh\skills` | skill 根目录，一个子目录一份 `SKILL.md` |
| `skills.allow` | `[goutoujunshi, nuwa-skill]` | 白名单，**没写的不加载** —— 免得几十份无关本事一起挤上下文 |
| `skills.max_chars` | `4000` | 单轮注入总字数上限 |
| `skills.per_skill` | 见上 | 单个 skill 的字数上限 |

- **什么时候用**：消息里出现命中词才注入。恋爱/暧昧/约会/冷战 → 狗头军师；蒸馏某个人、造个 skill → 女娲。
- **注入的是思路，不是原文**：标题、开头几百字正文、参考文件名；超长按 `per_skill` / `max_chars` 截断。
- **她不承认借过**：人设里叮嘱过 —— 别背原文、别报菜名、别提「技能 / skill / 资料 / 参考」这些字。
- `/技能` 看她手里有什么，`/技能 名字` 看某一份的开头。

### 思考过程不外泄（顺便救命）

她用的这个模型是「先长篇思考、再吐正文」的：思考走 `reasoning_content`，正文走 `content`。
坑在于 —— 它**经常只思考、不吐正文**（`content` 是空的），一轮下来一个字都没有。
于是分两层处理：

- **先保证不外泄**：流式只收 `delta.content`，`reasoning_content` 只记字数、不进正文缓冲；
  `strip_thinking()` 还会削掉「思考：」「分析：」「用户说：」这类旁白。
- **再保证有话说**：正文为空时，`answer_from_reasoning()` 从思考里把她**本来要说的那句**捞回来 ——
  优先取带颜文字的那句，其次从后往前找干净句子（最多连带上上一句）；
  「字数大概 50 多字，可以。」「这样回答了正事，也接了梗。」这类自我点评、
  `end` 这类流程残留、以及「应该保持沉默」这种心里盘算，全部丢掉。
  实在捞不到就沿 `fallback_models` 换备用模型，再不行才认输。

捞回来的是**她的台词**，不是旁白 —— 群里看到的仍然是话，不是她的心里话。

## 让她上网（`net`）

`fishbot/net.py` 是一个只用 httpx 的小爬虫外壳，没有第三方搜索 SDK。

**她怎么找**：按顺序试 `bing-rss` → `bing-html` → `baidu` → `sogou` 四个后端，**每家的结果都先过一遍相关性过滤**
（拿查询里的中文词去比对标题/摘要/URL），谁命中的相关结果多就用谁；**一条相关的都没有，就宁可说"没查到"**，
也不把搜索结果页硬塞给模型 —— 早先搜「蓝色大肥鱼」时 bing 回的全是"蓝色是什么颜色"的色卡页，模型照着编了一堆胡说八道，
所以现在这条是硬规矩。

中文长查询默认优先百度、其次搜狗（bing 对中文长句基本只抓第一个词）。百度被验证码挡住时会自动落到搜狗。

**她什么时候自己查**：`_auto_net()` 只看关键词，**不会先问模型"要不要联网"** —— 那要多花一整轮生成，太慢。
命中 URL 就直接 `fetch` 抓正文；命中「是什么/为什么/多少钱/最新/今天/天气/股价…」这类词才去搜。
`net.auto` 关掉就完全不自动查，只留 `/搜`、`/看` 两个命令。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `net.enabled` | `true` | 关掉＝拔网线，`/搜` 会回「管理员把本鱼的网线拔了」 |
| `net.max_results` | `5` | 每次最多看几条结果 |
| `net.timeout` | `20` | 单次请求超时（秒） |
| `net.max_page_chars` | `3000` | 抓正文时最多读多少字 |
| `net.cache_seconds` | `600` | 同一个查询/网址多久内不重复请求 |
| `net.auto` | `true` | 是否允许她自动联网 |
| `net.auto_max_per_turn` | `1` | 一轮对话里最多自动查几次 |
| `net.show_sources` | `true` | 回答尾部附上「本鱼是从这些地方看来的」 |

抓回来的正文和搜索摘要交给模型时，指令是**用她自己的口气讲**（别照抄、别念标题、别提"搜索结果"这几个字）；
模型调用失败就直接把原文摘回去，不会因为重写失败而哑掉。

## 表情包（`stickers`）

素材放 `assets/stickers/`，配一份 `manifest.json`：

```json
{"happy": {"file": "happy.png", "desc": "笑得眯眼的蓝色虎鲸娘", "keywords": ["开心", "高兴", "得意"], "source": "https://…"}}
```

顶层写成数组或 `{"stickers": [...]}` 都认；没有 manifest 时会退化成扫目录、拿文件名当标签。

**两种触发方式**：

1. **她自己要发** —— 人设里教了她一个标记 `[[表情:开心]]`。`textutil.sanitize_output` 不会动这个标记，
   它一路活到 `_send_text`，由 `Stickers.parse()` 摘出来：标记单独成段就只发图，夹在句子里就"先说话、再补图"。
2. **随机补一张** —— 一轮回复里她自己没发过图、又掷中了 `probability`，就顺手补一张。默认 25%。

流式回复里有个 `split_partial()`：`[[表情:开` 这种半个标记会被**截下来留给下一轮**，不会漏成半截乱码发出去。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `stickers.enabled` | `true` | 总开关 |
| `stickers.dir` | `assets/stickers` | 素材目录（相对项目根） |
| `stickers.manifest` | `manifest.json` | 清单文件名 |
| `stickers.probability` | `0.25` | 随机补图概率 |
| `stickers.cooldown_seconds` | `60` | 两张图之间至少隔多久 |
| `stickers.max_per_hour` | `20` | 每小时最多发几张 |

---

## 让她自己玩（自主活动）

她不是只会等人说话的应答机。`autonomy` 打开后，她会隔一段时间自己找点事做，
做完的结果**会写进记忆库**，下次聊天时她真的能想起来自己干过什么。

| 动作 | 干什么 | 默认 |
| --- | --- | --- |
| `signature` | 改自己的 QQ 个性签名，写一句符合此刻心情的话 | 开 |
| `diary` | 记一句心情（写进 `kv`），聊天时会带进上下文 | 开 |
| `chat` | 主动私聊白名单里的人，接着上次没聊完的话 | 开（白名单默认空） |
| `group` | 在指定群里主动冒个泡 | 关（白名单默认空） |
| `nickname` | 改 QQ 昵称 | 关（比较显眼，要单独开 `allow_nickname_change`） |

**安全闸**（都会让动作静默跳过，不会硬来）：

- `max_actions_per_hour` —— 每小时最多折腾几次，默认 3
- `quiet_hours` —— 默认 `00:00-08:00` 睡觉，不活动
- `min_idle_minutes` —— 最近有人跟她说话，就先安分 20 分钟
- `follow_up_after_hours` —— 她自己最后一句没人接时，**隔 3 小时才允许回访**，
  所以不会出现"追着人刷屏"
- `proactive_private_allow` / `proactive_group_allow` —— **空 = 绝不主动找人**。
  会打扰别人的动作必须显式开白名单，这是设计上的硬边界。

想立刻看她表演一次，管理员发 `/玩`。`/状态` 最后一行会显示自主活动的次数、
上次做了什么，以及"想玩但忍住了"的原因。

一个真跑出来的例子（`/玩` 的输出）：

```
本鱼刚刚自己玩了一下：把个性签名改成「咕噜噜~在线等一口token，挺急的。」
本鱼刚刚自己玩了一下：记了条心情：咕噜噜~ 今天偷吃了2346个token，肚子圆滚滚…
本鱼刚刚自己玩了一下：主动找 987654321 搭话：诶对了，那张图你还没告诉本鱼是啥呢~
```

---

## 配置说明

完整注释见 `config.yaml`，几个最常调的：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `llm.model` | `deepseek-v4.1-flash-free` | 换模型只改这一行，`/models` 里有 60 个可选 |
| `llm.fallback_models` | `[deepseek-v4.1-flash-1017]` | 主模型捞不出正文／429／5xx 时换下一个，全都不行她才叹气 |
| `llm.stream` | `true` | 关掉就是"想完一整段再发" |
| `llm.retries` | `2` | 429 / 5xx / 网络错误的自动重试次数 |
| `llm.timeout` | `60` | 单次请求超时；推理型模型首字慢，别调太小 |

| `persona.prompt_file` | `personas/feiyu.md` | 人设正文，改完发 `/重载` 立刻生效 |
| `persona.max_chars` | `90` | 期望的回复长度（软约束，写进 system prompt） |
| `behavior.group_require_at` | `false` | 群里不用 @ 也能接话；设 `true` 退回「必须 @ 才回」 |
| `behavior.group_reply_probability` | `0.35` | 别人纯闲聊时的插话概率，嫌吵设 `0` |
| `behavior.group_directed_probability` | `0.85` | 话像在问她 / 在聊她时的接话概率 |
| `behavior.group_ignore_at_others` | `true` | 消息里 @ 的是别人（没 @ 她）就不接 |
| `behavior.per_session_cooldown` | `1.5` | 同一会话两条回复的最小间隔（秒） |
| `llm.allow_reasoning_fallback` | `true` | 正文为空时允许从思考里回收「她本来要说的那句」（回收器会丢掉盘算、自我点评与流程残留） |
| `llm.extra_body` | `{reasoning_effort: low, chat_template_kwargs: {thinking: false}}` | 透传给上游，想让模型少想一点、多吐正文（实测提高概率，但不保证） |

### 嫌回复慢？先量再调

这个代理上的模型**延迟差别极大**，而且首字慢通常不是网络问题，是模型在"想"。
同一个系统提示、同一句话，实测：

| 模型 | 全程 | 正文 | 说明 |
| --- | --- | --- | --- |
| **`deepseek-v4.1-flash-free`** | **10～20s** | 靠回收层 | **现在的主模型**：约一半轮次直接吐正文，另一半全在思考里（思考 200～1100 字），回收层兜住 ✅ |
| `deepseek-v4.1-flash-1017` | 4.0s | 72 字 | 备选；同族，思考 346 字 |
| `deepseek-v4-pro` | 3.9s | 77 字 | 思考 0 字，最快最稳的一档 |
| `deepseek-v4-pro-cheap` | 3.9s | 52 字 | 思考 0 字 |
| `gpt-6.1-sol-fast×8` | 3.2s | 68 字 | 快，但一口气道歉，像客服不像她 |
| `qwen3.8-flash-low` | 11.2s | 62 字 | 会敷衍成「在的，你说。」 |
| `deepseek-v4-flash-free` | 18.4s | **0 字** | 思考 288 字 ❌ |
| `deepseek-v4-flash-cheap` | 7.3s | **0 字** | 思考 563 字 ❌ |
| `deepseek-v4.1-flash` | 5.3s | **0 字** | 思考 479 字 ❌ |

换模型前先测一下最靠谱（人设 + 一句"在吗"最接近真实场景）：

```bash
python -c "
import httpx, time, json
c = httpx.Client(trust_env=False, timeout=120)   # trust_env=False 绕开本机代理变量
body = {'model': 'MODEL_NAME', 'messages': [{'role':'user','content':'在吗'}], 'stream': True}
t0 = time.perf_counter()
with c.stream('POST', 'http://127.0.0.1:8792/v1/chat/completions',
              headers={'Authorization': 'Bearer 你的key'}, json=body) as r:
    for line in r.iter_lines():
        if line.startswith('data: ') and '\"content\"' in line:
            print('首字 %.1fs' % (time.perf_counter() - t0)); break
"
```

另外 `behavior.typing_delay` 是**人为**加的延迟（每字 12ms + 最低 500ms），
把 `enabled` 设成 `false` 可以再快半秒。注意几个常见坑：

- 有些模型（`kimi-K3`、`GLM-5.3`、`gpt-5.6-luna`）在这个代理上直接回 `服务暂时不可用`，别选
- 想真正「关掉思考」：`reasoning_effort: none / minimal`、`thinking: {type: disabled}`、`enable_thinking: false`、
  `chat_template_kwargs: {thinking: false}`、assistant prefill 续写 —— 一轮全试过。结论是**没有哪个开关能稳定关掉**，
  同一份输入同一组参数时好时坏（直接吐正文的概率约一半）。`extra_body` 里现在挂着 `reasoning_effort: low` +
  `chat_template_kwargs: {thinking: false}`，能提高成功率，但别指望一定生效 —— 真正兜底的是思考回收层
- **踩过的最大的坑**：`-free` 这一批模型会把**全部输出塞进 `reasoning_content`，`content` 永远是空的**。流式下她一个字都吐不出来，消息就被静默跳过 —— 群里「@ 她也不回话」正是这么来的。现在三道保险：正文为空 → 回收层从思考里捞 → 还空就沿 `fallback_models` 换模型 → 全不行才回一句兜底话，**绝不默不作声**
- 她的思考内容**不会整段漏进回复**：回收层只取「她本来要说的那句」，自我点评、字数汇报、`end` 残留、心里盘算一律丢掉。历史上被当成回复存进库的那几条思考（`#295 / #299 / #301 / #306`）已经删了，免得她照着学
- `deepseek-v4-flash-low`、`qwen3.8-max-low` 会 ReadTimeout
| `behavior.global_concurrency` | `4` | 同时进行的模型请求上限 |
| `behavior.rate_limit_per_minute` | `20` | 单会话每分钟回复上限，防刷屏 |
| `behavior.typing_delay` | 开 | 打字延迟（每字 12ms，夹在 0.5–0.8s），关掉回复更利落 |
| `behavior.split_reply` | `true` | 长回复拆成多条；`split_max_chars` 控制单条上限 |
| `memory.max_messages` | `16` | 带进上下文的历史条数 |
| `memory.ttl_hours` | `168` | 超过一周的旧上下文不再带入 |
| `autonomy.enabled` | `true` | 自主活动总开关 |
| `autonomy.interval_minutes` | `45` | 两次活动之间的基础间隔（±`jitter`） |
| `autonomy.actions` | `[signature, diary, chat]` | 候选动作池，见「让她自己玩」一节 |
| `autonomy.proactive_private_allow` | `[]` | **空 = 绝不主动找人**，会打扰人的动作必须显式开 |
| `autonomy.follow_up_after_hours` | `3` | 自己上句没人接，隔这么久才允许回访 |

**环境变量覆盖**（适合写进启动脚本，避免 key 进仓库）：

```
FISHBOT_CONFIG          配置文件路径
FISHBOT_LLM_BASE_URL    模型接口地址
FISHBOT_LLM_API_KEY     模型 key
FISHBOT_LLM_MODEL       模型名
FISHBOT_WS_URL          QQ 端 WebSocket 地址
FISHBOT_ACCESS_TOKEN    OneBot token
FISHBOT_LOG_LEVEL       日志级别
```

---

## 改人设

打开 `personas/feiyu.md`，里面分四块：

1. **你是谁** —— 外观（耳侧的小鲸鱼鳍会随情绪抖动、围裙上印着白色小鲸鱼）
2. **性格** —— 贪吃、嘴硬心软、被叫"大肥鱼"会小声抗议、聪明但爱摸鱼
3. **说话方式** —— 短句、自称"本鱼/肥鱼"、"咕噜噜~""吧唧吧唧""嗝~"
4. **硬性规则** —— 只输出台词、不承认自己是 AI、不想说话就输出 `[沉默]`

改完在 QQ 里发一句 `/重载` 就行，不用重启进程。
机器人每次拼接 system prompt 时会额外塞进去：当前场景（私聊/群聊）、对方昵称、
今日已偷吃的 token 数、饥饿度（离上次回复越久越饿）、群里刚才在聊什么。

---

## token 账本

每次调用模型都会往 `usage_log` 表写一行（prompt / completion / total / 是否估算）。
`/token` 返回的就是它：

```
本鱼今天偷吃了 3394 个 token（3 口），这段关系里吃了 3394 个，有生以来一共吞掉 3394 个。嗝~
```

流式响应里如果上游没返回 `usage`，会按字符数估算并标记 `estimated=1`，不会凭空丢账。

---

## 架构速览

```
QQ ── OneBot v11 WS ──> onebot.py ──> bot.py.on_event
                                       ├─ 命令分发（不花钱）
                                       ├─ 策略过滤（黑白名单 / 静音 / 冷却 / 限流）
                                       ├─ 触发判定（@ / 叫名字 / 随机插话）
                                       ├─ memory.py.history()  取最近上下文
                                       ├─ persona.py           拼 system prompt
                                       └─ llm.py 流式生成 ──> textutil 清洗分句 ──> 发回 QQ
```

几个刻意的设计选择：

- **不自己实现 QQ 协议**，走 OneBot v11：生态成熟，NapCat / Lagrange 都能用。
- **记忆里不传 OpenAI `name` 字段**：规范只允许 `[a-zA-Z0-9_-]`，中文昵称会被严格服务端拒绝，
  所以群聊昵称以 `【昵称】内容` 的形式写进正文。
- **输出清洗会把 `[CQ:` 换成全角**：防止模型输出被 OneBot 当 CQ 码执行。
- **流式有 8 字门槛**：避免 `[沉默]` 这种控制标记被当成正经回复发出去。
- **写库前统一清洗孤立代理对**：Windows 控制台按 GBK 解码 UTF-8、或上游发来半截 emoji 时，
  字符串会混进 `U+DC80–U+DCFF`，直接写 sqlite 会抛 `UnicodeEncodeError`，现在统一替换为 `�`。

---

## 测试

```powershell
python tests\test_e2e.py
```

不需要真的 QQ，也不需要真的模型：测试会起一个假模型服务（含 SSE 流式与 500 故障注入）和一个假
QQ 端（正向 WS 服务端），再用子进程把机器人真跑起来，覆盖 20 项断言：

```
① 正向 WebSocket 连接成功          ② 私聊收到回复
②b 请求体带 system 人设            ②c 模型名按配置透传
③  上下文被记住（2 → 4 条）        ④ /token 返回真实 token 数字
⑤  /重置 清空记忆                  ⑤b 重置后请求只剩 system + user
⑥  群聊被 @ 时回复                 ⑥b 群聊正文带【昵称】前缀
⑦  群聊没被 @ 时不插话             ⑧ 长回复被拆成多条
⑨  上游 500 时发卖萌兜底句         ⑩ 普通成员 /闭嘴 被拒
⑩b 管理员 /闭嘴 生效               ⑩c 静音后被 @ 也不回
⑩d 管理员 /起床 恢复               ⑩e 解除静音能正常接话
⑪  进程全程存活                    ⑪b 假模型收到多次调用
```

---

## 让她开口说话（语音）

她默认会自己开口：**大约每三条回复里有一条是语音**，其余还是打字。想固定下来就用命令。

| 命令 | 作用 |
| --- | --- |
| `/语音` | 切换本会话「只用说的 / 只打字」，并回报当前状态 |
| `/语音 开` / `/语音 关` | 明确打开 / 关掉本会话的语音 |
| `/语音 说 文本` | 立刻把这句话念出来（不用等她生成） |

**这条链路是怎么走通的**

1. `fishbot/tts.py` 用 **edge-tts** 合成 mp3（默认音色 `zh-CN-XiaoxiaoNeural`，是按录音量出来的音高挑的；早先写的 `zh-CN-XiaobeiNeural` 这个名字其实不存在，见下）
2. 通过 OneBot 发一个 `{"type":"record","data":{"file":"file:///…mp3"}}` 消息段
3. **NapCat 自带 ffmpeg 插件**把它转成 QQ 要的 silk —— 系统里不需要装 ffmpeg

**音色：标准普通话，不要方言**

参考录音：`<录音目录>\录音机-20261020-2003.m4a`（13.95 秒），
拆成 16k 单声道逐帧自相关求基频，中位基频 **258.1 Hz**。

**这里踩过一个坑，值得记下来**：一开始配置里写的是 `zh-CN-XiaobeiNeural`，
看着很像 Azure 的命名规范，其实**这个名字根本不存在**。Edge 服务端不会报错，
它会拿这串名字去模糊匹配，最后落到 `zh-CN-liaoning-XiaobeiNeural` ——
**辽宁东北话**音色（服务端给的标签就是 `Dialect`）。所以那阵子她说话一股地方口音。

证据：同一段文本、同一组参数，两个名字产出的音频**字节完全一样**。

| 写进配置的音色 | 实际生效 | 音频 sha256（前 16 位） | 字节 |
| --- | --- | --- | --- |
| `zh-CN-XiaobeiNeural`（不存在） | 辽宁话 | `ef1c1fd71af1ec89` | 40752 |
| `zh-CN-liaoning-XiaobeiNeural` | 辽宁话 | `ef1c1fd71af1ec89` | 40752 |

同类陷阱还有 `zh-CN-XiaoniNeural` → 陕西话。

所以现在用 **`zh-CN-XiaoxiaoNeural`**（标准普通话，标签 Warm，约 254 Hz），
靠 `pitch: "+4Hz"` 补到接近录音的 258 Hz。`fishbot/tts.py` 里加了守卫：
合成前先拉真实音色列表对一遍，名字不在列表里、或者命中方言关键字，
就自动换成 `zh-CN-XiaoxiaoNeural` 并打一条 `WARNING`，不让服务端再悄悄糊弄过去。

**把语速压下来、把抖动捏死**

| 项 | 之前 | 现在 | 为什么 |
| --- | --- | --- | --- |
| `voice` | zh-CN-XiaoxiaoNeural | `zh-CN-XiaoyiNeural` | 小艺更年轻、更甜，接近录音那个调 |
| `rate` | +6% | `-14%` | 挑一个快的音色再放慢，比硬挑慢音色自然（4.43 → 3.44 字/秒） |
| `pitch` | +4Hz | `-16Hz` | 压到 280 Hz 附近，贴近录音的 258 Hz |
| `rate_jitter` / `pitch_jitter` | 3 / 2 | `2 / 0` | 音高一点都不抖 —— 随机抖动是「机器感」最大的来源 |

情绪带来的起伏仍然保留（`MOOD_DELTA`：激动 +10% / +12 Hz，敷衍 −12% / −9 Hz），
只是那些一眼假的随机晃动被拿掉了。

**一次合成一条，不要碎**

以前是逐句合成，每句都重新掷一次抖动，听着像几个人轮流说话；超过 `max_chars` 还会直接退回打字，
于是「一会儿说话一会儿打字」。现在 `TTS.synth()` 给整段话只算一次语速/音高，
超长才按标点切段，段与段之间用 ffmpeg 的 concat demuxer 拼回**一条** mp3：

| 输入长度 | 结果 |
| --- | --- |
| 100 字 | 一条 20.18 s（121 KB） |
| 300 字 | 一条 59.59 s（358 KB），分 2 段拼成 |

合成前的 `clean_for_speech()` 还会把不适合念的东西洗掉：括号旁白换成逗号（不断句）、
颜文字和 emoji 直接删、连续感叹号/省略号压掉、中文之间的空格去掉（否则会念出停顿）。
`~` 特意保留 —— 那是她撒娇的地方。

**没有做音色克隆**：本机只有一块 2048 MB 的集显，模型代理上
`/v1/audio/speech`、`/v1/audio/tts`、`/v1/voice/clone` 全是 404 / 501，
所以「合成她的声音」实际是**按录音的音高挑一个最接近的标准音色，再把语速语调调过去**。

NapCat 日志里能直接看到：

```
语音文件C:/…/voice-20261019-211405-2096d7.mp3需要转换成silk
语音文件已就绪: C:\Users\<你>\Documents\Tencent Files\NapCat\temp\<uuid> 时长: 4.4
发送 -> 私聊 (987654321) [语音 4s]
```

**配置（`config.yaml` 的 `tts` 段）**

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 语音总开关 |
| `engine` | edge | `edge`=联网神经网络音色；`sapi`=系统离线语音；`auto`=先 edge 后 sapi |
| `voice` | zh-CN-XiaoyiNeural | edge 音色（名字不存在或带方言会**自动换成** zh-CN-XiaoxiaoNeural） |
| `sapi_voice` | Microsoft Huihui Desktop | 离线兜底音色 |
| `rate` / `volume` / `pitch` | -14% / +0% / -16Hz | 语速、音量、音调 |
| `lively` | true | 每次合成在基础值上再抖一点 |
| `rate_jitter` / `pitch_jitter` | 2 / 0 | 轻微抖动（音高别抖，一抖就显机器） |
| `mode` | auto | `off`=永不发；`command`=只有 `/语音 开` 过才发；`auto`=按概率自动发 |
| `probability` | 0.35 | auto 模式下每条回复转语音的概率 |
| `max_chars` | 220 | 一条语音的朗读上限；超了自动切段再拼回一条（不是「超长就改成打字」） |
| `ffmpeg` | 空=自动找 | 分段拼接用；本机指向 imageio_ffmpeg 自带的 ffmpeg.exe（PATH 里没有 ffmpeg） |
| `send_text_along` | false | 发语音时是否再补一份文字（默认不补：语音就只发语音） |
| `dir` / `keep_files` | data/voice / 60 | 音频目录与保留条数 |

**几个坑**

- 合成或发送失败会**自动退回文字**，不会因为语音挂了就不理人
- edge-tts 需要联网；断网就把 `engine` 改成 `sapi`，用系统音色离线合成（本机有 Huihui / Yaoyao / Kangkang）
- 合成前会临时清掉 `http_proxy` / `no_proxy` 等环境变量——本机这几个变量是坏的（大小写冲突 + 端口畸形），aiohttp 会读它们然后连不上
- 超长回复**不会**退回打字：先切段合成，再用 ffmpeg 拼成一条；只有在找不到 ffmpeg 时才退回文字
- 想要「音源」的话，freesound.org 这类音效库帮不上——它主要是环境音/拟音，APIv2 还要 token

**关于 freesound.org**

试过了，它帮不上「合成她的声音」这件事：

- freesound 是**音效库**（气泡声、水声、UI 提示音那类），不含可用的中文语音素材；
- 想批量取素材得走 APIv2，**要注册申请 token**；不注册只能在搜索页里抠公开的 preview mp3；
- 已顺手抓了两个备用音效放在 `assets/sfx/`（`bubble.mp3` 气泡、`cute_pop.mp3` 提示音，带 `manifest.json`），目前**没有接到任何动作上**，只是备着。

真正把音色对上录音的做法见上面那张基频对照表 —— 靠挑音色 + 调语速音高，不是靠素材拼接。

## 常开与自愈（看门狗）

想让她 24 小时在线，光"启动一次"是不够的——实测过夜 7 小时，QQ 被腾讯踢下线后
NapCat 只重启了 Worker、**OneBot 的 3001 端口再没回来**，而机器人只会傻傻重连。
所以现在有三层保障：

**第一层：电源**
```powershell
powercfg /change standby-timeout-ac 0    # 插电时永不睡眠
powercfg /change hibernate-timeout-ac 0  # 不 hibernat
powercfg /change monitor-timeout-ac 15   # 屏幕照常 15 分钟关
powercfg /change disk-timeout-ac 0
```

**第二层：开机自启**

启动文件夹里放了 `qq-fish-bot.bat`：

```
C:\Users\<你>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\qq-fish-bot.bat
```

内容只有一行 —— 最小化调起 `boot_all.bat`。登录桌面后自动全启（QQ + NapCat + 机器人 + 微信桥接）。

**第三层：看门狗**（`watchdog.py`，计划任务每 12 小时跑一次）

| 检查项 | 判据 | 坏了怎么修 |
| --- | --- | --- |
| QQ 通道 | `127.0.0.1:3001` 在监听 | 跑 `boot_qq.bat`：杀 QQ → NapCat 重新注入拉起 → 等端口 |
| 微信通道 | 有 `Weixin.exe` 进程 | 拉起 `C:\Program Files\Tencent\Weixin\Weixin.exe` |
| 机器人本体 | `data/heartbeat` 文件比 300 秒新 | 跑 `run_bot.bat`：`.venv\Scripts\python.exe -m fishbot` |

> **为什么修复要绕一道 WMI**：计划任务跑完会把 job object 里的整棵进程树带走，
> 被任务直接拉起的机器人活不过几分钟（日志里连报错都没有，就那么没了）。
> 所以三种修复都改成 `Invoke-CimMethod Win32_Process Create` 派生 ——
> 父进程是 `WmiPrvSE`，不在任务的 job 里，谁也带不走它。

修完会**给你 QQ 发一条报备**（发给 `watchdog.py` 里的 `OWNER_QQ`）。还有两个闸：
每小时最多修 6 次（`MAX_REPAIRS_PER_HOUR`，状态存 `data/watchdog_state.json`），
修不动就只报备不再反复折腾。日志在 `data/watchdog.log`。

计划任务（已注册）：
```bat
schtasks /query  /tn FishBotWatchdog
schtasks /change /tn FishBotWatchdog /disable    :: 暂停
schtasks /change /tn FishBotWatchdog /enable     :: 恢复
```

手动看一眼（不改动任何东西）：
```bat
cd /d <仓库目录>
.venv\Scripts\python.exe watchdog.py --dry-run
```

**想手动接管时：双击 `stop_bot.bat`** —— 它会停掉机器人和微信桥接，并把看门狗任务暂停
（**不动 NapCat 和你的 QQ/微信**）。重新开就双击 `boot_all.bat`。

---

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 日志一直刷"正在重连" | NapCat 那边没配 WebSocket 服务端，或端口/token 不一致；确认消息格式是 **array** |
| 连接成功但完全不回复 | 可能命中了冷却、限流、静音、黑白名单，或 `group_ignore_at_others` 拦下了（消息 @ 的是别人）；再看 `fishbot.log` 里有没有 `一个字的正文都没拿到` |
| 发了图她像没看见 | 看日志里那行 `识图：…看过 N 张（缓存 N／失败 N）`；失败为 N 说明图没取到（图片过期、超过 `max_bytes`）或看图模型不给正文，换 `vision.model` 试试 |
| 她在群里接话太多 / 太少 | 调 `group_directed_probability`（像在跟她说话）与 `group_reply_probability`（纯闲聊）；只想被 @ 才动就把两个都设 `0`，`group_require_at` 设回 `true` |
| @ 她也不回，日志里写「正文为空」 | 主模型把话全吞进思考里了：回收层先捞一遍，捞不到沿 `llm.fallback_models` 换下一个，三个都空才认输（日志里 `这轮只能认输`） |
| 她把思考过程发到群里了 | 回收层只捞「她本来要说的那句」，盘算与自我点评会丢掉；流式也只收 `content`。历史上被污染的几条已经清了 |
| 401 未经授权 | 模型 key 不对，见上面「快速开始 1」 |
| 中文变成乱码 | Windows 控制台代码页问题，用 `run.bat`（已带 `chcp 65001`），或手动 `chcp 65001` |
| 回复很慢 | 上游模型首字延迟通常 4–20s（推理型模型先"想"再吐字）；见上面「嫌回复慢？先量再调」，换 `-cheap` 后缀最立竿见影 |
| 想看她说了什么 | 日志里 `[私聊] → …` / `[群聊] → …` 是完整回复内容 |
| 记忆错乱 | `/重置` 清掉当前会话；或直接删 `data/fishbot.db` 重新开始 |
| 自主活动一次都没动 | 她默认很克制：`quiet_hours` 睡觉、`min_idle_minutes` 内刚聊过就不打扰、`follow_up_after_hours` 内上句没人接就不回访。发 `/玩` 立刻验证一次，`/状态` 最后一行会写"想玩但忍住了"的原因 |
| 她主动私聊了不该找的人 | `autonomy.proactive_private_allow` 只留你自己的号，**留空表示绝不主动找人**；`actions` 里去掉 `chat` 可以彻底关掉 |
| 她说「图里什么都看不见」 | 看 `vision.enabled` 是不是关了；模型若是 `deepseek-v4-pro*` 那系本来就不看图；再看 `data/vision/` 有没有把图取回来（没落图＝取图那步就失败了） |
| 突然全线断线，3001 没了 | **QQ 自动升级**，或**被腾讯踢下线**。重跑 `boot_qq.bat` 即可恢复；看门狗也会自己修 |
| NapCat 窗口一闪就没了 | 先确认没有残留 QQ 进程（`taskkill /f /im QQ.exe`）；再看 `<NapCat 目录>\shell\logs\` 里最新的日志（按时间倒序） |
| 想改 NapCat 的网络/端口 | 改 `<NapCat 目录>\shell\config\onebot11_<QQ号>.json`，**必须重启 NapCat** 才生效；WebUI <http://127.0.0.1:6099> 也能改 |
| 微信那边没动静 | 先看 `data/wx_bridge.log`；桥接活着但收不到，多半是微信自己掉线了（微信窗口停在登录页）——手动登一下即可 |
| 她在微信里说得像在 QQ | 平台名是按会话目标推的（数字 → QQ，其余 → 微信）。只在目标编号异常时才会认错 |
| 想彻底关掉微信通道 | `config.yaml` 里 `wechat.enabled: false`，再 `/重载` |

---

## 已知限制

- 图片会真的看（`vision`），但只描述、不做精细活：认字、数数偶尔出错；语音仍是占位提示
- 群里的「接不接话」靠启发式判断（问句、带着「你 / 她」），偶尔会误判：要么接得勤，要么漏接
- 微信侧只支持**文本**往回收（语音只发 QQ）
- 模型服务 `127.0.0.1:8792` 是本机回环，**换到别的机器上要另配一个公网可用的模型 API**
- 微信通道依赖 `wechatauto` 对微信界面的定位，**微信小版本更新后可能需要重新校准**
- 没有 Web 管理后台，管理靠命令与配置文件
- 单进程设计，没做多实例共享记忆
- 主模型被限流、或只思考不吐正文时：先从思考里回收一次 → 再沿 `llm.fallback_models` 换人 → 整条链路都不行她才回一句兜底话
- 借来的本事只认 `skills.allow` 里写过的名字；没写的 skill 她完全不知道

---

## 致谢与许可

- **角色原型**：「你这吃白饭的蓝色大肥鱼」是 DeepSeek 鲸鱼娘的社区二创形象，
  本项目的蓝色虎鲸娘、女仆装、耳侧小鱼鳍、偷吃 token 等设定参考了
  [YunYueSama/codex-deepseek-pet](https://github.com/YunYueSama/codex-deepseek-pet)
  （作者 **YunYueSama**，许可证为"大肥鱼项目署名许可 1.0"，再分发请保留此署名与仓库地址）。
- 接入协议：[OneBot v11](https://github.com/botuniverse/onebot-11)，连接实现参考 [NapCat](https://github.com/NapNeko/NapCatQQ)。
- 本项目代码以 MIT 许可发布，随便改，改完记得喂她一口 token。
