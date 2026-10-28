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
| `user/message` | 用户消息；文本在 `data.content[]`（元素 `type:"text"`） |
| `assistant/message` | 助手消息；文本在 `data.message.content[]`（**多包一层 `message`**） |
| `tool/call` | 工具调用（`data.name` / `data.arguments`） |
| `tool/result` | 工具结果 |
| `turn/start` / `step/start` 等 | 生命周期事件 |

两种事件的载荷**形状不对称**：只读 `data.message.content` 会静默丢掉全部用户消息（0.1.1 修复），
`dshtool.msg_content()` 先看 `data.message.content`、再回退到 `data.content`。

`dshtool.py` 只抽取 `user/message` 与 `assistant/message` 的文本，所以 App 关着也能读。

会话元数据（标题、cwd、tokenUsage）在：

```
~/.dsh/storages/session_projcache/sessions/<sessionId>.json
```

路径形如 `record.rows.title.val`、`record.identity.cwd`、`record.rows.tokenUsage.val.totals`。

`session.lock` 是给**跨客户端互斥**用的 flock：锁被持有 = 「该会话正在某个客户端里打开着」，
**不等于** agent 正在跑（实测 23 个会话里 11 个被持有、只有 1 个 `running`）。判断是否在跑只能看 RPC 的 `running`；
离线场景可以组合「flock 被持有 + 转录 mtime < 5 分钟」近似为「活跃」。

---

## 6. WebSocket 流：`session/follow`

实时跟随不走 HTTP，而是走网关唯一的流多路复用 WebSocket：

```
ws://127.0.0.1:<port>/api/remote.mux
Cookie: dsh-auth-<suffix>=v1.<payload>.<sig>
```

握手鉴权与 HTTP 同源：upgrade 时按 `Host`/`Origin` fence 与同一个 `dsh-auth-*` cookie 判定 peer，
成功返回 `101` + 合法的 `Sec-WebSocket-Accept`。客户端帧（一行一个 JSON 文本）：

```json
{"type":"open","streamId":"<任意非空字符串>","endpoint":"session/follow","payload":{"args":{"request":{…}}}}
{"type":"item","streamId":"…","value":{…}}
{"type":"end","streamId":"…"}
{"type":"cancel","streamId":"…"}
```

服务端帧：`{"type":"item","streamId","value"}`、`{"type":"end","streamId"}`、
`{"type":"error","streamId","error":{"code","message","details"}}`。
服务端每 2s ping 一次，连续两次无活动就 terminate —— 客户端必须回 pong（`tools/ws.py` 自动处理）。

### 6.1 `session/follow` 的 request

```json
{
  "address": {"kind": "session", "sessionId": "session-<uuid>"},
  "assistantStream": true,
  "maxMessages": 1
}
```

- `address` 另一种形状是 subagent 会话：
  `{"kind":"subagent","parentSessionId":"session-<uuid>","childSessionId":"<uuid>","mode":"one-shot|continuable|unknown"}`。
  subagent 会话**必须**用父地址，否则报 `session/agent-busy subagent Sessions require their durable parent address`。
- `assistantStream`（可选，`true`）：带上才有逐字增量帧。
- `maxMessages`（可选）：限制开场 snapshot 携带的历史条数。
- `turnWindow`（可选）：`{"minMessages":n,"minTurns":n}`，按轮次切窗。

### 6.2 服务端 `value` 的三种形状

1. **snapshot**（开场基线，只来一次）

```json
{"type":"snapshot","header":{…},"cursor":42,"records":[{"type":"event","event":{…}}],
 "hasMore":false,"projections":{…},"assistantStream":{"revision":0}}
```

`cursor` 是基线最后一条事件的 `seq`；之后的 event 从 `cursor+1` 起连续。

2. **event**（增量）

```json
{"type":"event","event":{"type":"user/message","seq":43,"time":1793…,"data":{…}}}
```

读取文本时注意形状差异：`user/message` 在 `data.content`，`assistant/message` 在 `data.message.content`（见 §5）。

3. **assistant-stream**（仅 `assistantStream:true`）

```json
{"type":"assistant-stream","frame":{"type":"start","attemptId":"…","revision":0,"startedAfterSeq":43,"turn":7,"step":1}}
{"type":"assistant-stream","frame":{"type":"chunk","attemptId":"…","revision":1,"index":0,"time":…,
                                    "chunk":{"type":"text-delta","index":0,"text":"你"}}}
{"type":"assistant-stream","frame":{"type":"end","attemptId":"…","revision":2,"index":1,
  "outcome":{"kind":"committed","eventType":"assistant/message","seq":44}}}
```

- `chunk.type` 另有 `reasoning-delta`、`tool-call-delta`（`{index,id,name?,argumentsDelta}`）等。
- `revision` 必须严格 `+1`，乱序或跳号会被断开（4000）。
- `end.outcome.kind` 是 `committed`（已落盘，附 `eventType`/`seq`）或 `abandoned`（本轮作废）。

`dshctl watch` 的取舍：先本地回放最后 N 条（`_recent()`），再开 `maxMessages:1` 的流；
只打印 `seq > cursor` 的 event；若同一段正文已经通过 `assistant-stream` 逐字打印过，
落盘的 `assistant/message` 就跳过，避免重复。

---

## 7. 已知边界

1. **短 id 不被 RPC 接受**：`session/prompt` 必须完整 `session-<uuid>`；`session/list` 不认标题，标题解析在客户端做。
2. **desktop 专属 preset**：`computer-use` 等 preset 只有桌面 App 能 compose。用 `dsh` helper web 实例续跑这类会话会报
   `resume failed for session "…": RemoteError: Unknown agent preset: computer-use`。
3. **headless one-shot 不接受带 preset 的会话**（`assertAdoptable`）：会对已有 GUI 会话报
   `runs under agent preset "standard", which the one-shot runner does not compose`。
4. **端口漂移**：App 重启后端口可能变（例如 51413 → 51499），所以端口必须从 cookie payload 读，不要硬编码。
5. **Cookie 会过期**：payload 有 `expiresAt`；过期后重新打开 App 或 `/?token=` 刷新即可。
6. **`session.lock` ≠ running**：锁只表示「打开着」，见 §5。
7. **端口不能靠"能不能连上"判断**：本机其它服务也会应答 `/`（随机端口实测返回 `404 Not found`）；
   只认 `2xx/3xx` 或「`401/403` 且 body 含 `dsh`」（`_port_alive` 的实现）。

---

## 8. 错误码速查

| code | 触发场景 |
|---|---|
| `gateway/arguments-invalid` | args 里字段名不对（如 `missing "_request"`） |
| `gateway/input-invalid` | 字段值不合法（枚举值、类型） |
| `session/agent-busy` | subagent 会话未用父地址（`subagent Sessions require their durable parent address`） |
| `session/follow` 静默断开 | WS 未回 pong（服务端 2s 一次心跳，两次无活动即 terminate） |
| `unauthorized` / 401 | cookie 缺失或过期 |
| `no session matching` | 客户端解析失败（非 RPC 错误） |
