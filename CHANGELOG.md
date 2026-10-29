# Changelog

## Unreleased
- README 新增「和 DSH 原生 `session_search` 的关系」：原生是只读的历史检索（实验性 bundle，
  默认不开），写消息/实时跟随/会话清单/脱离宿主的 CLI 仍由本插件提供。

## 0.2.0 — 2026-10-29

**新功能**
- `dshctl watch <会话>`：走 `/api/remote.mux` + `session/follow` 实时跟随（`--lines/--no-stream/--reasoning/--json`），
  落盘消息与逐字增量二选一或都要；`tools/ws.py` 是纯标准库 RFC 6455 客户端（握手校验、掩码、分片、ping/pong、32 MiB 上限）。
- 插件新增第 4 个工具 `session_chat`：`open/say/peek/close/status`，和 `dshctl chat` 共用 `~/.cache/dshctl-chat.json` 焦点。
- `dshctl doctor`：打印平台、App 数据目录、Cookie 库、DSH CLI、helper 状态、auth cookie 端口、活端口，排障第一步。
- `tail --full` 透传到 `dshtool --json` 的完整事件行。

**跨平台**
- 新增 `tools/appenv.py`：Electron `userData` 三平台路径（macOS `~/Library/Application Support`、Linux `$XDG_CONFIG_HOME`、
  Windows `%APPDATA%`），内置 CLI 候选，`default_ports()`，JWT authority 解析，以及 Chromium cookie 解密：
  Linux v10（AES-128-CBC + PBKDF2 `peanuts`）、v11（`secret-tool`/libsecret）、Windows v10（DPAPI + AES-256-GCM）。
  加密值解不开时返回 `None`（不伪造），macOS 走明文 `value` 列。
- npm 包名改为 `@dylanyeung/dsh-session-bridge`（不带 scope 的 `dsh-session-bridge` 已被其他作者占用，
  仓库名不变；`cordis.patch.yml`、README、教程、CI 断言同步）。
- npm 包里不再夹带 `__pycache__/*.pyc`（`files` 只留 `tools/*.py`、`tests/*.py|mjs`），0.2.0 包体 132 kB → 56 kB。
- `package.json` 去掉 `os: ["darwin"]` 硬门禁（否则 Linux/Windows 用户 `npm install` 直接 EBADPLATFORM，
  与 beta 支持的声明矛盾）。
- `dshctl` 不再硬编码 macOS 路径；`auth cookie` 选择改为遍历全部 `dsh-auth-*` 取第一个探活成功的端口。

**修复**
- `session/follow` 的 args 是 `{"request":{…}}`，不是扁平 `address`；subagent 会话必须用
  `{"kind":"subagent","parentSessionId","childSessionId","mode"}` 地址（否则 `session/agent-busy`）。
- 开场 snapshot 的 `cursor` 用作去重基线，只打印 `seq > cursor` 的事件；`object.__init__` 空正文不再输出空行。
- `doctor` 的 `live ports` 原先只看 `default_ports()`，现在并入 cookie 里的端口。

**测试 / CI**
- `tests/`：68 个离线单测（消息提取、resolve 消歧、lock 语义、跨平台路径、AES/GCM 向量、WebSocket 帧、watch 渲染、插件契约）。
- `.github/workflows/ci.yml`：python 3.10/3.13 × ubuntu/macOS，`py_compile` 覆盖 `ws.py`/`appenv.py`；node 20/22。

---

## 0.1.1 — 2026-10-28

**修复**
- **用户消息丢失**：`user/message` 的文本在 `data.content`，`assistant/message` 才在 `data.message.content`；旧代码只读后者，
  导致 `tail` / `session_read` / `chat` 的 `3上下文` 里 USER 侧全空。新增 `dshtool.msg_content()` 兼容两种形状。
- **读错会话**：同名或同前缀的会话原先静默取第一条（`dshtool.resolve` / `dshctl.resolve`）。现在解析顺序固定为
  完整 id → 唯一 id 前缀 → 唯一标题 → 唯一标题子串，命中多个立即报错并列出候选；`prompt session-<不存在>` 也不再直接当成功。
- **`--full` 无效**：`dshctl list --json` 不管有没有 `--full` 都吐完整行。现在默认给紧凑字段
  （id/标题/cwd/running/updatedAt/outputTokens），`--full` 才给原始完整行；插件 `session_list` 同步。
- **端口误判**：`_port_alive` 把任何 `HTTPError` 当成「App 在跑」，本机别的服务回 `404` 也会命中。现在只认
  `200/3xx`，或 `401/403` 且 body 含 `dsh web` 提示。
- **API key 落盘**：`dshctl start` 不再把 `DEEPSEEK_API_KEY` 写进 `~/.cache/dshctl.json`（只经子进程 env 传递）；
  旧版本留下的 `key` 字段会在下次 `start` 时自动清除。
- **活跃标记失真**：原先只要 `session.lock` 存在就算活跃，而 23 个会话全都有这个文件。现在用 flock 探测：
  `●` = 打开且近 5 分钟有写入，`◐` = 仅打开（`session.lock` 被持有 ≠ `running`，PROTOCOL.md 已补说明）。
- **`chat` 菜单语义**：空行直接发「继续」（原先重画菜单），`1` 后再空行也发「继续」，与 README/教程一致。
- **退出码**：`dshctl tail` 透传读取端退出码（歧义 → 2）；`dshtool read` 缺参数不再抛 `IndexError`；
  两个 CLI 都按 SIGPIPE 默认行为处理 `| head`（不再打 `BrokenPipeError`）。

**文档**
- `docs/PROTOCOL.md`：修正 §5 载荷形状写反的问题；补 `session.lock` 语义与端口探测边界。
- `docs/TUTORIAL.md` / README：补充插件必须在 `dsh.profile.bundles` 里登记（`npm install` 不会自动登记）、
  `●/◐` 的区别、歧义报错的排查行。

**测试 / CI**
- `tests/test_dshtool.py`：`msg_content` 两种形状、`text_of`、`resolve`（精确/前缀/裸标题/歧义/未知）、
  `lock_held`（子进程持锁 → True，释放 → False）。
- `.github/workflows/ci.yml`：python 3.9/3.12 × ubuntu/macOS，跑 `py_compile`、`node --check`、`unittest`。

## 0.1.0 — 2026-10-28

首个发布版本。

**CLI (`tools/dshctl.py`)**
- `status` / `list` / `tail` / `prompt` / `chat` / `focus` / `where` / `raw` / `start` / `stop`
- 直连运行中的 DSH 桌面 App（复用其 `dsh-auth-*` cookie，端口从 JWT payload 读取）
- App 未运行时可用 `dshctl start` 起 helper web 实例兜底
- 全部命令支持 `--json`
- `chat` 每轮后输出一行菜单 `1继续 2切换 3上下文 4退出`
- `focus` / `where` 用 `~/.cache/dshctl-chat.json`(600) 记住跨会话对话对象，供 IM 场景使用

**离线读取 (`tools/dshtool.py`)**
- `list` / `read <会话> [N] [--json]`，直接解 `session.v4.jsonl.zstd`，App 关闭也可用

**DSH 插件 (`lib/index.mjs`)**
- 注册三个工具：`session_list`、`session_read`、`session_send`

**文档**
- README（安装/CLI/插件/协议摘要/安全/排错）
- `docs/PROTOCOL.md`（逆向协议、错误码、已知边界）
- `docs/TUTORIAL.md`（10 分钟教程）
- `examples/`（两个可复制的小脚本）
