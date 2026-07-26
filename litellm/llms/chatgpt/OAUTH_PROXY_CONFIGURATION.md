# ChatGPT OAuth 环境代理配置设计

## 背景

ChatGPT OAuth runtime 当前使用 `httpx.AsyncClient(trust_env=False)`。这会忽略进程中的标准代理环境变量，导致必须通过代理访问 OpenAI OAuth 服务的部署无法启动 Device Flow。

本配置只控制 ChatGPT OAuth 出站客户端，不改变模型请求、数据库连接或 LiteLLM 其他 HTTP 客户端的代理行为。

## 配置

新增环境变量 `CHATGPT_OAUTH_TRUST_ENV`：

| 值 | 行为 |
| --- | --- |
| 未设置 | 使用默认值 `false`，保持现有行为 |
| `false` | 不读取标准代理环境变量 |
| `true` | 由 httpx 读取标准代理环境变量 |
| 其他值 | 初始化失败并报告配置错误 |

值的解析忽略大小写和首尾空白，但只接受 `true` 和 `false`，避免拼写错误静默改变网络路径。

当值为 `true` 时，实际代理选择由 httpx 完成。常用标准变量包括 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY`。如果使用 SOCKS 代理，运行环境还必须安装 httpx 的 SOCKS 依赖。

## 部署示例

不允许 ChatGPT OAuth 读取环境代理：

```bash
export CHATGPT_OAUTH_TRUST_ENV=false
```

允许 ChatGPT OAuth 读取 HTTP/HTTPS 代理：

```bash
export CHATGPT_OAUTH_TRUST_ENV=true
export HTTPS_PROXY=http://proxy.example.com:8080
export NO_PROXY=localhost,127.0.0.1
```

## 验证要求

单元测试覆盖默认值、显式 `true`、显式 `false`、大小写与空白，以及非法值。网络验证使用相同 runtime 配置请求真实 OpenAI Device Authorization endpoint，确认必须经过环境代理的运行环境能够获得 Device Flow，而 `false` 仍保持不读取代理的行为。完整管理接口验证需要使用新环境变量重启 LiteLLM Proxy。

## TDD 验证证据

| 保证 | 测试或命令 | 结果 |
| --- | --- | --- |
| 未配置时保持 `false` | `pytest tests/proxy_unit_tests/chatgpt_oauth/test_runtime.py -q` | PASS |
| `true`、`false`、大小写和空白被正确解析 | 同上 | PASS |
| 非法值不会静默改变网络路径 | 同上 | PASS |
| `false` 不读取 HTTPS 代理 | 使用真实 OpenAI device authorization endpoint | HTTP 403，复现直连地区限制 |
| `true` 读取 HTTPS 代理 | 使用真实 OpenAI device authorization endpoint | HTTP 200，返回 Device Auth ID 和 User Code |

RED 阶段测试因 `get_chatgpt_oauth_trust_env` 尚不存在而无法导入。GREEN 阶段同一测试文件共 6 项通过。全量 lint、类型预算、循环导入和 import safety 检查使用本地已有的 `origin/litellm_internal_staging` ref 通过；内部 GitLab fetch 在当前网络环境中无法完成。覆盖率插件运行出现异常高 CPU 且长时间不结束，已停止该次覆盖率采集；相关纯函数的全部分支由 6 个测试用例覆盖。
