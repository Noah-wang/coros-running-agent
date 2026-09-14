# 多用户控制平面

这一层把一份 COROS Running Agent 部署改造成可逐步开放给多位用户的服务。
应用进程、模型客户端和 Discord Bot 可以复用，但每位用户的运动数据、OAuth、
长期记忆、会话和用量记录都按 `tenant_id` 隔离。

## 当前能力

- `/admin`：管理员后台，使用 `WEB_SETTINGS_TOKEN` 解锁。
- 用户与订阅：创建用户，维护套餐、订阅状态、到期日期和账号状态。
- 渠道身份：把 Discord User ID 或飞书 `open_id` 绑定到指定用户。
- COROS 隔离：非默认用户使用独立的 `mcp-remote` 配置目录和回调端口。
- 数据隔离：新用户的数据写入 `data/tenants/<tenant_id>/`。
- 用量统计：记录每位用户的模型调用次数和 token，并保留全局费用估算。
- 审计记录：保存创建用户、修改订阅、绑定和移除身份等操作。

后台只返回“某项服务是否配置”。API Key、Discord Token、COROS OAuth Token、
用户运动数据和会话内容不会进入后台响应。

## 上线步骤

1. 在 `.env` 设置一个足够长的随机 `WEB_SETTINGS_TOKEN`。
2. 保持 `MULTI_TENANT_ENABLED=false`，启动服务并打开 `/admin`。
3. 运行 `uv run python scripts/bootstrap_default_tenant.py` 创建主账号记录。
4. 在后台给主账号绑定实际使用的 Discord User ID。
5. 创建测试用户并绑定测试平台账号，完成 COROS 授权和数据隔离验收。
6. 将 `MULTI_TENANT_ENABLED=true`，重启 Bot 后再开放多人访问。

不要在主账号尚未绑定时直接打开多人路由。开启后，未绑定的平台账号会被拒绝，
这是为了避免陌生用户消耗模型额度或读写错误的租户空间。

## 数据位置

| 内容 | 默认位置 |
| --- | --- |
| 控制数据库 | `data/control/control.db` |
| 新用户私有数据 | `data/tenants/<tenant_id>/` |
| 默认主账号旧数据 | `data/`（保持原路径，不迁移） |
| 默认主账号 COROS OAuth | `~/.mcp-auth/` |
| 新用户 COROS OAuth | `data/tenants/<tenant_id>/mcp-auth/` |

可以用 `CONTROL_DB_PATH` 和 `TENANT_DATA_ROOT` 覆盖前两个位置。

## 备份

至少同时备份以下三部分：

- `.env`，单独加密保存。
- `data/control/control.db` 以及同目录的 WAL 文件。
- `data/tenants/` 和原有 `data/` 私有数据。

部署代码时应排除 `.env`、`.venv`、`data/` 和 OAuth 配置目录，不要使用会删除
服务器私有文件的无保护同步命令。

## 仍需继续建设

当前版本完成了多人交互入口和管理控制面。每位用户的自动运动/睡眠报告还需要增加
独立投递目标和按租户轮询；飞书需要补齐应用凭据、事件验签和消息发送适配；支付平台
则应通过订单回调更新 `subscription_status`，不应让支付平台直接接触 Agent 数据。
