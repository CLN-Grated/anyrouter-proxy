# Xcode Intelligence 集成指南

> 配置 Xcode 26.3 的 Claude Agent 使用 AnyRouter Proxy 作为自定义 API 端点。

## 概述

AnyRouter Proxy 目前仅支持 Claude 模型（Anthropic 协议）。通过本指南，你可以将 Xcode 的 Claude Agent 连接到你自己部署的 AnyRouter Proxy 实例。

支持两种接入方式：
- **直连模式**：Xcode → AnyRouter Proxy → AnyRouter 上游
- **网关模式**：Xcode → NewAPI 等 API 网关 → AnyRouter Proxy → AnyRouter 上游

## 架构

### 直连模式（推荐）

```
┌──────────────────────────────────────────┐
│ Xcode 26.3                               │
│  └── Claude Agent (Anthropic /v1/messages)│
└─────────────────┬────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────┐
│ AnyRouter Proxy (your-server:8765)        │
│  ├── Claude CLI 指纹伪装（headers + TLS） │
│  ├── anthropic-beta 标志（含 context-1m） │
│  ├── 裸客户端：注入 Claude Code 伪装      │
│  └── 富客户端（Xcode）：透传 body         │
└─────────────────┬────────────────────────┘
                  │
                  ▼
           AnyRouter 上游
```

### 网关模式（可选）

如果你已有 [NewAPI](https://github.com/QuantumNous/new-api) 等 API 网关，可以在中间加一层，实现令牌管理、用量统计等功能：

```
Xcode → NewAPI Gateway → AnyRouter Proxy → AnyRouter 上游
```

网关配置要点：
- 添加 Anthropic 类型通道（type=14），Base URL 指向你的 AnyRouter Proxy 地址
- Key 填写你的 AnyRouter 上游 API Key
- 创建 API 令牌给 Xcode 使用

## 前置条件

- macOS 26.2+，Xcode 26.3+
- 已部署的 AnyRouter Proxy 实例（参见 [部署文档](部署.md) 或主 [README](../README.md)）

## Xcode Claude Agent 配置

### 1. 绕过内置登录

Xcode 默认要求使用 Anthropic 账号登录。设置一个占位值即可绕过：

```bash
defaults write com.apple.dt.Xcode IDEChatClaudeAgentAPIKeyOverride ' '
```

### 2. 设置首选模型

```bash
defaults write com.apple.dt.Xcode IDEChatClaudeAgentModelConfigurationAlias 'opus'
```

### 3. 创建配置文件

```bash
mkdir -p ~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig
```

编辑 `~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/settings.json`：

#### 直连模式

```json
{
  "model": "claude-opus-4-6",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<your-anyrouter-api-key>",
    "ANTHROPIC_BASE_URL": "https://<your-anyrouter-proxy-domain>",
    "ANTHROPIC_MODEL": "claude-opus-4-6"
  }
}
```

- `ANTHROPIC_AUTH_TOKEN`：你的 AnyRouter 上游 API Key（Proxy 会透传到上游）
- `ANTHROPIC_BASE_URL`：你部署的 AnyRouter Proxy 地址（不含 `/v1`）

#### 网关模式

```json
{
  "model": "claude-opus-4-6",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<your-gateway-token>",
    "ANTHROPIC_BASE_URL": "https://<your-gateway-domain>",
    "ANTHROPIC_MODEL": "claude-opus-4-6"
  }
}
```

- `ANTHROPIC_AUTH_TOKEN`：API 网关上创建的令牌
- `ANTHROPIC_BASE_URL`：API 网关地址

### 4. 重启

```bash
killall -9 ClaudeAgent 2>/dev/null
# 完全退出 Xcode（Cmd+Q）后重新打开
```

## 代理对不同客户端的处理方式

| 客户端类型 | 检测方式 | 行为 |
|-----------|---------|------|
| **Xcode Claude Agent** | 请求 body 包含 `tools` + `system` | 透传 body，仅伪装 HTTP 头 |
| **Claude Code CLI** | 请求 body 包含 `tools` + `system` | 同上 |
| **裸客户端**（curl 等） | 无 `tools` 或 `system` | 注入 Claude Code tools/system 等伪装模板 |

所有情况下：
- HTTP 头始终改写为 Claude CLI 指纹
- `metadata.user_id` 始终确保存在
- `?beta=true` 会附加到 `/v1/messages` 请求

## 1M 上下文支持

AnyRouter Proxy 的 `_ANTHROPIC_BETA_FULL` 中已包含 `context-1m-2025-08-07`。如果上游返回类似以下错误：

```
{"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试"}
```

请检查 `main.py` 中的 beta 标志列表是否包含该项。

## 验证

```bash
# 健康检查
curl -s https://<your-proxy-domain>/health

# 发送测试请求
curl -s -X POST "https://<your-proxy-domain>/v1/messages" \
  -H "x-api-key: <your-anyrouter-api-key>" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":50,"messages":[{"role":"user","content":"Say hi"}]}'
```

成功响应中应包含 `cache_creation_input_tokens` > 0，表明 1M 上下文已激活。

## 自定义互联网托管提供商（可选）

Xcode 还支持通过 Preferences → Intelligence 添加自定义提供商，但该方式不支持完整的 Agent 功能（MCP、工具调用等）：

1. **Xcode → Preferences → Intelligence → Add a Model Provider → Internet Hosted**
2. **Endpoint**：`https://<your-proxy-domain>`（不含 `/v1/...`）
3. **Header**：`Authorization`（替代默认的 `x-api-key`）
4. **API Key**：你的 API Key

> 注意：此方式添加的提供商不支持完整 Agent 功能。

## 常见问题

| 错误 | 原因 | 解决方案 |
|-----|------|---------|
| `1m context ... enable 1m context` | beta 标志缺少 `context-1m-2025-08-07` | 在 `main.py` 的 `_ANTHROPIC_BETA_FULL` 中添加，重启 |
| Claude Agent 回复"我是基于 Sonnet 4.5 的 Claude Code" | 代理覆盖了客户端的 system prompt | 更新到最新版（条件注入逻辑） |
| Xcode Claude Agent 无响应 | 未设置 API key override | 执行 `defaults write` 命令绕过内置登录 |
| `无可用账号` | 上游通道 key 失效 | 检查/更换 API Key |

## 参考

- [Xcode Claude Code integration with third-party APIs (Gist)](https://gist.github.com/zoltan-magyar/be846eb36cf5ee33c882ef5f932b754b)
- [Use Custom Models in Xcode 26 Intelligence](https://wendyliga.com/blog/xcode-26-custom-model/)
- [Setting up coding intelligence – Apple Developer](https://developer.apple.com/documentation/Xcode/setting-up-coding-intelligence)
