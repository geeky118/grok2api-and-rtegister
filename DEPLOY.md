# 部署文档

## 服务器信息

- **IP**: `<SERVER_IP>`
- **用户**: `<SSH_USER>`
- **SSH 密钥**: `<SSH_KEY_PATH>`
- **SSH 命令**:
  ```bash
  ssh -i "<SSH_KEY_PATH>" <SSH_USER>@<SERVER_IP>
  ```

## 服务架构

```
┌─────────────────────────────────────────────────┐
│  Deployment host                                │
│                                                  │
│  ~/grok-stack/                                   │
│  ├── docker-compose.yml                          │
│  ├── image_override.py  (生图修复 volume mount)   │
│  ├── models_override.py (额度配置 volume mount)   │
│  ├── runtime/grok2api/data/config.toml           │
│  ├── apps/console/        (grok-register)        │
│  └── apps/console/runtime/tasks/task_*/          │
│                                                  │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐  │
│  │  WARP    │  │FlareSolverr  │  │ grok2api  │  │
│  │socks5:// │  │  :8191       │  │  :18089   │  │
│  │warp:1080 │  │              │  │           │  │
│  └────┬─────┘  └──────┬───────┘  └─────┬─────┘  │
│       └───────────────┬┘                │        │
│                       └─────────────────┘        │
│                                                  │
│  ┌──────────────────────┐                        │
│  │ grok-register        │                        │
│  │ console :18600       │                        │
│  │ DrissionPage + Xvfb  │                        │
│  └──────────────────────┘                        │
└─────────────────────────────────────────────────┘
```

## 端口映射

| 端口 | 服务 | 说明 |
|------|------|------|
| 18089 | grok2api | OpenAI 兼容 API + 管理面板 (`/admin/login`) |
| 18600 | grok-register | 注册控制台 Web UI |

## 认证信息

| 名称 | 值 |
|------|------|
| app_key (管理面板) | `<APP_KEY>` |
| api_key (API 调用) | `<API_KEY>` |

## Docker 容器

| 容器名 | 镜像 | 说明 |
|--------|------|------|
| grok-stack-warp-1 | caomingjun/warp | Cloudflare WARP 代理 |
| grok-stack-flaresolverr-1 | ghcr.io/flaresolverr/flaresolverr | CF Clearance 自动刷新 |
| grok-stack-grok2api-1 | ghcr.io/xeanyu/grok2api | Python grok2api (curl_cffi) |
| grok-stack-console-1 | grok-register | 注册控制台 + DrissionPage |

---

## 已修复问题

### 0. Grok app-chat 403 反爬修复 (2026-06-08)

**问题**: `Chat`、`玩法 Chat`、`生图` 同时返回 502，日志中上游错误为 `Request rejected by anti-bot rules`。

**结论**: Grok 网页端 app-chat 请求格式已变化，旧的 `modelName/modelMode` payload 和全局 CF cookie 方式会被反爬拒绝。已根据真实浏览器 UI 抓包改为 `modeId:"fast"` payload、app-chat 专用浏览器 headers，并在发 app-chat 前 warmup `https://grok.com/` 建立当前会话 cookie。

**当前可用节点**: `2x专线-日本-1`。不要随意切换；切换后需要重新验证 Chat、玩法 Chat 和生图。

**维护 runbook**: [grok-app-chat-antibot-runbook.md](grok2api-python-stack/docs/grok-app-chat-antibot-runbook.md)

### 0a. 玩法 Imagine WS/SSE 卡在生成中修复 (2026-06-08)

**问题**: 普通 `/v1/images/generations` 已经可用，但玩法页面 Imagine 瀑布流的 `/v1/function/imagine/ws` 或 `/v1/function/imagine/sse` 会长时间停在“生成中”。

**结论**: WS/SSE 传输、nginx、SSL 和鉴权都是正常的；根因是玩法瀑布流原先使用 `n=6` + `stream=True`，而当前 Grok app-chat 生图流式最终事件不稳定。

**修复**:
- WS/SSE 玩法生图改为 `n=1`
- WS/SSE 玩法生图改为 `stream=False`
- 成功时直接返回 `type:"image"` + `b64_json`
- 上游 429 或空结果改为 `status:"retrying"` 并退避重试，不再直接向前端发送错误态

**验证**:
- 本机公网请求 `https://grok2api.hello4am.com/v1/function/imagine/sse` 已收到 `type:"image"`
- 本机公网连接 `wss://grok2api.hello4am.com/v1/function/imagine/ws` 已收到 `type:"image"`

### 1. 图片生成 403 修复 (2026-05-17)

**问题**: `POST /v1/images/generations` 返回 403 "Model is not found"

**根因**: `image.py` 中调用 `GrokChatService().chat()` 时传递 `model=None`，上游无法识别

**修复**: 传递 `model="grok-3"` + `mode="MODEL_MODE_FAST"`，移除 `overrides["modeId"] = "auto"`

**持久化**:
- `~/grok-stack/image_override.py` — 从容器内 `/app/app/services/grok/services/image.py` 复制后修改
- volume mount: `./image_override.py:/app/app/services/grok/services/image.py:ro`
- config.toml: `image_format = "b64_json"`

**API 调用**:
```bash
curl -X POST http://<SERVER_IP>:18089/v1/images/generations \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-imagine-1.0","prompt":"a cat","n":1,"size":"720x1280","response_format":"b64_json"}'
```

### 2. 免费账号额度配置 (2026-05-17)

**问题**: 默认 `BASIC__DEFAULT_QUOTA = 80` 远高于实际值，导致 429 错误

**修复**:

| 参数 | 原值 | 新值 |
|------|------|------|
| `BASIC__DEFAULT_QUOTA` | 80 | 25 |
| `EFFORT_COST[HIGH]` | 4 | 8 |
| `EFFORT_COST[LOW]` | 1 | 1 |

额度计算: 对话 25÷1=25 次/天，生图 25÷8≈3 次/天

**持久化**:
- `~/grok-stack/models_override.py` — volume mount 到 `/app/app/services/token/models.py:ro`

### 3. Cloud Mail 邮箱集成 (2026-05-17)

**背景**: grok-register 原生不支持 Cloud Mail (cloudflare_temp_email)，需要通过自定义 catch-all 域名接收验证码。

**修改文件**:

1. **email_register.py** — 添加 `cloudmail` provider
   - 不创建邮箱，直接随机生成 catch-all 地址
   - `_get_cloudmail_token()` → `_create_cloudmail_email()` → `_fetch_cloudmail_emails()`

2. **DrissionPage_example.py** — SOCKS5 代理修复
   - DrissionPage `set_proxy()` 不支持 socks5
   - 改为 `co.set_argument(f"--proxy-server={proxy}")`

3. **apps/console/app.py** — 配置字段传播到新任务
   - `merged_defaults()` 添加 `temp_mail_provider` 和 `cloudmail_admin_email`
   - 环境变量映射添加对应字段

4. **docker-compose.yml** — console 服务环境变量
   ```yaml
   GROK_REGISTER_DEFAULT_TEMP_MAIL_PROVIDER: cloudmail
   GROK_REGISTER_DEFAULT_CLOUDMAIL_ADMIN_EMAIL: <CLOUDMAIL_ADMIN_EMAIL>
   GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE: <TEMP_MAIL_API_BASE>
   GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD: <TEMP_MAIL_ADMIN_PASSWORD>
   GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN: <TEMP_MAIL_DOMAIN>
   ```

### 4. Turnstile 反检测 + 僵尸进程修复 (2026-05-19 ~ 2026-05-21)

**问题**:
- grok-register 任务卡在 Cloudflare Turnstile CAPTCHA
- 容器内积累 4574 个僵尸进程，`can't start new thread` 错误
- 浏览器每轮重启后连接失败 (`BrowserConnectError` on port 9222)
- 连续注册 20+ 轮后 Chrome 在 Turnstile 步骤崩溃，进程静默死亡

#### 4a. Turnstile 反检测 (2026-05-19)

- `turnstilePatch/script.js` v3.0 — 10 项反检测: navigator.webdriver, plugins, languages, WebGL, chrome.runtime 等
- `getTurnstileToken()` 重写为重试 + 详细日志

**Chrome 反检测参数**:
```
--disable-blink-features=AutomationControlled
--disable-features=AutomationControlled
--disable-infobars
--window-size=1920,1080
--start-maximized
```

#### 4b. 浏览器重启崩溃修复 (2026-05-20)

**问题**: `restart_browser()` 后 `Chromium(co)` 连接 9222 端口失败

**根因**: `auto_port()` 复用旧端口，旧 Chrome 进程未完全释放

**修复**:
- `_pick_free_port()` — 每轮绑定随机可用端口 (`co.set_local_port(port)`)
- `stop_browser()` 增加 `time.sleep(1)` 等待端口释放
- `restart_browser()` 增加 `time.sleep(1)` 间隔

#### 4c. 僵尸进程根因修复 (2026-05-21)

**根因**: Docker 容器内 Python 作为 PID 1，不会回收子进程僵尸。每次浏览器重启遗留 chrome 子进程，最终堆积到几千个。

**关键修复**: `docker-compose.yml` 所有服务添加 `init: true`
```yaml
console:
  init: true  # tini 作为 PID 1，自动回收僵尸进程
```

**`stop_browser()` 改进**:
1. `browser.quit()` 优雅关闭
2. `pkill -9 -f <temp_dir>` 杀当前会话进程
3. `pgrep -x chrome/chrome_crashpad` 清理所有孤儿 chrome 进程
4. 清理所有 `/tmp/chrome_run_*` 临时目录

#### 4d. 每 N 轮定期重启 + Turnstile 超时保护 (2026-05-21)

**问题**: 连续注册 20+ 轮后 Chrome 内存泄漏或 x.ai 加大 Turnstile 难度，导致浏览器崩溃

**修复**:
- 主循环改为每 5 轮定期重启浏览器，失败时立即重启
- `_check_browser_alive()` — 通过 CDP `Runtime.evaluate` 检测浏览器是否存活
- `getTurnstileToken()` 浏览器断开时立即放弃重试，不再空转
- 错误日志每 5 次重试打印一次

**测试结果**:
- Task 17: 43 成功 / 4 失败 / 47 轮，成功率 ~91%
- Task 40: 23 成功 / 2 失败（切换新域名后，定期重启前）

### 5. x.ai 域名封禁 (2026-05-21)

**问题**: x.ai 封禁了注册邮箱域名，提交邮箱后返回"您的邮箱域名已被拒绝"

**表现**: 脚本打印"已填写邮箱并点击注册"后继续轮询验证码，永远收不到（0 封邮件）

**修复**:
- `fill_email_and_submit` 添加域名拒绝错误检测，点击注册后等待 2 秒检查页面错误信息
- 切换到新的可用邮箱域名
- `docker-compose.yml` 中 `TEMP_MAIL_DOMAIN` 改为新的可用邮箱域名

**检测关键词**: `已被拒绝` / `has been rejected` / `blocked` / `not allowed`

---

## 踩坑记录

- Console 每次创建新任务会从模板复制文件，只改单个 task 目录不够，必须改 app.py + docker-compose 环境变量
- GCP 防火墙需手动开端口: `gcloud compute firewall-rules create grok-console-allow-18600 --allow tcp:18600`
- `pkill -f` 需要容器内有 pkill 命令（Alpine 用 busybox，Debian/Ubuntu 默认有）
- Python f-string 在 SSH heredoc 中嵌套引号会出错，改用 `.format()` 或写入文件
- 已有 token 的 quota 值保存在 `data/token.json`，不会因默认值变更自动更新
- `sync_usage` 会从 Grok API 同步实际剩余额度，覆盖本地值
- **Docker PID 1 僵尸问题**: Python 作为 PID 1 不回收僵尸，必须加 `init: true`
- **DrissionPage `run_js` 无 timeout**: Chrome crash 后 `run_js()` 无限挂起，需显式传 `timeout` 参数
- **`run_cdp` 不在 `Chromium` 类上**: 在 `ChromiumPage` / `ChromiumTab` 上，检查浏览器存活用 `page.run_cdp()`
- **supervisor 死进程检测延迟**: 进程死亡后 supervisor 可能未及时检测到，需要手动更新 DB 状态
