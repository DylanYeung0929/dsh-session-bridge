# dsh-session-bridge

[![CI](https://github.com/DylanYeung0929/dsh-session-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/DylanYeung0929/dsh-session-bridge/actions/workflows/ci.yml)

> **One CLI (and one DSH plugin) to read, list and message every DeepSeek Harness conversation.**
> 在终端或任意 DSH 会话里，查看 / 读取 / 给**所有 DeepSeek Harness 对话**发消息。

[English summary](#english-summary) · [快速开始](#快速开始) · [教程](docs/TUTORIAL.md) · [协议](docs/PROTOCOL.md)

---

## 这个工具解决什么问题

DeepSeek Harness（下称 DSH）里的每个对话都是独立 agent，跑在自己的会话里，互相看不见。想「让 A 对话知道 B 对话在干什么」「在手机上继续电脑上的那个对话」，默认是做不到的。

`dsh-session-bridge` 把你机器上所有 DSH 会话变成**可枚举、可读、可写**的：

- **可枚举** —— 列出全部会话：标题、工作目录、是否正在跑、token 用量。
- **可读** —— 直接读磁盘上的转录（`session.v4.jsonl.zstd`），App 关着也能读。
- **可写** —— 通过 DSH 桌面 App 自己的本地 RPC，把消息追加进任意会话，让它自己的 agent 真的跑一轮。
- **可聊** —— `dshctl chat` 提供一个带「每轮一行菜单」的跨会话聊天模式：`1继续 2切换 3上下文 4退出`。

```
┌──────────────┐   ① list / read（读磁盘，零依赖）
│  你的终端 /   │  ─────────────────────────────────────┐
│  IM / 脚本    │                                       ▼
│              │   ② prompt（本地 HTTP JSON-RPC）   ┌─────────────┐
│  dshctl.py   │  ─────────────────────────────────▶│ DSH 桌面 App │
└──────────────┘        Cookie: dsh-auth-…           │  :51413     │
                                                     └──────┬──────┘
                                                            │ 该会话自己的 agent 跑一轮
                                                            ▼
                                              ~/.dsh/sessions/**/session.v4.jsonl.zstd
```

---

## 特性

| 能力 | 命令 | 需要 App 在跑？ |
|---|---|---|
| 列出所有会话 | `dshctl list` | 是（走 RPC） |
| 读某个会话最近 N 条 | `dshctl tail <会话> 20` | **否**（直接读转录） |
| 给某个会话发消息 | `dshctl prompt <会话> "…"` | 是 |
| 交互式跨会话聊天（带菜单） | `dshctl chat <会话>` | 是 |
| 记住「当前在跟谁聊」 | `dshctl focus <会话>` / `where` / `focus -` | 是 |
| 调任意 RPC 端点 | `dshctl raw <ns/method> '<json>'` | 是 |
| 在 DSH 里当工具用 | `session_list` / `session_read` / `session_send` | 是 |

- 纯 Python 3 标准库 + Node ≥ 20，**无第三方依赖**（读转录只额外需要 `zstd` 命令）。
- 会话解析支持 `session-<uuid>` 全名、短前缀或**唯一标题子串**；命中多个时直接报错并列出候选，绝不悄悄选第一个（读错会话比报错更糟）。
- `--json` 全部命令可用，方便脚本化。
- 发消息前会打印目标会话 id + 标题，防止发错人；`list` 的 `●` = 打开且近 5 分钟有写入，`◐` = 打开但近期没动。

---

## 前置条件

| 项 | 要求 |
|---|---|
| 系统 | macOS（依赖 DSH 桌面 App 的本地 RPC，路径与 Cookie 库随 App 走） |
| DSH | 桌面 App 正在运行（`dshctl status` 能看到 `backend: app` 即可） |
| Python | 3.9+（`python3 --version`） |
| Node | ≥ 20（只有用插件/`npx` 时才需要） |
| zstd | `brew install zstd`（只在读转录时用；`tail` / `read` 命令） |

---

## 安装

### 方式 A：只当 CLI 用（最快，推荐先试这个）

```bash
git clone https://github.com/DylanYeung0929/dsh-session-bridge.git
cd dsh-session-bridge

brew install zstd            # 若还没有
./tools/dshctl.py status     # 应该输出 backend: app  port: 51413
```

### 方式 B：装成全局命令

```bash
npm install -g ./            # 在仓库目录里执行
dshctl status                # 之后任意目录可用
```

### 方式 C：装成 DSH 插件（在任意 DSH 会话里用工具）

在 DSH 的 profile 目录（macOS 默认 `~/.dsh/profiles/desktop`）里：

```bash
cd ~/.dsh/profiles/desktop
npm install /绝对路径/dsh-session-bridge
```

然后把它加进 `package.json` 的 `dsh.profile.bundles`：

```json
"bundles": [
  "...",
  "dsh-session-bridge"
]
```

重启 DSH 后，会话里就多了三个工具：`session_list`、`session_read`、`session_send`。

> `npm install` 只装包，**不会**自动登记 bundle —— 上面那步必须做，否则重启后看不到这三个工具
> （验证：`~/.dsh/profiles/desktop/package.json` 的 `dsh.profile.bundles` 里要有 `dsh-session-bridge`）。

> 插件只是把 CLI 包了一层，所以仍然需要 `python3` 在 PATH 里；想换解释器就设 `DSH_SESSION_BRIDGE_PYTHON=/opt/homebrew/bin/python3`。

---

## 快速开始

```bash
# 1. 看看有哪些会话（● 表示正在跑）
./tools/dshctl.py list

# 2. 读某个会话最近 10 条（App 关了也能读）
./tools/dshctl.py tail "安装插件" 10

# 3. 给那个会话发一句话，并等它跑完
./tools/dshctl.py prompt "安装插件" "用一句话汇报你现在的进度" --wait 90

# 4. 进入交互式跨会话聊天（每轮后给一行菜单）
./tools/dshctl.py chat "安装插件"
```

`chat` 模式每轮回复后固定打印一行，在手机上也能盲打：

```
1继续 2切换 3上下文 4退出   ← 直接打字=继续
```

按 `1`（或「继续」）会再问一句要说什么，**直接回车就发「继续」**；空行同样等于继续。

---

## CLI 参考

```
dshctl.py status
dshctl.py list [--full] [--json]
dshctl.py tail <会话> [N] [--json]
dshctl.py prompt <会话> <文本> [--steer] [--wait SECS]
dshctl.py chat <会话> [--steer] [--timeout SECS] [--context N]
dshctl.py focus <会话> | focus -
dshctl.py where
dshctl.py raw <namespace/method> '<args json>'
dshctl.py start [--port N] [--key K]   # 兜底：App 没开时起一个 helper web 实例
dshctl.py stop
```

| 参数 | 说明 |
|---|---|
| `--steer` | 打断当前正在跑的轮次，把消息插进去（默认 `queue`，排在后面） |
| `--wait SECS` | 等该会话的轮次结束，最多 SECS 秒 |
| `--json` | 机器可读输出（`list --json` 默认给紧凑字段，加 `--full` 给原始完整行） |
| 会话选择 | `session-<uuid>` 全名 / 唯一短前缀 / 唯一标题子串；有歧义时报错并列出候选 |

---

## 插件工具

| 工具 | 参数 | 作用 |
|---|---|---|
| `session_list` | `full?: boolean` | 列出全部会话；默认紧凑（id / 标题 / cwd / running / updatedAt / outputTokens），`full:true` 返回原始完整行 |
| `session_read` | `session: string`, `count?: number` | 读该会话最近 N 条（磁盘转录） |
| `session_send` | `session: string`, `message: string`, `steer?: boolean`, `wait?: number` | 追加消息并让该会话的 agent 跑一轮 |

模型侧看到的例子：

```text
session_list({ full: true })
session_read({ session: "安装插件", count: 6 })
session_send({ session: "安装插件", message: "把结论压缩成三行", wait: 60 })
```

---

## 跨会话对话协议（给聊天机器人 / IM 场景）

如果中间隔着一层聊天通道（微信、Slack、IM 机器人），**不要**指望用 `chat` 的终端 REPL —— 对方按键到不了你的进程。正确做法是「状态外置 + 每轮渲染菜单」：

```bash
./tools/dshctl.py focus "安装插件"   # 记住当前对话对象 → ~/.cache/dshctl-chat.json (600)
./tools/dshctl.py where              # 看当前在跟谁聊
./tools/dshctl.py focus -            # 退出
```

之后用户回 `1/2/3/4`（或 `继续/切换/上下文/退出`）时：

| 输入 | 动作 |
|---|---|
| `1` / 任意文字 | 把文字发给当前会话；`1` 后再空行、或直接空行都等于发「继续」 |
| `2` | 列出会话，选一个后切换 |
| `3` | 打印最近 N 条上下文 |
| `4` | 退出（`focus -`） |

菜单固定一行，因为小屏上多行选项很难点：`1继续 2切换 3上下文 4退出`。

---

## 工作原理（三分钟版）

1. **鉴权**：从 DSH 桌面 App 的 Chromium Cookie 库
   `~/Library/Application Support/@deepseek-ai/dsh-desktop/Cookies` 里取 `dsh-auth-*`
   （`value` 列就是明文，无需解 Keychain），payload 里带 `authority: "127.0.0.1:51413"`。
2. **调用**：`POST http://127.0.0.1:51413/api/<namespace>/<method>`，body 是
   `{"type":"client-request","rpcId":…,"method":"session/prompt","payload":{"args":{…}}}`。
3. **发消息**：`session/prompt` 的 args 是 `{"request":{requestId, sessionId, content:[{type:"text",text}], mode:"queue"|"steer"}}` —— 注意 `session/list` 用的是 `{"_request":{}}`，参数名不一样。
4. **读回复**：不依赖 RPC，直接 `zstd -dc` 解 `~/.dsh/sessions/<项目>/session-<uuid>/session.v4.jsonl.zstd`，取最后一条 `assistant/message`。

完整字段、错误码、边界条件见 **[docs/PROTOCOL.md](docs/PROTOCOL.md)**；手把手操作见 **[docs/TUTORIAL.md](docs/TUTORIAL.md)**。

---

## 安全模型与红线

- **凭据只经过内存**：helper 启动时 `DEEPSEEK_API_KEY` 只通过子进程 env 传入、**不落盘**（0.1.1 起；旧版写进 `~/.cache/dshctl.json` 的 key 会在下次 `start` 时自动清掉）。helper 自己的短期 token 存在 600 的 `~/.cache/dshctl.json` 里，供后续命令复用；`focus` 状态文件只存会话 id 和标题。
- 所有写操作都**必须显式指名会话**；解析失败宁可不发（报 `no session matching`）。
- 本项目作者的默认红线（你也可以写进自己的 agent 规则里）：
  - 不代转账、不代答应、不发文件/图片、不加好友/退群；
  - 代表用户发言前先核对「目标会话标题」，发完回报结果；
  - 不要把 `~/.dsh/.credentials.yaml`、Cookie 库内容贴到任何对话或日志里。
- 这个工具等价于「让当前会话拥有给别的会话发消息的能力」——**它不会绕过任何模型侧的权限**，只是把 DSH 已有的 RPC 暴露给你。

---

## 故障排查

| 症状 | 原因 | 解决 |
|---|---|---|
| `dshctl: cannot find the DSH app cookie` | App 没开，或 Cookie 库路径不同 | 先启动 DSH 桌面 App；确认 `~/Library/Application Support/@deepseek-ai/dsh-desktop/Cookies` 存在 |
| `connect ECONNREFUSED 127.0.0.1:51413` | 端口变了（DSH 重启会换端口） | `dshctl status` 会读 Cookie payload 里的端口；仍失败就用 `dshctl start` 起 helper |
| `会话名不唯一` / `dshctl: ambiguous …` | 标题或前缀命中多个会话（这是有意的保护） | 按列出的候选补全 id，或改用更长的标题 |
| `no session matching "xxx"` | 标题 / 前缀不存在 | 用完整 `session-<uuid>`，或改用唯一标题子串 |
| `missing "_request"` / `missing "request"` | 参数名写错 | `list` 用 `_request`，`prompt` 用 `request`（见协议文档） |
| 能读不能发 | App 在跑但会话 preset 是 desktop 专属 | 这是设计限制：helper 实例不 compose `computer-use` 之类 preset，只能直连 App |
| `tail` 报 zstd 不存在 | 没装 zstd | `brew install zstd` |

---

## 目录结构

```
dsh-session-bridge/
├── bin/dshctl.mjs          # npm bin 垫片 → tools/dshctl.py
├── lib/index.mjs           # DSH 插件：session_list / session_read / session_send
├── tools/dshctl.py         # CLI：本地 RPC 客户端 + 交互式 chat
├── tools/dshtool.py        # 纯离线：读 zstd 转录
├── cordis.patch.yml        # 插件注册声明
├── docs/PROTOCOL.md        # RPC 协议（逆向所得，含错误码）
├── docs/TUTORIAL.md        # 10 分钟教程
└── examples/               # 可复制的小脚本
```

---

## 开发与测试

```bash
python3 -m py_compile tools/dshctl.py tools/dshtool.py
node --check lib/index.mjs
python3 -m unittest discover -s tests -v      # 13 个离线单测：resolve 消歧、消息提取、lock 语义
node tests/plugin.test.mjs                    # 插件契约：stub defineTool → 断言注册 3 个工具
./tools/dshctl.py list --json | head          # 端到端可用性（需要 App 在跑）
```

CI（`.github/workflows/ci.yml`）每次 push 都会跑：Python 3.10 / 3.13 × ubuntu / macOS（语法闸 + 单测 + CLI 冒烟 + 文档文件存在性）、Node 20 / 22（package.json 契约 + 插件注册测试 + cordis patch 形状）。全部离线，不需要 DeepSeek Harness 或任何凭据。

本项目是在实际使用中逆向出来的，边界条件都记在 `docs/PROTOCOL.md`；欢迎提 issue / PR 补平台支持（Windows、纯 headless 无 App 场景）。

## Roadmap

- [ ] Linux / Windows 的 Cookie 与端口发现
- [ ] `dshctl watch <会话>`：实时跟随某会话输出（RPC 有 `session/follow` 流）
- [ ] 插件里直接暴露 `session_chat`（带菜单状态机）
- [x] 单元测试 + CI（GitHub Actions：python 3.10/3.13 × ubuntu/macOS、node 20/22）

## License

MIT © 2026 Dylan Yeung

---

## English summary

`dsh-session-bridge` lets you list, read and **message every DeepSeek Harness session** from one CLI or as a DSH plugin.

- `dshctl list | tail | prompt | chat | focus | raw` — Python 3 stdlib only.
- `session_list` / `session_read` / `session_send` — three first-party DSH tools.
- Auth = reuse the desktop app's own `dsh-auth-*` cookie; transport = local JSON-RPC at `127.0.0.1:51413`; transcripts are read straight off disk with `zstd`.
- `chat` prints a one-line menu after every turn (`1 continue 2 switch 3 context 4 quit`) so it stays usable on a phone.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the reverse-engineered protocol and [docs/TUTORIAL.md](docs/TUTORIAL.md) for a 10-minute walkthrough.
