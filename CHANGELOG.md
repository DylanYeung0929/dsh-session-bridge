# Changelog

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
