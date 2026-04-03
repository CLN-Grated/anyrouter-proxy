# Xcode Intelligence Integration Guide

> Use AnyRouter Proxy with a NewAPI gateway to configure custom Claude Agent and Codex Agent in Xcode 26.3.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ Xcode 26.3                                               │
│  ├── Claude Agent (Anthropic protocol, /v1/messages)     │
│  └── Codex Agent  (OpenAI protocol, /v1/responses)       │
└────────────┬───────────────────────────┬─────────────────┘
             │                           │
             ▼                           ▼
┌──────────────────────────────────────────────────────────┐
│ NewAPI Gateway (your-server:8003)                         │
│ https://your-newapi-domain.example.com                    │
│  ├── Claude channels (type=14) → AnyRouter Proxy         │
│  └── Codex channels  (type=1)  → OpenAI-compatible API   │
└────────────┬─────────────────────────────────────────────┘
             │ Claude requests
             ▼
┌──────────────────────────────────────────────────────────┐
│ AnyRouter Proxy (your-server:8765)                        │
│  ├── Claude CLI fingerprint (headers + TLS)               │
│  ├── anthropic-beta flags (incl. context-1m)              │
│  ├── Bare client: inject Claude Code camouflage           │
│  └── Rich client (Xcode): passthrough body as-is         │
└────────────┬─────────────────────────────────────────────┘
             │
             ▼
        AnyRouter Upstream (anyrouter.top)
```

## Prerequisites

- macOS 26.2+, Xcode 26.3+
- A running AnyRouter Proxy instance (see main [README](../README.md))
- A NewAPI gateway (e.g., [new-api](https://github.com/QuantumNous/new-api)) with Anthropic-type channels pointing to your AnyRouter Proxy

## NewAPI Gateway Setup

### Add Anthropic Channel

1. Open your NewAPI admin panel
2. Add a new channel:
   - **Type**: Anthropic (type 14)
   - **Base URL**: `https://your-anyrouter-proxy-domain.example.com` (your AnyRouter Proxy)
   - **Key**: Your AnyRouter upstream API key
   - **Models**: `claude-opus-4-6`, `claude-haiku-4-5-20251001`, etc.
3. Create an API token for Xcode (no model limits, unlimited quota recommended)

### Important: 1M Context Support

The AnyRouter Proxy's `_ANTHROPIC_BETA_FULL` in `main.py` **must** include `context-1m-2025-08-07`. Without it, the upstream will reject requests with:

```
{"error":"1m context is available, please enable 1m context and retry"}
```

This is already included in the latest version. If you see this error after upgrading, verify the beta flags list in `main.py`.

## Xcode Client Configuration

### 1. Claude Agent

#### 1.1 Bypass built-in login

Xcode requires signing in with an Anthropic account. To use a custom endpoint, set a placeholder:

```bash
defaults write com.apple.dt.Xcode IDEChatClaudeAgentAPIKeyOverride ' '
```

#### 1.2 Set preferred model

```bash
defaults write com.apple.dt.Xcode IDEChatClaudeAgentModelConfigurationAlias 'opus'
```

#### 1.3 Create config file

```bash
mkdir -p ~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig
```

Write `~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/settings.json`:

```json
{
  "model": "claude-opus-4-6",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<your-newapi-token>",
    "ANTHROPIC_BASE_URL": "https://<your-newapi-domain>",
    "ANTHROPIC_MODEL": "claude-opus-4-6"
  }
}
```

Replace `<your-newapi-token>` and `<your-newapi-domain>` with your actual values.

#### 1.4 Restart

```bash
killall -9 ClaudeAgent 2>/dev/null
# Fully quit Xcode (Cmd+Q) and reopen
```

### 2. Codex Agent

Codex config is at `~/Library/Developer/Xcode/CodingAssistant/codex/config.toml`.

Add or modify the provider section:

```toml
model = "gpt-5.3-codex"

[model_providers.OpenAI]
name = "OpenAI"
base_url = "https://<your-newapi-domain>/v1"
env_key = "<your-newapi-token>"
wire_api = "responses"
requires_openai_auth = true
```

### 3. Custom Internet-Hosted Provider (Optional)

For basic chat completion (no agent features):

1. **Xcode → Preferences → Intelligence → Add a Model Provider → Internet Hosted**
2. **Endpoint**: `https://<your-newapi-domain>` (without `/v1/...`)
3. **Header**: `Authorization` (instead of default `x-api-key`)
4. **API Key**: Your NewAPI token

> Note: Providers added this way do not support full agent features (MCP, tool calling, etc.).

## How the Proxy Handles Different Clients

| Client Type | Detection | Behavior |
|-------------|-----------|----------|
| **Xcode Claude Agent** | Has `tools` + `system` in request body | Passthrough body as-is; only headers are disguised |
| **Claude Code CLI** | Has `tools` + `system` in request body | Same as above |
| **Bare client** (curl, OpenCode, etc.) | No `tools` or `system` | Injects Claude Code tools, system prompt, thinking config as camouflage |

In all cases:
- HTTP headers are always rewritten to match Claude CLI fingerprint
- `metadata.user_id` is always ensured
- `?beta=true` is appended to `/v1/messages` requests

## Verification

```bash
curl -s -X POST "https://<your-newapi-domain>/v1/messages" \
  -H "x-api-key: <your-token>" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":50,"messages":[{"role":"user","content":"Say hi"}]}'
```

A successful response should include `cache_creation_input_tokens` > 0, confirming 1M context is active.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `1m context ... enable 1m context` | Missing `context-1m-2025-08-07` in beta flags | Add to `_ANTHROPIC_BETA_FULL` in `main.py`, restart |
| Claude Agent says "I am Claude Code based on Sonnet 4.5" | Proxy overwrote client's system prompt | Update to latest proxy version (conditional injection) |
| Xcode Claude Agent no response | API key override not set | Run `defaults write com.apple.dt.Xcode IDEChatClaudeAgentAPIKeyOverride ' '` |
| `无可用账号` / No available account | Upstream channel key expired | Check/replace channel key in NewAPI |

## References

- [Xcode Claude Code integration with third-party APIs (Gist)](https://gist.github.com/zoltan-magyar/be846eb36cf5ee33c882ef5f932b754b)
- [Use Custom Models in Xcode 26 Intelligence](https://wendyliga.com/blog/xcode-26-custom-model/)
- [Setting up coding intelligence – Apple Developer](https://developer.apple.com/documentation/Xcode/setting-up-coding-intelligence)
