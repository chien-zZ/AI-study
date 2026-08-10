"""邀请码保护的 FastAPI Web Chat 接口。"""

import json
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from starlette.background import BackgroundTask
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.admin_security import (
    credential_fingerprint,
    generate_invite_code,
    verify_admin_password,
)
from app.chat_service import ChatService, ContentSafetyError, ensure_safe_content
from app.database import ChatDatabase
from app.memory_service import ConversationMemory
from app.security import create_session_token, secret_digest, validate_invite_code
from app.web_settings import WebSettings, load_web_settings


COOKIE_NAME = "ai_chat_session"
ADMIN_COOKIE_NAME = "ai_chat_admin_session"
logger = logging.getLogger(__name__)


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息内容不能为空")
        return value


# 角色白名单模块：只允许服务端已经注册的角色 ID 进入聊天链路。
PersonaId = Literal["brat", "douluo_dalu", "normal", "vue"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4_000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=20)
    persona: PersonaId = "normal"
    conversation_id: str | None = Field(default=None, max_length=64)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空")
        return value

    @model_validator(mode="after")
    def validate_history_size(self) -> "ChatRequest":
        if sum(len(item.content) for item in self.history) > 40_000:
            raise ValueError("历史消息总长度不能超过 40000 个字符")
        return self


class CreateConversationRequest(BaseModel):
    persona: PersonaId = "normal"
    local_only: bool = False


class UpdateConversationRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    persona: PersonaId | None = None


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=500)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AdminCreateInviteRequest(BaseModel):
    mode: Literal["generated", "custom"] = "generated"
    label: str = Field(min_length=1, max_length=80)
    minute_limit: int = Field(ge=1, le=10_000)
    day_limit: int = Field(ge=1, le=1_000_000)
    code: str | None = Field(default=None, max_length=64)
    code_confirmation: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_custom_code(self) -> "AdminCreateInviteRequest":
        self.label = self.label.strip()
        if not self.label:
            raise ValueError("邀请码备注不能为空")
        if self.mode == "custom":
            if not self.code or self.code != self.code_confirmation:
                raise ValueError("两次输入的邀请码不一致")
            validate_invite_code(self.code)
        return self


class AdminUpdateInviteRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    minute_limit: int | None = Field(default=None, ge=1, le=10_000)
    day_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    active: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "AdminUpdateInviteRequest":
        if self.label is not None:
            self.label = self.label.strip()
            if not self.label:
                raise ValueError("邀请码备注不能为空")
        if all(value is None for value in (
            self.label,
            self.minute_limit,
            self.day_limit,
            self.active,
        )):
            raise ValueError("至少需要修改一个字段")
        return self


@dataclass(frozen=True)
class AuthContext:
    token_digest: str
    invite_id: str
    expires_at: int
    minute_limit: int
    day_limit: int


@dataclass(frozen=True)
class AdminContext:
    token_digest: str
    expires_at: int


class ActiveRequestRegistry:
    """单进程内限制每个邀请码同时只进行一个流式请求。"""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, invite_id: str) -> bool:
        with self._lock:
            if invite_id in self._active:
                return False
            self._active.add(invite_id)
            return True

    def release(self, invite_id: str) -> None:
        with self._lock:
            self._active.discard(invite_id)


def sse_event(event: str, payload: dict[str, object]) -> str:
    """生成不会破坏中文和换行内容的标准 SSE 事件。"""

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def conversation_payload(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "title": row["title"],
        "persona": row["persona"],
        "localOnly": False,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def admin_invite_payload(row) -> dict[str, object]:
    """把数据库邀请码聚合转换成稳定的管理员接口字段。"""

    keys = set(row.keys())
    value = lambda name, default=None: row[name] if name in keys else default
    return {
        "id": row["id"],
        "label": row["label"],
        "active": bool(row["active"]),
        "minuteLimit": row["minute_limit"],
        "dayLimit": row["day_limit"],
        "createdAt": row["created_at"],
        "revokedAt": row["revoked_at"],
        "lastUsedAt": row["last_used_at"],
        "conversationCount": value("conversation_count", 0),
        "lastChatAt": value("last_chat_at"),
        "totalUsed": value("total_used", 0),
        "inputTokens": value("input_tokens", 0),
        "outputTokens": value("output_tokens", 0),
        "estimatedCostUsd": value("estimated_cost_usd", 0),
    }


def admin_conversation_payload(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "title": row["title"],
        "persona": row["persona"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "messageCount": row["message_count"],
    }


def public_sources(results: list[dict]) -> list[dict[str, object]]:
    """只返回展示引用所需的元数据，不把检索切片正文重复发送到浏览器。"""

    sources: list[dict[str, object]] = []
    for result in results:
        source_file = str(result["source_file"])
        sources.append(
            {
                "file": source_file,
                "documentTitle": result["document_title"],
                "sectionTitle": result["section_title"],
                "subsectionTitle": result["subsection_title"],
                "score": round(float(result["score"]), 4),
                "url": "https://github.com/chien-zz/AI-study/blob/master/projects/minimal-rag/"
                + source_file,
            }
        )
    return sources


def classify_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, ContentSafetyError):
        return "content_safety", "内容不符合安全边界，请换一种方式提问"
    if isinstance(error, TimeoutError):
        return "model_timeout", "模型响应超时，请重试"
    name = type(error).__name__.lower()
    if "insufficientknowledge" in name:
        return "insufficient_knowledge", "根据现有 Vue 知识库无法确定"
    if "rate" in name or "429" in str(error):
        return "model_rate_limit", "模型服务繁忙，请稍后重试"
    if "connect" in name:
        return "model_connection", "暂时无法连接模型服务"
    if "embedding" in str(error).lower():
        return "retrieval_error", "知识库检索暂时不可用"
    return "upstream_error", "模型服务暂时不可用，请稍后重试"


def create_app(
    settings: WebSettings | None = None,
    database: ChatDatabase | None = None,
    chat_service: ChatService | None = None,
) -> FastAPI:
    """创建应用；依赖可注入，便于测试时完全隔离真实模型和数据库。"""

    resolved_settings = settings or load_web_settings()
    resolved_database = database or ChatDatabase(resolved_settings.database_path)
    resolved_chat_service = chat_service or ChatService.from_environment()
    resolved_database.prune_metadata(resolved_settings.metadata_retention_days)
    resolved_database.prune_conversations(resolved_settings.conversation_retention_days)
    registry = ActiveRequestRegistry()

    app = FastAPI(title="AI Study Web Chat", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def disable_admin_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/admin/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def ensure_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in resolved_settings.allowed_origins:
            raise HTTPException(status_code=403, detail="请求来源不受信任")

    def get_auth(request: Request) -> AuthContext | None:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        digest = secret_digest(resolved_settings.session_token_pepper, token)
        row = resolved_database.get_session(digest)
        if not row:
            return None
        return AuthContext(
            token_digest=digest,
            invite_id=row["invite_id"],
            expires_at=row["expires_at"],
            minute_limit=row["minute_limit"],
            day_limit=row["day_limit"],
        )

    def require_auth(request: Request) -> AuthContext:
        auth = get_auth(request)
        if not auth:
            raise HTTPException(status_code=401, detail="请先输入有效邀请码")
        return auth

    def get_admin_auth(request: Request) -> AdminContext | None:
        token = request.cookies.get(ADMIN_COOKIE_NAME)
        if not token or not resolved_settings.admin_session_token_pepper:
            return None
        digest = secret_digest(resolved_settings.admin_session_token_pepper, token)
        fingerprint = credential_fingerprint(resolved_settings.admin_password_hash)
        row = resolved_database.get_admin_session(digest, fingerprint)
        if not row:
            return None
        return AdminContext(token_digest=digest, expires_at=row["expires_at"])

    def require_admin(request: Request) -> AdminContext:
        admin = get_admin_auth(request)
        if not admin:
            raise HTTPException(status_code=401, detail="请先登录管理员后台")
        return admin

    def auth_payload(auth: AuthContext) -> dict[str, object]:
        quota = resolved_database.quota_status(
            auth.invite_id,
            auth.minute_limit,
            auth.day_limit,
        )
        return {
            "authenticated": True,
            "viewerId": auth.invite_id,
            "expiresAt": auth.expires_at,
            "limits": {
                "minute": auth.minute_limit,
                "day": auth.day_limit,
                "minuteRemaining": quota.minute_remaining,
                "dayRemaining": quota.day_remaining,
            },
        }

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready")
    def readiness() -> dict[str, str]:
        connection = None
        try:
            connection = resolved_database.connect()
            connection.execute("SELECT 1").fetchone()
            from app.rag_bridge import RAG_PROJECT_ROOT
            if not (RAG_PROJECT_ROOT / "data" / "vectors.jsonl").is_file():
                raise RuntimeError("知识库向量文件不存在")
        except Exception as error:
            raise HTTPException(status_code=503, detail="数据库未就绪") from error
        finally:
            if connection is not None:
                connection.close()
        return {"status": "ready"}

    @app.get("/api/auth/session")
    def session_state(request: Request) -> dict[str, object]:
        auth = get_auth(request)
        return auth_payload(auth) if auth else {"authenticated": False}

    @app.post("/api/auth/redeem")
    def redeem(payload: RedeemRequest, request: Request) -> JSONResponse:
        ensure_origin(request)
        client_host = request.client.host if request.client else "unknown"
        client_digest = secret_digest(resolved_settings.session_token_pepper, client_host)
        if resolved_database.login_blocked(
            client_digest,
            resolved_settings.login_attempt_limit,
        ):
            raise HTTPException(
                status_code=429,
                detail="尝试次数过多，请稍后再试",
                headers={"Retry-After": "60"},
            )

        code_digest = secret_digest(resolved_settings.invite_code_pepper, payload.code)
        invite = resolved_database.find_invite_by_digest(code_digest)
        resolved_database.record_login_attempt(client_digest, invite is not None)
        if not invite:
            raise HTTPException(status_code=401, detail="邀请码无效或已停用")

        token = create_session_token()
        token_digest = secret_digest(resolved_settings.session_token_pepper, token)
        expires_at = int(time.time()) + resolved_settings.session_days * 86400
        resolved_database.create_session(token_digest, invite["id"], expires_at)
        auth = AuthContext(
            token_digest,
            invite["id"],
            expires_at,
            invite["minute_limit"],
            invite["day_limit"],
        )
        response = JSONResponse(auth_payload(auth))
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=resolved_settings.session_days * 86400,
            httponly=True,
            secure=resolved_settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request) -> Response:
        ensure_origin(request)
        auth = get_auth(request)
        if auth:
            resolved_database.delete_session(auth.token_digest)
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            secure=resolved_settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/admin/auth/session")
    def admin_session_state(request: Request) -> dict[str, object]:
        admin = get_admin_auth(request)
        return (
            {"authenticated": True, "expiresAt": admin.expires_at}
            if admin else {"authenticated": False}
        )

    @app.post("/api/admin/auth/login")
    def admin_login(payload: AdminLoginRequest, request: Request) -> JSONResponse:
        ensure_origin(request)
        if not resolved_settings.admin_password_hash.startswith("scrypt$"):
            raise HTTPException(status_code=503, detail="管理员后台尚未配置")
        client_host = request.client.host if request.client else "unknown"
        client_digest = secret_digest(
            resolved_settings.admin_session_token_pepper,
            client_host,
        )
        if resolved_database.admin_login_blocked(
            client_digest,
            resolved_settings.admin_login_attempt_limit,
        ):
            raise HTTPException(
                status_code=429,
                detail="尝试次数过多，请稍后再试",
                headers={"Retry-After": "60"},
            )
        valid = verify_admin_password(
            payload.password,
            resolved_settings.admin_password_hash,
        )
        resolved_database.record_admin_login_attempt(client_digest, valid)
        if not valid:
            raise HTTPException(status_code=401, detail="管理员密码错误")

        token = create_session_token()
        token_digest = secret_digest(resolved_settings.admin_session_token_pepper, token)
        expires_at = int(time.time()) + resolved_settings.admin_session_hours * 3600
        resolved_database.create_admin_session(
            token_digest,
            credential_fingerprint(resolved_settings.admin_password_hash),
            expires_at,
        )
        resolved_database.record_admin_audit("admin.login")
        response = JSONResponse({"authenticated": True, "expiresAt": expires_at})
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            token,
            max_age=resolved_settings.admin_session_hours * 3600,
            httponly=True,
            secure=resolved_settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/admin/auth/logout", status_code=204)
    def admin_logout(request: Request) -> Response:
        ensure_origin(request)
        admin = get_admin_auth(request)
        if admin:
            resolved_database.delete_admin_session(admin.token_digest)
            resolved_database.record_admin_audit("admin.logout")
        response = Response(status_code=204)
        response.delete_cookie(
            ADMIN_COOKIE_NAME,
            path="/",
            secure=resolved_settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/admin/invites")
    def admin_list_invites(
        request: Request,
        query: str = Query(default="", max_length=80),
        status: Literal["all", "active", "revoked"] = "all",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        require_admin(request)
        result = resolved_database.list_admin_invites(
            query=query.strip(),
            status=status,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [admin_invite_payload(item) for item in result["items"]],
            "page": page,
            "pageSize": page_size,
            "total": result["total"],
        }

    @app.post("/api/admin/invites", status_code=201)
    def admin_create_invite(
        payload: AdminCreateInviteRequest,
        request: Request,
    ) -> dict[str, object]:
        require_admin(request)
        ensure_origin(request)
        code = generate_invite_code() if payload.mode == "generated" else payload.code or ""
        validate_invite_code(code)
        try:
            invite_id = resolved_database.create_invite(
                secret_digest(resolved_settings.invite_code_pepper, code),
                payload.label,
                payload.minute_limit,
                payload.day_limit,
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="该邀请码已经存在") from error
        resolved_database.record_admin_audit(
            "invite.create",
            target_type="invite",
            target_id=invite_id,
            details={
                "mode": payload.mode,
                "label": payload.label,
                "minuteLimit": payload.minute_limit,
                "dayLimit": payload.day_limit,
            },
        )
        row = resolved_database.update_invite_admin(invite_id)
        return {"invite": admin_invite_payload(row), "oneTimeCode": code}

    @app.patch("/api/admin/invites/{invite_id}")
    def admin_update_invite(
        invite_id: str,
        payload: AdminUpdateInviteRequest,
        request: Request,
    ) -> dict[str, object]:
        require_admin(request)
        ensure_origin(request)
        row = resolved_database.update_invite_admin(
            invite_id,
            label=payload.label,
            minute_limit=payload.minute_limit,
            day_limit=payload.day_limit,
            active=payload.active,
        )
        if not row:
            raise HTTPException(status_code=404, detail="邀请码不存在")
        resolved_database.record_admin_audit(
            "invite.update",
            target_type="invite",
            target_id=invite_id,
            details={
                key: value for key, value in {
                    "label": payload.label,
                    "minuteLimit": payload.minute_limit,
                    "dayLimit": payload.day_limit,
                    "active": payload.active,
                }.items() if value is not None
            },
        )
        return admin_invite_payload(row)

    @app.get("/api/admin/invites/{invite_id}/conversations")
    def admin_list_conversations(
        invite_id: str,
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        require_admin(request)
        result = resolved_database.list_admin_conversations(
            invite_id,
            page=page,
            page_size=page_size,
        )
        if not result:
            raise HTTPException(status_code=404, detail="邀请码不存在")
        return {
            "invite": {
                "id": result["invite"]["id"],
                "label": result["invite"]["label"],
                "active": bool(result["invite"]["active"]),
            },
            "items": [admin_conversation_payload(item) for item in result["items"]],
            "page": page,
            "pageSize": page_size,
            "total": result["total"],
        }

    @app.get("/api/admin/conversations/{conversation_id}/messages")
    def admin_conversation_messages(
        conversation_id: str,
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, object]:
        require_admin(request)
        result = resolved_database.get_admin_conversation_messages(
            conversation_id,
            page=page,
            page_size=page_size,
        )
        if not result:
            raise HTTPException(status_code=404, detail="会话不存在")
        resolved_database.record_admin_audit(
            "conversation.view",
            target_type="conversation",
            target_id=conversation_id,
            details={"page": page, "pageSize": page_size},
        )
        conversation = result["conversation"]
        messages = [
            {
                "id": item["id"],
                "role": item["role"],
                "content": item["content"],
                "sources": item["sources"],
                "createdAt": item["created_at"],
                "feedback": item["feedback"],
                "feedbackComment": item["feedback_comment"],
            }
            for item in result["items"]
        ]
        memory = result["memory"]
        if memory:
            memory = {
                "summary": memory["summary"],
                "facts": memory["facts"],
                "decisions": memory["decisions"],
                "openItems": memory["open_items"],
                "summarizedThroughMessageId": memory["summarized_through_message_id"],
                "updatedAt": memory["updated_at"],
            }
        return {
            "conversation": {
                "id": conversation["id"],
                "title": conversation["title"],
                "persona": conversation["persona"],
                "inviteId": conversation["invite_id"],
                "inviteLabel": conversation["invite_label"],
                "createdAt": conversation["created_at"],
                "updatedAt": conversation["updated_at"],
            },
            "memory": memory,
            "items": messages,
            "page": page,
            "pageSize": page_size,
            "total": result["total"],
        }

    # 会话接口：同步会话存放在服务端；“仅本机”会话不会调用这些写接口。
    @app.get("/api/conversations")
    def list_conversations(request: Request) -> list[dict[str, object]]:
        auth = require_auth(request)
        return [conversation_payload(row) for row in resolved_database.list_conversations(auth.invite_id)]

    @app.post("/api/conversations", status_code=201)
    def create_conversation(payload: CreateConversationRequest, request: Request) -> dict[str, object]:
        ensure_origin(request)
        auth = require_auth(request)
        if payload.local_only:
            raise HTTPException(status_code=400, detail="仅本机会话应由浏览器创建")
        row = resolved_database.create_conversation(auth.invite_id, payload.persona)
        return conversation_payload(row)

    @app.patch("/api/conversations/{conversation_id}")
    def update_conversation(
        conversation_id: str,
        payload: UpdateConversationRequest,
        request: Request,
    ) -> dict[str, object]:
        ensure_origin(request)
        auth = require_auth(request)
        if payload.persona is not None and resolved_database.conversation_has_messages(
            auth.invite_id,
            conversation_id,
        ):
            current = resolved_database.get_conversation(auth.invite_id, conversation_id)
            if current and current["persona"] != payload.persona:
                raise HTTPException(
                    status_code=409,
                    detail="当前不允许切换人格，如需切换人格请新建对话",
                )
        row = resolved_database.update_conversation(
            auth.invite_id,
            conversation_id,
            title=payload.title.strip() if payload.title else None,
            persona=payload.persona,
        )
        if not row:
            raise HTTPException(status_code=404, detail="会话不存在")
        return conversation_payload(row)

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str, request: Request) -> Response:
        ensure_origin(request)
        auth = require_auth(request)
        if not resolved_database.delete_conversation(auth.invite_id, conversation_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return Response(status_code=204)

    @app.get("/api/conversations/{conversation_id}/messages")
    def list_messages(conversation_id: str, request: Request) -> list[dict[str, object]]:
        auth = require_auth(request)
        if not resolved_database.get_conversation(auth.invite_id, conversation_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return resolved_database.list_messages(auth.invite_id, conversation_id)

    @app.delete("/api/messages/{message_id}", status_code=204)
    def delete_message(message_id: str, request: Request) -> Response:
        ensure_origin(request)
        auth = require_auth(request)
        if not resolved_database.delete_message(auth.invite_id, message_id):
            raise HTTPException(status_code=404, detail="消息不存在")
        return Response(status_code=204)

    @app.put("/api/messages/{message_id}/feedback", status_code=204)
    def set_feedback(message_id: str, payload: FeedbackRequest, request: Request) -> Response:
        ensure_origin(request)
        auth = require_auth(request)
        if not resolved_database.set_feedback(
            auth.invite_id,
            message_id,
            payload.rating,
            payload.comment.strip() if payload.comment else None,
        ):
            raise HTTPException(status_code=404, detail="助手消息不存在")
        return Response(status_code=204)

    @app.post("/api/chat/stream")
    def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        ensure_origin(request)
        auth = require_auth(request)
        try:
            ensure_safe_content(payload.message)
        except ContentSafetyError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        mode = "rag" if payload.persona == "vue" else "chat"
        conversation = None
        if payload.conversation_id:
            conversation = resolved_database.get_conversation(auth.invite_id, payload.conversation_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="会话不存在")

        if conversation:
            stored_messages = resolved_database.list_messages(auth.invite_id, payload.conversation_id or "")
            memory = ConversationMemory.from_record(
                resolved_database.get_conversation_memory(
                    auth.invite_id,
                    payload.conversation_id or "",
                )
            )
            active_messages = stored_messages
            if memory.summarized_through_message_id:
                for index, item in enumerate(stored_messages):
                    if item["id"] == memory.summarized_through_message_id:
                        active_messages = stored_messages[index + 1 :]
                        break
                else:
                    # 摘要边界缺失说明消息被外部修改，旧摘要不再可信。
                    resolved_database.clear_conversation_memory(
                        auth.invite_id,
                        payload.conversation_id or "",
                    )
                    memory = ConversationMemory()
            history = [
                {"role": item["role"], "content": item["content"]}
                for item in active_messages
            ]
            if conversation["persona"] != payload.persona:
                resolved_database.update_conversation(
                    auth.invite_id,
                    payload.conversation_id or "",
                    persona=payload.persona,
                )
        else:
            memory = ConversationMemory()
            history = [item.model_dump() for item in payload.history]

        if not registry.acquire(auth.invite_id):
            raise HTTPException(status_code=409, detail="已有回答正在生成")
        try:
            request_id, quota = resolved_database.reserve_usage(
                auth.invite_id,
                mode,
                auth.minute_limit,
                auth.day_limit,
                model=getattr(resolved_chat_service, "model", "test-model"),
                persona=payload.persona,
                input_characters=len(payload.message) + sum(len(item["content"]) for item in history),
            )
        except Exception:
            registry.release(auth.invite_id)
            raise
        if not request_id:
            registry.release(auth.invite_id)
            raise HTTPException(
                status_code=429,
                detail="当前邀请码的使用额度已用完",
                headers={"Retry-After": str(quota.retry_after)},
            )

        def generate() -> Iterator[str]:
            started = time.monotonic()
            outcome = "success"
            error_code: str | None = None
            first_token_ms: int | None = None
            answer_parts: list[str] = []
            sources: list[dict[str, object]] = []
            input_tokens: int | None = None
            output_tokens: int | None = None
            events: queue.Queue[tuple[str, object]] = queue.Queue()
            cancel_event = threading.Event()

            def capture_sources(results: list[dict]) -> None:
                events.put(("sources", public_sources(results)))

            def capture_usage(prompt_tokens: int | None, completion_tokens: int | None) -> None:
                events.put(("usage", (prompt_tokens, completion_tokens)))

            def run_model() -> None:
                try:
                    for token in resolved_chat_service.stream_reply(
                        payload.message,
                        history,
                        rag_enabled=payload.persona == "vue",
                        persona_id="normal" if payload.persona == "vue" else payload.persona,
                        memory=memory if payload.persona != "vue" else None,
                        on_sources=capture_sources,
                        on_usage=capture_usage,
                        cancel_event=cancel_event,
                    ):
                        events.put(("token", token))
                    events.put(("complete", None))
                except Exception as error:
                    events.put(("error", error))

            worker = threading.Thread(target=run_model, daemon=True)
            worker.start()
            try:
                while True:
                    try:
                        event, value = events.get(timeout=resolved_settings.sse_heartbeat_seconds)
                    except queue.Empty:
                        yield sse_event("ping", {"timestamp": int(time.time())})
                        continue
                    if event == "sources":
                        sources = value  # type: ignore[assignment]
                        yield sse_event("sources", {"items": sources})
                    elif event == "token":
                        token = str(value)
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - started) * 1000)
                        answer_parts.append(token)
                        yield sse_event("token", {"text": token})
                    elif event == "usage":
                        input_tokens, output_tokens = value  # type: ignore[misc]
                    elif event == "error":
                        raise value  # type: ignore[misc]
                    else:
                        break

                saved_messages: list[dict[str, object]] = []
                if payload.conversation_id:
                    saved_messages = list(resolved_database.append_exchange(
                        auth.invite_id,
                        payload.conversation_id,
                        payload.message,
                        "".join(answer_parts),
                        sources,
                    ))
                    if payload.persona != "vue":
                        compact_memory = getattr(resolved_chat_service, "compact_memory", None)
                        if compact_memory:
                            try:
                                all_messages = resolved_database.list_messages(
                                    auth.invite_id,
                                    payload.conversation_id,
                                )
                                updated_memory = compact_memory(memory, all_messages)
                                if updated_memory:
                                    resolved_database.save_conversation_memory(
                                        auth.invite_id,
                                        payload.conversation_id,
                                        summary=updated_memory.summary,
                                        facts=list(updated_memory.facts),
                                        decisions=list(updated_memory.decisions),
                                        open_items=list(updated_memory.open_items),
                                        summarized_through_message_id=(
                                            updated_memory.summarized_through_message_id or ""
                                        ),
                                    )
                            except Exception:
                                # 记忆压缩属于派生能力，失败不能让已生成的正常回答回滚。
                                logger.exception(
                                    "conversation memory compaction failed for %s",
                                    payload.conversation_id,
                                )
                yield sse_event(
                    "done",
                    {"requestId": request_id, "messages": saved_messages},
                )
            except GeneratorExit:
                outcome = "cancelled"
                error_code = "client_cancelled"
                cancel_event.set()
                raise
            except Exception as error:
                outcome = "error"
                error_code, public_message = classify_error(error)
                logger.warning(
                    "chat request %s failed with %s",
                    request_id,
                    type(error).__name__,
                )
                yield sse_event(
                    "error",
                    {"code": error_code, "message": public_message},
                )
            finally:
                cancel_event.set()
                duration_ms = int((time.monotonic() - started) * 1000)
                answer = "".join(answer_parts)
                estimated_input_tokens = input_tokens or max(
                    1,
                    (len(payload.message) + sum(len(item["content"]) for item in history)) // 4,
                )
                estimated_output_tokens = output_tokens if output_tokens is not None else (
                    max(1, len(answer) // 4) if answer else 0
                )
                estimated_cost = (
                    estimated_input_tokens * resolved_settings.input_price_per_million
                    + estimated_output_tokens * resolved_settings.output_price_per_million
                ) / 1_000_000
                resolved_database.finish_usage(
                    request_id,
                    outcome,
                    duration_ms,
                    first_token_ms=first_token_ms,
                    output_characters=len(answer),
                    input_tokens=estimated_input_tokens,
                    output_tokens=estimated_output_tokens,
                    estimated_cost_usd=round(estimated_cost, 8),
                    error_code=error_code,
                )
                registry.release(auth.invite_id)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            # 即使客户端在生成器第一次迭代前断开，也释放该邀请码的并发锁。
            background=BackgroundTask(registry.release, auth.invite_id),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return app
