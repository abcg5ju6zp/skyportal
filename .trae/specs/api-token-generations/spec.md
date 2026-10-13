# 作用域 API 令牌世代与轮换 - 产品需求文档

## Overview
- **Summary**: 为自动化集成提供具有独立名称、稳定系列/世代、明确组与数据流范围、到期时间的 API 令牌；鉴权只使用令牌自身的授权快照；支持同系列轮换（可配置短暂重叠期）、显式撤销，并在到期、所有者停用、权限来源移除时拒绝新请求。
- **Purpose**: 现状下长期令牌（baselayer `Token`）动态继承所有者当前的组与数据流（`Token.groups` 返回 `created_by.groups`，`accessible_streams` 返回 `created_by.streams`），只能整体继承、无法最小授权，也无法在不断流的情况下轮换；密文（即 token id）还直接展示在个人资料页。
- **Target Users**: 创建自动化集成的团队成员（通过 UI 或 API 自助管理令牌）、系统管理员、以 API 令牌访问 HTTP 接口与实时连接的集成程序。

## Goals
- 创建作用域令牌：独立名称、显式 ACL/组/数据流范围、到期时间；范围超出所有者当前权限时拒绝创建。
- 令牌授权快照化：HTTP 与实时连接鉴权只认令牌世代自身的 ACL/组/数据流；所有者事后新增权限不扩张令牌。
- 系列与世代：轮换在同一系列下生成新世代（新密文），支持可配置短暂重叠期；新请求使用新世代，已建立的连接按认证时状态自然结束。
- 生命周期收敛：重叠期结束、到期、显式撤销、用户停用、权限来源被移除后，旧世代不能再建立新请求/新连接；并发轮换、撤销与服务重启后状态以数据库持久状态为准。
- 列表安全：令牌列表展示范围、到期、系列关系与可用状态，但不泄露密文；密文仅在创建/轮换成功时一次性返回。
- 向后兼容：未设置独立范围与有效期的现有令牌继续沿用原有动态继承语义。

## Non-Goals
- 不做服务端定时/自动轮换调度（cron）；轮换由用户/集成显式发起。重叠期已足以支撑“定时轮换”的运维节奏。
- 不改造浏览器 Cookie 会话的现有鉴权与 15 分钟 socket JWT 行为。
- 不做令牌细粒度的组管理员（GroupUser.admin）委派；作用域令牌不获得组管理员能力。
- 不做审计日志/使用量统计界面、不做 OAuth 式 scope 字符串体系（沿用现有 ACL + 组 + 数据流模型）。
- 不强制轮换既有旧令牌；旧令牌无限期保留兼容语义。

## Background & Context
- 令牌模型在 baselayer 子模块 `baselayer/app/models.py::Token`（id 即密文、name、created_by、token_acls 关联表）；SkyPortal 在 `skyportal/models/user_token.py` 动态挂接 `Token.groups`/`accessible_groups`/`accessible_streams`。
- HTTP 鉴权在 `baselayer/app/access.py`：每请求解析 `Authorization: token <id>`，查库后 `_authorize_token` 仅校验所有者 `is_active()`；令牌对象合并进 `VerifiedSession` 作为访问控制主体。
- 组成员 SQL 策略集中在 `skyportal/models/group.py`（`AccessibleIfGroupUserMatches`、`AccessibleIfGroupUserIsAdminAndUserMatches`、`accessible_by_streams_members` 等），底层遍历逻辑在 baselayer `AccessibleIfUserMatches.select_accessible_rows`，通过 `user_id_from_user_or_token` 将令牌解析为所有者 id，再经 group_users/stream_users 关联表过滤。Photometry 等读权限 = 组 ∪ 数据流 ∪ owner。
- 实时连接：浏览器经 `/api/.../socket_auth_token`（cookie 鉴权）换取 15 分钟 JWT，再由 `baselayer/services/websocket_server` 校验 JWT 建立长连接；连接建立后服务端不再复查凭证。
- 创建/列表/删除接口在 `skyportal/handlers/api/internal/token.py`；个人资料聚合在 `skyportal/handlers/api/internal/profile.py`；前端在 `static/js/components/user/{NewTokenForm,TokenList,UpdateTokenACLs}.tsx` 与 `static/js/ducks/profile.ts`；请求体模型在 `projects/api-models/skyportal_py_models/tokens.py`。
- Alembic 单一 head：`28442e0a1830`。
- 已确认决策：① 权限来源移除后该世代整体拒绝新请求；② 列表新旧令牌一律不显示密文；③ 重叠期默认 60 秒、可配范围 0–3600 秒；④ 作用域令牌组必选非空，数据流可空。

## Functional Requirements

### 创建与授权快照
- **FR-1**: 用户可创建“作用域令牌”：独立名称（同一所有者下唯一）、显式 ACL 列表、显式组列表（≥1）、显式数据流列表（可为空）、到期时间（未来时刻，必填）。创建成功返回新密文一次。
- **FR-2**: 创建时服务端逐项校验：ACL ⊆ 所有者当前 permissions；组 ⊆ 所有者当前组；数据流 ⊆ 所有者当前数据流。任一项超出即 400 拒绝，且不落库任何部分数据。
- **FR-3**: 作用域令牌不得包含 “System admin” ACL（管理员集成继续使用旧型令牌）。
- **FR-4**: 令牌的有效授权恒等于世代快照：ACL 取自 token_acls；组取自新关联表 group_tokens；数据流取自 stream_tokens。所有者事后新增 ACL/组/数据流，令牌访问范围不扩张。
- **FR-5**: 未携带作用域参数创建的令牌仍为旧型（兼容）：无系列、无到期、组/数据流动态继承所有者；`model_util.create_token` 与预置/服务令牌路径行为不变。非管理员每用户旧型令牌数量上限沿用现状。

### 系列、世代与轮换
- **FR-6**: 每个作用域令牌属于一个令牌系列（series），系列持有稳定名称与默认重叠秒数；首次创建生成世代 1。世代拥有独立密文、世代序号与不可变快照。
- **FR-7**: 轮换生成同一系列的世代 N+1：新密文、新到期时间（缺省继承被轮换世代的到期时间），ACL/组/数据流快照默认与被轮换世代一致；轮换时按所有者当前权限重新校验快照，来源已缺失则拒绝轮换。
- **FR-8**: 轮换请求接受 `overlap_seconds`（0–3600，缺省取系列默认值 60）。轮换后：旧世代 `retire_at = now + overlap_seconds`；重叠期内旧世代仍可建立新请求，期后立即不能；无论重叠期与否，已建立的连接/会话不被强制中断，按其认证时状态自然结束。
- **FR-9**: 轮换在数据库事务内以系列行级锁串行化；并发轮换必然产生连续、无空洞的世代序号，每次成功轮换恰好产生一个新世代；轮换与撤销并发时以提交的持久状态为准。

### 撤销、到期与失权
- **FR-10**: 可显式撤销单个世代，也可撤销整个系列（所有世代，含最新世代）。撤销即时持久化，之后该世代不能建立新请求/新连接，不影响已建立连接。
- **FR-11**: 到期（`now >= expires_at`）或重叠期结束（`now >= retire_at`）的世代不能建立新请求/新连接。
- **FR-12**: 所有者被停用（用户不存在或 `is_active()` 为假，含 `expiration_date` 到期）时，其名下所有世代不能建立新请求。
- **FR-13**: 快照中任一权限来源消失（所有者失去某 ACL、被移出某组或某数据流）时，该世代不能建立新请求（整体拒绝，而非收窄）。
- **FR-14**: 鉴权判定每次请求/连接建立时读取数据库持久状态，不做跨请求缓存；服务重启后以库中状态为准。

### HTTP 与实时连接
- **FR-15**: HTTP 鉴权（`auth_or_token`）对作用域令牌执行 FR-10–FR-13 的全部生命周期与来源校验，并以快照作为后续所有数据访问控制（读/写/关联行校验）的主体；校验失败返回 401（凭证无效/到期/撤销/停用）或 403（权限不足/来源缺失）。
- **FR-16**: 实时连接支持以 API 令牌换取建立连接所需的短期 JWT：换取时执行与 HTTP 相同的生命周期与来源校验，通过后签发仅用于立即建连的短有效期 JWT；令牌已失效则无法换取、无法建立新连接；已建立的 websocket 不因轮换/撤销被主动关闭。
- **FR-17**: 组/数据流成员关系 SQL 策略对作用域令牌按快照过滤：能且只能看到快照组/数据流内的数据（含 Obj/Source、Photometry、Candidate 等经 group_users/stream_users 链路的模型）；旧型令牌维持按所有者成员关系解析。

### 列表与展示
- **FR-18**: 令牌列表（含 profile 聚合）对每个世代返回：名称、ACL、组（id+名称）、数据流（id+名称）、创建时间、到期时间、系列 id、世代序号、旧世代 `retire_at`、撤销时间、派生可用状态（active/overlapping/expired/revoked/superseded/owner_disabled/source_removed）；不返回完整密文，仅返回短指纹用于辨识。
- **FR-19**: 密文只在创建与轮换成功响应中出现一次；旧型令牌也不再在列表中暴露密文。
- **FR-20**: 前端提供：创建表单（名称、ACL 勾选、组/数据流多选、到期时间、重叠期默认值）、列表（范围/到期/系列世代/状态、密文不展示）、轮换与撤销操作、创建/轮换后一次性密文展示与复制。

## Non-Functional Requirements
- **NFR-1（并发正确）**: 轮换/撤销在默认事务隔离与行级锁下收敛；并发调用后最终世代链、retire_at、revoked_at 与“可建立新请求”判定一致。
- **NFR-2（兼容）**: 既有令牌、预置管理员令牌、助手服务令牌（`SERVICE_TOKEN_PREFIX`）行为与展示过滤规则不变；既有令牌相关测试全部通过。
- **NFR-3（可演进）**: 迁移可前滚/回滚，旧数据无需回填即获得兼容语义；不引入对 baselayer 既有非令牌行为的改变。
- **NFR-4（性能）**: 鉴权新增的快照/来源校验在既有每请求查库路径上完成，通过索引与预加载避免 N+1；不引入跨请求缓存。
- **NFR-5（可观测）**: 拒绝原因（到期/撤销/重叠结束/停用/来源缺失）在日志中可区分，响应不泄露内部结构。

## Constraints
- **Technical**: Python/Tornado + SQLAlchemy + Alembic/PostgreSQL；baselayer 以子模块固定版本交付，对其 `access.py`/`models.py` 的改动须保持通用且不破坏上游既有语义；前端 React + MUI + RTK Query；接口模型走 `projects/api-models` 的 pydantic 模型。
- **Business**: 密文一旦丢失不可恢复（只能轮换/重建）；用户仅能管理自己的令牌，管理员可代创建（沿用现有 `user_id` 委托规则）。
- **Dependencies**: baselayer 子模块已检出（`367c508`）；测试依赖 Docker 提供的 PostgreSQL/Redis（本机 docker 可用）。

## Assumptions
- “实时连接”指 baselayer websocket（经短期 JWT 建立）；MCP 等走 HTTP 流式响应的接口已由 `auth_or_token` 覆盖。
- “权限来源被移除”按已确认决策解释为整体拒绝该世代新请求；已建立连接不追溯中断。
- 到期时间为 UTC 感知时间；重叠期以应用服务器数据库时钟为准。
- 组删除/数据流删除会经由关联表级联与来源校验自然表现为“来源移除”。

## Acceptance Criteria

### AC-1: 创建时超范围被拒、合规创建成功
- **Type**: `rule`
- **Given**: 已知所有者的 ACL/组/数据流集合
- **When**: 调用创建接口，分别提交①超出的 ACL、②不在内的组、③不在内的数据流、④“System admin”ACL、⑤空组列表、⑥缺得到期时间；以及提交全部合规的参数
- **Then**: 前六类请求返回 400 且库中无新增系列/世代/关联行；合规请求返回 200 与一次性密文，世代快照与请求一致
- **Pass Condition**: 六类拒绝均返回 400 且数据库无残留；合规请求 200 且 group_tokens/stream_tokens/token_acls 行精确等于请求集合
- **Evidence**: 新增 API 测试（`skyportal/tests/api_tests/.../test_token_scopes.py`）通过；数据库断言输出

### AC-2: 授权快照不随后续授权扩张
- **Type**: `rule`
- **Given**: 以所有者仅含组 A、数据流 S 的快照创建作用域令牌
- **When**: 事后将所有者加入组 B、授予数据流 T 与新 ACL，再用该令牌访问仅 B/T 可见的数据与接口
- **Then**: 令牌仍只能访问 A/S 与快照 ACL 范围；B/T 数据不可见、相关写操作 403
- **Pass Condition**: 令牌对 B 组源数据查询为空/403、对 T 数据流 photometry 不可见；同一所有者浏览器会话可见 B/T（对照）
- **Evidence**: API 测试断言源/photometry 可见性差异

### AC-3: 轮换产生同系列新世代并按重叠期收敛
- **Type**: `rule`
- **Given**: 一个 active 作用域令牌（世代 1）
- **When**: 以 overlap_seconds=2 轮换，随后立即用旧、新密文分别请求；等待重叠期结束后再次用旧密文请求
- **Then**: 轮换返回新密文且二者系列 id 相同、世代序号 1→2；重叠期内新（200）旧（200）均可建立新请求；期后旧 401、新 200；旧世代 `retire_at` ≈ 轮换时刻+2s
- **Pass Condition**: 上述时间窗内状态码与列表状态（overlapping→superseded）全部符合
- **Evidence**: API 测试（含可控等待/直接置位 retire_at）通过

### AC-4: 到期、撤销、停用、来源移除拒绝新请求
- **Type**: `rule`
- **Given**: 同一作用域令牌的不同样本/世代
- **When**: 分别触发 expires_at 到期、世代撤销、整系列撤销、所有者停用（expiration_date 过去时间）、所有者被移出快照组
- **Then**: 触发后该世代新 HTTP 请求被拒（401/403），实时连接 JWT 换取被拒；已建立连接不被主动关闭
- **Pass Condition**: 五种场景新请求全部拒绝，拒绝前建立的 websocket 仍保持（实现层面以“换取 JWT 只在建连前校验、建连后不复查”为证据）
- **Evidence**: API 测试五种场景通过；socket_auth 令牌路径测试通过

### AC-5: 并发轮换/撤销/重启收敛到持久最新状态
- **Type**: `rule`
- **Given**: 单个系列，多个客户端同时轮换、穿插撤销
- **When**: 并发执行 K 次轮换与撤销，结束后用新会话（模拟重启，无任何进程内缓存）读取系列全部世代并尝试各密文
- **Then**: 世代序号连续无重号；每次轮换恰好一个新世代；可建立新请求的密文恰为持久状态允许的那一个（或撤销后为零个）；retire_at/revoked_at 一致
- **Pass Condition**: 并发测试 K=10 通过；世代集合与可用性判定在全新会话中复现一致
- **Evidence**: 并发 API/模型测试输出 + 数据库终态断言

### AC-6: 列表不泄露密文且展示完整元数据与状态
- **Type**: `rule`
- **When**: 获取令牌列表与 profile
- **Then**: 任何列表响应不含完整密文（创建/轮换响应除外）；含 ACL/组/数据流/到期/series_id/generation/retire_at/revoked_at/状态字段；短指纹不等于密文
- **Pass Condition**: 测试断言响应中不存在任一已知完整 token id；各元数据字段存在且状态派生正确（覆盖 active/overlapping/expired/revoked/superseded/source_removed/owner_disabled）
- **Evidence**: API 测试通过

### AC-7: 旧型令牌兼容语义不变
- **Type**: `rule`
- **Given**: 迁移前已存在的令牌（series_id 为空）以及 `create_token`/预置/服务令牌
- **When**: 迁移后运行既有令牌测试，并动态改变所有者组成员资格
- **Then**: 旧令牌继续动态继承所有者当前组/数据流；数量上限、服务前缀隐藏、增删改接口行为不变
- **Evidence**: 既有 `test_token.py`、profile 等相关测试全部通过

### AC-8: 实时连接令牌换取与建连语义
- **Type**: `rule`
- **When**: 分别用有效作用域令牌、重叠期结束旧世代、已撤销令牌调用 socket JWT 换取接口
- **Then**: 有效令牌获得短有效期 JWT（token 路径 TTL ≤ 60 秒，载荷可用）；失效令牌换取失败；换取不改变 websocket 建立后的既有行为
- **Pass Condition**: 接口测试断言 JWT 可被同一 secret 解码且 exp 符合上限、失败场景 401
- **Evidence**: API 测试通过

### AC-9: 数据面成员关系策略全面令牌感知
- **Type**: `rule`
- **Given**: 跨组/数据流共享的 Obj/Photometry/Candidate 等数据
- **When**: 以仅含部分组/数据流快照的令牌经各成员关系策略路径（groups.users、group.users、streams.users、group_users.user 等链路）查询
- **Then**: 各路径返回集合 = 快照范围；不依赖遍历写法而遗漏直连链路（如 followup/observation/candidate）
- **Pass Condition**: 覆盖各类策略链路的参数化测试全部按快照收敛
- **Evidence**: 模型/API 测试通过

### AC-10: 前端管理体验
- **Type**: `rubric`
- **Dimension**: 可用性与信息完整性
- **Scale**: 1-5
- **Anchors**: 1 = 无法创建作用域令牌或列表缺关键信息；3 = 功能齐备但范围选择/状态辨识不清；5 = 创建（名称/ACL/组/数据流/到期）、一次性密文、轮换、撤销、状态与系列展示清晰可用
- **Pass Threshold**: >= 4
- **Evidence**: 前端 Playwright 测试或人工走查记录（截图/步骤）

### AC-11: 工程质量与一致性
- **Type**: `rubric`
- **Dimension**: 最小改动与代码风格一致性（沿用现有 handler/model/migration/测试模式，注释简洁，diff 聚焦）
- **Scale**: 1-5
- **Anchors**: 1 = 大改无关模块或风格割裂；3 = 可用但有冗余抽象；5 = 改动紧贴现有模式、无多余抽象与无关重构
- **Pass Threshold**: >= 4
- **Evidence**: lint/typecheck 结果、代码评审

## Open Questions
- 无（四项关键语义已经用户确认；其余按 Assumptions 执行）。
