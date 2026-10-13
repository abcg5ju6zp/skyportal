# 作用域 API 令牌世代与轮换 - 实现计划

## Task 1: 数据模型与 Alembic 迁移
- **Status**: `completed`
- **Completion Evidence**:
  - `baselayer/app/models.py`：新增 `TokenSeries`（显式 `__tablename__="token_series"`，修正默认表名 `tokenseriess` 问题），Token 增加 series_id/generation/expires_at/revoked_at/retire_at 列与 series 关系，User 增加 token_series 关系。
  - `skyportal/models/user_token.py`：新增 group_tokens/stream_tokens composite_pk 关联模型与 `Token.scoped_groups/scoped_streams`（viewonly，关联行由服务层显式写入）；`Token.is_scoped`，groups/streams 属性按快照分支。
  - 迁移 `c3f9a2e7b411`（down_revision=28442e0a1830）；在 55433 端口 PG 实例的 create_all 快照库上完成 stamp→downgrade -1（表与 5 列全部移除，version=28442e0a1830）→upgrade head（表、FK `tokens_series_id_fkey`、复合主键、reverse 索引、默认值 60/1 均符合，旧行 generation 默认 1、series_id 空）。
  - 注：全量 from-zero 升级在历史迁移 d0195eb7ecd0 即失败（既有问题，与本次改动无关）；CI 亦从近期参考提交起测。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新增 `TokenSeries` 模型（id UUID 主键、name、created_by_id FK→users、overlap_seconds 默认 60、created_at；(created_by_id,name) 唯一约束；tokens 关系按 generation 排序）。
  - baselayer `Token` 扩展列（在 skyportal 层以“猴子补丁”加列不可行，需在 baselayer `Token` 上新增；保持可空/默认以兼容上游）：`series_id` FK→token_series（可空）、`generation` Integer NOT NULL 默认 1、`expires_at` DateTime 可空、`revoked_at` DateTime 可空、`retire_at` DateTime 可空。
  - 新增关联表模型 `GroupToken`（group_tokens，token_id/group_id 复合主键、双外键 CASCADE）、`StreamToken`（stream_tokens，同构），并在 skyportal `Token`/`Group`/`Stream` 上挂接 scoped 关系（`Token.scoped_groups`、`Token.scoped_streams`）。
  - Alembic 迁移（down_revision=`28442e0a1830`）：建 token_series、tokens 加 5 列（含 series_id 索引）、建 group_tokens/stream_tokens；downgrade 完整回滚。旧行不回填：series_id 为空即旧型。
  - `skyportal/models/__init__.py` 导出新模型；遵循现有 `join_model`/表命名风格。
- **Acceptance Criteria Addressed**: AC-1, AC-7
- **Test Requirements**:
  - `rule` TR-1.1: 对干净/已有库执行 `alembic upgrade head` 成功，再 downgrade 一版无错误（以 `make db_migrate`/测试迁移流程或本地 alembic 命令输出为证据）。
  - `rule` TR-1.2: 迁移后旧令牌行 series_id 为空、generation=1、expires_at/revoked_at/retire_at 为空；新关联表存在且外键级联生效（删除令牌/组后关联行消失），以迁移测试或 SQL 断言为证据。
- **Notes**: baselayer 是固定子模块；新增列须全部可空或带默认值，不得破坏 baselayer 既有测试与其他 Token 构造路径。

## Task 2: 世代快照与生命周期核心逻辑
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 `skyportal/models/token_scopes.py`：validate_scopes（ACL/组/数据流子集、组非空、拒绝 System admin）、snapshot_is_current、token_generation_status（active/overlapping/expired/revoked/superseded/owner_disabled/source_removed，边界用 `>=`）、create_scoped_token/rotate_token/revoke_generation/revoke_series；轮换/撤销先 `SELECT ... FOR UPDATE` 锁系列行，旧世代 retire_at=now+clamp(overlap,0,3600)，快照复制 group_tokens/stream_tokens/token_acls。
  - 临时库烟测（/tmp/smoke_tokens.py）：超范围组/过去到期被拒；10 线程并发轮换 0 错误，世代 2–11 连续唯一、每次恰一世代；最新 active、旧世代 superseded 且不可建新请求；整系列撤销后全部 revoked。
  - 迁移补充：升级时删除 tokens 旧 (created_by_id,name) 唯一约束（同系列世代同名），系列级唯一约束在 token_series；downgrade 恢复旧约束，已在 PG 往返验证。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 在 skyportal 模型/服务层（建议新建 `skyportal/models/token_series.py` 或并入 user_token.py 的小型服务模块）实现：
    - `Token.is_scoped`（series_id 非空）。
    - 快照来源集合访问器（ACL ids / group ids / stream ids；旧型回退现有动态语义）。
    - `validate_scopes(owner, acls, group_ids, stream_ids)`：子集校验、组非空、拒绝 “System admin”，返回结构化错误。
    - `token_generation_status(token, owner)`：派生 active/overlapping/expired/revoked/superseded/owner_disabled/source_removed（优先级与 FR 一致），以及 `can_establish_new_requests(now)`。
    - `create_scoped_token(...)`、`rotate_token(...)`、`revoke_generation(...)`、`revoke_series(...)`：全部在单事务内完成；轮换/撤销先 `SELECT ... FOR UPDATE` 锁定 series 行（PostgreSQL 行锁；测试库为 PG），轮换复制旧世代快照与 acls，设置旧世代 `retire_at=now+clamp(overlap,0,3600)`，新世代重置 retire_at/revoked_at，返回新密文。
    - 轮换前按所有者当前权限重新校验快照；缺失即失败回滚。
- **Acceptance Criteria Addressed**: AC-3, AC-4, AC-5
- **Test Requirements**:
  - `rule` TR-2.1: 状态派生单测覆盖七种状态与时间边界（now==expires_at/retire_at 判定为失效）。
  - `rule` TR-2.2: 并发测试（线程池 K=10 对同一系列轮换、穿插撤销）：世代序号 2..K+1 连续唯一，每次轮换恰好一世代，终态可建连密文集合与规则一致；全新 session 复现判定一致。
  - `rule` TR-2.3: overlap 被钳制到 [0,3600]；缺省取 series.overlap_seconds；来源缺失时轮换抛错且无新世代落库。
- **Notes**: 并发测试可直接调用同步服务函数 + `DBSession`（conftest 已有 PG 环境），不依赖 HTTP。

## Task 3: HTTP 鉴权边界接入生命周期与快照
- **Status**: `completed`
- **Completion Evidence**:
  - `baselayer/app/access.py`：令牌查询预加载 acls/scoped_groups/scoped_streams 及 owner 全链；`_authorize_token` 调用应用层 `validate_generation` 钩子（baselayer 不反向依赖 skyportal，缺省即旧行为）。
  - skyportal 挂载 Token.validate_generation：active/overlapping 放行；source_removed→403，其余失效→401，旧令牌直通。`base.py` 异步会话对 scoped 令牌显式载入快照，杜绝异步 lazy IO。
  - 端到端验证：到期/撤销 401、停用/来源移除 403、重叠窗行为经 HTTP 实测（Task 10 测试 14/14）。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - `baselayer/app/access.py`：令牌查询预加载 created_by（acls/roles/groups/streams）与令牌 acls/scoped_groups/scoped_streams；`_authorize_token` 增加：世代生命周期校验（revoked/expires/retire）、所有者 active、作用域令牌的快照⊆所有者当前权限校验；失败分别 401/403。
  - `skyportal/models/user_token.py`：`Token.groups`/`accessible_groups`/`accessible_group_ids`/`accessible_streams` 对 scoped 令牌返回快照集合（不触发 System admin 全量旁路）；旧型保持 `created_by.*` 现状；scoped 令牌 `is_admin` 不为真（System admin ACL 创建时已拒）。
  - 保证每请求查库、无跨请求缓存；日志区分拒绝原因。
- **Acceptance Criteria Addressed**: AC-4, AC-7, AC-11
- **Test Requirements**:
  - `rule` TR-3.1: API 测试：到期/撤销/retire/所有者过期/来源缺失各场景经真实 HTTP 请求返回 401 或 403；未触发时 200。
  - `rule` TR-3.2: 旧型令牌在所有者新增/移除组后访问范围随之变化（兼容对照测试）。
  - `rule` TR-3.3: 鉴权查询无 N+1（以查询计数或 SQL 日志审查为证据，评审时核对）。
- **Notes**: baselayer 改动需最小且通用：生命周期方法调用 skyportal 挂载在 Token 上的钩子（缺失时回退原逻辑），避免 baselayer 反向依赖 skyportal。

## Task 4: 成员关系 SQL 策略的令牌快照感知
- **Status**: `completed`
- **Completion Evidence**:
  - baselayer `AccessibleIfUserMatches`：链经过 groups/streams 表时对 scoped Token 调用应用层 `membership_scope_condition`（关联子查询 group_tokens/stream_tokens，按 token_id 绑定，无需会话内加载），非成员链（author/requester）不变。
  - 组管理员策略、GroupUser 更新、组删除、Stream 读取/删除自定义策略对 scoped 令牌拒绝或走 stream_tokens 分支。
  - `test_access_control_sql.py` 197/197（无笛卡尔积）；test_groups/test_streams 46/46；test_sources/test_filters/test_obj_photometry/test_mcp 155/155（含 Photometry groups|streams、Candidate filter.group.group_users.user 等链）；SQL 编译烟测确认 Photometry/Source/Candidate 注入快照、旧令牌无 group_tokens。
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - baselayer `AccessibleIfUserMatches.select_accessible_rows` 增加通用钩子：当访问主体是 scoped Token 且关系链途经 Group/Stream 实体时，在既有 owner 成员关系过滤上追加 “该 Group/Stream id ∈ 令牌快照” 条件；不途经 Group/Stream 的链（author/requester/created_by 等）行为不变；非 scoped 主体零影响。
  - skyportal `AccessibleIfGroupUserIsAdminAndUserMatches`：scoped Token 直接返回不匹配（令牌不具组管理员语义）。
  - 复核/修正直接使用 `AccessibleIfUserMatches("....group.users"/"streams.users"/"group_users.user")` 的模型（followup_request、observation_plan、catalog、candidate、filter、photometry 等）确认经通用钩子覆盖；`stream_read_access_logic` 等自定义函数为 scoped 令牌增加 stream_tokens 分支（auto_join 仍可见）；`groupuser_update_access_logic` 对 scoped 令牌拒绝。
  - VerifiedSession 写校验路径（共享到组/数据流）同样按快照收敛。
- **Acceptance Criteria Addressed**: AC-2, AC-9
- **Test Requirements**:
  - `rule` TR-4.1: 参数化测试覆盖链路 groups.users、group.users、streams.users、stream.users、filter.group.group_users.user、allocation.group.users：scoped 令牌可见集合恰为快照交集；旧型令牌与 User 结果与改动前一致。
  - `rule` TR-4.2: scoped 令牌向快照外组共享/写入数据被提交时校验拒绝（VerifiedSession/策略层）。
  - `rule` TR-4.3: 以 scoped 令牌访问组管理类策略（Group.update/GroupUser admin）返回不可达/403。

## Task 5: 创建作用域令牌 API 与请求模型
- **Status**: `completed`
- **Completion Evidence**:
  - `projects/api-models/skyportal_py_models/tokens.py`：TokenPostBody 增 group_ids/stream_ids/expires_at/overlap_seconds；新增 TokenSecretResponse/TokenRotateBody/ApiTokenResponse（指纹+元数据）。
  - POST 按是否带 scope 分流：scoped→create_scoped_token（子集/必填/未来到期校验，400 + 不落库）；旧型路径保留服务前缀、重名、非管理员 1 枚上限（仅统计 series_id IS NULL）。
  - HTTP 实测：6 类非法请求均 400（System admin ACL、超范围 ACL/组/流、空组、缺到期），合法创建 200 且密文仅返回一次。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 扩展 `projects/api-models/skyportal_py_models/tokens.py`：`TokenPostBody` 增加 group_ids、stream_ids、expires_at、overlap_seconds（均可选，承载旧型兼容路径）；新增创建/轮换响应模型（密文 + 元数据）；更新 `ApiTokenResponse` 元数据字段（不含量完整密文）。
  - `handlers/api/internal/token.py::post`：识别作用域创建参数（group_ids/expires_at 任一出现即按作用域令牌处理，要求组非空、到期时间合法）；做 ACL/组/数据流子集与 System admin 校验；调用 `create_scoped_token`；成功响应一次性返回密文与元数据。旧型路径（含非管理员 1 枚上限、服务前缀保留、重名检查）维持现状，且上限仅统计旧型令牌。
- **Acceptance Criteria Addressed**: AC-1, AC-7
- **Test Requirements**:
  - `rule` TR-5.1: API 测试覆盖 spec AC-1 的六类 400 与合规 200；400 时系列/世代/关联表无残留。
  - `rule` TR-5.2: 旧型创建路径既有行为不变（`test_multiple_tokens`、服务前缀测试保持通过）。

## Task 6: 轮换与撤销 API
- **Status**: `completed`
- **Completion Evidence**:
  - 路由：POST /api/internal/tokens/&lt;id&gt;/rotate|revoke、POST /api/internal/token-series/&lt;sid&gt;/rotate|revoke、POST/DELETE /api/internal/token-series/&lt;sid&gt;/generations/&lt;n&gt;、PUT/DELETE /api/internal/legacy-tokens/&lt;fingerprint&gt;（前端无需密文即可管理）。
  - HTTP 实测：overlap=2 时新旧世代同活、窗后旧 401 新 200、列表 active/superseded 正确；整系列撤销两代均 401；10 并发 HTTP 轮换世代 2–11 连续且仅最新可建连；非所有者/旧型轮换 400。
- **Priority**: high
- **Depends On**: Task 5
- **Description**:
  - 新增路由（`app_server.py` 内部路由区）：`POST /api/internal/tokens/<id>/rotate`、`POST /api/internal/tokens/<id>/revoke`、`POST /api/internal/token-series/<series_id>/revoke`；权限：系列所有者或系统管理员（沿用 Token 可读/可更新策略）。
  - 处理器调用 Task 2 的事务函数；rotate 接受 overlap_seconds/expires_at；成功一次性返回新密文；撤销返回成功并推送 profile 刷新。
  - 对旧型令牌调用轮换/撤销系列：轮换返回 400（旧型无系列）；撤销单世代复用既有 delete 或提供 revoke（旧型 revoke 等价于即时失效，保持最小实现：仅 scoped 支持）。
- **Acceptance Criteria Addressed**: AC-3, AC-4, AC-5
- **Test Requirements**:
  - `rule` TR-6.1: API 轮换测试：同系列、世代递增、重叠窗内旧新均 200、窗后旧 401（测试可直接写短 retire_at 或等待 2s）。
  - `rule` TR-6.2: API 撤销（世代/系列）测试：撤销后新请求 401；系列撤销使全部世代失效。
  - `rule` TR-6.3: 并发轮换 HTTP 场景（asyncio.gather/线程）最终状态与 TR-2.2 一致；非所有者 403/400。

## Task 7: 实时连接的 API 令牌鉴权
- **Status**: `completed`
- **Completion Evidence**:
  - `baselayer/app/handlers/socket_auth.py`：支持 Authorization: token 头，复用与 HTTP 相同的令牌加载与生命周期校验；令牌路径 JWT TTL=60s（仅用于建连），Cookie 会话路径保持 15 分钟。
  - HTTP 实测：有效 scoped 令牌换取 JWT 200 且 user_id 正确、exp 距签发 ≤65s；撤销后换取 401；Cookie 路径行为不变。
  - websocket 建连后不复查（既有行为），服务端不主动断连；已建立连接按认证时状态自然结束。
- **Priority**: medium
- **Depends On**: Task 3
- **Description**:
  - 扩展 baselayer `handlers/socket_auth.py`：除 cookie 会话外接受 `Authorization: token`；复用 access.py 的令牌加载与生命周期/快照校验；通过后签发 JWT，token 路径 exp ≤ 60 秒，载荷含 user_id（消息订阅主体不变）；cookie 路径维持 15 分钟。
  - websocket_server 不改动：建立后不复查（既有连接不因轮换/撤销中断，新连接须重新换取 JWT）。
- **Acceptance Criteria Addressed**: AC-8, AC-4
- **Test Requirements**:
  - `rule` TR-7.1: 有效 scoped 令牌换取 JWT：200、同一 secret 可解码、exp−iat ≤ 60、user_id 为所有者。
  - `rule` TR-7.2: 到期/撤销/超重叠期/来源缺失令牌换取 JWT 返回 401；cookie 路径行为不变（既有前端测试通过）。

## Task 8: 列表与 Profile 序列化（不泄密文）
- **Status**: `completed`
- **Completion Evidence**:
  - `generation_metadata()` 统一输出 fingerprint（id 前 8 位）、acls、groups{id,name}、streams、series_id、generation、expires_at/retire_at/revoked_at、status；GET /internal/tokens（单/多）与 GET /internal/profile 均改用它；profile 增补 groups 供前端选择。
  - HTTP 实测：profile/list 不含完整密文（JSON 全文断言 secret 不出现），指纹与状态字段齐全；旧型令牌列表同样无密文；既有 test_token.py 已按新契约更新并通过。
  - scoped 令牌 PUT 快照不可变 → 400（旧型 PUT 经 fingerprint 端点保持可用）。
- **Priority**: medium
- **Depends On**: Task 5
- **Description**:
  - `handlers/api/internal/token.py::get` 与 `handlers/api/internal/profile.py::get`：世代输出改为元数据视图——短指纹（如 id 前 8 位）、name、acls、groups(id+name)、streams(id+name)、created_at、expires_at、series_id、generation、retire_at、revoked_at、status；任何列表不再输出完整 id（含旧型令牌）；服务令牌仍对 profile 隐藏。
  - 删除/更新接口沿用；UpdateTokenACLs 仅对旧型保持可用，scoped 令牌快照不可变（冲突时 400 引导轮换/重建）。
- **Acceptance Criteria Addressed**: AC-6
- **Test Requirements**:
  - `rule` TR-8.1: 测试断言列表/profile 中不存在已知完整 token id 字符串；字段齐全；七种 status 派生与数据一致。
  - `rule` TR-8.2: 对 scoped 令牌 PUT 改 ACL 返回 400；旧型 PUT 行为不变。

## Task 9: 前端管理界面
- **Status**: `completed`
- **Completion Evidence**:
  - `static/js/components/user/NewTokenForm.tsx`：名称/到期（UTC datetime-local，默认 +180 天）/重叠秒数（0–3600，默认 60）/组必选多选/数据流可选/ACL 勾选；提交 group_ids/stream_ids/expires_at/overlap_seconds；成功后密文仅弹一次（testid `new-token-secret`，可复制，关闭不可复现）。
  - `TokenList.tsx`：Fingerprint 列（id 前 8 位，无密文）、ACL/Groups/Streams chips、到期/overlap ends、系列指纹+`gen N`、七态状态 Chip（active 实心/其余描边、配色区分）；行内操作：仅最新 active 世代显示 Rotate（对话框可改重叠秒数，`confirm-rotate`，一次性展示 `rotated-token-secret`）、世代 Revoke、整系列 Revoke（已撤销时）、Delete（世代或旧型指纹端点）；getRowId 用 series+generation 防重键。
  - `ducks/profile.ts`：createToken 入参扩展；新增 rotate/revoke（世代与系列）/deleteGeneration/legacy 指纹管理 mutations，全部失效后 invalidate Profile；`UserPreferences.tsx` 透传 profile.groups/streams/permissions；profile handler 返回 groups 供选择。
  - Playwright 走查 `skyportal/tests/frontend/users_groups_and_content/test_tokens.py` 6/6 通过：创建（密文仅一次+列表只显指纹）、作用域令牌不受旧型 1 枚上限、轮换后同系列两行（旧 overlapping/新 active）→撤销新世代变 revoked、旧型指纹删除、重名 400 “Duplicate token name”、管理员可建多枚。
  - 环境注记：本机 macOS 27 上 Playwright 1.63 自带的 Firefox 1543 无法启动（任何 profile 路径均报 “Could not find profile folder”，属该旧 Nightly 与新 OS 不兼容）；给测试夹具加了 `PLAYWRIGHT_BROWSER`（默认 firefox，CI 不变）与 `PLAYWRIGHT_BASE_URL` 覆盖，证据用 Chromium + 独立 nginx 反代（5200→app/fakeoauth/websocket）取得；CI 仍按 test_frontend.py 的 firefox 全栈路径运行。
  - eslint/prettier@3.1.0/tsc 对改动文件均无错误。
- **Priority**: medium
- **Depends On**: Task 6, Task 8
- **Description**:
  - `ducks/profile.ts`：新增 rotate/revoke（世代与系列）mutation；create 入参增加 group_ids/stream_ids/expires_at/overlap_seconds。
  - `NewTokenForm.tsx`：增加组/数据流多选（数据来自 profile 的 groups/streams）、到期时间（UTC datetime）、重叠期（可选，默认 60s）；创建成功弹出一次性密文对话框（可复制，关闭后不再可查）。
  - `TokenList.tsx`：移除密文列，改为短指纹；列含 ACL/组/数据流 chips、到期、系列+世代（如 `name #2`）、状态 Chip（配色区分 active/overlapping/expired/revoked/superseded/disabled）；行操作 Rotate（重叠秒数输入）、Revoke（世代）、系列撤销、Delete（仅旧型/已失效，按现有策略）。
  - profile 需返回用户可用 groups/streams 供选择（若无则补最小字段）。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rubric` TR-9.1: 维度=可用性与信息完整性；scale 1-5；anchors 1=无法完成创建/轮换或看不到状态，3=功能齐但选择与状态辨识混乱，5=全流程顺畅且密文仅出现一次；threshold ≥ 4；证据=Playwright 走查步骤或截图（创建→一次性密文→列表状态→轮换→旧世代 overlapping→撤销）。
  - `rule` TR-9.2: `eslint`/`tsc`（项目既有 lint/typecheck 流程）对改动文件无新增错误。

## Task 10: 端到端集成测试与回归验证
- **Status**: `completed`
- **Completion Evidence**（终验会话复跑）:
  - 新增 `skyportal/tests/api_tests/groups_users_analysis/test_token_scopes.py` 14 用例 + 既有 `spectra_misc/test_token.py` 4 用例：**18/18 通过**（含超范围六类拒绝、快照不扩张、overlap=2 窗内双活/窗后旧 401、到期/世代撤销/整系列撤销/owner 停用 401/403、来源移除 403、旧型动态继承对照、快照不可变 PUT 400、socket JWT 换取、系列/世代管理端点、指纹端点、10 并发轮换世代连续）。
  - 前端 Playwright **6/6 通过**（见 Task 9）。
  - 回归（本会话逐套件复跑）：`test_access_control_sql.py`+`test_user.py`+`test_groups.py` **251 通过**；`test_streams.py`+`test_filters.py` **25 通过**；`test_obj_photometry.py` **3 通过**；`test_sources.py`+`test_mcp.py` **136 通过**；无回归。
  - 迁移在含同名世代数据的 scratch 库完成 downgrade→upgrade 往返（修复了 downgrade 恢复旧唯一约束前未清理同名字世代的顺序问题）。
  - ruff 0.15.17、eslint、prettier@3.1.0、tsc 均无错误。
- **Priority**: high
- **Depends On**: Task 4, Task 6, Task 7, Task 8
- **Description**:
  - 新增 `skyportal/tests/api_tests/.../test_token_scopes.py`（归入 spectra_misc 或 groups_users_analysis，沿用目录约定）：快照不扩张（AC-2）、跨组/数据流 Obj/Photometry/Candidate 可见性（AC-9）、生命周期端到端、轮换重叠窗、并发轮换/撤销、来源移除、密文不泄露、旧型兼容、socket JWT 换取。
  - 运行既有令牌/profile/权限相关测试与 `make lint`/typecheck；修复回归。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9
- **Test Requirements**:
  - `rule` TR-10.1: 新测试文件全部通过；既有 `test_token.py`、`test_profile.py`、`test_user.py`、websocket/socket 相关测试无回归（附 pytest 输出）。
  - `rubric` TR-10.2: 维度=覆盖质量；scale 1-5；anchors 1=仅快乐路径，3=主路径+部分边界，5=每个 AC 至少一条可观测证据且含并发与兼容对照；threshold ≥ 4；证据=测试清单与通过输出。

## 独立评审与修复闭环
- **Status**: `completed`
- **评审方式**：独立只读评审，逐文件对照 spec FR/AC 给出 file:line 证据。
- **发现与修复**：
  - M-1（major，越权）：`/api/internal/tokens/<id>/rotate|revoke` 原先只凭密文不校验所有者；已在两个 handler 前加 `Token.select(user_or_token, mode="update")` 所有权检查（持密文者可轮换自身、所有者/管理员可管理、他人 400）。新增 `test_non_owner_cannot_rotate_or_revoke`、`test_token_can_rotate_itself`。
  - M-2（major，越权代建）：重构创建 handler 时丢弃了 `user_id` 目标用户查询结果，非管理员可代他人建牌；已恢复“仅管理员可委托 user_id、且目标用户必须存在”，并恢复以目标用户权限校验 ACL。新增 `test_non_admin_cannot_delegate_token_creation`（作用域+旧型双路径、无残留）。
  - m-1：scoped 所有者停用按 FR-15 返回 401（生命周期钩子前置），旧型令牌保留既有 403 契约；测试与 doc/permissions.md 同步。
  - m-2：`delete_generation_by_ref` 补系列行锁，杜绝删除最新世代与轮换交错时的世代号复用。
  - m-3：列表/profile 元数据中旧型令牌展示其动态继承的 owner groups/streams（此前恒为空）；新增 `test_legacy_metadata_lists_inherited_scope`。
- **回归（修复后复跑）**：令牌 API 套件 22/22；前端 6/6；access SQL+user+groups+streams+filters 276 通过；obj_photometry+sources 59 通过；（修复前 sources+mcp 136 通过）；ruff 0.15.17/eslint/prettier@3.1.0/tsc 干净；迁移在含同名世代数据的 scratch 库 downgrade→upgrade 往返通过。

## Task 11: 权限文档更新
- **Status**: `completed`
- **Completion Evidence**:
  - `doc/permissions.md` 增补“Scoped API token generations”小节：系列/世代/快照/重叠期、七种状态与 401/403 语义、成员策略收窄、旧型兼容、密文仅一次性返回，与最终实现逐项一致。
- **Priority**: low
- **Depends On**: Task 8
- **Description**:
  - 在既有 `doc/permissions.md` 增补小节：作用域令牌、系列/世代、轮换重叠期、失效条件、密文一次性展示与旧型兼容语义；不新建文档文件。
- **Acceptance Criteria Addressed**: AC-11
- **Test Requirements**:
  - `rubric` TR-11.1: 维度=文档准确简洁；scale 1-5；anchors 1=与实现不符或冗长，3=覆盖要点但不精确，5=与最终行为逐项一致且简短；threshold ≥ 4；证据=评审对照实现。
