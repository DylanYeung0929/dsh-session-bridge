# Changelog

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
