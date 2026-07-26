# ChatGPT OAuth 账号负载均衡与 Token 轮转

## 文档目的

本文单独定义 ChatGPT OAuth 多账号场景下的对象关系、请求选路、Token 轮转、故障切换和监控归因。
主设计只定义功能边界；本文件是这些运行时行为的详细设计。

## 对象关系

四个对象不能混为一谈：

| 对象 | 含义 | 示例 |
| --- | --- | --- |
| Provider | LiteLLM 的上游协议实现 | `chatgpt` |
| Provider Model | 实际请求的上游模型，可以是固定值或 wildcard 动态值 | `chatgpt/responses/gpt-5.4` |
| OAuth Credential | 一个已登录的 ChatGPT 账号身份 | 账号 A |
| Deployment | 一份可独立选路、限流、健康判断和监控的调用配置 | `gpt-5-account-a` |
| Model Group | 客户端看到的逻辑模型名及其 Deployment 池 | `codex-gpt-5` |

关系固定为：

```text
Model Group 1 ── N Deployment
Deployment  N ── 1 OAuth Credential
Deployment  N ── 1 Provider Model 配置（固定值或 wildcard）
OAuth Credential 1 ── N Deployment
```

因此，一个 Deployment 在本设计中只绑定一个账号和一个 Provider；模型可以选择固定模式或 wildcard 动态模式。

### 固定模型模式

一个 Deployment 固定指向一个 Provider Model。如果账号 A 同时调用 `gpt-5.4` 和 `gpt-5-mini`，需要建立两个
Deployment，并让它们复用账号 A 的 Credential。

示例：

```yaml
model_list:
  - model_name: codex-gpt-5
    litellm_params:
      model: chatgpt/gpt-5
      chatgpt_oauth_credential_id: account-a
    model_info:
      id: gpt-5-account-a

  - model_name: codex-gpt-5
    litellm_params:
      model: chatgpt/gpt-5
      chatgpt_oauth_credential_id: account-b
    model_info:
      id: gpt-5-account-b

  - model_name: codex-gpt-5-mini
    litellm_params:
      model: chatgpt/gpt-5-mini
      chatgpt_oauth_credential_id: account-a
    model_info:
      id: gpt-5-mini-account-a
```

前两个 Deployment 组成 `codex-gpt-5` 账号池。第三个 Deployment 使用相同账号 A，但属于另一个模型组。
账号级配额不能仅按 Deployment 分别计算；同一 Credential 被多个 Deployment 复用时，账号级限流和 429 状态必须共享。

### Wildcard 动态模型模式

LiteLLM 的 Pattern Router 支持用请求模型名替换 Deployment 中的 `*`。如果希望用户请求不同模型时自动使用同一个账号池，
可以为每个账号配置一个 wildcard Deployment：

```yaml
model_list:
  - model_name: codex/*
    litellm_params:
      model: chatgpt/responses/*
      chatgpt_oauth_credential_id: account-a
    model_info:
      id: chatgpt-account-a

  - model_name: codex/*
    litellm_params:
      model: chatgpt/responses/*
      chatgpt_oauth_credential_id: account-b
    model_info:
      id: chatgpt-account-b
```

请求的动态映射为：

```text
model=codex/gpt-5.4      -> 账号池选择 A/B -> chatgpt/responses/gpt-5.4
model=codex/gpt-5-mini   -> 账号池选择 A/B -> chatgpt/responses/gpt-5-mini
```

这里动态变化的只有当前请求的解析后模型名；Deployment ID、Provider 和 Credential 绑定不变。Pattern Router 为当前请求
复制 Deployment 并替换 `litellm_params.model`，不能把解析结果写回共享配置对象。

Wildcard 模式避免创建“账号数 × 模型数”份配置，适合作为多账号池默认方式，但必须增加允许模型列表或模式校验，不能允许用户
借助 `*` 调用未授权模型。固定模式适合需要为不同模型设置独立 RPM、TPM、预算、权重或 Fallback 的场景。两种模式可以同时存在，
具体模型配置应优先于 wildcard 配置。

## 谁触发 Token 轮转

数据库 OAuth 多账号模式默认启用专用后台刷新器，因此正常情况下由后台任务在业务请求到达前完成轮转：

```text
后台每60秒扫描
  -> 找出未来5分钟内到期的 active 且已被 Deployment 引用的 Credential
  -> 抢占单 Credential 刷新租约
  -> 调用 OAuth Token endpoint
  -> 加密并 CAS 更新数据库
```

业务请求仍保留惰性刷新作为兜底。当后台刷新器尚未运行、发生延迟或刚好错过扫描窗口时，轮转发生在 Router 已选中
Deployment 之后：

1. 客户端请求逻辑模型名，例如 `codex-gpt-5`
2. Router 从健康 Deployment 中选中 `gpt-5-account-b`
3. Provider resolver 根据该 Deployment 的 `chatgpt_oauth_credential_id` 读取账号 B
4. 若账号 B 的 Access Token 已进入 `expires_at - 60秒` 窗口，当前业务请求触发账号 B 的刷新
5. 刷新成功后，当前请求继续调用 `chatgpt/gpt-5`

这个请求只刷新所选 Deployment 绑定的 Credential，不会轮转模型组中的所有账号。后台刷新器和请求路径必须调用同一个
`ChatGPTOAuthAccountService.refresh_if_needed(credential_id, refresh_ahead_seconds)`：后台传入300秒，请求传入60秒，
不能维护两套刷新实现。Kubernetes 的
`/health/liveliness` 和 `/health/readiness` 探针不调用模型，也不触发 Token 刷新。

## Token 轮转规则

| 对象 | 轮转时机 | 触发方 |
| --- | --- | --- |
| Access Token | 后台任务在到期前5分钟主动刷新；请求路径在到期前60秒兜底 | 后台刷新器或 `get_valid_access_token` |
| Refresh Token | 刷新 Access Token 时，上游返回了新的 Refresh Token | OpenAI Token endpoint |
| 重新授权 | `invalid_grant`、授权撤销或管理员主动操作 | 状态机或管理员 |
| `LITELLM_SALT_KEY` | 不做周期轮换；仅安全事件或制度要求时专项处理 | 运维流程 |

刷新步骤：

1. 解密 Credential 的 Token record
2. 未进入提前刷新窗口时直接复用 Access Token
3. 进入窗口后，抢占单 Credential 短租约
4. 锁内重新读取；其他副本已经刷新时直接使用新版本
5. 调用 Token endpoint
6. 验证新的 ID Token，并更新 Access Token、到期时间及身份派生字段
7. 上游返回新 Refresh Token 时替换；未返回时保留旧值
8. 加密并以 `token_version` CAS原子写回，令版本加一，随后使旧缓存失效

刷新租约和 CAS 的粒度是 Credential，不是 Deployment。账号 A 被多个 Deployment 复用时，仍只允许一次有效刷新；不同账号可以并行刷新。

## 后台主动刷新器

### 启用范围

- 数据库 OAuth 多账号模式默认启用，不复用 `general_settings.background_health_checks`
- 只扫描 `status=active` 且至少被一个启用的 Deployment 引用的 Credential
- `disabled`、`reauth_required` 和未被引用的账号不自动刷新
- 只调用 OAuth Token endpoint，不发送模型请求，因此不产生模型 Token/调用费用
- 后台任务失效时，请求路径的提前60秒刷新继续作为安全兜底

### 调度参数

| 参数 | 首版默认值 | 语义 |
| --- | --- | --- |
| `enabled` | `true` | 数据库 OAuth 模式是否启动后台刷新器 |
| `scan_interval_seconds` | `60` | 扫描周期，启动后立即执行一次，不先等待完整周期 |
| `refresh_ahead_seconds` | `300` | 刷新未来5分钟内到期的 Token |
| `batch_size` | `100` | 单次查询和处理的最大 Credential 数量 |
| `max_concurrency` | `5` | 单个实例同时调用 Token endpoint 的最大数量 |
| `lease_seconds` | `30` | 单 Credential 刷新租约；必须大于 Token endpoint 超时 |
| `request_refresh_ahead_seconds` | `60` | 后台任务未及时完成时，请求路径的兜底窗口 |

配置应放在 Proxy 的数据库 OAuth/ChatGPT 设置中；不把这些参数混入 Deployment，因为调度对象是 Credential。扫描周期加入小幅随机
jitter，避免多个副本在同一时刻同时查询和抢占。

### 多副本抢占

不能在调用外部 Token endpoint 的整个网络等待期间持有数据库行锁。每个副本按以下流程执行：

1. 分页查询 `status=active`、已被引用、`expires_at <= now + refresh_ahead`、退避已结束的候选账号
2. 以条件更新抢占短租约：记录 `refresh_lease_owner` 和 `refresh_lease_until`；已有未到期租约时跳过
3. 在事务外解密 Token，并调用统一的 `refresh_if_needed`
4. 以原 `token_version` 做 CAS；成功时写入新密文、到期时间并令版本加一
5. 成功后清除租约、失败次数和退避时间，并发布缓存失效事件
6. 临时失败时清除租约，更新 `refresh_failure_count` 和 `refresh_backoff_until`
7. 进程崩溃时不需要人工清锁；租约到期后其他副本可以接管

请求路径发现后台租约存在时：旧 Access Token 仍有效则直接使用；已经进入60秒兜底窗口时短暂重新读取版本，后台仍未完成则由当前
请求参与相同的租约抢占。任何路径都不得绕过 Credential 租约直接并发刷新。

### 失败与降级

- 网络错误、超时或 5xx：保留旧 Token，使用30秒起步、最大5分钟的指数退避，并优先遵循合法 `Retry-After`
- `invalid_grant` 或授权撤销：立即置为 `reauth_required`，停止后台重试并通知管理员
- 数据库写入或加密失败：不得仅在内存使用新 Token；保留可审计错误并退避
- 后台循环自身异常：隔离单账号错误，继续处理其他账号；循环级异常记录指标并在下一周期恢复
- 后台刷新器整体不可用：不把 Proxy 全局 readiness 置为失败，避免停止全部业务流量；请求刷新兜底，同时触发高优先级告警

“持续刷新”指周期扫描并仅刷新接近到期的 Token，不是每分钟对全部账号调用刷新接口，也不承诺上游授权永远不会撤销。

## 负载均衡、同组故障切换与 Fallback

三个概念按以下顺序发生：

```text
模型组内正常负载均衡
  -> 选中一个 Deployment
  -> 解析并刷新该 Deployment 的 Credential
  -> 失败时排除该 Deployment，在同模型组重新选择
  -> 同模型组耗尽后，才进入显式配置的跨模型 Fallback
```

- **负载均衡**：正常请求按 `routing_strategy` 在同模型组健康 Deployment 之间分流
- **同组故障切换**：某个账号实际失效后，当前请求排除失败的 `model_info.id`，重新选择同组其他账号
- **Fallback**：同模型组全部不可用后，按照 `fallbacks` 切换到另一个模型组

不能只依赖普通 Retry 随机重选，因为它可能再次选择同一个失效 Deployment。目标实现必须把失败的 `model_info.id`
带回 Router，并在当前请求后续尝试中明确排除。当前 LiteLLM 的 `enable_weighted_failover` 能提供相近的同组排除行为，
但它默认关闭且存在调用路径限制；ChatGPT OAuth 认证失效不能依赖管理员碰巧启用该选项。

失败语义：

| 情况 | 当前请求 | 后续请求 |
| --- | --- | --- |
| 刷新成功 | 等待刷新后继续 | 使用新 Token |
| 临时网络错误/超时/5xx，旧 Access Token 未实际到期 | 使用旧 Token 继续 | 后续请求再次尝试刷新 |
| Token 已到期且刷新暂时失败 | 同模型组重新选择账号 | 该 Credential 的全部引用暂时不可选，按退避恢复 |
| `invalid_grant` 或授权撤销 | 同模型组重新选择账号 | Credential 置为 `reauth_required`，所有引用它的 Deployment 均不可选 |
| 同模型组全部账号不可用 | 进入显式配置的跨模型 Fallback；未配置则失败 | 持续告警，等待恢复或重新授权 |

## 失败的 Deployment 什么时候重新加入

“排除”分为三个作用域：

| 失败类型 | 排除范围 | 重新加入条件 |
| --- | --- | --- |
| 当前请求已尝试失败 | 仅当前请求的 Deployment ID 集合 | 请求结束后集合销毁；同一个请求内绝不再次选择 |
| 模型调用超时、5xx、429等临时故障 | Router Deployment Cooldown | 现有 `cooldown_time` 到期后重新成为候选，遵循上游 `Retry-After` 时以其为准 |
| Token 已到期且 Refresh 出现网络错误、超时或 5xx | OAuth Credential 级刷新退避，影响所有引用它的 Deployment | `refresh_backoff_until` 到期后优先由后台刷新器试刷新；后台不可用时由下一次请求兜底 |
| `invalid_grant`、Refresh Token 被撤销 | OAuth Credential 持久状态 `reauth_required` | 管理员完成重新授权并成功验证新 Token 后恢复为 `active` |
| 管理员停用 | OAuth Credential 持久状态 `disabled` | 管理员启用且 Token 校验成功 |

Credential 级临时退避不能只给一个 Deployment 设置 Cooldown，否则同一账号被其他固定模型或 wildcard Deployment 引用时会继续
重复刷新。实现应按 Credential ID 共享 `refresh_backoff_until`；第一阶段采用30秒起步的指数退避，最大5分钟，并优先遵循上游
合法的 `Retry-After`。退避到期表示“允许试探”，不是已经确认健康：后台刷新器优先试刷新，后台不可用时才由下一次选中它的请求
负责刷新；刷新成功后才清零失败计数。

如果旧 Access Token 仍未到 `expires_at`，刷新临时失败不会排除 Deployment；当前请求继续用旧 Token，仅记录下一次刷新时间。

## 健康检查

- `/health/liveliness`：Proxy 进程存活，不检查账号
- `/health/readiness`：数据库、缓存、回调等基础依赖，不逐个检查账号
- `general_settings.background_health_checks`：可选的 Provider/模型调用检查，默认关闭；可能顺带触发 resolver，
  但可能产生调用成本，不能作为 Token 轮转正确性的依赖
- OAuth 后台刷新器：专门调用 Token endpoint 的账号保活任务，与模型后台健康检查相互独立
- OAuth Credential 健康：由账号状态、Token 到期、刷新结果维护
- Deployment 可选性：同时取决于 Deployment 自身状态和其 Credential 状态

## 监控数据存在哪里

不新增一张包办所有数据的“监控表”。不同生命周期的数据放在不同位置：

| 数据 | 主存储 | 典型字段/标识 | 原因 |
| --- | --- | --- | --- |
| 账号持久状态 | OAuth Account 表 | `status`、`expires_at`、`token_version`、`refresh_backoff_until`、`refresh_failure_count`、`refresh_lease_owner/until`、`last_refresh_at`、`last_auth_error_code/at`、`last_used_at` | 需要跨副本一致并支持管理页面查询 |
| Deployment 配置和账号绑定 | LiteLLM Model/Deployment 配置存储 | `model_info.id`、`model_name`、`model`、`chatgpt_oauth_credential_id` | 属于路由配置；`model` 可为 wildcard |
| 短期健康与 Cooldown | Router 现有 Cache，生产多副本使用 Redis | Deployment ID、失败次数、冷却截止时间 | 高频、短生命周期，不适合持续写数据库 |
| 请求明细 | LiteLLM Spend Logs/Callbacks | Deployment ID、模型、Token、延迟、状态码 | 复用现有观测链路 |
| 时序指标 | Prometheus/OpenTelemetry 后端 | 请求量、错误、429、刷新成功/失败、刷新延迟、候选数、积压数、循环时间和后台心跳 | 用于 Dashboard 和告警 |

监控关联键以无 PII 的 `model_info.id` 和 Credential ID 为主；wildcard Deployment 还必须记录当前请求解析后的实际模型名，
否则多个动态模型会错误聚合到同一条模型指标。Admin UI 可以展示账号邮箱；日志、指标标签和 Trace
不得使用邮箱、OpenAI Account ID、Access Token 或 Refresh Token。

如果一个 Credential 被多个 Deployment 复用：

- `invalid_grant` 是账号级故障，必须使所有引用该 Credential 的 Deployment 不可选
- 单个模型调用 5xx、延迟或内容错误是 Deployment/Provider Model 级故障，不应直接停用整个账号
- 429 需要同时记录 Deployment 维度和 Credential 维度，避免同账号跨模型复用时重复消耗配额

## 覆盖矩阵

| 场景 | 输入构造 | 预期结果 | 决定性证据 |
| --- | --- | --- | --- |
| 两账号属于同一模型组 | 两个 Deployment 绑定不同 Credential | 正常请求按策略分流 | Header、Deployment ID 与 Credential 一一对应 |
| Wildcard 动态模型 | 两个账号配置 `codex/* -> chatgpt/responses/*` | 不同请求模型使用同一账号池并解析到对应上游模型 | Deployment ID稳定，实际模型名分别正确 |
| Wildcard 越权模型 | 请求不在允许列表中的模型 | 路由前拒绝 | 无 OAuth 解析、Refresh 或上游模型调用 |
| 首个请求命中提前刷新窗口 | Access Token 距到期不足60秒 | 只刷新所选 Deployment 绑定的 Credential | 单次 Refresh 调用，当前请求随后调用模型 |
| 后台提前刷新 | Token 在未来5分钟内到期 | 无业务请求也完成刷新 | Token endpoint被调用，模型 endpoint调用计数为零 |
| 后台跳过无需刷新账号 | Token 剩余时间超过5分钟或账号未被引用 | 不调用 Token endpoint | Refresh调用计数为零 |
| 后台与请求竞争 | 后台刷新时业务请求进入60秒窗口 | 只有一个租约持有者执行刷新 | 单次 Refresh调用，无 Token版本覆盖 |
| 后台多副本竞争 | 两个 Proxy 同时扫描相同账号 | 只有一个副本抢到租约 | 单次 Refresh调用，租约所有者明确 |
| 刷新进程崩溃 | 抢占租约后终止实例 | 租约到期后其他副本接管 | 最终版本只递增一次，无永久死锁 |
| 后台循环故障 | 扫描查询或循环任务异常 | 请求路径仍能兜底刷新 | 请求成功且后台心跳/错误告警触发 |
| Refresh Token 轮换 | 上游返回新 Refresh Token | 新值替换旧值并持久化 | 解密新版本匹配返回值，旧值不再使用 |
| Refresh Token 保留 | 上游不返回新 Refresh Token | 保留旧值 | 新密文中的 Refresh Token 与旧值相同 |
| 提前刷新临时失败 | 旧 Access Token 未实际到期，Refresh 超时/5xx | 当前请求使用旧 Token | 模型请求成功，数据库 Token 未被覆盖 |
| 到期刷新失败 | Access Token 已到期，Refresh 超时/5xx | 排除当前 Deployment并同组重选 | 后备 Deployment 收到同一请求 |
| 同 Credential 进程内并发 | 一个账号收到并发请求 | 只有一次有效刷新 | Refresh 调用计数一，`token_version + 1` |
| 同 Credential 多副本并发 | 两个 Service 实例同时刷新 | 行锁/CAS只允许一个写入 | 无旧版本覆盖，其他副本读取新版本 |
| 不同 Credential 并发 | 两个账号同时到期 | 两行独立并行刷新 | 无全局锁串行 |
| 同 Credential 跨 Deployment | 相同账号绑定两个 Provider Model | 共享一次账号刷新和账号级状态 | 两个 Deployment 观察相同 `token_version` |
| `invalid_grant` | Refresh endpoint拒绝账号 A | 账号置为 `reauth_required`，所有引用均不可选 | 同组重选排除 A，其他模型也不再选择 A |
| 普通 Retry | 第一次已确认 Deployment 失效 | 不得再次随机选中它 | 本请求尝试列表中 Deployment ID 不重复 |
| 临时故障恢复 | Cooldown/刷新退避到期 | Deployment重新成为候选，成功调用后恢复健康 | 到期前无调用，到期后单次试探成功 |
| 永久授权失效恢复 | `reauth_required` 后等待 | 不自动加入；重新授权成功后加入 | 新 `token_version` 持久化且状态恢复 `active` |
| 同组耗尽 | 所有账号不可用 | 仅在配置后切换其他模型组 | Fallback记录包含原组和目标组 |
| Kubernetes 探针 | 调用存活和就绪端点 | 不调用 OpenAI、不刷新 Token | Refresh 和模型调用计数均为零 |
| Provider 后台检查 | 启用 `background_health_checks` | 可走统一 resolver，但轮转不依赖它 | 不存在第二套刷新逻辑 |
| 缓存失效 | 刷新、停用、重授权 | 多副本不再使用旧版本 | 其他实例观察新 version/status |
| Deployment 监控 | 两账号分别成功、失败、429 | 按 Deployment与Credential维度归因且无 PII | Spend/OTel/指标标签关联正确 ID |
