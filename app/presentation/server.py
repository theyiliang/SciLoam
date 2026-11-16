"""Authenticated, same-origin research application. No legacy library routes mounted."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.domain.errors import DomainError
from app.application.usecases.facade import ResearchService
from app.composition import build_container
from app.presentation.idempotency import execute_write
from app.presentation.research_dto import (Login, Register, ChangePassword, SpaceInput, InviteInput, AcceptInvite, Grant, VersionInput, DocumentInput, ShareInput, DeleteVersion, WithdrawVersion, ReviewInput, DecisionInput, TaskInput, TaskPatch, ProgressInput, ConversationInput, ChatInput, GoalInput, GoalPatch, GoalStatus, GoalArchive, GoalKnowledge, GoalUpdate, GoalDraftInput, GoalDraftPatch, GoalDraftPublish, GoalDraftRegenerate, GoalDraftAdopt, GoalDraftReject, ChatRunInput)

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)
SESSION_COOKIE = "research_session"
CSRF_COOKIE = "research_csrf"


class GoalDraftRunInput(GoalDraftInput):
    model_config = ConfigDict(extra='forbid')
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    regenerates_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, strict=True, ge=1)


class UserActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_type: str = Field(..., pattern="^(user\\.answer_feedback|user\\.session_action)$")
    action_type: str = Field(..., min_length=1, max_length=64)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    space_id: str | None = Field(default=None, min_length=1, max_length=128)
    document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)
    # Strict length limit: events carry metadata only, never prose.
    _MAX_PAYLOAD = 2048



def create_app(db_path: Path | None = None, frontend_dir: Path | None = None) -> FastAPI:
    secure = os.getenv("RESEARCH_COOKIE_SECURE", "0") == "1"
    production = os.getenv("RESEARCH_ENV", "development") == "production"
    if production and not secure:
        raise RuntimeError("生产环境必须启用 RESEARCH_COOKIE_SECURE=1，并配置HTTPS反向代理")
    data = Path(os.getenv("RESEARCH_DATA_DIR", str(ROOT / "research-data")))
    selected_db = db_path or data / "research.db"
    frontend = frontend_dir or ROOT / "frontend" / "dist"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = build_container(selected_db)
        app.state.service = app.state.container.service
        app.state.chat_slots = asyncio.Semaphore(2)
        app.state.container.runs.execution_limiter = app.state.chat_slots
        try:
            yield
        finally:
            await app.state.container.goal_runs.aclose()
            await app.state.container.runs.aclose()
            await asyncio.to_thread(app.state.container.close)

    app = FastAPI(title="SciLoam · 科研知识协同助手", version="1.0.0", lifespan=lifespan,
                  description="中文接口文档。登录后按知识空间授权访问；AI仅提供检索、对齐及进度草稿，审核与验收由人执行。")
    hosts = [h.strip() for h in os.getenv("RESEARCH_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def security(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 8_000_000):
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            cookie = request.cookies.get(CSRF_COOKIE, "")
            header = request.headers.get("x-csrf-token", "")
            if not (32 <= len(cookie) <= 128 and cookie.isascii() and header.isascii()) or not secrets.compare_digest(cookie, header):
                return JSONResponse({"detail": "请求校验失败，请刷新页面后重试"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 8_000_000:
                    return JSONResponse({"detail": "请求过大"}, status_code=413)
            request._body = bytes(raw)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, error: DomainError):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError):
        # Pydantic error input can contain passwords or private documents; never echo it.
        return JSONResponse({"detail": "请求字段格式不正确", "fields": [".".join(map(str, e["loc"])) for e in error.errors()]}, status_code=422)

    def service(request: Request) -> ResearchService:
        return request.app.state.service

    def user(request: Request, svc: ResearchService = Depends(service)) -> dict:
        return svc.authenticate(request.cookies.get(SESSION_COOKIE, ""))

    def csrf(response: Response, token: str | None = None) -> str:
        token = token or secrets.token_urlsafe(32)
        response.set_cookie(CSRF_COOKIE, token, secure=secure, httponly=False, samesite="strict", max_age=43200)
        return token

    @app.get("/health", tags=["服务状态"], summary="查看服务状态")
    def health():
        return {"status": "ok", "product": "research", "authentication": "required", "llm_configured": bool(os.getenv("LLM_API_KEY"))}

    @app.get("/api/auth/me", tags=["账号"], summary="获取当前身份及请求校验凭证")
    def me(request: Request, response: Response, svc: ResearchService = Depends(service)):
        existing_csrf = request.cookies.get(CSRF_COOKIE, "")
        token = csrf(response, existing_csrf if 32 <= len(existing_csrf) <= 128 and existing_csrf.isascii() else None)
        try:
            current = svc.authenticate(request.cookies.get(SESSION_COOKIE, ""))
            return {**svc.get_me(current["id"]), "csrf_token": token}
        except DomainError as err:
            if err.status_code != 401:
                raise
            return {"user": None, "project": None, "csrf_token": token}

    @app.post("/api/auth/login", tags=["账号"], summary="登录课题账号")
    def login(body: Login, response: Response, svc: ResearchService = Depends(service)):
        result = svc.login(body.email, body.password)
        response.set_cookie(SESSION_COOKIE, result.pop("token"), secure=secure, httponly=True, samesite="strict", max_age=43200)
        return {**result, "csrf_token": csrf(response)}

    @app.post("/api/auth/register", tags=["账号"], summary="凭邀请创建成员账号")
    def register(body: Register, response: Response, svc: ResearchService = Depends(service)):
        svc.register(body.email, body.password, body.invite_token)
        result = svc.login(body.email, body.password)
        response.set_cookie(SESSION_COOKIE, result.pop("token"), secure=secure, httponly=True, samesite="strict", max_age=43200)
        return {**result, "csrf_token": csrf(response)}

    @app.post("/api/auth/logout", tags=["账号"], summary="退出并撤销登录状态")
    def logout(request: Request, response: Response, svc: ResearchService = Depends(service)):
        svc.logout(request.cookies.get(SESSION_COOKIE, ""))
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"ok": True}

    @app.post("/api/auth/password", tags=["账号"], summary="校验当前密码后修改本人密码，撤销所有旧会话")
    def change_password(body: ChangePassword, response: Response, actor=Depends(user), svc=Depends(service)):
        result = svc.change_password(actor["id"], body.current_password, body.new_password)
        response.delete_cookie(SESSION_COOKIE, secure=secure, httponly=True, samesite="strict")
        response.delete_cookie(CSRF_COOKIE, secure=secure, samesite="strict")
        return result

    @app.get("/api/members", tags=["成员"], summary="查看课题成员")
    def members(actor=Depends(user), svc=Depends(service)):
        return svc.list_members(actor["id"])

    @app.post("/api/invites", tags=["成员"], summary="负责人创建限时成员邀请")
    def invite(body: InviteInput, actor=Depends(user), svc=Depends(service)):
        return svc.create_invite(actor["id"], body.email)

    @app.post("/api/invites/accept", tags=["成员"], summary="接受邀请")
    def accept(body: AcceptInvite, actor=Depends(user), svc=Depends(service)):
        return svc.accept_invite(actor["id"], body.token)

    @app.delete("/api/members/{user_id}", tags=["成员"], summary="停用成员并撤销其登录和空间权限")
    def remove_member(user_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.remove_member(actor["id"], user_id)

    @app.get("/api/spaces", tags=["知识空间"], summary="列出当前用户可见空间")
    def spaces(actor=Depends(user), svc=Depends(service)):
        return svc.list_spaces(actor["id"])

    @app.post("/api/spaces", tags=["知识空间"], summary="创建任务协作空间")
    def create_space(body: SpaceInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.create_space(actor["id"], **body.model_dump()))

    @app.delete("/api/spaces/{space_id}", tags=["知识空间"], summary="所有者删除自建协作知识库，保留独立成果与审计")
    def delete_space(space_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.delete_space(actor["id"], space_id)

    @app.get("/api/spaces/{space_id}/members", tags=["知识空间"], summary="查看空间成员")
    def space_members(space_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.list_space_members(actor["id"], space_id)

    @app.put("/api/spaces/{space_id}/members/{user_id}", tags=["知识空间"], summary="设置空间读写权限")
    def grant(space_id: str, user_id: str, body: Grant, actor=Depends(user), svc=Depends(service)):
        return svc.grant_space(actor["id"], space_id, user_id, body.role)

    @app.delete("/api/spaces/{space_id}/members/{user_id}", tags=["知识空间"], summary="撤销空间权限并使旧会话失效")
    def revoke(space_id: str, user_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.revoke_space(actor["id"], space_id, user_id)

    @app.get("/api/spaces/{space_id}/documents", tags=["资料"], summary="列出空间文档与版本")
    def documents(space_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.list_documents(actor["id"], space_id)

    @app.post("/api/spaces/{space_id}/documents", tags=["资料"], summary="上传文本或Markdown内容")
    def add_document(space_id: str, body: DocumentInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.add_document(actor["id"], space_id, **body.model_dump()))

    @app.delete("/api/documents/{document_id}", tags=["资料"], summary="撤回资料并使旧会话失效，已共享副本独立")
    def withdraw(document_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.withdraw_document(actor["id"], document_id)

    @app.post("/api/spaces/{space_id}/imports", tags=["知识"], summary="导入UTF-8文本或DOCX，不执行附件，不支持扫描与PDF")
    async def import_knowledge(space_id: str, request: Request, actor=Depends(user)):
        def decoded_header(name: str, maximum: int):
            raw = request.headers.get(name, "")
            if len(raw) > maximum * 12:
                raise HTTPException(422, "文件描述过长")
            try:
                value = unquote(raw, encoding="utf-8", errors="strict")
            except UnicodeError as error:
                raise HTTPException(422, "文件描述编码错误") from error
            if len(value) > maximum or any(ord(char) < 32 for char in value):
                raise HTTPException(422, "文件描述格式错误")
            return value
        filename = decoded_header("x-filename", 255)
        if not filename:
            raise HTTPException(422, "缺少文件名")
        title = decoded_header("x-knowledge-title", 300)
        source = decoded_header("x-source", 1800)
        body = await request.body()
        return await asyncio.to_thread(request.app.state.container.importer.import_content,
                                       actor["id"], space_id, filename, body, title, source)

    @app.get("/api/versions/{version_id}", tags=["资料"], summary="读取授权版本原文")
    def version(version_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.get_version(actor["id"], version_id)

    @app.delete("/api/versions/{version_id}", tags=["知识"], summary="逻辑删除未受保护版本；当前版须明确保留版本")
    def delete_version(version_id: str, body: DeleteVersion, actor=Depends(user), svc=Depends(service)):
        return svc.delete_version(actor["id"], version_id, **body.model_dump())

    @app.post("/api/versions/{version_id}/withdraw", tags=["知识"], summary="撤回受引用或审核保护的旧版本，不物理删除审计")
    def withdraw_version(version_id: str, body: WithdrawVersion, actor=Depends(user), svc=Depends(service)):
        return svc.withdraw_version(actor["id"], version_id, body.reason)

    @app.post("/api/documents/{document_id}/versions", tags=["资料"], summary="创建不可变的新版本")
    def new_version(document_id: str, body: VersionInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.add_version(actor["id"], document_id, **body.model_dump()))

    @app.post("/api/versions/{version_id}/share", tags=["资料"], summary="明确发布指定版本副本到协作库")
    def share(version_id: str, body: ShareInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.share_version(actor["id"], version_id, body.target_space_id))

    @app.get("/api/reviews", tags=["成果审核"], summary="查看当前用户可见的审核记录")
    def reviews(actor=Depends(user), svc=Depends(service)):
        return svc.list_reviews(actor["id"])

    @app.get("/api/reviews/{review_id}/snapshot", tags=["成果审核"], summary="读取本次送审快照，不授予原知识库阅读权")
    def review_snapshot(review_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.read_review_snapshot(actor["id"], review_id)

    @app.post("/api/reviews", tags=["成果审核"], summary="直接提交个人或协作知识指定版本快照审核")
    def submit_review(body: ReviewInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.submit_review(actor["id"], **body.model_dump()))

    @app.post("/api/reviews/{review_id}/decision", tags=["成果审核"], summary="负责人进行人工审核，不允许自审")
    def decide(review_id: str, body: DecisionInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.decide_review(actor["id"], review_id, "approve" if body.decision == "approved" else "reject", body.comment))

    @app.post('/api/goal-draft-runs', tags=['目标提取运行'])
    async def start_goal_run(body: GoalDraftRunInput, request: Request, actor=Depends(user), svc=Depends(service)):
        body.require_local_method()
        return await request.app.state.container.goal_runs.start(actor['id'], body.version_ids, body.request_id,
            regenerates_id=body.regenerates_id, expected_revision=body.expected_revision,
            authorize_actor=session_check(request, actor, svc))

    @app.get('/api/goal-draft-runs/{run_id}', tags=['目标提取运行'])
    async def get_goal_run(run_id: str, request: Request, actor=Depends(user), svc=Depends(service)):
        return await request.app.state.container.goal_runs.get(actor['id'], run_id,
            authorize_actor=session_check(request, actor, svc))

    @app.post('/api/goal-draft-runs/{run_id}/cancel', tags=['目标提取运行'])
    async def cancel_goal_run(run_id: str, request: Request, actor=Depends(user), svc=Depends(service)):
        return await request.app.state.container.goal_runs.cancel(actor['id'], run_id,
            authorize_actor=session_check(request, actor, svc))

    @app.get('/api/goal-draft-runs/{run_id}/events', tags=['目标提取运行'])
    async def goal_run_events(run_id: str, request: Request, after_seq: int = 0, actor=Depends(user), svc=Depends(service)):
        from app.presentation.streaming import stream_sse
        from app.domain.run_events import RunReplayExpired, RunBackpressure
        if after_seq < 0:
            raise HTTPException(422, '事件序号不能为负')
        manager = request.app.state.container.goal_runs
        check = session_check(request, actor, svc)
        await manager.get(actor['id'], run_id, authorize_actor=check)
        stream = stream_sse(manager, actor['id'], run_id, after_seq, authorize_actor=check)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = None
        except RunReplayExpired as error:
            raise HTTPException(409, '事件已过期，请读取本次目标运行结果，不要重复启动生成') from error
        except RunBackpressure as error:
            raise HTTPException(429, '目标运行订阅数量已达上限') from error
        async def body():
            try:
                if first is not None:
                    yield first
                async for event in stream:
                    yield event
            except DomainError:
                # A live permission/session failure ends the wire without replay.
                return
            finally:
                await stream.aclose()
        return StreamingResponse(body(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.get("/api/goal-drafts", tags=["目标草稿"], summary="负责人查看生成草稿历史及当前可发布状态")
    def goal_drafts(actor=Depends(user), svc=Depends(service)):
        return svc.list_goal_drafts(actor["id"])

    @app.post("/api/goal-drafts", tags=["目标草稿"], summary="本地原文辅助提取（规则），需人工核对；AI目标生成已暂停")
    def generate_draft(body: GoalDraftInput, actor=Depends(user), svc=Depends(service)):
        body.require_local_method()
        return svc.generate_goal_draft(actor["id"], body.version_ids)

    @app.get("/api/goal-drafts/{draft_id}", tags=["目标草稿"], summary="核对草稿与来源版本")
    def goal_draft(draft_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.get_goal_draft(actor["id"], draft_id)

    @app.patch("/api/goal-drafts/{draft_id}", tags=["目标草稿"], summary="负责人编辑草稿，保留原文出处")
    def edit_draft(draft_id: str, body: GoalDraftPatch, actor=Depends(user), svc=Depends(service)):
        return svc.update_goal_draft(actor["id"], draft_id, body.expected_revision, body.nodes)

    @app.post("/api/goal-drafts/{draft_id}/publish", tags=["目标草稿"], summary="负责人明确确认后追加目标，不覆盖现有树")
    def publish_draft(draft_id: str, body: GoalDraftPublish, actor=Depends(user), svc=Depends(service)):
        return svc.publish_goal_draft(actor["id"], draft_id, body.expected_revision, confirmed=body.confirmed)

    @app.post("/api/goal-drafts/{draft_id}/regenerate", tags=["目标草稿"], summary="本地规则重新提取差异草案，需逐项人工确认；AI重新生成已暂停")
    def regenerate_draft(draft_id: str, body: GoalDraftRegenerate, actor=Depends(user), svc=Depends(service)):
        body.require_local_method()
        return svc.regenerate_goal_draft(actor["id"], draft_id, **body.model_dump(exclude={'method'}))

    @app.post('/api/goal-drafts/{draft_id}/abandon', tags=['目标草稿'], summary='负责人废弃未发布草稿，保留依据与历史')
    def abandon_draft(draft_id: str, body: GoalDraftPublish, actor=Depends(user), svc=Depends(service)):
        return svc.abandon_goal_draft(actor['id'], draft_id, **body.model_dump())

    @app.post('/api/goal-drafts/{draft_id}/reject', tags=['目标草稿'], summary='负责人逐项拒绝差异，不修改正式树')
    def reject_draft(draft_id: str, body: GoalDraftReject, actor=Depends(user), svc=Depends(service)):
        return svc.reject_goal_draft_suggestion(actor['id'], draft_id, **body.model_dump())

    @app.post("/api/goal-drafts/{draft_id}/adopt", tags=["目标草稿"], summary="负责人逐项明确确认并校验修订后采纳差异")
    def adopt_draft(draft_id: str, body: GoalDraftAdopt, actor=Depends(user), svc=Depends(service)):
        return svc.adopt_goal_draft_suggestion(actor["id"], draft_id, **body.model_dump())

    @app.get("/api/goals", tags=["课题目标"], summary="查看课题目标树；不分配执行人或自动计算完成度")
    def goals(actor=Depends(user), svc=Depends(service)):
        return svc.list_goals(actor["id"])

    @app.get("/api/goals/{goal_id}", tags=["课题目标"], summary="查看目标要求、授权知识与进展记录")
    def goal_detail(goal_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.get_goal(actor["id"], goal_id)

    @app.post("/api/goals", tags=["课题目标"], summary="负责人创建目标或子目标，无需派单")
    def create_goal(body: GoalInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.create_goal(actor["id"], **body.model_dump()))

    @app.patch("/api/goals/{goal_id}", tags=["课题目标"], summary="负责人编辑或移动目标；修订冲突不覆盖")
    def update_goal(goal_id: str, body: GoalPatch, actor=Depends(user), svc=Depends(service)):
        return svc.update_goal(actor["id"], goal_id, **body.model_dump(exclude_unset=True))

    @app.post("/api/goals/{goal_id}/status", tags=["课题目标"], summary="负责人结合验收要求确认目标状态")
    def confirm_goal(goal_id: str, body: GoalStatus, actor=Depends(user), svc=Depends(service)):
        return svc.confirm_goal_status(actor["id"], goal_id, **body.model_dump())

    @app.post("/api/goals/{goal_id}/archive", tags=["课题目标"], summary="负责人归档叶目标并保留历史")
    def archive_goal(goal_id: str, body: GoalArchive, actor=Depends(user), svc=Depends(service)):
        return svc.archive_goal(actor["id"], goal_id, **body.model_dump())

    @app.post("/api/goals/{goal_id}/knowledge", tags=["课题目标"], summary="自主关联可访问知识，关联不授予阅读权")
    def link_goal(goal_id: str, body: GoalKnowledge, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.link_goal_knowledge(actor["id"], goal_id, body.version_id))

    @app.delete("/api/goals/{goal_id}/knowledge/{link_id}", tags=["课题目标"], summary="移除有权管理的知识关联，不删除原知识")
    def unlink_goal(goal_id: str, link_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.unlink_goal_knowledge(actor["id"], goal_id, link_id)

    @app.post("/api/goals/{goal_id}/updates", tags=["课题目标"], summary="成员自主提交进展；公开正文不应包含私有内容")
    def goal_update(goal_id: str, body: GoalUpdate, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.add_goal_update(actor["id"], goal_id, **body.model_dump()))

    @app.get("/api/tasks", tags=["研究任务"], summary="列出课题任务")
    def tasks(actor=Depends(user), svc=Depends(service)):
        return svc.list_tasks(actor["id"])

    @app.post("/api/tasks", tags=["研究任务"], summary="负责人分配任务")
    def create_task(body: TaskInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.create_task(actor["id"], **body.model_dump()))

    @app.patch("/api/tasks/{task_id}", tags=["研究任务"], summary="更新任务或由负责人验收")
    def update_task(task_id: str, body: TaskPatch, actor=Depends(user), svc=Depends(service)):
        return svc.update_task(actor["id"], task_id, **body.model_dump(exclude_unset=True))

    @app.post("/api/tasks/{task_id}/progress", tags=["研究任务"], summary="记录进展及关联证据")
    def progress(task_id: str, body: ProgressInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.add_progress(actor["id"], task_id, **body.model_dump()))

    @app.get("/api/tasks/{task_id}/progress", tags=["研究任务"], summary="查看任务进展")
    def progress_list(task_id: str, actor=Depends(user), svc=Depends(service)):
        for task in svc.list_tasks(actor["id"]):
            if task["id"] == task_id:
                return task.get("progress", [])
        raise DomainError(404, "任务不存在")

    @app.post("/api/conversations", tags=["空间问答"], summary="创建单空间会话")
    def conversation(body: ConversationInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return execute_write(request, actor, svc, body.model_dump(), lambda: svc.create_conversation(actor["id"], body.space_id))

    @app.get("/api/conversations", tags=["空间问答"], summary="列出本人的有效会话")
    def conversations(space_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.list_conversations(actor["id"], space_id)

    @app.get("/api/conversations/{conversation_id}", tags=["空间问答"], summary="读取会话并复核空间权限")
    def conversation_detail(conversation_id: str, actor=Depends(user), svc=Depends(service)):
        return svc.get_conversation(actor["id"], conversation_id)

    def session_check(request: Request, actor: dict, svc: ResearchService):
        token = request.cookies.get(SESSION_COOKIE, "")
        async def check():
            current = await asyncio.to_thread(svc.authenticate, token)
            if current["id"] != actor["id"]:
                raise DomainError(401, "登录状态已改变，请重新登录")
        def repository_check(repository):
            # Same transaction as message persistence: no nested SQLite write
            # transaction and no event-loop wait while the COMMIT lock is held.
            session = repository.get("sessions", svc._security.hash_token(token))
            if not session or session["user_id"] != actor["id"] or session["expires_at"] <= svc._clock():
                raise DomainError(401, "登录状态已改变，请重新登录")
            svc._user(repository, actor["id"])
        check.repository_check = repository_check
        return check

    @app.post("/api/chat/runs", tags=["实时问答"], summary="创建授权问答运行，不重复启动相同请求")
    async def start_run(body: ChatRunInput, request: Request, actor=Depends(user), svc=Depends(service)):
        return await request.app.state.container.runs.start(actor["id"], body.space_id, body.conversation_id,
                 body.question, body.mode, body.request_id, selected_version_id=body.selected_version_id,
                 authorize_actor=session_check(request, actor, svc))

    @app.get("/api/chat/runs/{run_id}", tags=["实时问答"], summary="查看本人运行状态，不返回知识正文")
    async def get_run(run_id: str, request: Request, actor=Depends(user), svc=Depends(service)):
        return await request.app.state.container.runs.get(actor["id"], run_id, authorize_actor=session_check(request, actor, svc))

    @app.post("/api/chat/runs/{run_id}/cancel", tags=["实时问答"], summary="停止本人运行，不保存未完成的回答")
    async def cancel_run(run_id: str, request: Request, actor=Depends(user), svc=Depends(service)):
        return await request.app.state.container.runs.cancel(actor["id"], run_id, authorize_actor=session_check(request, actor, svc))

    @app.get("/api/chat/runs/{run_id}/events", tags=["实时问答"], summary="订阅本人授权运行SSE，逐事件复核登录与知识权限")
    async def run_events(run_id: str, request: Request, after_seq: int = 0, actor=Depends(user), svc=Depends(service)):
        from app.presentation.streaming import stream_sse
        from app.domain.run_events import RunReplayExpired
        if after_seq < 0:
            raise HTTPException(422, "事件序号不能为负")
        manager = request.app.state.container.runs
        check = session_check(request, actor, svc)
        await manager.get(actor["id"], run_id, authorize_actor=check)
        stream = stream_sse(manager, actor["id"], run_id, after_seq, authorize_actor=check)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            first = None
        except RunReplayExpired as error:
            raise HTTPException(409, "事件已过期，请刷新对话历史，不要自动重新发送问题") from error
        async def body():
            try:
                if first is not None:
                    yield first
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()
        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/chat", tags=["空间问答"], summary="多Agent证据问答、对齐报告或进度草稿")
    async def chat(body: ChatRunInput, request: Request, actor=Depends(user), svc=Depends(service)):
        manager = request.app.state.container.runs
        check = session_check(request, actor, svc)
        run = await manager.start(actor["id"], body.space_id, body.conversation_id, body.question,
                                  body.mode, body.request_id, selected_version_id=body.selected_version_id, authorize_actor=check)
        return await manager.wait_result(actor["id"], run["run_id"], authorize_actor=check)

    @app.get("/api/audit", tags=["审计"], summary="负责人查看业务操作记录，不含资料正文")
    def audit(actor=Depends(user), svc=Depends(service)):
        return svc.audit(actor["id"])

    @app.post("/api/events", tags=["埋点"], summary="上报用户交互事件（仅存元数据，不含正文）")
    def track_event(body: UserActionInput, actor=Depends(user), request: Request = None):
        from app.infrastructure.persistence.repositories import SQLiteResearchRepository
        now = time.time()
        space_id = body.space_id
        conv_id = body.conversation_id
        payload_str = json.dumps(body.payload, ensure_ascii=False)[:UserActionInput._MAX_PAYLOAD]
        with request.app.state.container.db.transaction() as conn:
            repo = SQLiteResearchRepository(conn)
            if conv_id and not space_id:
                space_id = repo.resolve_space_id(conv_id)
            repo.track_user_action(body.event_type, body.action_type, actor["id"], space_id,
                                   conv_id, body.run_id, body.document_version_id, payload_str, now)
        return {"ok": True}

    if (frontend / "assets").exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        if not (frontend / "index.html").is_file():
            return JSONResponse({"message": "前端尚未构建，请运行 npm --prefix frontend run build", "docs": "/docs"})
        return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
