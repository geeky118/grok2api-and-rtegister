# Token Manual Generate

## Outcome

Token 管理页在“导入”按钮左侧提供“生成”按钮。管理员输入生成数量后，grok2api 后台创建一个 grok-register 注册任务，由现有注册控制台执行实际注册和入池流程。

## Scope

- 新增 admin 鉴权接口：`POST /v1/admin/tokens/generate`。
- 复用 `grok_register.task_api_url` 和 `request_timeout_seconds` 配置创建任务。
- Token 管理页新增生成按钮、数量弹窗、提交状态和中英文文案。

## Decisions

- 手动生成不走低水位阈值和冷却时间。
- 保留 `grok_register.skip_if_active_task` 约束，避免管理员误触时重复创建 queued/running/stopping 注册任务。
- 数量限制为 `1..1000`，前后端同时校验。

## Verification

- `python -m py_compile grok2api-python-stack\vendor\grok2api\app\api\v1\admin\token.py grok2api-python-stack\vendor\grok2api\app\services\grok_register\scheduler.py`
- `node -e "JSON.parse(require('fs').readFileSync('grok2api-python-stack/vendor/grok2api/_public/static/i18n/locales/zh.json','utf8')); JSON.parse(require('fs').readFileSync('grok2api-python-stack/vendor/grok2api/_public/static/i18n/locales/en.json','utf8')); console.log('json ok')"`
