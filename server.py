import os
import json
import asyncio
import hashlib
import secrets
import base64
from typing import Any

import anyio

# Temporary store for PKCE code verifiers keyed by OAuth state
_pkce_store: dict[str, str] = {}

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse

from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp import types

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

mcp = Server("google-workspace")


def _client_config() -> dict:
    raw = os.environ["GOOGLE_CLIENT_CONFIG"]
    return json.loads(raw)


def get_creds() -> Credentials | None:
    raw = os.getenv("GOOGLE_TOKEN_JSON")
    if not raw:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
    return creds


def require_creds() -> Credentials:
    creds = get_creds()
    if not creds or not creds.valid:
        raise RuntimeError("Not authenticated — visit /auth/start")
    return creds


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="gmail_list",
            description="List Gmail messages matching a query",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Gmail search query (e.g. 'is:unread')"},
                    "max_results": {"type": "integer", "default": 10},
                },
            },
        ),
        types.Tool(
            name="gmail_get",
            description="Get full content of a Gmail message",
            inputSchema={
                "type": "object",
                "required": ["message_id"],
                "properties": {
                    "message_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="gmail_send",
            description="Send an email via Gmail",
            inputSchema={
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to":      {"type": "string"},
                    "subject": {"type": "string"},
                    "body":    {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="calendar_list",
            description="List upcoming calendar events",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results":  {"type": "integer", "default": 10},
                    "calendar_id":  {"type": "string", "default": "primary"},
                },
            },
        ),
        types.Tool(
            name="calendar_create",
            description="Create a calendar event",
            inputSchema={
                "type": "object",
                "required": ["summary", "start", "end"],
                "properties": {
                    "summary":     {"type": "string"},
                    "start":       {"type": "string", "description": "ISO 8601 datetime"},
                    "end":         {"type": "string", "description": "ISO 8601 datetime"},
                    "description": {"type": "string"},
                    "calendar_id": {"type": "string", "default": "primary"},
                },
            },
        ),
        types.Tool(
            name="drive_list",
            description="List files in Google Drive",
            inputSchema={
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Drive query string"},
                    "max_results": {"type": "integer", "default": 10},
                },
            },
        ),
        types.Tool(
            name="drive_read",
            description="Read a Google Doc or plain-text file from Drive",
            inputSchema={
                "type": "object",
                "required": ["file_id"],
                "properties": {
                    "file_id": {"type": "string"},
                },
            },
        ),
    ]


@mcp.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    creds = require_creds()

    if name == "gmail_list":
        svc = build("gmail", "v1", credentials=creds)
        res = svc.users().messages().list(
            userId="me",
            q=arguments.get("query", ""),
            maxResults=arguments.get("max_results", 10),
        ).execute()
        msgs = res.get("messages", [])
        # Fetch snippet for each
        details = []
        for m in msgs:
            detail = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            details.append({"id": m["id"], **headers, "snippet": detail.get("snippet", "")})
        return [types.TextContent(type="text", text=json.dumps(details, ensure_ascii=False))]

    if name == "gmail_get":
        svc = build("gmail", "v1", credentials=creds)
        msg = svc.users().messages().get(
            userId="me", id=arguments["message_id"], format="full",
        ).execute()
        return [types.TextContent(type="text", text=json.dumps(msg, ensure_ascii=False))]

    if name == "gmail_send":
        import base64
        from email.message import EmailMessage
        svc = build("gmail", "v1", credentials=creds)
        em = EmailMessage()
        em["To"] = arguments["to"]
        em["Subject"] = arguments["subject"]
        em.set_content(arguments["body"])
        encoded = base64.urlsafe_b64encode(em.as_bytes()).decode()
        result = svc.users().messages().send(
            userId="me", body={"raw": encoded}
        ).execute()
        return [types.TextContent(type="text", text=json.dumps(result))]

    if name == "calendar_list":
        from datetime import datetime, timezone
        svc = build("calendar", "v3", credentials=creds)
        now = datetime.now(timezone.utc).isoformat()
        res = svc.events().list(
            calendarId=arguments.get("calendar_id", "primary"),
            timeMin=now,
            maxResults=arguments.get("max_results", 10),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return [types.TextContent(type="text", text=json.dumps(res.get("items", []), ensure_ascii=False))]

    if name == "calendar_create":
        svc = build("calendar", "v3", credentials=creds)
        event = {
            "summary": arguments["summary"],
            "start":   {"dateTime": arguments["start"], "timeZone": "UTC"},
            "end":     {"dateTime": arguments["end"],   "timeZone": "UTC"},
        }
        if "description" in arguments:
            event["description"] = arguments["description"]
        result = svc.events().insert(
            calendarId=arguments.get("calendar_id", "primary"), body=event
        ).execute()
        return [types.TextContent(type="text", text=json.dumps(result))]

    if name == "drive_list":
        svc = build("drive", "v3", credentials=creds)
        res = svc.files().list(
            q=arguments.get("query", ""),
            pageSize=arguments.get("max_results", 10),
            fields="files(id,name,mimeType,modifiedTime)",
        ).execute()
        return [types.TextContent(type="text", text=json.dumps(res.get("files", []), ensure_ascii=False))]

    if name == "drive_read":
        svc = build("drive", "v3", credentials=creds)
        meta = svc.files().get(fileId=arguments["file_id"], fields="mimeType,name").execute()
        mime = meta["mimeType"]
        if mime == "application/vnd.google-apps.document":
            content = svc.files().export(
                fileId=arguments["file_id"], mimeType="text/plain"
            ).execute()
            text = content.decode() if isinstance(content, bytes) else content
        else:
            content = svc.files().get_media(fileId=arguments["file_id"]).execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
        return [types.TextContent(type="text", text=text)]

    raise ValueError(f"Unknown tool: {name}")


# ── OAuth routes ───────────────────────────────────────────────────────────

def _redirect_uri(request: Request) -> str:
    # BASE_URL must be set explicitly to avoid http/https mismatch behind Railway proxy
    base = os.environ["BASE_URL"].rstrip("/")
    return f"{base}/auth/callback"


async def auth_start(request: Request):
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri(request))
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    _pkce_store[state] = code_verifier
    return RedirectResponse(url)


async def auth_callback(request: Request):
    error = request.query_params.get("error")
    if error:
        desc = request.query_params.get("error_description", "")
        return HTMLResponse(f"<h1>OAuth Error: {error}</h1><p>{desc}</p>", status_code=400)

    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>Error: missing code</h1>", status_code=400)

    try:
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        state = request.query_params.get("state", "")
        code_verifier = _pkce_store.pop(state, None)
        flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri(request))
        flow.fetch_token(code=code, code_verifier=code_verifier)
        token_json = flow.credentials.to_json()
    except Exception as exc:
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{exc}</pre>", status_code=500)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Authorized</title>
<style>body{{font-family:monospace;padding:2rem}}pre{{background:#f4f4f4;padding:1rem;word-break:break-all;white-space:pre-wrap}}</style>
</head><body>
<h2>&#x2705; Authorization successful</h2>
<p>Copy the value below and set it as Railway environment variable <code>GOOGLE_TOKEN_JSON</code>:</p>
<pre id="tok">{token_json}</pre>
<button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent)">Copy</button>
</body></html>""")


async def health(request: Request):
    authed = get_creds() is not None
    return HTMLResponse(f"google-workspace-mcp | authenticated={authed}")


# ── Streamable HTTP transport + app assembly ────────────────────────────────

async def handle_mcp(request: Request):
    transport = StreamableHTTPServerTransport(
        mcp_session_id=None,
        is_json_response_enabled=False,
    )

    async def run_mcp():
        async with transport.connect() as (read_stream, write_stream):
            await mcp.run(read_stream, write_stream, mcp.create_initialization_options())

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_mcp)
        await transport.handle_request(request.scope, request.receive, request._send)
        tg.cancel_scope.cancel()


app = Starlette(
    routes=[
        Route("/",              health),
        Route("/auth/start",    auth_start),
        Route("/auth/callback", auth_callback),
        Route("/mcp",           handle_mcp, methods=["GET", "POST", "DELETE"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
