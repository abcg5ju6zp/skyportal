# 外部星表查询作业可靠性改造 - 产品需求文档

## Overview

- **Summary**: 将 `/api/catalog_queries` 外部星表查询从"请求线程 fire-and-forget、执行器内才建作业、直接写正式表"改造为"接收即落库的稳定作业 + worker 按目录领取执行 + 尝试/分页持久化 + 隔离暂存 + 校验后发布 + 终态一次性通知"的可靠流水线，支持多目录、坐标/半径显式落库、只暂存不发布（read-only）模式、取消与幂等重提，并保持既有单目录、无结果、现有对象匹配等行为兼容。
- **Purpose**: 消除"远端请求已发出而数据库作业尚未建立"导致的孤儿请求；消除重试造成的来源、测光、通知重复写入；使单目录失败不波及其余目录且作业状态如实反映部分成功；使 worker 重启、并发接管、取消、迟到响应、新版查询等场景下数据归属与终态唯一。
- **Target Users**: 通过 GCN 事件页面（及 API）提交外部星表（ZTF-Fink / Swift LSXPS / Gaia / TESS）查询的研究者；运维 worker 服务的管理员。

## Goals

- 接受请求时在同一数据库事务内持久化稳定查询身份、坐标/半径、所选目录集合、操作者权限快照与每条目录子请求；任何远端调用只在事务提交后由 worker 发起。
- worker 逐个领取目录子请求；每次尝试的请求摘要、响应摘要、分页进度、失败原因持久可查；响应不确定时复用原尝试与远端句柄继续核对，不重复提交。
- 解析出的候选来源与测光先写入该查询私有的隔离暂存区；完整校验并解决与现有 Obj 的关联后，才发布为正式可见变更。
- 单目录超时或数据错误不丢弃其他目录的成功结果，也不得把作业标记为全部成功。
- 取消、重复提交、worker 重启、并发接管只作用于尚未确认的目录与发布阶段；迟到响应在取消或新版查询后不得写入；确定的终态只产生一次 `UserNotification` 和一次终态 Flow 推送。
- 保持兼容：旧版单目录 payload（`catalogName` 字符串）、正常单目录查询、无结果返回、现有对象精确 ID 匹配规则、GCN 事件查询列表接口、`/api/catalogs/swift_lsxps` 与 `/api/catalogs/gaia_alerts` 独立端点。

## Non-Goals

- 不改造 `/api/catalogs/swift_lsxps`、`/api/catalogs/gaia_alerts`、`TessTransientsQueryHandler` 三个独立端点的提交与执行方式（其底层抓取函数保留可复用，但不纳入新作业模型）。
- 不改造 broker cone_search 等只读交叉匹配接口。
- 不引入新的基于坐标相似度/天区距离的对象交叉匹配算法；"现有对象关联"沿用现有精确 `Obj.id` 匹配规则。
- 不改造各外部目录本身的查询语义（日期过滤、localization 派生锥形中心、60 角分半径、Fink 选源阈值等保持不变）。
- 前端多目录选择 UI 留待后续；本期前端只做状态展示的最小适配，API 先行支持多目录。

## Background & Context

- 现状入口：[CatalogQueryHandler.post](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L59-L124) 仅校验参数后通过 `IOLoop.current().run_in_executor(None, fetch_transients)` 立即返回；[fetch_transients](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L127-L310) 在执行器线程内才创建 [CatalogQuery](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/models/catalog.py#L22-L76) 行，随后直接调远端并把 Obj、Photometry、PhotometricSeries 写入正式表；异常仅 `log(...)` 吞掉，作业状态可能永久停留在旧值。
- 重复提交无幂等键：每次 POST 都产生新作业、重发远端请求；防重复仅靠各 fetcher 内 `Obj.select(...).first()` 的存在检查，测光以 `duplicates="update"` 覆盖，通知每次终态都发。
- 现有可借鉴模式：[FacilityTransactionRequest](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/models/facility_transaction.py#L65-L144) 的 `status`/`last_query` 持久请求模式；[facility_queue.py](file:///Users/ding/Documents/swe/0910125/project-05/services/facility_queue/facility_queue.py) 与 [observation_plan_queue.py](file:///Users/ding/Documents/swe/0910125/project-05/services/observation_plan_queue/observation_plan_queue.py) 的独立常驻服务、`DBSession` 轮询、`SET LOCAL statement_timeout`、服务内 `Flow()` 推送。
- Alembic 当前单一 head 为 `28442e0a1830`（discussions）。
- 现有对象匹配规则：[post_source](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/source.py#L1067-L1102) 先按 `Obj.id` 精确查找，存在则不新建、不加组；测光随后挂到该对象。外部测光提交入口为 [commit_external_photometry](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/photometry.py#L1796-L1830)（`duplicates="update"`）。
- 前端列表 [CatalogQueryLists.tsx](file:///Users/ding/Documents/swe/0910125/project-05/static/js/components/catalog_query/CatalogQueryLists.tsx#L88-L97) 依赖 `"completed: Added "` 状态前缀渲染来源链接；表单 [CatalogQueryForm.tsx](file:///Users/ding/Documents/swe/0910125/project-05/static/js/components/catalog_query/CatalogQueryForm.tsx#L151-L178) 提交单目录 `catalogName`。

## Functional Requirements

- **FR-1 接收即持久化**：POST 处理函数在一个同步数据库事务内创建 `CatalogQuery`（含稳定 `query_uid`、坐标/半径、`publish_results`、权限快照、去重键、初始状态）与每个所选目录一条 `pending` 子请求，事务提交成功后才返回 200 与作业 id；handler 不再发起任何远端请求、不再使用 `run_in_executor` 执行抓取。
- **FR-2 提交体归一化与兼容**：接受 `payload.catalogNames: list[str]` 或旧版 `payload.catalogName: str`（归一化为单元素列表）；接受可选顶层/ payload 内 `ra`、`dec`、`radius` 与 `publish_results`（默认 `true`）；保留 `localizationDateobs`/`localizationName`/日期范围等现有 payload 字段及其 GCN 本地化权限校验。
- **FR-3 权限快照**：接收时快照化操作者信息（user_id、is_admin、role、可访问组 id 列表、目标组 id 列表、allocation 组 id），存入作业 JSONB 列；worker 全程以请求者身份执行。
- **FR-4 重复提交幂等**：对（requester、allocation、归一化几何与目录集合、日期范围、publish 标志）计算稳定 `request_key`；存在相同 key 的活动作业（非终态）时返回该作业 id，不新建作业/子请求，也不触发任何远端请求。终态后再提交视为新版查询，分配新 `query_uid`。
- **FR-5 worker 领取**：新增独立常驻服务 `services/catalog_query_queue`，轮询数据库，用 `SELECT ... FOR UPDATE SKIP LOCKED` 原子领取 `pending` 或租约过期的子请求；记录 `claimed_by`/`claimed_at`/心跳/租约到期时间；未过期租约不得被其他 worker 抢走。
- **FR-6 尝试与分页持久化**：每次领取创建或续用一条尝试记录，持久化尝试序号、状态（in_flight/succeeded/failed/uncertain）、请求摘要、响应摘要（状态码/体摘要/行数等）、分页进度（如 Fink 已完成 tile、Gaia/TESS 已处理行、Swift 已取详情/光变的 transient 名）、远端句柄、错误原因与时间戳。
- **FR-7 不确定响应续查**：超时、连接中断、5xx 等不确定结果把尝试置为 `uncertain` 并保留远端句柄与分页进度；下一轮复用同一尝试记录与已保存进度核对/续传，不得新建查询、新建子请求或重新提交远端作业；候选按 `(子请求, 外部目录内 id)` upsert，天然不重复。
- **FR-8 适配器化抓取**：Fink / LSXPS / Gaia / TESS 各实现一个可注入替换的目录适配器，封装现有抓取与逐行解析逻辑（过滤条件、列名映射、光变/光谱处理不变），输出统一候选结构（外部 id、ra、dec、原始数据 JSON、测光或测光序列载荷），所有远端 HTTP 调用必须带显式超时。
- **FR-9 隔离暂存**：候选来源与其测光/测光序列只写入该查询私有的暂存表（带 query/subjob 归属与唯一约束）；在进入发布阶段前，正式 `objs`、`photometry`、`photometric_series`（及 Source 组关联等）不得发生任何变化。
- **FR-10 候选校验**：发布前对每个候选做完整校验：来源 ID 字符规则与 `post_source` 一致、ra/dec 合法、测光必需列/量齐全；不合法候选标记 `invalid` 与原因，永不发布且计数可查；单候选非法不影响其余候选与其他目录。
- **FR-11 关联解析与发布**：发布时再次按精确 `Obj.id` 解析关联：已存在则不新建来源、测光挂到现有对象（保持现有不加组行为）；不存在则用快照目标组交集调用现有 `post_source` 建源；随后幂等提交测光（TESS 走 photometric_series 的现有建/更逻辑）；已 `published_at` 的记录跳过，使发布可安全重入。
- **FR-12 权限门控发布**：发布目标组 = 接收时快照目标组 ∩ 请求者当前仍可访问的组；交集为空时发布整体安全中止（无任何部分写入）：作业保持在 `publishing` 活动状态、原因持久化到子请求结果中、不产生终态与通知，后续 worker 周期重试；成员资格恢复后发布成功并一次性终态通知。
- **FR-13 read-only 模式**：`publish_results=false` 的作业完成抓取/校验后即终态，永不调用发布、永不产生正式对象/测光/序列；暂存候选可通过 API 查看。
- **FR-14 部分成功与终态聚合**：所有子请求进入终态后聚合：全部成功并发布完成 → `completed`；有目录失败但至少一个成功（或 read-only 下至少一个成功）→ `completed_partial`；无成功目录或发布失败 → `failed`；无候选结果保持"无新增对象"语义。状态文本与结构化字段同时保留每目录成败、计数与失败原因。
- **FR-15 终态唯一**：终态转换在作业行锁 + `terminal_at IS NULL` 条件下一次性完成；仅首次转换创建一条 `UserNotification`（沿用 `gcnevent_catalog_query` 类型）并推送一次终态 `REFRESH_GCNEVENT_CATALOG_QUERIES` Flow；接收时仍允许一次列表刷新推送。
- **FR-16 取消**：新增 `POST /api/catalog_queries/:id/cancel`（沿用 CatalogQuery update ACL）。设置取消意向；worker 只取消尚未确认（未到 succeeded/failed 终态）的子请求且不执行发布；已确认目录的暂存结果保留；终态为 `cancelled`，通知与 Flow 仅一次；对已终态作业的取消请求返回 409 且无副作用。
- **FR-17 迟到/跨版本写入防护**：任何暂存写入落库前必须校验所属子请求状态为 `claimed`、所属作业未取消且未终态；迟到响应写入被丢弃并记录日志。终态后重新提交生成新 `query_uid` 与新子请求；旧尝试的迟到写入只能归属旧作业并被拒绝，不得影响新作业。
- **FR-18 查询 API**：新增 `GET /api/catalog_queries/:id`（作业详情：子请求状态、尝试摘要、分页、计数、失败原因、已发布对象 id）与 `GET /api/catalog_queries/:id/candidates`（分页列出暂存候选与测光元数据），读权限沿用现有 allocation read ACL；现有按 GCN 事件列出查询的 GET 行为不变（字段为新增）。
- **FR-19 worker 崩溃恢复**：worker 进程在领取后任意时刻退出，子请求保持 `claimed` + 租约；租约过期后由任一存活 worker 接管，uncertain 尝试按 FR-7 续查，已发布内容按 FR-11 跳过，作业最终到达正确终态且无重复发布、无重复通知。
- **FR-20 前端最小适配**：查询列表优先展示结构化的每目录状态/计数，保留对旧 `"completed: Added ..."` 文本的回退渲染；表单增加 read-only（只暂存不发布）勾选；单目录提交流程保持可用。

## Non-Functional Requirements

- **NFR-1 可配置性**：轮询间隔、每周期领取上限、租约时长、心跳间隔、最大尝试次数、远端请求超时通过 `config.yaml.defaults` 中 `app.catalog_query_queue` 配置段提供默认值，可被部署配置覆盖。
- **NFR-2 数据库安全**：worker 每个事务设置 `SET LOCAL statement_timeout`（仿 observation_plan_queue）；领取与终态聚合使用短事务、行锁，避免长事务跨远端 IO。
- **NFR-3 可测试性**：worker 单步处理逻辑（领取→执行→聚合）以可直接调用的函数暴露，适配器注册表可被 monkeypatch 替换为假适配器；测试不依赖任何外部目录服务、不发起真实网络请求。
- **NFR-4 可观测性**：沿用 `make_log("...")` 风格记录领取、续查、发布、取消、终态；失败原因在子请求行与尝试行均可查询，不允许只落日志。
- **NFR-5 迁移安全**：新 Alembic 迁移基于 head `28442e0a1830`；upgrade/downgrade 均可执行；对 `catalog_queries` 的变更全部为新增可空列或带默认值列，不锁写现有行；新表带必要索引（含活动作业 `request_key` 部分唯一索引、租约到期索引）。

## Constraints

- **Technical**: Python + SQLAlchemy（同步 `DBSession`/`new_session`，发布测光沿用现有 asyncio 桥接）、PostgreSQL（SKIP LOCKED、JSONB、部分唯一索引）、Tornado handler、baselayer ACL/Flow/模型基类；迁移走 Alembic；服务脚本经 `init_db(**cfg["database"])` 独立启动并有 supervisor.conf。
- **Business**: 不得破坏线上既有 payload 与前端渲染；外部目录（Fink、Swift LSXPS、Gaia、TESS）选源与过滤语义保持不变；权限模型不得放宽（allocation read 可创建，update 规则可取消，目标组发布仍受请求者组成员资格约束）。
- **Dependencies**: `requests`、`swifttools.ukssdc.query`、`astropy`、`pandas` 等现有依赖；不引入新第三方依赖。

## Assumptions

- worker 服务可由部署方按现有 supervisor 模式启动多个副本；`claimed_by` 以 `hostname:pid:uuid` 标识，仅用于排障。
- read-only 作业的暂存数据保留期与其他作业一致（跟随 CatalogQuery 生命周期，删除作业级联清理），本期不做单独保留期/清理任务。
- 无 GCN localization 上下文的纯坐标查询，通知 `url` 允许为空；有 `localizationDateobs` 时沿用 `/gcn_events/{dateobs}`。
- 终态失败作业（含发布权限失败）不自动无限重试；worker 负责重试未确认子请求（uncertain/租约过期），是否重新提交由用户在前端/API 另行决定。
- 暂存测光以 JSONB 载荷保存（列数与行数随目录而异），发布时走与线上完全相同的标准化与校验入口，避免两套测光规则漂移。

## Acceptance Criteria

### AC-1: 接收即持久化，进程退出不产生孤儿请求
- **Type**: `rule`
- **Given**: 具备 allocation read 权限的研究者提交包含 2 个目录的查询
- **When**: POST 返回 200 后、worker 尚未执行前，模拟 web/worker 进程退出并随后启动 worker
- **Then**: 数据库中作业（含 `query_uid`、坐标/半径、目录集合、权限快照、`publish_results`）与 2 条 `pending` 子请求在返回前已提交；worker 启动后两条子请求均被正常执行到终态
- **Pass Condition**: POST 成功返回后无需任何进程存活即可在库中查到完整作业与子请求；重启 worker 后作业终态正确
- **Evidence**: API 测试：POST 后立即断言行存在；直接调用 worker tick 函数完成处理并断言终态

### AC-2: 活动作业重复提交幂等
- **Type**: `rule`
- **Given**: 同一请求者用相同 allocation、几何/日期/目录集合、publish 标志提交过一次且作业处于活动状态
- **When**: 在终态前以相同归一化参数再次 POST（含旧版 `catalogName` 字符串与新版 `catalogNames` 列表两种等价写法）
- **Then**: 两次返回同一个作业 id；子请求数量不增加；假适配器断言零次额外远端调用
- **Pass Condition**: 归一化等价请求在活动期共享同一作业；终态后再提交得到新 `query_uid`
- **Evidence**: API 测试断言作业 id 相同、子请求计数相同、适配器调用计数

### AC-3: worker 领取互斥与租约接管
- **Type**: `rule`
- **Given**: 多条 pending 子请求与两个 worker 标识
- **When**: 并发执行领取；随后构造一个 `claimed` 但租约未过期、一个租约已过期的子请求再次领取
- **Then**: 同一子请求只被一个 worker 领取（SKIP LOCKED）；未过期租约不被接管；过期租约被另一 worker 接管且 `claimed_by` 更新、接管事实在尝试历史中可查
- **Pass Condition**: 无重复领取；过期可接管、未过期不可抢
- **Evidence**: 作业核心模块单元测试：领取计数、租约边界断言

### AC-4: 尝试/响应/分页持久化与 uncertain 续查
- **Type**: `rule`
- **Given**: 某目录适配器首次执行在第 2 页后抛超时
- **When**: worker 再次处理该子请求
- **Then**: 首次尝试以 `uncertain` 持久化（含请求摘要、响应摘要、已完成分页进度、错误文本、远端句柄）；续查复用同一 `attempt_no` 与句柄从断点继续；不产生第二次远端作业提交（适配器提交入口仅 1 次）；暂存候选无重复
- **Pass Condition**: 进度从断点续传；尝试不新增编号；远端不重复提交；候选唯一
- **Evidence**: 单元/API 测试检查 attempts 行、pagination 内容、适配器调用计数、候选计数

### AC-5: 候选与测光在发布前完全隔离
- **Type**: `rule`
- **Given**: 一个正在执行、尚未进入发布阶段的查询
- **When**: 检查正式表并通过 `GET /api/catalog_queries/:id/candidates` 读取暂存
- **Then**: `objs`/`photometry`/`photometric_series` 相对提交前零变化；候选与测光载荷只存在于暂存表且可通过 API 分页读取
- **Pass Condition**: 发布前正式表零写入；暂存可读
- **Evidence**: API 测试前后快照正式表行数 + 暂存接口断言

### AC-6: 候选校验拒绝非法数据
- **Type**: `rule`
- **Given**: 假适配器产出一个非法 ID（含空格/非法字符）、一个 ra 越界、一个缺测光必需列的候选，以及一个合法候选
- **When**: 子请求完成解析与校验
- **Then**: 三个非法候选标记 `invalid` 且各自带原因，发布阶段不产生对应对象/测光；合法候选正常进入可发布集合
- **Pass Condition**: 非法候选 100% 被隔离且有原因；合法候选不受影响
- **Evidence**: API 测试断言暂存 validation_status/error 与发布结果

### AC-7: 现有对象精确 ID 关联保持兼容
- **Type**: `rule`
- **Given**: 数据库已存在 id 为 X 的 Obj（及其 Source 归属），适配器返回含 X 的合法候选与测光
- **When**: 发布执行
- **Then**: 不新建 Obj、不改变其现有组归属；新测光挂到 X；其余无匹配候选按现有 `post_source` 组规则新建
- **Pass Condition**: 与改造前逐对象行为一致
- **Evidence**: API 测试断言 Obj 计数不变、测光 obj_id 为 X、新对象按快照目标组创建

### AC-8: 单目录失败不丢弃其余成功且状态如实
- **Type**: `rule`
- **Given**: 三目录查询，适配器 A 成功、B 始终超时（达最大尝试）、C 返回数据错误
- **When**: 全部子请求终态并完成发布
- **Then**: A 的候选正常发布；作业状态为 `completed_partial`（不得为 `completed`），B/C 的失败原因与尝试记录在子请求与尝试行可查；无结果目录成功时计入零结果而非失败
- **Pass Condition**: 成功目录已发布；状态不是全成功；失败原因持久
- **Evidence**: API 测试断言状态、已发布对象、失败子请求 error

### AC-9: 终态通知与 Flow 推送恰好一次
- **Type**: `rule`
- **Given**: 两个 worker 几乎同时对同一完成作业执行终态聚合
- **When**: 聚合完成（含重复/并发重入）
- **Then**: 该作业仅 1 条 `UserNotification`、终态 Flow action 仅推送 1 次、`terminal_at` 单次写入；重复聚合为无操作
- **Pass Condition**: 通知与终态推送计数均恰为 1
- **Evidence**: 测试中 mock Flow 计数 push 次数并统计 UserNotification 行数

### AC-10: 取消只作用于未确认目录并拒绝迟到写入
- **Type**: `rule`
- **Given**: 作业含一个已 succeeded（已暂存）与一个 claimed 执行中的子请求
- **When**: 调用取消接口；随后被取消子请求的适配器迟到返回尝试写候选
- **Then**: 作业终态 `cancelled`；未确认子请求被置 cancelled、发布永不执行；已确认目录暂存保留；迟到候选写入 0 行；通知/Flow 仅一次；对已终态作业再取消返回 409
- **Pass Condition**: 取消后无新暂存/正式写入；终态唯一
- **Evidence**: API 测试断言各状态、暂存计数与 409 响应

### AC-11: 新版查询与旧作业迟到响应隔离
- **Type**: `rule`
- **Given**: 作业 Q1 已终态（cancelled/completed），相同参数重新提交生成 Q2
- **When**: 模拟 Q1 旧尝试的迟到响应到达并尝试写入
- **Then**: 写入因 Q1 子请求已终态被拒绝；Q2 的暂存与正式数据不受任何影响；Q1、Q2 `query_uid` 不同
- **Pass Condition**: 跨版本零污染
- **Evidence**: 测试直接调用暂存写入路径并断言 Q2 计数不变

### AC-12: read-only 只暂存不发布
- **Type**: `rule`
- **Given**: `publish_results=false` 的多目录查询，适配器返回合法候选与测光
- **When**: 作业终态
- **Then**: 状态为完成类终态；正式对象/测光/序列零新增；候选可经 candidates API 读取；无发布相关错误
- **Pass Condition**: 正式表零写入且暂存可查
- **Evidence**: API 测试表计数 + 接口读取

### AC-13: 旧 payload、无结果与既有端点兼容
- **Type**: `rule`
- **Given**: 旧版前端以 `catalogName` 单字符串提交；以及一个适配器返回零候选的作业
- **When**: 完成执行
- **Then**: 单目录作业正常创建并完成；零结果作业完成且保留"无新增对象"语义（状态文本与零计数并存）；GCN 事件查询列表 GET 仍可返回该作业；`/api/catalogs/swift_lsxps`、`/api/catalogs/gaia_alerts` 端点行为与路由不变
- **Pass Condition**: 四类兼容场景全部保持
- **Evidence**: API 测试 + 路由/导入检查

### AC-14: 数据库迁移与模型一致
- **Type**: `rule`
- **Given**: 位于 `28442e0a1830` 的测试库
- **When**: 执行新迁移 upgrade 再 downgrade
- **Then**: upgrade 后所有新表/列/索引（含活动作业 request_key 部分唯一索引、租约索引）存在且与 SQLAlchemy 模型一致；downgrade 干净回滚
- **Pass Condition**: upgrade/downgrade 均成功无残留
- **Evidence**: 迁移命令输出 + 模型/表结构对照检查

### AC-15: 权限快照门控发布
- **Type**: `rule`
- **Given**: 作业接收时快照目标组为 G；发布前把快照目标改为请求者当前不可访问的组（模拟被移出 G）
- **When**: worker 执行发布；随后恢复目标组再次执行
- **Then**: 交集为空时不产生任何对象/测光写入，作业保持 `publishing` 活动状态且子请求结果中持久化发布错误、不产生通知；恢复后重跑发布成功并终态完成，整条查询仅一条通知
- **Pass Condition**: 无部分发布；阻断期无通知；恢复后成功且通知一次
- **Evidence**: API 测试断言零写入、活动状态与错误记录、零通知、恢复后成功

### AC-16: worker 崩溃后接管无重复发布
- **Type**: `rule`
- **Given**: 子请求 claimed、部分候选已发布时 worker 崩溃
- **When**: 租约过期后另一 worker 接管至作业终态
- **Then**: 已发布候选被跳过、无重复对象/测光；作业到达正确终态且仅一条通知
- **Pass Condition**: 接管后无重复发布、终态正确
- **Evidence**: 测试模拟崩溃（中途抛异常退出 tick）后再次 tick 的计数断言

### AC-U1: 与既有代码模式的一致性
- **Type**: `rubric`
- **Dimension**: 新增模型/服务/适配器与仓库既有惯例（baselayer Base/ACL、`DBSession` 短事务、facility_queue 服务结构、make_log、状态字符串风格、JSONB 用法）的契合度
- **Scale**: 1-5
- **Anchors**: 1 = 引入并行的新范式与新依赖、难以维护；3 = 基本一致但存在突兀抽象；5 = 自然复用既有模式，评审者几乎无需额外学习
- **Pass Threshold**: >= 4
- **Evidence**: 独立代码审查对照现有队列服务与模型

### AC-U2: 状态机与失败信息的可观测性
- **Type**: `rubric`
- **Dimension**: 研究者/运维仅凭数据库状态与 API 能否看清作业、每目录、每尝试的进展与失败原因
- **Scale**: 1-5
- **Anchors**: 1 = 失败只在日志；3 = 状态可查但成败聚合含糊；5 = 作业/子请求/尝试三级状态、计数、分页、错误齐全且终态如实
- **Pass Threshold**: >= 4
- **Evidence**: 审查 GET 详情接口返回结构与状态文本设计

### AC-U3: 测试设计的可维护性
- **Type**: `rubric`
- **Dimension**: 测试是否快速、确定、不触网、失败定位清晰
- **Scale**: 1-5
- **Anchors**: 1 = 依赖真实外网或 sleep 等待；3 = 可离线但用例臃肿；5 = 假适配器注入、直接驱动 tick、用例按场景聚焦
- **Pass Threshold**: >= 4
- **Evidence**: 审查新增测试文件与运行方式

### AC-U4: 兼容性与改动克制度
- **Type**: `rubric`
- **Dimension**: 在达成可靠性目标的同时是否保持 payload/前端/独立端点兼容、diff 聚焦
- **Scale**: 1-5
- **Anchors**: 1 = 顺带重写无关路径或破坏旧 payload；3 = 兼容但存在不必要重构；5 = 旧 payload/渲染回退/独立端点全部保留，改动集中在作业流水线
- **Pass Threshold**: >= 4
- **Evidence**: 审查 diff 边界与兼容用例

## Open Questions

无未决问题（范围、read-only 语义、几何泛化、worker 形态已由用户确认为：仅 `/api/catalog_queries`；只暂存不发布模式；显式坐标/半径+目录列表且旧 payload 兼容；新增独立常驻 worker 服务）。
