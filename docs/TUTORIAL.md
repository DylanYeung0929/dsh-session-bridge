# 10 分钟教程：从 0 到「在 A 会话里跟 B 会话聊天」

> 目标：让当前的 DSH 会话（A）能列出、读取、并给另一个会话（B）发消息，而且每轮之后给你一行选项。
> 环境：macOS + 正在运行的 DSH 桌面 App。

---

## 第 0 步：确认 App 在跑

```bash
$ ./tools/dshctl.py status
backend: app  port: 51413
sessions: 22   running: 1
  ● session-a3508076-d76f-  微信 · Session 8
```

- `backend: app` = 直连桌面 App（推荐）。
- 如果报 `cannot find the DSH app cookie`：先把 DSH 桌面 App 打开再试。
- `port` 是从 Cookie 的 JWT payload 里读出来的，所以 App 重启换端口也不怕。

---

## 第 1 步：列出所有会话

```bash
$ ./tools/dshctl.py list
● session-a3508076-d76f-  微信 · Session 8
○ session-e3b87775-c0b4-  安装插件
○ session-02b63fdb-4c1e-  修复聊天记录卡顿问题
...
```

`●` 表示正在跑，`○` 表示空闲。加 `--full` 还能看到 cwd、agent 是否可用、输出 token 数：

```bash
$ ./tools/dshctl.py list --full
```

---

## 第 2 步：读一个会话（不需要 App）

```bash
$ ./tools/dshctl.py tail "安装插件" 6
### 安装插件  [session-e3b87775-…]
### cwd=/Users/you/Documents/…/default-workspace

【USER】你那边插件装得怎么样了？
【ASSISTANT】插件侧已完成：dshmarket、dsh-im、better-sidebar…
```

读的是磁盘上的转录（`~/.dsh/sessions/**/session.v4.jsonl.zstd`），所以**App 关着也能读**，也不消耗任何模型额度。

想要 JSON：

```bash
$ ./tools/dshctl.py tail "安装插件" 6 --json
{
  "sessionId": "session-e3b87775-…",
  "title": "安装插件",
  "messages": [{"role": "assistant", "text": "…"}]
}
```

---

## 第 2.5 步：实时跟随一个会话

不想反复 `tail`，就 subscribe 它的实时流：

```bash
$ ./tools/dshctl.py watch "安装插件" --lines 3
👀 跟随 安装插件（Ctrl-C 退出）

你 > 你那边插件装得怎么样了？
它 > 插件侧已完成…
它 > 正在补测试…       ← 落盘一条就打印一条
```

- 默认同时打印「半成品」逐字增量（`assistant-stream`）；嫌吵加 `--no-stream`，只在消息落盘后打印。
- 想看思考过程加 `--reasoning`；`--json` 输出原始帧，方便自己解析。
- subagent 会话会自动用父会话地址跟随，不用手动传。

---

## 第 3 步：给那个会话发一句话

```bash
$ ./tools/dshctl.py prompt "安装插件" "用三行以内汇报你现在卡在哪" --wait 90
sent -> session-e3b87775-…  {"accepted": true}
turn finished
read it with: dshctl.py tail session-e3b87775-…
```

关键点：

- 消息不是「伪造一条用户消息」进历史，而是**真的交给那个会话的 agent**跑一轮，所以它的工具、上下文、模型预设都照旧。
- `--wait 90` 会轮询 `session/list` 的 `running` 字段，等它跑完（默认 0 = 不等）。
- 想打断正在跑的轮次插队：`--steer`。

立刻看回复：

```bash
$ ./tools/dshctl.py tail "安装插件" 1
【ASSISTANT】卡在手机端配对：等用户扫一次带 dsh-pair.<code> 的新码。
```

---

## 第 4 步：进入「带菜单」的跨会话聊天

```bash
$ ./tools/dshctl.py chat "安装插件"
💬 会话：安装插件
（/help 看命令；回车=继续；4=退出）

你 > 你现在在等谁做什么？
它 > 等用户在手机 App 里删掉旧网关、再扫新二维码。

1继续 2切换 3上下文 4退出   ← 直接打字=继续
你 > 3
…最近 6 条上下文…
1继续 2切换 3上下文 4退出
你 > 4
已退出。
```

菜单刻意压成**一行**：在手机上多行选项很难点。按 `1` 会再问一句「要说什么」，**直接回车就发「继续」**；
什么都不按直接回车也等于发「继续」。

---

## 第 5 步：在微信 / IM 里用（状态外置）

聊天通道里没有终端，`input()` 读不到用户按键，所以要：

```bash
$ ./tools/dshctl.py focus "安装插件"
已进入：安装插件
（后续消息会发给它；`dshctl.py focus -` 退出）

$ ./tools/dshctl.py where
{"sessionId": "session-e3b87775-…", "title": "安装插件", "at": 1793165955}
```

然后你的机器人/agent 在每条回复末尾渲染同一行菜单，用户回数字时：

| 用户回 | agent 做的事 |
|---|---|
| `1` 或任意文字 | `dshctl.py prompt "$(where 里的会话)" "<文字>" --wait 90`，再 `tail` 取回复 |
| `2` | `dshctl.py list`，让用户选，再 `focus <新的>` |
| `3` | `dshctl.py tail <会话> 6` |
| `4` | `dshctl.py focus -` |

状态文件在 `~/.cache/dshctl-chat.json`（权限 600，只存 id/标题/时间），随时可删。

---

## 第 6 步（可选）：装成 DSH 插件

```bash
cd ~/.dsh/profiles/desktop
npm install /绝对路径/dsh-session-bridge
```

编辑同目录 `package.json`，在 `dsh.profile.bundles` 里加 `"@dylanyeung/dsh-session-bridge"`，重启 DSH。

> 发布到 npm 之后也可以直接 `npm install @dylanyeung/dsh-session-bridge`（同样要手动登记 bundle）。

> `npm install` 只装包、不登记 bundle；漏了这一步，重启后会话里不会出现 `session_*` 工具。
> 想看自己登记上没有：`python3 -c "import json;print(json.load(open('$HOME/.dsh/profiles/desktop/package.json'))['dsh']['profile']['bundles'])"`。

之后任意会话都能直接调用：

```text
session_list({ full: true })
session_read({ session: "安装插件", count: 6 })
session_send({ session: "安装插件", message: "把结论压成三行", wait: 60 })
session_chat({ action: "open", session: "安装插件" })   // 后续可连续 say / peek
session_chat({ action: "say", text: "把结论压成三行", wait: 60 })
```

---

## 常见坑

1. **短 id 不认**：`prompt`/`chat` 要完整 `session-<uuid>` 或唯一标题；`e3b87775` 这种前缀只有 `tail`/`read` 接受。
2. **标题 / 前缀要唯一**：命中多个会话时直接报错并列出候选（不会猜第一个），补全 id 即可。
3. **App 重启换端口**：不用改配置，cookie payload 里有端口；`status` 会显示当前值。
4. **desktop 专属 preset 的会话**：用 `dshctl start` 起的 helper 实例无法 compose（会报 `Unknown agent preset: computer-use`），只能直连 App。
5. **读不到 zstd**：`brew install zstd`。
6. **`watch` 一直在等新事件**：它是跟随流，不会自己退出；`Ctrl-C` 结束，`--lines 0` 只看新内容。
7. **`●` 和 `◐` 不是一回事**：`●` = 在 GUI 里打开且近 5 分钟有写入，`◐` = 打开但近期没动；两者都不代表 agent 正在跑（在跑看 RPC 的 `running`）。

---

完成。下一步可以看 [PROTOCOL.md](PROTOCOL.md) 了解每个字段，或直接在 `session_send` 之上写你自己的编排脚本（见 `examples/`）。
