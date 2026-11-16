# 接口约定

完整接口与请求体以运行中的 `/docs`（OpenAPI）为准，本文只说明通用约定。

## 鉴权

- 登录成功后服务端设置会话 Cookie，写请求需携带 CSRF 令牌。
- 前端启动时调用 `/api/auth/me` 获取当前用户、项目与 CSRF 令牌。
- 未登录返回 401；无权限返回 403；资源不存在返回 404。

## 主要资源

| 前缀 | 说明 |
| --- | --- |
| `/api/auth/*` | 登录、登出、当前会话、修改密码 |
| `/api/spaces` | 知识库空间与成员授权 |
| `/api/spaces/{id}/documents`、`/api/versions` | 文档、版本、上传、删除与撤回 |
| `/api/spaces/{id}/imports` | 导入 UTF-8 文本或 DOCX |
| `/api/reviews` | 送审、审核列表、审核决定与快照 |
| `/api/goals`、`/api/goal-drafts` | 目标树、草稿、状态、进展与知识关联 |
| `/api/tasks` | 兼容任务接口 |
| `/api/members`、`/api/invites` | 项目成员与邀请 |
| `/api/chat`、`/api/chat/runs` | 问答、运行事件流与取消 |
| `/api/conversations` | 会话列表与历史 |
| `/health` | 健康检查（无需登录） |

## 写请求约定

- 支持幂等键的接口重复提交不会创建重复资源。
- 状态变更需携带期望版本号，冲突返回 409。
- 错误响应包含可读消息与稳定错误码，前端据此提示而非猜测。

## 时间与标识

- 时间使用 ISO 8601 字符串或 Unix 秒，具体以 OpenAPI 为准。
- 标识为服务端生成的字符串，不应在前端推断含义。
