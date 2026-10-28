# 协议说明（逆向所得）

本文件记录 `dshctl` 依赖的全部事实：鉴权怎么来、请求长什么样、哪些参数名不统一、错误码含义、以及已知边界。

---

## 1. 传输层

```
POST http://127.0.0.1:<port>/api/<namespace>/<method>
Content-Type: application/json
Cookie: dsh-auth-<suffix>=v1.<payload>.<sig>
```

请求体（`client-request`）：

```json
{
  "type": "client-request",
  "rpcId": "0f8a…（uuid，响应用同一个）",
  "method": "session/prompt",
  "payload": { "args": { "request": { … } } }
}
```

成功响应：

```json
{ "type": "server-response", "rpcId": "0f8a…", "result": { "ok": true, "value": … } }
```

失败响应：

```json
{ "type": "server-response", "rpcId": "0f8a…",
  "result": { "ok": false, "error": { "code": "gateway/input-invalid", "message": "…", "details": {…} } } }
```

`rpcId` 必须回显一致；`ok:false` 时 `dshctl` 直接抛出 `error` 的 JSON。

---

## 2. 鉴权

桌面 App 是一个 Chromium/Electron 应用，它的本地 RPC 用 Cookie 鉴权：

- Cookie 库：`~/Library/Application Support/@deepseek-ai/dsh-desktop/Cookies`（SQLite）
- 取行：`host_key = '127.0.0.1' AND name LIKE 'dsh-auth%'`
- **`value` 列即明文**（`v1.<base64url payload>.<signature>`），`encrypted_value` 为空 —— 不需要解 Keychain。
- payload 是 JWT 的中间段，解出来含：

```json
{ "authority": "127.0.0.1:51413", "expiresAt": 1793…, … }
```

- 选择策略：按 `expires_utc` 降序，取第一条且 `authority` 里的端口仍可连接的。
- 端口从 payload 读，所以 App 重启换端口无需改配置。

> `dsh web` 打印的 URL 形如 `http://127.0.0.1:<port>/?token=<tok>`；用该 URL 请求会 `303` + `Set-Cookie` 下发同一个 `dsh-auth-*` cookie。`dshctl start` 的 helper 走的就是这条路。

---

## 3. 命名空间与方法

端点名是 `<namespace>/<method>`（斜杠，不是点号）。

| namespace | 用途 |
|---|---|
| `session` | 会话：list / search / create / rename / fork / **prompt** / updateQueue / cancel / page / follow / projections / control / selectModel / modelCatalog |
| `workspace` | 工作区 |
| `terminal` | 终端 |
| `job` | 后台任务 |
| `skills` / `settings` / `credentials` / `account` | 配置类 |
| `fileReferences` / `directoryPicker` / `HTML` / `speech` / `cotTranslation` | 其它 |

`session` 里最常用的三个：

| 方法 | args 形状 |
|---|---|
| `session/list` | `{"_request": {}}` |
| `session/prompt` | `{"request": {…}}` |
| `session/page` | 分页读取消息 |

**注意**：`list` 用 `_request`，`prompt` 用 `request`。写错会得到 `missing "_request"` / `missing "request"`。

---

## 4. session/prompt 请求体

严格 schema（`additionalProperties: false`）：

```json
{
  "requestId": "uuid",
  "sessionId": "session-<uuid>",
  "content": [
    { "type": "text",  "text": "hello" },
    { "type": "image", "mediaType": "image/png", "data": "<base64>" },
    { "type": "file",  "receiptId": "…" }
  ],
  "mode": "queue",              // 枚举：queue | steer
  "clientTimeZone": "Asia/Shanghai"
}
```

- `mode: "queue"` —— 排在当前轮次之后（默认）。
- `mode: "steer"` —— 打断当前轮次，插进去。
- 写成 `followup` 之类会得到 `gateway/input-invalid … failed boundary validation`。

返回：`{"accepted": true}`。真正跑完要看 `session/list` 里该会话的 `running` 变 `false`。

---

## 5. 读取回复（不依赖 RPC）

转录在：

```
~/.dsh/sessions/<项目编码>/session-<uuid>/session.v4.jsonl.zstd
```

它是**多帧拼接的 zstd 流**，用 `zstd -dc` 解开后逐行 JSON：

| `type` | 说明 |
|---|---|
| `user/message` | 用户消息；文本在 `data.message.content[]`（`type:"text"`） |
| `assistant/message` | 助手消息，结构同上 |
| `tool/call` | 工具调用（`data.name` / `data.arguments`） |
| `tool/result` | 工具结果 |
| `turn/start` / `step/start` 等 | 生命周期事件 |

`dshtool.py` 只抽取 `user/message` 与 `assistant/message` 的文本，所以 App 关着也能读。

会话元数据（标题、cwd、tokenUsage）在：

```
~/.dsh/storages/session_projcache/sessions/<sessionId>.json
```

路径形如 `record.rows.title.val`、`record.identity.cwd`、`record.rows.tokenUsage.val.totals`；`session.lock` 与转录 mtime 可判断「是否活跃」。

---

## 6. 已知边界

1. **短 id 不被 RPC 接受**：`session/prompt` 必须完整 `session-<uuid>`；`session/list` 不认标题，标题解析在客户端做。
2. **desktop 专属 preset**：`computer-use` 等 preset 只有桌面 App 能 compose。用 `dsh` helper web 实例续跑这类会话会报
   `resume failed for session "…": RemoteError: Unknown agent preset: computer-use`。
3. **headless one-shot 不接受带 preset 的会话**（`assertAdoptable`）：会对已有 GUI 会话报
   `runs under agent preset "standard", which the one-shot runner does not compose`。
4. **端口漂移**：App 重启后端口可能变（例如 51413 → 51499），所以端口必须从 cookie payload 读，不要硬编码。
5. **Cookie 会过期**：payload 有 `expiresAt`；过期后重新打开 App 或 `/?token=` 刷新即可。

---

## 7. 错误码速查

| code | 触发场景 |
|---|---|
| `gateway/arguments-invalid` | args 里字段名不对（如 `missing "_request"`） |
| `gateway/input-invalid` | 字段值不合法（枚举值、类型） |
| `unauthorized` / 401 | cookie 缺失或过期 |
| `no session matching` | 客户端解析失败（非 RPC 错误） |
