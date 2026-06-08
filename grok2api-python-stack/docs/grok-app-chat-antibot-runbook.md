# Grok App-Chat 403 反爬排障记录

本文记录 2026-06-08 线上 `grok2api` 的 Chat、玩法 Chat、生图同时返回 403/502 的排查结论和修复方向。后续如果再次遇到 `Request rejected by anti-bot rules`，优先按这里的顺序处理。

## 现象

公网 API 入口正常：

- `GET /` 返回 200
- `GET /v1/models` 返回 200

真正调用 Grok 网页端 app-chat 的接口失败：

- `POST /v1/chat/completions` 返回 502
- `POST /v1/function/chat/completions` 返回 502
- `POST /v1/images/generations` 返回 502

服务端日志里的上游错误是：

```json
{"error":{"code":7,"message":"Request rejected by anti-bot rules.","details":[]}}
```

这表示请求已经到达 Grok 上游，但被 Grok 的反爬规则拒绝。

## 根因

这次不是单一原因，而是三个因素叠加：

1. `curl_cffi` 不支持 FlareSolverr 刷出的 `chrome148` 指纹。
   - 容器内原始依赖不支持 `impersonate=chrome148`。
   - 已改为 `curl-cffi>=0.15.0,<0.16`，并把高版本 Chrome UA 映射到 `chrome146`。

2. `app-chat` payload 已落后于 Grok 最新网页端。
   - 旧 payload 使用 `modelName/modelMode/responseMetadata.requestModelDetails`。
   - 真实 Grok UI 当前 Fast 聊天使用 `modeId:"fast"`，并包含 `collectionIds`、`disabledConnectorIds`、`linkQuery` 等字段。

3. 直接带全局 Cloudflare cookie 调 `app-chat` 不稳定。
   - 真实浏览器会先访问 `https://grok.com/`，在当前出口 IP 和当前 SSO 下建立会话 cookie，再发 app-chat。
   - 已在 `AppChatReverse` 中增加 warmup GET，并让 app-chat 使用当前 session cookie。

此外，代理出口 IP 仍然会影响结果。同样代码下，部分节点仍会被 Grok 拒绝。

## 已修改代码

### `app/services/cf_refresh/solver.py`

目的：避免 FlareSolverr 返回 Chrome 148 后写入 `chrome148`，导致 `curl_cffi` 不支持。

当前策略：

- Chrome major `>=137` 时映射到 `chrome146`
- 同步把 UA 中的 `Chrome/<major>.0.0.0` 改成 `Chrome/146.0.0.0`

### `pyproject.toml` / `uv.lock`

目的：固化支持 `chrome146` 的依赖。

```toml
curl-cffi>=0.15.0,<0.16
```

注意：Dockerfile 使用 `uv sync --frozen`，所以改 `pyproject.toml` 后必须同步更新 `uv.lock`。

### `app/services/reverse/app_chat.py`

目的：把 app-chat 请求改成真实 Grok UI 当前可用形态。

关键点：

- 新增 `GROK_HOME = "https://grok.com/"`
- app-chat 发送前先 `GET https://grok.com/` 做 warmup
- app-chat Cookie 只放基础 `sso` 和 `sso-rw`，由 session 自己拿当前出口下的 cookie
- Chat Fast payload 使用 `modeId:"fast"`
- 保留 `request_overrides`，保证生图等调用仍可覆盖 `imageGenerationCount`、`modeId`、`enableNsfw` 等字段
- app-chat 单独覆盖为真实 Windows Chrome 风格 headers，不改全局 headers

真实 UI 成功 payload 的核心形态：

```json
{
  "temporary": false,
  "message": "say hi",
  "fileAttachments": [],
  "imageAttachments": [],
  "disableSearch": false,
  "enableImageGeneration": true,
  "returnImageBytes": false,
  "returnRawGrokInXaiRequest": false,
  "enableImageStreaming": true,
  "imageGenerationCount": 2,
  "forceConcise": false,
  "enableSideBySide": true,
  "sendFinalMetadata": true,
  "disableTextFollowUps": false,
  "responseMetadata": {},
  "disableMemory": false,
  "forceSideBySide": false,
  "isAsyncChat": false,
  "disableSelfHarmShortCircuit": false,
  "collectionIds": [],
  "disabledConnectorIds": [],
  "deviceEnvInfo": {
    "darkModeEnabled": false,
    "devicePixelRatio": 1,
    "screenWidth": 1365,
    "screenHeight": 900,
    "viewportWidth": 1365,
    "viewportHeight": 900
  },
  "modeId": "fast",
  "linkQuery": false
}
```

## 当前线上部署状态

当前线上部署使用：

- API 域名：`https://grok2api.hello4am.com`
- API 容器：`grok2api-prod-api`
- 注册控制台容器：`grok2api-prod-console`
- mihomo 容器：`sub2api-prod-mihomo`
- mihomo API：`127.0.0.1:19090`
- app-chat 代理：`http://mihomo-shared:7890`
- 当前可用节点：`2x专线-日本-1`

注意：

- 当前节点不要随意切换。
- 如果必须切节点，切完后必须重新测试 Chat、玩法 Chat、生图。
- 美国节点并不一定可用。本次测试中多个美国节点仍被 app-chat 反爬拒绝。

## 本次验证结果

本地真实浏览器测试：

- 注入服务器 SSO 后，`grok.com` 首页可登录
- `/rest/rate-limits` 返回 200
- `/rest/assets` 返回 200
- 页面内手写 `fetch('/rest/app-chat/conversations/new')` 仍可能 403
- 真实 UI 点击发送按钮返回 200

根据真实 UI 抓包改完后：

- 本地 `curl_cffi` + 真实 UI payload 返回 200
- 服务器容器内使用 `2x专线-日本-1` 返回 200
- 公网 API 验证：
  - `/v1/chat/completions` 返回 200
  - `/v1/function/chat/completions` 返回 200
  - `/v1/images/generations` 返回 200

另外，149 个 active SSO token 均已调用 `/rest/auth/set-birth-date` 补过年龄确认，全部成功。

## 玩法 Imagine WS/SSE 修复

2026-06-08 继续排查时，普通 `/v1/images/generations` 已经可用，但玩法页面的 Imagine 瀑布流仍卡在“生成中”。

确认结果：

- `GET /v1/function/imagine/sse` 能建立 SSE 连接。
- `GET /v1/function/imagine/ws` 能完成 WebSocket 升级。
- 问题不在 nginx、SSL、鉴权或公网域名。
- 原实现对玩法瀑布流使用 `n=6` 和 `stream=True`，依赖 app-chat 流式事件持续返回最终图片。
- 当前 Grok app-chat 生图流式事件不稳定，容易只返回 running，或最终没有可用图片，导致前端长时间停留在生成中。

已改为：

- WS/SSE 玩法生图使用 `n=1`
- WS/SSE 玩法生图使用 `stream=False`
- 后端收到图片后直接发送 `type:"image"` 和 `b64_json`
- 瞬时空结果、429 或上游波动不再发送 `type:"error"`，改为发送 `type:"status","status":"retrying"` 并退避重试

当前验证：

- 公网 `https://grok2api.hello4am.com/v1/function/imagine/sse` 已收到 `type:"image"`。
- 公网 `wss://grok2api.hello4am.com/v1/function/imagine/ws` 已收到 `type:"image"`。
- 日志中仍可能出现 Grok 上游 429 或 `Image generation returned no results`，这是上游额度/波动；现在会自动重试，不会把前端直接打成错误态。

后续如果玩法 Imagine 又卡住，先按这个顺序排查：

1. 从本机公网测 `/v1/function/imagine/sse`，不要只在服务器内测。
2. 从本机公网测 `/v1/function/imagine/ws`，确认 WebSocket 升级正常。
3. 看日志里是否是 429 或空结果；如果是，优先检查 token 额度和节点质量。
4. 不要先改 nginx。只有 WS 握手失败、SSE 连接失败或公网路径 404/502 时，才回头看宿主机代理配置。

## 验证命令

从服务器读取 API key 后测试公网接口：

```bash
API_KEY=$(grep '^GROK2API_API_KEY=' /etc/grok2api-rs-fork/grok.env | cut -d= -f2-)

curl -sS -i --max-time 180 https://grok2api.hello4am.com/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"grok-3","messages":[{"role":"user","content":"say hi"}],"stream":false}'

curl -sS -i --max-time 180 https://grok2api.hello4am.com/v1/function/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"grok-3","messages":[{"role":"user","content":"say hi"}],"stream":false}'

curl -sS -i --max-time 300 https://grok2api.hello4am.com/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"grok-imagine-1.0","prompt":"a small blue cube on a white table","n":1,"size":"1024x1024","response_format":"url"}'
```

验证当前 mihomo 出口：

```bash
docker exec grok2api-prod-console sh -lc 'python - <<PY
import requests
proxies={"http":"http://mihomo-shared:7890","https":"http://mihomo-shared:7890"}
print(requests.get("https://www.cloudflare.com/cdn-cgi/trace",proxies=proxies,timeout=20).text)
PY'
```

查看 Grok API 日志：

```bash
docker logs --since 10m grok2api-prod-api 2>&1 | grep -E \
  'AppChatReverse|Chat connected|Chat failed|images/generations|Response: POST|anti-bot|403|502'
```

## 后续如果再次 403

优先按以下顺序排查：

1. 确认节点是否被切换。
   - 先查 mihomo 当前 `GLOBAL` 和主选择组。
   - 当前已知可用节点是 `2x专线-日本-1`。

2. 确认是否仍然是同一个上游错误。
   - 如果日志仍是 `Request rejected by anti-bot rules`，优先怀疑节点或 app-chat 请求形态。

3. 用真实浏览器重新抓 Grok UI 的 app-chat 请求。
   - 看 payload 是否又新增/删除字段。
   - 看 headers 的 `sec-ch-ua`、`x-statsig-id`、`baggage`、`sentry-trace` 是否有明显变化。

4. 用同一个 SSO token 做分层测试。
   - `grok.com` 首页能否 200
   - `/rest/rate-limits` 能否 200
   - `/rest/auth/set-birth-date` 能否 200
   - `/rest/app-chat/conversations/new` 是否 200

5. 如果真实 UI 能 200，但接口 403。
   - 对比真实 UI payload 和 `AppChatReverse.build_payload()`。
   - 对比真实 UI headers 和 `_apply_app_chat_browser_headers()`。
   - 检查 warmup GET 是否执行，是否在同一个 session 内发 POST。

6. 如果真实 UI 也 403。
   - 优先换节点。
   - 普通共享机场节点可能随时被 Grok 风控。
   - 更稳的方案是给 grok2api 单独配置低滥用率出口，不与其他项目共用全局 mihomo。

## 不建议的方向

- 不要只反复刷新全局 Cloudflare cookie。当前问题不只是 `cf_clearance`。
- 不要只切美国节点。本次美国节点多次验证仍 403。
- 不要把 app-chat 修复扩散到所有 reverse 接口。当前 header/payload 覆盖只针对 app-chat，避免影响上传、资产、rate-limits、WebSocket 等其他路径。
- 不要把 API key、SSO token、mihomo secret 写进文档或仓库。
