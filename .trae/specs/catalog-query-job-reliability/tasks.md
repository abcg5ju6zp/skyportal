# 外部星表查询作业可靠性改造 - 实施计划

## Task 1: 持久化模型与 Alembic 迁移

- **Status**: `pending`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 [catalog.py](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/models/catalog.py) 扩展 `CatalogQuery`：新增 `query_uid`（UUID，唯一，默认 uuid4）、`ra`/`dec`/`radius`（Float，可空）、`publish_results`（Boolean，非空默认 true）、`permission_snapshot`（JSONB，非空默认 dict）、`request_key`（String，可空）、`cancel_requested`（Boolean，非空默认 false）、`terminal_at`（DateTime，可空，索引）、`notified`（Boolean，非空默认 false）；保留既有 `payload`/`status`/`allocation_id`/`requester_id` 与 ACL 不变。
  - 新增 `CatalogQuerySubjob`（表 `catalog_query_subjobs`）：`query_id` FK CASCADE、`catalog_name`、`status`（pending/claimed/succeeded/failed/cancelled，索引）、`request_key`、`claimed_by`/`claimed_at`/`last_heartbeat`/`lease_expires`（后两者索引）、`attempts_count`、`pagination`（JSONB）、`result_summary`（JSONB）、`error`、`confirmed_at`；`UniqueConstraint(query_id, catalog_name)`。
  - 新增 `CatalogQueryAttempt`（表 `catalog_query_attempts`）：`subjob_id` FK CASCADE、`attempt_no`、`state`（in_flight/uncertain/succeeded/failed，索引）、`remote_handle`（JSONB）、`request_summary`/`response_summary`（JSONB）、`error`、`started_at`/`finished_at`；`UniqueConstraint(subjob_id, attempt_no)`。
  - 新增 `CatalogQueryCandidate`（表 `catalog_query_candidates`）：`query_id`、`subjob_id` FK CASCADE、`catalog_name`、`external_id`、`ra`/`dec`、`raw_data`（JSONB）、`validation_status`（pending/valid/invalid）、`validation_error`、`resolution`（new/existing/rejected，可空）、`resolved_obj_id`（FK `objs.id` ON DELETE SET NULL，可空）、`published_at`（可空）；`UniqueConstraint(subjob_id, external_id)`、`(query_id)` 索引。
  - 新增 `CatalogQueryCandidateData`（表 `catalog_query_candidate_data`）：`candidate_id` FK CASCADE、`kind`（photometry/photometric_series）、`payload`（JSONB 非空）、`validation_status`/`validation_error`、`published_at`；`UniqueConstraint(candidate_id, kind)`。
  - 各新模型配置与 `CatalogQuery` 一致的读权限（allocation read 链），更新 `__all__`，并在 [models/__init__.py](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/models/__init__.py) 增加 `from .catalog import *`。
  - 新 Alembic 迁移（down_revision=`28442e0a1830`）：4 张新表 + `catalog_queries` 新增列（可空或带 server_default，避免锁写）+ 活动作业 `request_key` 部分唯一索引（状态取 pending/running/awaiting_publish/publishing/cancel_requested 的作业）+ `lease_expires`/`terminal_at` 等索引；提供对称 downgrade。
- **Acceptance Criteria Addressed**: AC-14、AC-U1；为 AC-1/AC-3/AC-4/AC-5/AC-9/AC-10/AC-11 提供存储基础
- **Test Requirements**:
  - `rule` TR-1.1: 对干净测试库执行 `alembic upgrade head` 成功，`catalog_queries` 新列与 4 张新表、部分唯一索引、租约索引存在；证据为迁移命令输出与 `\d`/`inspect` 结构核对。
  - `rule` TR-1.2: `alembic downgrade -1` 成功回滚全部新对象，再次 upgrade 恢复；证据为命令输出。
  - `rule` TR-1.3: 模型可正常 import 且 `Base.metadata` 中表定义与迁移后的实际表结构一致（列名/类型/可空性/索引）；证据为对比脚本输出。
- **Notes**: 状态机字符串常量集中在新作业包中定义，模型不建数据库 enum（与现有 status 列风格一致）。

## Task 2: 接收事务：作业/子请求/快照/幂等键

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 重写 [CatalogQueryHandler.post](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L59-L124)：在 `self.AsyncSession()` 单事务内完成参数校验、目录名白名单校验（ZTF-Fink/LSXPS/Gaia/TESS）、坐标与半径范围校验、localization 既有组限制查询；归一化 `catalogName`（str）与 `catalogNames`（list）两种写法；归一化几何（显式 ra/dec/radius，或沿用 localization 上下文留空）与 `publish_results`；构建操作者权限快照（user id、is_admin、role、可访问组、目标组合集含 allocation 组）；计算稳定 `request_key`（requester+allocation+归一化 payload 摘要+目录集排序+publish）。
  - 活动作业幂等：命中相同 `request_key` 的活动作业时直接返回其 id（200，data.id），不新增任何行、不推送重复通知；终态后允许新建。
  - 创建 `CatalogQuery` 与每条目录一条 `pending` 子请求、`catalog_queries_groups` 行（沿用现有目标组逻辑），提交后返回 `{"id": query.id}`；删除 `run_in_executor(fetch_transients)` 调度；保留接收后一次 `REFRESH_GCNEVENT_CATALOG_QUERIES` 推送。
  - 更新 [catalog_queries.py pydantic models](file:///Users/ding/Documents/swe/0910125/project-05/projects/api-models/skyportal_py_models/catalog_queries.py)：body 增加可选 `ra`/`dec`/`radius`/`publish_results`，payload dict 形状不变（extra=forbid 保持）；同步 [schema.py CatalogQueryPost](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/models/schema.py#L1520-L1545) 可选字段。
  - 无候选结果的语义在状态初始化与后续聚合中预留（completed + 零计数 + "No new objects" 文本）。
- **Acceptance Criteria Addressed**: AC-1、AC-2、AC-13
- **Test Requirements**:
  - `rule` TR-2.1: POST 成功提交后、不启动任何 worker 的情况下，库中存在 1 条作业（query_uid 非空、快照非空、目录集与几何正确）与 N 条 pending 子请求；证据为 API 测试直接查库断言。
  - `rule` TR-2.2: 相同参数（含 catalogName str 与 catalogNames list 等价形态）连续 POST 返回同一 id，子请求与组关联行数不增加；不同 publish 标志或终态后再提交产生新 query_uid；证据为 API 测试。
  - `rule` TR-2.3: 非法目录名、ra/dec/radius 越界、缺失 catalogName 返回 400 且无作业行；localization 组限制沿用旧行为；证据为 API 测试。
  - `rule` TR-2.4: 旧版单目录 payload（前端 CatalogQueryForm 原样构造）可被接受并归一化为单条带 catalogName 的子请求；证据为 API 测试。

## Task 3: worker 核心库：领取、尝试、暂存写入、终态聚合

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增包 `skyportal/utils/catalog_query/`（`__init__.py`、`jobs.py`、`adapters.py` 在 Task 4 填充）：定义状态字符串常量、worker 标识生成（hostname:pid:uuid）、`claim_subjobs(session, worker_id, limit, lease_seconds)`（`SELECT ... FOR UPDATE SKIP LOCKED`，候选条件 status=pending 或 claimed 且 lease_expires<now；原子写 claimed_by/claimed_at/heartbeat/lease_expires，attempts_count++，创建或续用 in_flight/uncertain attempt）、`heartbeat(session, subjob_id, worker_id, pagination)`、`record_attempt(...)`、`upsert_candidate(...)`（按 (subjob, external_id) 幂等 upsert 候选与 data 行；落库前强制校验作业未取消未终态、子请求属于该作业且 status=claimed、worker 标识匹配，否则丢弃并日志计数）。
  - `finalize_query(session, query_id, worker_id)`：`SELECT ... FOR UPDATE` 锁作业行；若仍有活动子请求则返回未完成；处理 cancel_requested（未确认子请求置 cancelled，不发布，终态 cancelled）；否则聚合全部子请求：全部 succeeded → 触发 Task 5 发布钩子（read-only 跳过）→ completed；存在失败 → completed_partial（read-only 同样适用）；无成功 → failed；无候选保留 "No new objects" 文本。终态转换以 `terminal_at IS NULL` 为条件；仅在转换成功的事务 after_commit 后创建唯一 `UserNotification` 与一次终态 Flow 推送（`notified` 标志 + 行锁保证一次）。
  - `request_cancel(session, query_id)`（供 API 调用）：活动作业置 cancel_requested；已终态返回冲突标志。
  - 所有函数接受显式 session 与可注入的适配器注册表/发布钩子；worker 循环逻辑拆为可单测的 `tick(worker_id, session_factory, adapters=..., now=...)`。
  - 轮询/领取/聚合事务均 `SET LOCAL statement_timeout`；不把远端 IO 包在持有行锁的事务内（领取事务先提交，执行用短事务更新心跳/进度）。
- **Acceptance Criteria Addressed**: AC-3、AC-4（续查机制部分）、AC-9、AC-10（取消与迟到防护）、AC-11、AC-16（接管机制部分）、AC-U1、AC-U2；NFR-3、NFR-4
- **Test Requirements**:
  - `rule` TR-3.1: 并发两个 worker 领取同一批 pending 子请求，每条子请求恰被领取一次；租约未过期不被另一 worker 抢走，过期后可被接管且 claimed_by 更新；证据为核心库单元测试（计数与状态断言）。
  - `rule` TR-3.2: 接管 uncertain 子请求时复用原 attempt_no/remote_handle，attempts_count 语义正确；证据为单元测试断言 attempts 行数与字段。
  - `rule` TR-3.3: 对已取消/已终态作业或非 claimed 子请求调用暂存写入，返回拒绝且暂存表零新增；以旧作业子请求写入不影响同参数新作业；证据为单元测试。
  - `rule` TR-3.4: 并发/重复调用 finalize 仅产生一次终态转换、一条 UserNotification、一次终态 Flow push（Flow mock 计数）；已终态再调为无操作；证据为单元测试。
  - `rule` TR-3.5: cancel 后 finalize 只取消未确认子请求、保留 succeeded 子请求暂存、终态 cancelled；已终态作业取消返回冲突；证据为单元测试。

## Task 4: 目录适配器：Fink/LSXPS/Gaia/TESS 分页式抓取到暂存

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 在 `skyportal/utils/catalog_query/adapters.py` 实现统一适配器接口：`run(subjob, query, attempt, session) -> summary`，内部按持久化 pagination 逐页抓取（Fink：沿用 [query_fink](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/utils/catalog.py#L90-L164) 的 localization tile 锥形与选源逻辑，tile 级与逐源测光进度分页；LSXPS：沿用 [fetch_swift_transients](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L366-L513) 的 submit/getDetails/getLightCurves/getSpectra 阶段与逐 transient 进度；Gaia/TESS：沿用 [fetch_gaia_transients](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L578-L709)/[fetch_tess_transients](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/catalog_services.py#L776-L941) 的下载、日期过滤、列名映射/重试逻辑）。
  - 与旧实现共享解析代码：把可复用的纯解析片段（df 列裁剪/重命名、Gaia magerr 公式、TESS 质量过滤、Swift 光谱落盘内容）提取为两边共用的 helper；旧 `fetch_*` 函数与独立端点行为保持可用。
  - 适配器只做：远端 IO（全部带显式 timeout，超时/连接错误/5xx 抛"不确定"异常）→ 解析候选 → 经 jobs.upsert_candidate 写暂存（测光/序列以 JSONB 载荷保存，形状与 `commit_external_photometry`/photometric_series 现有入参一致）→ 更新 attempt 的 response_summary/pagination/heartbeat；不触碰正式表。
  - 不确定异常由 tick 捕获：attempt 置 uncertain、保留句柄与进度；确定性数据错误（缺列、整响应损坏、未知目录）置 failed 并写 error；达到配置最大尝试次数后 failed。
  - 注册表 `ADAPTERS = {"ZTF-Fink": ..., "LSXPS": ..., "Gaia": ..., "TESS": ...}` 支持测试 monkeypatch。
- **Acceptance Criteria Addressed**: AC-4、AC-8（目录级失败隔离的执行侧）、AC-U1；NFR-1
- **Test Requirements**:
  - `rule` TR-4.1: 假适配器（monkeypatch 注册表）在第 2 页超时后，attempt=uncertain 且 pagination 记录断点；再次 tick 复用同一 attempt 从断点完成，远端"提交"仅发生 1 次、候选与测光行无重复；证据为单元测试（调用与行计数）。
  - `rule` TR-4.2: 一个目录适配器始终失败、其余成功时，成功目录候选全部入暂存，失败子请求 error/attempts 可查，其他子请求状态不受影响；证据为单元测试。
  - `rule` TR-4.3: 真实适配器构造代码与旧 fetch_* 共用同一套过滤/列映射 helper（同一函数引用），独立端点 `fetch_swift_transients`/`fetch_gaia_transients`/`fetch_tess_transients` 仍可 import 调用；证据为静态检查与轻量导入测试。
  - `rule` TR-4.4: 所有真实远端 HTTP 调用点均传 timeout 参数；证据为代码审查 + grep 证据。

## Task 5: 候选校验、关联解析与幂等发布阶段

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 3, Task 4
- **Description**:
  - 校验：在适配器写暂存时/发布前对每个候选执行 validation（ID 规则与 [post_source](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/handlers/api/source.py#L1086-L1094) 一致、ra/dec 范围、测光必需列与合法数值、kind 与载荷匹配）；非法标 invalid+原因；data 行载荷经 JSON 安全归一化。
  - 发布钩子 `publish_query(session, query)`：作业置 awaiting_publish→publishing；计算目标组 = 快照目标组 ∩ 请求者当前可访问组（以 requester_id 重新加载 User），交集为空则整体失败（error 记录、零写入、可重入重试）。
  - 逐候选在短事务内：重新按精确 `Obj.id` 解析（resolution=existing/new）；existing 沿用现状（不重建、不加组）；new 调 `post_source`（目标组、请求者身份不变）；随后按 kind 调 `commit_external_photometry(..., duplicates="update")` 或 photometric_series 现有 post/update 路径；成功后写 candidate/data 的 `published_at` 与 `resolved_obj_id`；已 published 的行跳过（崩溃重入安全）；invalid/rejected 永不发布。
  - read-only（publish_results=false）：不进入发布钩子，候选停留在 valid 暂存。
  - 发布结果计数写回作业/子请求 result_summary（新增对象 id 列表、挂载对象、跳过、非法计数）。
- **Acceptance Criteria Addressed**: AC-5、AC-6、AC-7、AC-12、AC-15
- **Test Requirements**:
  - `rule` TR-5.1: 含非法 ID/越界 ra/缺列候选与合法候选的暂存集执行发布后，仅合法候选产生正式写入，非法候选保持 invalid+原因；证据为 API/单元测试。
  - `rule` TR-5.2: 预置同 id Obj+Source 时发布不新增 Obj、不改其组、测光挂到该对象；无匹配候选按快照目标组新建；证据为单元测试。
  - `rule` TR-5.3: 已 `published_at` 的候选重跑发布被跳过，正式对象/测光计数不增加；证据为单元测试（模拟崩溃重入）。
  - `rule` TR-5.4: 快照目标组不可访问时发布零写入、作业保持 publishing 活动状态并持久化 publish_error、不产生通知；恢复目标组后重跑成功且仅一条通知；证据为单元测试。
  - `rule` TR-5.5: read-only 作业终态后 objs/photometry/photometric_series 计数零变化，暂存候选 validation 已完成且可读；证据为单元/API 测试。

## Task 6: catalog_query_queue 常驻服务与端到端编排

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 3, Task 4, Task 5
- **Description**:
  - 新增 `services/catalog_query_queue/catalog_query_queue.py`：`init_db`、`make_log`、`SET LOCAL statement_timeout`、轮询间隔与空闲休眠（仿 facility_queue/observation_plan_queue）；每轮调用 `tick`：领取上限内子请求→执行适配器→心跳→单目录终态→`finalize_query`（含发布钩子）；异常隔离在单子请求级别，不中断其余处理。
  - 新增 `services/catalog_query_queue/supervisor.conf`；`config.yaml.defaults` 增加 `app.catalog_query_queue`（poll_interval、claim_limit、lease_seconds、heartbeat_seconds、max_attempts、request_timeout_seconds），代码侧带内置默认值。
  - 崩溃恢复路径：服务启动不做特殊清扫，靠租约过期接管（Task 3）+ uncertain 续查（Task 4）+ 发布幂等跳过（Task 5）闭环。
  - 终态通知文本/类型沿用 `gcnevent_catalog_query`，url 有 localizationDateobs 时沿用 `/gcn_events/{dateobs}`，否则为空；Flow action 沿用 `REFRESH_GCNEVENT_CATALOG_QUERIES`。
- **Acceptance Criteria Addressed**: AC-1（端到端）、AC-8（端到端）、AC-16、AC-19、AC-U1；NFR-1、NFR-2
- **Test Requirements**:
  - `rule` TR-6.1: 通过作业 API + 直接驱动 tick（假适配器，不启动常驻循环）跑通两目录成功的完整生命周期：pending→claimed→succeeded→publishing→completed，正式对象/测光与通知各按预期；证据为集成测试（Task 9 总装，本任务先提供可运行 tick）。
  - `rule` TR-6.2: 单目录持续失败、其余成功时作业 completed_partial 且成功目录已发布、失败原因持久；证据为集成测试。
  - `rule` TR-6.3: 在子请求 claimed 且部分候选已发布后中断 tick（抛异常模拟崩溃），过期租约后再次 tick：无重复对象/测光，作业到正确终态，通知 1 条；证据为集成测试。
  - `rule` TR-6.4: 服务脚本可独立 import/启动（`python -c` 导入并验证 service 函数存在），配置缺省时使用内置默认；证据为命令输出。

## Task 7: 作业详情、暂存候选与取消 API

- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 3
- **Description**:
  - 在 catalog_services handler 增加：`GET /api/catalog_queries/:id`（作业 + 子请求状态/attempts 摘要/pagination/error/计数 + 已发布对象 id；读权限走现有 allocation read ACL）、`GET /api/catalog_queries/:id/candidates`（分页参数，返回暂存候选与 data 元数据，不回传过大载荷时给出概要字段）、`POST /api/catalog_queries/:id/cancel`（update ACL；已终态 → 409）。
  - 在 [app_server.py](file:///Users/ding/Documents/swe/0910125/project-05/skyportal/app_server.py#L379-L381) 注册三条路由；更新 projects/api-models 对应请求/响应模型。
  - 保持 `GcnEventCatalogQueryHandler` 列表 GET 兼容（新增字段随模型序列化自动出现，payload.gcnevent_id 过滤不变）。
- **Acceptance Criteria Addressed**: AC-10（取消入口）、AC-18、AC-U2；支撑 AC-5 可查性
- **Test Requirements**:
  - `rule` TR-7.1: 无读权限用户 GET 详情/候选返回 403；有权限用户能读到子请求/尝试/分页/错误与候选；证据为 API 测试。
  - `rule` TR-7.2: 取消接口权限、幂等与 409 行为符合 AC-10；证据为 API 测试。
  - `rule` TR-7.3: GCN 事件 catalog_query 列表 GET 对新老作业均返回 200 且包含新字段；证据为 API 测试。

## Task 8: 前端最小适配与 read-only 入口

- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 2, Task 7
- **Description**:
  - [CatalogQueryLists.tsx](file:///Users/ding/Documents/swe/0910125/project-05/static/js/components/catalog_query/CatalogQueryLists.tsx)：优先渲染结构化子请求状态/计数/失败原因与已发布对象链接；对旧作业保留 `"completed: Added ..."` 文本回退分支；增加取消操作入口（调用 cancel API，按权限/终态禁用）。
  - [CatalogQueryForm.tsx](file:///Users/ding/Documents/swe/0910125/project-05/static/js/components/catalog_query/CatalogQueryForm.tsx)：增加"只暂存不发布（read-only）"勾选并随提交发送 `publish_results`；单目录选择与现有 payload 构造保持不变。
  - 不重新生成整份 api.ts 生成类型；新增字段以局部类型/any 访问，控制 diff。
- **Acceptance Criteria Addressed**: FR-20、AC-12（入口侧）、AC-13（渲染兼容）；AC-U4
- **Test Requirements**:
  - `rule` TR-8.1: 前端 lint/typecheck（仓库既有 eslint/tsc 命令）通过；证据为命令输出。
  - `rule` TR-8.2: 旧状态字符串仍走回退渲染、新结构化状态按子请求展示的逻辑在代码评审中可核对；read-only 勾选产生的请求体包含 publish_results=false；证据为代码审查与请求体构造单测/评审证据。

## Task 9: 集成测试总装（假适配器、离线、全场景）

- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 6, Task 7
- **Description**:
  - 新增 `skyportal/tests/api_tests/catalog_queries/`（含 `__init__.py` 与测试文件）及假适配器/作业夹具：复用现有 token、group、allocation、telescope/instrument 夹具；通过 monkeypatch 替换适配器注册表，不发起任何真实网络；用直接调用 `tick(...)` 驱动状态推进，避免 sleep/轮询。
  - 覆盖端到端场景：接收即落库与崩溃恢复（AC-1）、重复提交幂等与新版 query_uid（AC-2/AC-11）、部分成功 completed_partial（AC-8）、uncertain 断点续查无重复（AC-4）、隔离与 candidates API（AC-5）、非法候选（AC-6）、existing Obj 关联（AC-7）、通知/Flow 一次（AC-9）、取消与迟到写入（AC-10）、read-only（AC-12）、旧 payload/无结果/独立端点兼容（AC-13）、权限快照门控（AC-15）、崩溃接管无重复发布（AC-16）。
- **Acceptance Criteria Addressed**: AC-1、AC-2、AC-4、AC-5、AC-6、AC-7、AC-8、AC-9、AC-10、AC-11、AC-12、AC-13、AC-15、AC-16
- **Test Requirements**:
  - `rule` TR-9.1: 新测试在测试配置下离线全部通过（命令与结果记录），且不触网、不依赖 Fink/Swift/Gaia/TESS 可用性；证据为 pytest 输出。
  - `rule` TR-9.2: 每条上述 AC 至少有一个对号入座的测试用例（用例名/断言与 AC 编号映射在完成证据中列出）；证据为测试清单。
  - `rubric` TR-9.3: 测试可维护性（AC-U3 同源维度）；scale 1-5；anchors 1=靠外网/睡眠/真实服务，3=离线但用例臃肿，5=假适配器注入+直接 tick+按场景聚焦；threshold >= 4；证据为审查新测试结构。
