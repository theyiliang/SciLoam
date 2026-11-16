# 部署说明

## 本地运行

见 README 的“快速开始 · 本地开发”。要点：

- 先执行 `bootstrap` 创建负责人账号。
- `RESEARCH_DATA_DIR` 默认 `./research-data`，包含数据库、上传文件与本地索引。
- 前端构建产物由后端静态托管；开发模式使用 Vite 代理。

## Docker

```bash
cp .env.research.example .env
# 必填：RESEARCH_ALLOWED_HOSTS、LLM_*（可选）
docker compose -f docker/research-compose.yaml up -d
docker compose -f docker/research-compose.yaml exec research \
  python -m app.presentation.cli bootstrap \
  --email owner@example.org --project "我的研究项目"
```

容器默认以生产模式运行：

- `RESEARCH_ENV=production`
- `RESEARCH_COOKIE_SECURE=1`
- 只读根文件系统，数据写入命名卷 `research-data`
- 仅监听 `127.0.0.1:8000`，需由反向代理提供 HTTPS

## 反向代理

- 必须转发 `Host`、`X-Forwarded-Proto`，并保持 SSE 不缓冲。
- WebSocket 不需要；问答进度使用 SSE。
- 将 `RESEARCH_ALLOWED_HOSTS` 设为实际域名，避免 Host 头攻击。

## 备份与恢复

```bash
uv run python scripts/research_backup.py --help
```

- 备份对象为整个数据目录（数据库、上传文件、索引）。
- 恢复时停止服务，替换数据目录后启动。
- 升级前先备份；跨版本升级后检查 `/health` 与登录。

## 健康检查

- `GET /health` 返回服务状态与是否配置模型。
- 生产监控建议同时检查磁盘空间与备份任务。
