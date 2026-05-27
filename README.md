# grok2api + grok-register 部署说明

本仓库把 `grok2api` API 网关、`grok-register` 注册控制台、WARP 出口、FlareSolverr 和注册运行环境放在同一套部署结构里，适合直接部署成一套可注册、可入池、可调度的服务。

## 目录结构

- `grok2api-python-stack/vendor/grok2api/`：grok2api 服务代码、管理后台、Token 池、配置默认值。
- `grok2api-python-stack/apps/console/`：随栈部署的 grok-register 控制台。
- `grok2api-python-stack/deploy/`：部署用 compose 文件和辅助脚本。
- `grok-register/`：独立版 grok-register 控制台副本。
- `DEPLOY.md`：运维记录，敏感值已替换为占位符。

## 服务组成

| 服务 | 默认端口 | 说明 |
|------|----------|------|
| `grok2api` | `8000` | OpenAI 兼容 API 与管理后台 |
| `console` | `18600` | grok-register 任务控制台 |
| `warp` | `1080` | 默认 SOCKS5 出口，供注册与代理请求使用 |
| `flaresolverr` | 容器内 `8191` | 自动刷新 Cloudflare clearance |

常用入口：

- grok2api 健康检查：`GET /health`
- grok2api 管理配置：`GET /v1/admin/config`、`POST /v1/admin/config`
- grok-register 任务接口：`GET /api/tasks`、`POST /api/tasks`

## 部署前准备

服务器建议准备：

- Docker Engine 与 Docker Compose Plugin
- 至少 2GB 内存；注册任务较多时建议 4GB+
- 可访问 Docker 镜像源与 x.ai 相关站点的网络环境
- 一个可用的临时邮箱服务或自建邮箱 API
- 一个长期保存运行数据的部署目录，例如 `/opt/grok-stack`

不要把以下真实值提交到仓库：

- `app_key`、`api_key`
- SSO Token
- SSH 用户、服务器 IP、私钥路径
- 邮箱域名、邮箱后台账号、邮箱后台密码
- `runtime/`、日志、SQLite 数据库、浏览器 Profile

## Docker 部署

推荐使用 `grok2api-python-stack/docker-compose.yml` 部署整套服务。

```bash
cd /opt
git clone <REPO_URL> grok-stack
cd grok-stack/grok2api-python-stack
sudo install -d -m 700 /etc/grok-stack
sudo install -m 600 .env.example /etc/grok-stack/grok2api.env
```

编辑仓库外部的环境文件，避免把真实 `.env` 放在 git 工作区里：

```bash
sudo nano /etc/grok-stack/grok2api.env
```

至少确认这些值：

```dotenv
GROK_STACK_CONSOLE_PORT=18600
GROK2API_HOST_PORT=8000
GROK2API_APP_KEY=<APP_KEY>
GROK2API_API_KEY=<API_KEY>
GROK_REGISTER_DEFAULT_API_ENDPOINT=http://grok2api:8000/v1/admin/tokens
GROK_REGISTER_DEFAULT_API_TOKEN=<APP_KEY>
GROK_REGISTER_DEFAULT_API_APPEND=true
GROK_REGISTER_DEFAULT_TEMP_MAIL_PROVIDER=cloudmail
GROK_REGISTER_DEFAULT_CLOUDMAIL_ADMIN_EMAIL=<CLOUDMAIL_ADMIN_EMAIL>
GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE=<TEMP_MAIL_API_BASE>
GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD=<TEMP_MAIL_ADMIN_PASSWORD>
GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN=<TEMP_MAIL_DOMAIN>
```

`GROK2API_APP_KEY`、`GROK2API_API_KEY` 和代理默认值只在首次初始化 `runtime/grok2api/data/config.toml` 时写入。已有运行数据的环境不会被外部 env 文件覆盖，需要在管理后台或 `config.toml` 中修改。

启动：

```bash
docker compose --env-file /etc/grok-stack/grok2api.env up -d --build
docker compose --env-file /etc/grok-stack/grok2api.env ps
```

访问：

- `http://<SERVER_IP>:8000/admin/login`
- `http://<SERVER_IP>:18600`

## 防火墙与反向代理

如果服务需要公网访问，放行对应端口：

```bash
sudo ufw allow 8000/tcp
sudo ufw allow 18600/tcp
```

生产环境建议放到 Nginx/Caddy 后面，并只暴露 HTTPS：

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

注册控制台如果只给内部使用，可以只绑定内网或通过堡垒机访问。

## 首次配置

1. 打开 grok2api 管理后台，使用 `GROK2API_APP_KEY` 登录。
2. 检查 `app.api_key`，如果需要对外提供 API，设置一个独立的 API Key。
3. 检查 `proxy` 配置，默认由 compose 内的 `warp` 和 `flaresolverr` 支持。
4. 打开 grok-register 控制台，保存系统默认配置。
5. 新建一个 `count=1` 的注册任务，确认邮箱、浏览器、代理与入池链路正常。
6. 确认 grok2api Token 池里能看到新入池账号后，再创建批量任务。

注册成功后的 Token 会通过 `GROK_REGISTER_DEFAULT_API_ENDPOINT` 推送到 grok2api：

```text
http://grok2api:8000/v1/admin/tokens
```

这个地址是容器内访问地址，不需要改成公网 IP。

## 自动注册调度

grok2api 已增加低账号数自动调度能力。当可用账号少于阈值时，会自动调用 grok-register 创建任务。

默认配置在：

```text
grok2api-python-stack/vendor/grok2api/config.defaults.toml
```

配置段：

```toml
[grok_register]
enabled = false
task_api_url = ""
min_available_accounts = 500
check_interval_seconds = 300
task_count = 100
trigger_cooldown_seconds = 3600
request_timeout_seconds = 15
pool_names = ["ssoBasic","ssoSuper"]
skip_if_active_task = true
task_name_prefix = "grok2api-auto-register"
```

部署后推荐在 grok2api 管理后台修改：

```toml
[grok_register]
enabled = true
task_api_url = "http://console:18600/api/tasks"
min_available_accounts = 500
task_count = 100
check_interval_seconds = 300
```

调度逻辑：

- 统计 `pool_names` 中当前可用 Token 数量。
- 数量低于 `min_available_accounts` 时触发注册。
- 调用 `task_api_url` 创建 grok-register 任务。
- 如果 grok-register 已有 `queued`、`running`、`stopping` 任务，默认不重复创建。
- 使用存储锁避免多 worker 重复触发。

## 运行数据与备份

关键数据目录：

- `grok2api-python-stack/runtime/grok2api/data/`：grok2api 配置与 Token 数据。
- `grok2api-python-stack/runtime/grok2api/logs/`：grok2api 日志。
- `grok2api-python-stack/apps/console/runtime/`：注册控制台任务数据。
- `grok2api-python-stack/apps/console/runtime/tasks/task_<id>/`：单个注册任务目录。

备份建议：

```bash
cd /opt/grok-stack/grok2api-python-stack
tar -czf runtime-backup-$(date +%Y%m%d-%H%M%S).tar.gz runtime apps/console/runtime
```

恢复时先停止服务，再覆盖数据目录：

```bash
docker compose --env-file /etc/grok-stack/grok2api.env down
tar -xzf runtime-backup-<DATE>.tar.gz
docker compose --env-file /etc/grok-stack/grok2api.env up -d
```

## 更新与重启

常规更新：

```bash
cd /opt/grok-stack
git pull
cd grok2api-python-stack
docker compose --env-file /etc/grok-stack/grok2api.env up -d --build
```

只重启单个服务：

```bash
docker compose --env-file /etc/grok-stack/grok2api.env restart grok2api
docker compose --env-file /etc/grok-stack/grok2api.env restart console
```

查看日志：

```bash
docker compose --env-file /etc/grok-stack/grok2api.env logs -f grok2api
docker compose --env-file /etc/grok-stack/grok2api.env logs -f console
docker compose --env-file /etc/grok-stack/grok2api.env logs -f flaresolverr
```

## 常见排查

### 管理后台打不开

- 确认 `docker compose --env-file /etc/grok-stack/grok2api.env ps` 中 `grok2api` 是 `running`。
- 确认宿主机端口没有被占用。
- 检查防火墙是否放行 `GROK2API_HOST_PORT`。
- 查看 `docker compose --env-file /etc/grok-stack/grok2api.env logs -f grok2api`。

### 注册任务创建后不运行

- 检查 `console` 容器日志。
- 确认 `GROK_REGISTER_PYTHON` 指向容器内可执行 Python。
- 确认 `GROK_REGISTER_SOURCE_DIR` 是 `/workspace`。
- 检查 `GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS` 是否为 `0` 或已有任务占满并发。

### 注册成功但没有入池

- 检查 `GROK_REGISTER_DEFAULT_API_ENDPOINT` 是否为 `http://grok2api:8000/v1/admin/tokens`。
- 检查 `GROK_REGISTER_DEFAULT_API_TOKEN` 是否等于 grok2api 的管理 `app_key`。
- 在 grok-register 任务日志里搜索 `已推送到 API`。
- 查看 grok2api 管理后台 Token 池。

### 自动注册没有触发

- 确认 `grok_register.enabled = true`。
- 确认 `task_api_url` 在 grok2api 容器内可访问，Docker 部署推荐 `http://console:18600/api/tasks`。
- 检查当前可用 Token 是否真的低于 `min_available_accounts`。
- 检查是否已有排队或运行中的 grok-register 任务。
- 查看 `docker compose --env-file /etc/grok-stack/grok2api.env logs -f grok2api` 中的 `grok-register scheduler` 日志。

### Cloudflare 或 x.ai 访问异常

- 检查 `warp` 容器是否正常。
- 检查 `flaresolverr` 容器日志。
- 确认代理配置没有指向宿主机不可达地址。
- 如果邮箱域名被拒绝，需要切换新的可用临时邮箱域名。

## 本地验证命令

```bash
python -m py_compile grok2api-python-stack/vendor/grok2api/app/services/grok_register/scheduler.py
python -m py_compile grok2api-python-stack/vendor/grok2api/main.py
node --check grok2api-python-stack/vendor/grok2api/_public/static/admin/js/config.js
```

完整运行 grok2api 需要安装 `grok2api-python-stack/vendor/grok2api/pyproject.toml` 中的依赖。
