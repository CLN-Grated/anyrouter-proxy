import json
import hashlib
import sys
import os
import codecs
import secrets
import uuid
import traceback
import argparse
import asyncio
import threading
import queue as thread_queue
from curl_cffi import requests as cf_requests
from fastapi import FastAPI, Request, Response, Form, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
import uvicorn

from auth import create_session_token, verify_session_token, verify_password, COOKIE_NAME, MAX_AGE
from dashboard import get_login_html, get_dashboard_html
import model_tester
from model_tester import MODELS, test_results, test_single_model, test_all_models

def resolve_config_path():
    env_path = os.environ.get("ANYROUTER_PROXY_CONFIG")
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if not xdg_config:
        xdg_config = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg_config, "anyrouter-proxy", "proxy_config.json")

CONFIG_FILE = resolve_config_path()

DEFAULT_CONFIG = {
    "proxy_url": "http://127.0.0.1:2080",
    "use_proxy": True,
    "debug": False,
    "buffer_stream": False,
    "target_base_url": "https://anyrouter.top",
    "max_tokens": "",
    "host": "127.0.0.1",
    "port": 8765,
    "dashboard_password": "",
    "dashboard_secret": "",
}

config = {}
SESSION = None
SESSION_HTTP_VERSION = None
CLAUDE_CODE_TOOLS = []
CLAUDE_CODE_SYSTEM = []

# --- Claude CLI fingerprint template (matches real claude-cli traffic) ---
_CLI_VERSION = "2.1.72"
_SDK_PACKAGE_VERSION = "0.74.0"
_ANTHROPIC_VERSION = "2023-06-01"
_NODE_VERSION = "v24.3.0"

# Full anthropic-beta flags matching real Claude CLI
_ANTHROPIC_BETA_FULL = ",".join([
    "claude-code-20250219",
    "interleaved-thinking-2025-05-14",
    "redact-thinking-2026-02-12",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "effort-2025-11-24",
    "context-1m-2025-08-07",
])
_ANTHROPIC_BETA_BASIC = "interleaved-thinking-2025-05-14"

# Stable per-instance session id (regenerated on each process start)
_SESSION_ID = str(uuid.uuid4())
_USER_HASH = hashlib.sha256(secrets.token_bytes(32)).hexdigest()

def _make_user_id():
    """Generate a user_id matching Claude CLI format: user_{hash}_account__session_{uuid}."""
    return f"user_{_USER_HASH}_account__session_{_SESSION_ID}"

def load_claude_code_templates():
    global CLAUDE_CODE_TOOLS, CLAUDE_CODE_SYSTEM
    tools_file = os.path.join(os.path.dirname(__file__), 'claude_code_tools.json')
    system_file = os.path.join(os.path.dirname(__file__), 'claude_code_system.json')
    if os.path.exists(tools_file):
        try:
            with open(tools_file, 'r', encoding='utf-8') as f:
                CLAUDE_CODE_TOOLS = json.load(f)
            print(f"[SYSTEM] Loaded {len(CLAUDE_CODE_TOOLS)} Claude Code tools")
        except Exception as e:
            print(f"[SYSTEM] Error loading tools: {e}")
    if os.path.exists(system_file):
        try:
            with open(system_file, 'r', encoding='utf-8') as f:
                CLAUDE_CODE_SYSTEM = json.load(f)
            print(f"[SYSTEM] Loaded Claude Code system prompt")
        except Exception as e:
            print(f"[SYSTEM] Error loading system: {e}")
    model_tester.claude_code_tools = CLAUDE_CODE_TOOLS
    model_tester.claude_code_system = CLAUDE_CODE_SYSTEM

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(loaded_config)
            _normalize_config()
            print(f"[SYSTEM] Configuration loaded from {CONFIG_FILE}")
            return True
        except Exception as e:
            print(f"[SYSTEM] Error loading config: {e}")
            config = DEFAULT_CONFIG.copy()
            _normalize_config()
            return False
    else:
        config = DEFAULT_CONFIG.copy()
        _normalize_config()
        return False

def _normalize_config():
    buffer_stream = config.get("buffer_stream", False)
    if isinstance(buffer_stream, str):
        config["buffer_stream"] = buffer_stream.strip().lower() in ("1", "true", "yes", "on", "y")
    else:
        config["buffer_stream"] = bool(buffer_stream)
    value = config.get("max_tokens", "")
    if value is None:
        config["max_tokens"] = ""
        return
    if isinstance(value, bool):
        config["max_tokens"] = ""
        return
    if isinstance(value, int):
        config["max_tokens"] = value if value > 0 else ""
        return
    if isinstance(value, str):
        value = value.strip()
        if not value:
            config["max_tokens"] = ""
            return
        try:
            parsed = int(value)
        except ValueError:
            config["max_tokens"] = ""
            return
        config["max_tokens"] = parsed if parsed > 0 else ""
        return
    config["max_tokens"] = ""

def _get_config_max_tokens():
    value = config.get("max_tokens", "")
    return value if isinstance(value, int) and value > 0 else None

def save_config():
    _normalize_config()
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        print(f"[SYSTEM] Configuration saved to {CONFIG_FILE}")
    except Exception as e:
        print(f"[SYSTEM] Error saving config: {e}")

def setup_wizard():
    print("\n" + "="*60)
    print("AnyRouter Proxy Setup Wizard")
    print("="*60)
    print("Please configure your proxy settings.\n")
    use_proxy_str = "y" if config.get('use_proxy', True) else "n"
    use_proxy = input(f"Use HTTP Proxy? (y/n) [{use_proxy_str}]: ").strip().lower()
    if use_proxy:
        config['use_proxy'] = (use_proxy == 'y')
    if config['use_proxy']:
        current_proxy = config.get('proxy_url', '')
        proxy_url = input(f"Proxy URL [{current_proxy}]: ").strip()
        if proxy_url:
            config['proxy_url'] = proxy_url
    debug_str = "y" if config.get('debug', False) else "n"
    debug_mode = input(f"Enable Debug Mode? (y/n) [{debug_str}]: ").strip().lower()
    if debug_mode:
        config['debug'] = (debug_mode == 'y')
    buffer_stream_str = "y" if config.get('buffer_stream', False) else "n"
    buffer_stream = input(
        f"Enable upstream stream buffering for non-stream requests? (y/n) [{buffer_stream_str}]: "
    ).strip().lower()
    if buffer_stream:
        config['buffer_stream'] = (buffer_stream == 'y')
    current_max_tokens = config.get('max_tokens', '')
    max_tokens = input(
        f"Override upstream max_tokens (blank = keep client value) [{current_max_tokens}]: "
    ).strip()
    if max_tokens or current_max_tokens:
        config['max_tokens'] = max_tokens
    save_config()
    print("\n" + "="*60)
    print("Setup complete!")
    print("="*60 + "\n")

app = FastAPI()

def get_claude_headers(is_stream=False, model="", client_headers=None):
    """Build headers matching Claude CLI fingerprint exactly.

    Replicates the header set sent by claude-cli (Node.js / @anthropic-ai/sdk).
    If client_headers are provided and contain Claude CLI signatures, certain
    fields (anthropic-beta, retry count) are forwarded from the client to
    preserve the most up-to-date beta flags.
    """
    is_code_model = "opus" in model.lower() or "sonnet" in model.lower()
    beta = _ANTHROPIC_BETA_FULL if is_code_model else _ANTHROPIC_BETA_BASIC

    headers = {
        "Accept": "text/event-stream" if is_stream else "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{_CLI_VERSION} (external, cli)",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Lang": "js",
        "X-Stainless-OS": "MacOS",
        "X-Stainless-Package-Version": _SDK_PACKAGE_VERSION,
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": _NODE_VERSION,
        "X-Stainless-Timeout": "600",
        "anthropic-beta": beta,
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": _ANTHROPIC_VERSION,
        "x-app": "cli",
        "Accept-Encoding": "gzip, deflate, br, zstd",
    }

    # If client already sends Claude CLI headers, prefer its values for
    # evolving fields (beta flags may be newer, retry count is per-request).
    if client_headers:
        client_beta = client_headers.get("anthropic-beta")
        if client_beta and len(client_beta) > len(beta):
            headers["anthropic-beta"] = client_beta
        client_retry = client_headers.get("X-Stainless-Retry-Count") or client_headers.get("x-stainless-retry-count")
        if client_retry is not None:
            headers["X-Stainless-Retry-Count"] = client_retry

    return headers

def _format_http_version(http_version):
    if http_version == "v1":
        return "HTTP/1.1"
    if http_version == "v2":
        return "HTTP/2"
    return "default"

def create_session(http_version=None):
    """Create curl_cffi session with Chrome TLS fingerprint."""
    proxies = None
    if config['use_proxy']:
        proxies = {
            "http": config['proxy_url'],
            "https": config['proxy_url'],
        }
    if config['debug']:
        print(
            f"[SYSTEM] Creating curl_cffi session "
            f"(impersonate=chrome, http_version={_format_http_version(http_version)}, "
            f"proxy={config['proxy_url'] if config['use_proxy'] else 'None'})"
        )
    return cf_requests.Session(
        impersonate="chrome",
        http_version=http_version,
        proxies=proxies,
        verify=False,
        timeout=600,
    )

def reset_session(http_version=None):
    global SESSION, SESSION_HTTP_VERSION
    if SESSION:
        SESSION.close()
    SESSION_HTTP_VERSION = http_version
    SESSION = create_session(http_version=http_version)
    return SESSION

def _is_http2_settings_error(exc):
    message = str(exc).lower()
    return "curl: (16)" in message and "settings frame" in message

def downgrade_session_to_http1(exc):
    if SESSION_HTTP_VERSION == "v1" or not _is_http2_settings_error(exc):
        return False
    print("[SYSTEM] HTTP/2 handshake failed, retrying with HTTP/1.1")
    reset_session(http_version="v1")
    return True

@app.on_event("startup")
async def startup():
    reset_session()
    if not config.get('dashboard_secret'):
        config['dashboard_secret'] = secrets.token_hex(32)
        save_config()

@app.on_event("shutdown")
async def shutdown():
    global SESSION
    if SESSION:
        SESSION.close()

# --- Streaming bridge: curl_cffi (sync) -> FastAPI (async) ---

_STREAM_END = object()

def _stream_worker(session, method, url, headers, json_data, q):
    """Worker thread: run curl_cffi streaming request, push chunks to queue."""
    try:
        resp = session.request(
            method=method, url=url, headers=headers, json=json_data,
            stream=True, timeout=600,
        )
        q.put(("status", resp.status_code))
        for chunk in resp.iter_content():
            q.put(("data", chunk))
        q.put(("end", None))
    except Exception as e:
        q.put(("error", e))

def _read_stream_bytes(resp):
    chunks = []
    for chunk in resp.iter_content():
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)

def _iter_sse_events(resp):
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()

    def iter_buffered_events():
        nonlocal buffer
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            event_str, buffer = buffer.split("\n\n", 1)
            event_str = event_str.strip()
            if not event_str:
                continue
            event_type = None
            data_lines = []
            for line in event_str.splitlines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if not data or data == "[DONE]":
                continue
            yield event_type, json.loads(data)

    for chunk in resp.iter_content():
        if isinstance(chunk, bytes):
            chunk = decoder.decode(chunk)
        buffer += chunk
        yield from iter_buffered_events()
    buffer += decoder.decode(b"", final=True)
    yield from iter_buffered_events()

def _build_buffered_message_response(events):
    message = None
    usage = {}
    content_blocks = []

    def ensure_block(index):
        while len(content_blocks) <= index:
            content_blocks.append(None)
        if content_blocks[index] is None:
            content_blocks[index] = {}
        return content_blocks[index]

    for event_type, payload in events:
        event_name = event_type or payload.get("type")
        if event_name == "message_start":
            message = dict(payload.get("message", {}))
            usage = dict(message.get("usage", {})) if isinstance(message.get("usage"), dict) else {}
            message["content"] = []
        elif event_name == "content_block_start":
            index = payload.get("index", 0)
            content_blocks.extend([None] * max(0, index - len(content_blocks) + 1))
            content_blocks[index] = dict(payload.get("content_block", {}))
        elif event_name == "content_block_delta":
            index = payload.get("index", 0)
            delta = payload.get("delta", {})
            block = ensure_block(index)
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["type"] = "text"
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta_type == "thinking_delta":
                block["type"] = block.get("type", "thinking")
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta_type == "signature_delta":
                block["signature"] = delta.get("signature")
            elif delta_type == "input_json_delta":
                block["type"] = block.get("type", "tool_use")
                block["_partial_input_json"] = block.get("_partial_input_json", "") + delta.get("partial_json", "")
        elif event_name == "message_delta":
            if message is None:
                message = {"type": "message", "content": []}
            delta = payload.get("delta", {})
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    message[key] = delta[key]
            if isinstance(payload.get("usage"), dict):
                usage.update(payload["usage"])
        elif event_name == "error":
            raise ValueError(payload.get("error", {}).get("message") or str(payload))

    if message is None:
        raise ValueError("Buffered stream ended without message_start event")

    finalized_blocks = []
    for block in content_blocks:
        if not block:
            continue
        partial_input = block.pop("_partial_input_json", None)
        if partial_input is not None:
            try:
                block["input"] = json.loads(partial_input)
            except json.JSONDecodeError:
                block["input"] = partial_input
        finalized_blocks.append(block)

    message["content"] = finalized_blocks
    if usage:
        message["usage"] = usage
    return message

def _buffered_stream_request(session, method, url, headers, json_data):
    resp = session.request(
        method=method, url=url, headers=headers, json=json_data,
        stream=True, timeout=600,
    )
    status_code = resp.status_code
    content_type = resp.headers.get("content-type", "")
    if status_code >= 400:
        return status_code, _read_stream_bytes(resp)
    if "text/event-stream" not in content_type.lower():
        return status_code, _read_stream_bytes(resp)
    buffered_message = _build_buffered_message_response(_iter_sse_events(resp))
    return status_code, json.dumps(buffered_message).encode("utf-8")

async def _async_chunks(q):
    """Async generator reading chunks from thread-safe queue."""
    loop = asyncio.get_running_loop()
    while True:
        msg_type, value = await loop.run_in_executor(None, q.get)
        if msg_type == "end":
            break
        if msg_type == "error":
            print(f"[PROXY] Stream error: {value}")
            break
        if msg_type == "data":
            yield value

@app.get("/config")
async def get_config(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    safe_config = config.copy()
    safe_config.pop('dashboard_secret', None)
    safe_config.pop('dashboard_password', None)
    return safe_config

@app.post("/config/reload")
async def reload_config(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    load_config()
    reset_session()
    return {"status": "ok", "message": "Configuration reloaded"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "v25",
        "proxy_enabled": config['use_proxy'],
        "tools_loaded": len(CLAUDE_CODE_TOOLS),
        "tls_fingerprint": "chrome",
    }

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    global SESSION
    # Normalize base URL: strip trailing /v1 if present, always prepend /v1
    base = config['target_base_url'].rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    target_url = f"{base}/v1/{path}"
    if path == "messages":
        target_url += "?beta=true"
    body = await request.body()
    body_json = {}
    client_wants_stream = False
    upstream_wants_stream = False
    buffer_stream_response = False
    if body:
        try:
            body_json = json.loads(body)
            # 透传客户端 body，仅做最小改写
            model = body_json.get('model', '')
            if 'anyrouter/' in model:
                body_json['model'] = model.replace('anyrouter/', '')

            if config['debug']:
                print(f"[PROXY] Request keys: {list(body_json.keys())}")
                print(f"[PROXY] Model: {model}, tools: {len(body_json.get('tools', []))}, "
                      f"system: {'yes' if body_json.get('system') else 'no'}")

            # 检测客户端类型：有 tools 或 system 的是富客户端（Xcode/Claude Code），直接透传
            is_bare_client = not body_json.get('tools') and not body_json.get('system')
            is_claude_model = any(k in model.lower() for k in ('sonnet', 'opus', 'haiku'))

            if is_bare_client and is_claude_model and CLAUDE_CODE_TOOLS:
                # 裸客户端（curl/OpenCode 等）：注入 Claude Code 伪装模板
                body_json['tools'] = CLAUDE_CODE_TOOLS
                if CLAUDE_CODE_SYSTEM:
                    body_json['system'] = CLAUDE_CODE_SYSTEM
                if 'thinking' not in body_json:
                    body_json['thinking'] = {"type": "adaptive"}
                if 'context_management' not in body_json:
                    body_json['context_management'] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
                if 'output_config' not in body_json:
                    body_json['output_config'] = {"effort": "medium"}
                if config['debug']:
                    print(f"[PROXY] Bare client detected, injected Claude Code camouflage")
            else:
                if config['debug']:
                    print(f"[PROXY] Rich client detected, passthrough body as-is")

            # 始终确保 metadata.user_id（伪装必需）
            if 'metadata' not in body_json:
                body_json['metadata'] = {"user_id": _make_user_id()}
            elif 'user_id' not in body_json.get('metadata', {}):
                body_json['metadata']['user_id'] = _make_user_id()

            config_max_tokens = _get_config_max_tokens()
            if path == "messages" and config_max_tokens is not None:
                original_max_tokens = body_json.get("max_tokens")
                body_json["max_tokens"] = config_max_tokens
                if config['debug']:
                    print(
                        f"[PROXY] Patched upstream max_tokens: "
                        f"{original_max_tokens!r} -> {config_max_tokens}"
                    )

            client_wants_stream = body_json.get('stream', False)
            buffer_stream_response = (
                path == "messages"
                and config.get("buffer_stream", False)
                and not client_wants_stream
            )
            upstream_wants_stream = client_wants_stream or buffer_stream_response
            if buffer_stream_response:
                body_json["stream"] = True
                if config['debug']:
                    print("[PROXY] Buffer stream enabled for downstream non-stream request")
        except Exception as e:
            if config['debug']:
                print(f"[PROXY] Body parse error: {e}")
    model_name = body_json.get('model', '')
    headers = get_claude_headers(
        is_stream=upstream_wants_stream,
        model=model_name,
        client_headers=dict(request.headers),
    )
    # Pass through client's API key to upstream
    req_api_key = request.headers.get("x-api-key", "")
    if not req_api_key:
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            req_api_key = bearer[7:]
    if req_api_key:
        headers["x-api-key"] = req_api_key
        if path == "models":
            headers["Authorization"] = f"Bearer {req_api_key}"
    if config['debug']:
        print(f"\n{'='*60}")
        print(f"[PROXY] Target: {target_url}")
        print(f"[PROXY] Model: {body_json.get('model', 'N/A')}")
        print(f"[PROXY] Client stream: {client_wants_stream}")
        print(f"[PROXY] Upstream stream: {upstream_wants_stream}")
        print(f"[PROXY] Buffer stream: {buffer_stream_response}")
        print(f"[PROXY] TLS: curl_cffi/chrome ({_format_http_version(SESSION_HTTP_VERSION)})")
    max_attempts = 5
    retry_delay = 1
    for attempt in range(max_attempts):
        try:
            if config['debug']:
                print(f"[PROXY] Attempt {attempt + 1}/{max_attempts}...")
                sys.stdout.flush()

            if client_wants_stream:
                q = thread_queue.Queue(maxsize=256)
                t = threading.Thread(
                    target=_stream_worker,
                    args=(SESSION, request.method, target_url, headers, body_json, q),
                    daemon=True,
                )
                t.start()
                loop = asyncio.get_running_loop()
                msg_type, value = await loop.run_in_executor(None, q.get)
                if msg_type == "error":
                    raise value
                status_code = value
                if config['debug']:
                    print(f"[PROXY] Status: {status_code}")
                if status_code in [520, 502]:
                    # drain queue
                    while True:
                        mt, _ = await loop.run_in_executor(None, q.get)
                        if mt in ("end", "error"):
                            break
                    if attempt < max_attempts - 1:
                        reset_session(http_version=SESSION_HTTP_VERSION)
                        await asyncio.sleep(retry_delay)
                        continue
                    return Response(content=b'{"error":{"message":"Network error after max retries"}}', status_code=502, media_type="application/json")
                if status_code in [403, 500]:
                    chunks = []
                    while True:
                        mt, v = await loop.run_in_executor(None, q.get)
                        if mt == "data":
                            chunks.append(v if isinstance(v, bytes) else v.encode())
                        elif mt in ("end", "error"):
                            break
                    error_content = b"".join(chunks)
                    if config['debug']:
                        print(f"[PROXY] Error response: {error_content.decode('utf-8', errors='ignore')[:500]}")
                    return Response(content=error_content, status_code=status_code, media_type="application/json")
                return StreamingResponse(
                    _async_chunks(q), status_code=status_code, media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            elif buffer_stream_response:
                status_code, buffered_content = await asyncio.to_thread(
                    _buffered_stream_request,
                    SESSION,
                    request.method,
                    target_url,
                    headers,
                    body_json,
                )
                if config['debug']:
                    print(f"[PROXY] Status: {status_code}")
                if status_code in [520, 502]:
                    if attempt < max_attempts - 1:
                        reset_session(http_version=SESSION_HTTP_VERSION)
                        await asyncio.sleep(retry_delay)
                        continue
                    return Response(content=b'{"error":{"message":"Network error after max retries"}}', status_code=502, media_type="application/json")
                if status_code in [403, 500]:
                    return Response(content=buffered_content, status_code=status_code, media_type="application/json")
                return Response(content=buffered_content, status_code=status_code, media_type="application/json")
            else:
                resp = await asyncio.to_thread(
                    SESSION.request, request.method, target_url,
                    headers=headers, json=body_json, timeout=600,
                )
                if config['debug']:
                    print(f"[PROXY] Status: {resp.status_code}")
                if resp.status_code in [520, 502]:
                    if attempt < max_attempts - 1:
                        reset_session(http_version=SESSION_HTTP_VERSION)
                        await asyncio.sleep(retry_delay)
                        continue
                    return Response(content=b'{"error":{"message":"Network error after max retries"}}', status_code=502, media_type="application/json")
                if resp.status_code in [403, 500]:
                    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except Exception as e:
            if config['debug']:
                print(f"[PROXY] Error: {type(e).__name__}: {e}")
                traceback.print_exc()
            if attempt < max_attempts - 1:
                if not downgrade_session_to_http1(e):
                    reset_session(http_version=SESSION_HTTP_VERSION)
            else:
                return Response(content=json.dumps({"error": {"message": str(e)}}), status_code=500)

# --- Auth Helpers ---

def _check_auth(request: Request):
    """Check dashboard cookie session."""
    token = request.cookies.get(COOKIE_NAME)
    if token and verify_session_token(token, config.get('dashboard_secret', '')):
        return True
    return False

# --- Dashboard Routes ---

@app.get("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_page(request: Request):
    if _check_auth(request):
        return RedirectResponse("/dashboard", status_code=302)
    return HTMLResponse(get_login_html())

@app.post("/dashboard/login")
async def dashboard_login(request: Request, password: str = Form(...)):
    stored = config.get('dashboard_password', '')
    if not stored:
        return HTMLResponse(get_login_html("Dashboard password not configured"), status_code=503)
    if not verify_password(password, stored):
        return HTMLResponse(get_login_html("Incorrect password"), status_code=401)
    token = create_session_token(config['dashboard_secret'])
    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=MAX_AGE, httponly=True, samesite="lax", path="/")
    return resp

@app.post("/dashboard/logout")
async def dashboard_logout():
    resp = RedirectResponse("/dashboard/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    if not _check_auth(request):
        return RedirectResponse("/dashboard/login", status_code=302)
    return HTMLResponse(get_dashboard_html(MODELS))

# --- Dashboard API ---

@app.post("/api/test-all")
async def api_test_all(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    api_key = request.headers.get("x-api-key", "")
    results = await test_all_models(SESSION, config, get_claude_headers, api_key)
    return results

@app.post("/api/test/{model_name}")
async def api_test_one(model_name: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if model_name not in MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_name}")
    api_key = request.headers.get("x-api-key", "")
    result = await test_single_model(SESSION, config, model_name, get_claude_headers, api_key)
    return result

@app.get("/api/model-status")
async def api_model_status(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return test_results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="AnyRouter Proxy Server")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--host", type=str, default=None, help="Bind host")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    args = parser.parse_args()
    config_loaded = load_config()
    load_claude_code_templates()
    if args.host:
        config['host'] = args.host
    if args.port:
        config['port'] = args.port
    needs_setup = args.setup or (not config_loaded)
    if needs_setup:
        if sys.stdin.isatty():
            setup_wizard()
        else:
            print("[SYSTEM] Setup required but stdin is not interactive; skipping wizard.")
    host = config.get('host', '127.0.0.1')
    port = config.get('port', 8765)
    print("=" * 60)
    print("AnyRouter Proxy Server v25 (curl_cffi/chrome)")
    print("=" * 60)
    print(f"Config:    {CONFIG_FILE}")
    print(f"Target:    {config['target_base_url']}")
    print(f"Proxy:     {config['proxy_url'] if config['use_proxy'] else 'Disabled'}")
    print(f"Debug:     {'Enabled' if config['debug'] else 'Disabled'}")
    print(f"BufStream: {'Enabled' if config['buffer_stream'] else 'Disabled'}")
    print(f"MaxTokens: {config['max_tokens'] or 'Passthrough'}")
    print(f"Tools:     {len(CLAUDE_CODE_TOOLS)} Claude Code tools loaded")
    print(f"TLS:       curl_cffi impersonate=chrome (Chrome JA3/JA4/H2)")
    print(f"Headers:   claude-cli/{_CLI_VERSION} (Node.js SDK {_SDK_PACKAGE_VERSION})")
    print(f"Dashboard: http://{host}:{port}/dashboard")
    print("-" * 60)
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8')
    log_level = "info" if config['debug'] else "warning"
    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    except KeyboardInterrupt:
        print("\nStopping server...")
