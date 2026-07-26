# ChatGPT OAuth 多账号测试计划

## 测试目的

在开发和发布前证明以下行为同时成立：管理员能安全管理多个 ChatGPT OAuth 账号；每个 Deployment 使用正确账号；
Token 能在多副本下主动刷新且不会重复刷新；账号失效后路由能隔离并恢复；升级不会破坏现有数据库和文件认证模式。

本文件是开发验收契约。功能尚未实现时不生成“通过”结论；实现完成后按本计划执行并单独输出测试报告。

## 判定原则

- 自动化测试不得依赖真实 OpenAI 账号、真实时间等待或互联网稳定性
- OAuth、模型 upstream 和时间统一使用可控制的 fake server / fake clock
- 主要证据来自 API response、数据库字段、捕获的 upstream request、Router 选择结果和 metric，不以日志作为唯一证据
- Token、邮箱和 Account ID 不得出现在测试失败输出、snapshot 或 CI artifact
- 结果只使用 `PASSED`、`FAILED`、`BLOCKED`、`SKIPPED`
- P0 用例全部通过且真实 smoke 通过后，功能才可发布

## 测试环境

### 自动化环境

```text
2 x LiteLLM Proxy
1 x PostgreSQL
1 x Redis
1 x Fake OAuth Server
1 x Fake ChatGPT Upstream
Fake Clock
```

Fake OAuth Server 必须支持：Device Flow pending/success/expired、Refresh Token 轮换、不返回新 Refresh Token、
`invalid_grant`、429 + `Retry-After`、超时和 5xx。

Fake ChatGPT Upstream 必须记录实际模型、`Authorization`、`ChatGPT-Account-Id`、Deployment ID 和调用次数，返回值只使用
测试标识，不回显敏感 Header。

### 真实 smoke 环境

- 独立测试 PostgreSQL、Redis 和稳定的 `LITELLM_SALT_KEY`
- 两个由管理员手工完成 Device Flow 的测试账号
- 不在脚本、环境日志或 CI 中保存密码、Cookie、MFA 和 Token
- 只执行最少量模型请求；后台刷新时序仍由 fake 环境验证

## 自动化覆盖矩阵

| Test ID | 优先级 | 场景 | 输入/操作 | 必须观察到的结果 | 决定性证据 |
| --- | --- | --- | --- | --- | --- |
| T01 | P0 | 新库 migration | 空 PostgreSQL 执行全部 migration | OAuth Account、Pending Flow 和索引与 Prisma schema 一致 | `prisma migrate deploy` 和 schema diff 均成功 |
| T02 | P0 | 旧库前向迁移 | 基线库写入旧业务数据后升级 | 只新增对象，旧数据数量和关键字段不变 | migration history、schema diff、迁移前后计数 |
| T03 | P0 | Token/邮箱加密 | 创建账号后直接查询数据库 | 数据库不存在任何明文 Token、邮箱或 Account ID 子串 | 原始测试值扫描为零，服务可正确解密 |
| T04 | P0 | 错误密钥与 AAD | 更换 Salt Key、交换两行密文 | 解密安全失败且不覆盖原数据 | 无 upstream request，数据库版本和密文未变化 |
| T05 | P0 | Device Flow成功 | pending -> authorized -> exchange | 创建唯一 `active` 账号，邮箱来自已验证 ID Token | API DTO、账号行、单次 exchange调用 |
| T06 | P0 | 身份 Token校验 | 伪造签名、issuer、audience 或 exp | 全部拒绝且不创建账号 | 账号表无新增，响应为受控错误码 |
| T07 | P0 | Flow安全与幂等 | 非管理员访问、跨管理员轮询、过期、并发成功轮询 | 未授权请求拒绝；成功流程只创建一个账号 | 403/expired响应、唯一账号行、单次 exchange |
| T08 | P0 | 账号生命周期 | disable、enable、reauthorize、删除被引用账号 | 状态正确传播；被引用删除返回冲突 | API response、账号状态、Deployment可选性 |
| T09 | P0 | Chat/Responses账号绑定 | 两个 Deployment 绑定不同 Credential | 两个 API 均发送绑定账号的 Header | Fake upstream 捕获的 Deployment ID与Header一一对应 |
| T10 | P0 | 身份覆盖防护 | 客户端伪造认证 Header | upstream 仍收到服务端解析的账号身份 | 捕获的最终 Header，不检查中间 kwargs |
| T11 | P0 | 禁止静默降级 | 指定不存在、停用或不可解密的数据库 Credential，同时存在有效 auth.json | 请求失败且不读取文件身份 | 文件 Authenticator 调用计数为零、无 upstream请求 |
| T12 | P0 | Wildcard动态模型 | `codex/* -> chatgpt/responses/*` 请求两个模型 | 使用同一账号池并解析到对应 upstream模型 | 实际模型名正确，Deployment ID稳定 |
| T13 | P0 | 后台主动刷新 | Fake Clock推进到到期前5分钟 | 无业务请求也刷新一次，且不调用模型 endpoint | Token endpoint计数一、模型计数零、版本加一 |
| T14 | P0 | 后台候选过滤 | 未到窗口、未被引用、disabled、reauth_required账号 | 全部跳过 | 对应 Credential 的 Token endpoint计数为零 |
| T15 | P0 | Refresh Token语义 | upstream分别返回/不返回新 Refresh Token | 返回时替换；不返回时保留旧值 | 解密后的新版本字段和单调递增的 `token_version` |
| T16 | P0 | 多副本并发刷新 | 两个 Proxy 同时扫描相同账号，并注入持租约进程崩溃 | 每轮只有一次有效刷新；租约到期可接管 | Refresh调用计数、lease owner/until、最终版本 |
| T17 | P0 | 临时刷新失败 | timeout、5xx、429，分别处于硬到期前后 | 未到期使用旧 Token；到期后隔离并按退避重试 | request结果、`refresh_backoff_until`、调用时间线 |
| T18 | P0 | 永久刷新失败 | Refresh返回 `invalid_grant`，随后重新授权 | 所有账号引用变为不可选；授权成功后恢复 | Credential状态、Router候选集、新 `token_version` |
| T19 | P0 | 请求路径兜底 | 停止后台刷新器并推进到请求刷新窗口 | 首个请求刷新成功，后续请求复用新版本 | 单次 Refresh调用、两个请求结果、版本变化 |
| T20 | P0 | 负载均衡和同组切换 | 两账号同组，令选中账号认证失败 | 当前请求排除失败 Deployment并选择另一账号 | 本请求尝试的 Deployment ID不重复，后备调用成功 |
| T21 | P0 | 监控与脱敏 | 成功、429、刷新失败和重授权 | 指标按 Deployment/Credential归因且不含 PII/Token | Spend/OTel/metric字段及全量敏感值扫描 |
| T22 | P0 | Admin UI | 添加、轮询、列表、邮箱检索、停用、重授权、删除 | 页面状态与 API 一致，浏览器存储无协议秘密 | DOM、network response、local/session storage快照 |
| T23 | P0 | 文件模式回归 | 不配置数据库 Credential，使用现有 auth.json | 行为与变更前一致 | 现有 Authenticator测试和 Chat/Responses请求成功 |
| T24 | P0 | 启动与部署配置 | 缺失/正确 Salt Key，Migration成功/失败 | OAuth功能安全失败或正常启动；Migration失败阻止 rollout | process状态、readiness、Migration Job状态 |

## 真实 smoke 矩阵

真实 smoke 只证明 OpenAI 当前协议兼容性，不替代自动化异常和并发测试。

| Test ID | 操作 | 预期结果 | 决定性证据 |
| --- | --- | --- | --- |
| S01 | 管理员通过 UI 添加账号 A、B | 两个账号均为 `active`，邮箱正确且无 Token展示 | Admin API DTO和账号状态 |
| S02 | 创建两个同模型 Deployment并分别绑定 A、B | 两个 Deployment 均可被 Router选择 | Model配置中的 Credential ID和健康状态 |
| S03 | 分别发起最小 Chat/Responses请求 | 请求成功并归因到实际 Deployment | response、Spend Log中的 Deployment ID |
| S04 | 停用 A 后请求同一模型组 | 不再选择 A，请求由 B完成 | Router候选、Spend Log和请求结果 |
| S05 | 恢复或重新授权 A | A重新加入且能完成最小请求 | 账号状态、Token版本和调用结果 |
| S06 | 检查日志、Trace、指标和浏览器存储 | 不包含 Token、Account ID和非Admin场景邮箱 | 对已知测试秘密做全量扫描结果为零 |

## 测试实现位置

建议按现有仓库结构新增或扩展：

| 范围 | 位置 |
| --- | --- |
| Cipher、Account Service、刷新器 | `tests/test_litellm/llms/chatgpt/` |
| Management API、RBAC、DTO | `tests/test_litellm/proxy/management_endpoints/` |
| Router、wildcard、failover、Cooldown | `tests/test_litellm/` 和 `tests/test_litellm/router_utils/` |
| Prisma migration | `tests/proxy_migration_tests/` |
| Admin UI组件 | `ui/litellm-dashboard/tests/` |
| Admin UI端到端 | `ui/litellm-dashboard/e2e_tests/` |

## 执行阶段

### 1. 开发中的快速反馈

```bash
uv run pytest tests/test_litellm/llms/chatgpt -x -vv
uv run pytest <本次修改关联的proxy/router测试文件> -x -vv
cd ui/litellm-dashboard && npm test -- --run <关联测试文件>
```

### 2. PR Gate

```bash
make pre-commit
make lint
make test-unit-llms
make test-unit-proxy-core
make test-unit-proxy-misc
make test-unit-root
```

再运行 ChatGPT OAuth 专项集成测试、PostgreSQL migration 测试和 Dashboard build/e2e。数据库命令使用测试专用
`DATABASE_URL`，不得指向开发或生产数据库。

### 3. 候选版本验收

使用候选镜像启动 PostgreSQL、Redis、Fake OAuth、Fake ChatGPT 和两个 Proxy 副本，执行 T01-T24。并发与调度相关用例
至少连续运行3次，不允许通过简单重跑掩盖确定性失败。

### 4. 发布前真实 smoke

由管理员手工提供账号并执行 S01-S06。测试完成后删除测试账号密文、撤销上游授权并记录清理状态，不保留密码或 Token artifact。

## 时间线与取证要求

刷新、Cooldown 和状态恢复用例至少记录：

```text
T0: Credential状态、expires_at、token_version、lease/backoff
T1: 扫描或请求触发，选中的Deployment和租约所有者
T2: Fake OAuth响应和数据库CAS结果
Tfinal: Router候选、最终request结果、指标和敏感数据扫描
```

涉及字段变化时，报告必须给出对象、字段、before和after。例如：

```text
object=OAuthAccount/account-a
field=token_version
before=7
after=8
semantics=本轮只有一次有效刷新写入
```

## 发布 Gate

以下条件全部满足才允许发布：

- T01-T24 全部 `PASSED`
- S01-S06 全部 `PASSED`；没有真实测试账号时标记 `BLOCKED`，不能用 mock结果替代协议兼容结论
- migration 在空库和基线旧库均通过
- 多副本刷新、租约崩溃恢复和请求兜底连续3轮无重复刷新
- 日志、Trace、Spend Logs、API response和浏览器存储敏感值扫描为零
- `make pre-commit`、相关 lint、Python测试、UI测试和 build通过
- 测试报告包含每个失败/阻塞用例的明确证据与清理状态
