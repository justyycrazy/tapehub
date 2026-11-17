# TqSdk（tqsdk）

天勤（TianQin）官方 Python SDK 接入的中国行情 Provider：CN 期货（合约/主力连续/加权指数）、期权三子类（commodity-option/index-option/etf-option，CN 八所，2026-11-16 收编并 asset_type 三分）、股票、指数、ETF 的历史 K 线、实时快照与 SSE 快照流。

## 概要

- 接入方式：官方 Python SDK `tqsdk`——`TqApi(auth=TqAuth(账户, 密码))`，快期账户鉴权必填；接口签名以官方文档为准：
  - usage/mddatas：<https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html>
  - API 参考：<https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html>
- SDK 包：pyproject extras `tqsdk`；可选依赖，取数路径内部延迟导入，未安装返回 `PROVIDER_NOT_IMPLEMENTED`（不影响服务启动）；
- 凭据变量：`TAPEHUB_TQSDK_AUTH_USER` / `TAPEHUB_TQSDK_AUTH_PASSWORD`（快期账户，只经环境变量注入并传入 SDK 客户端，禁止写入代码/manifest/日志）；两个变量全部非空才启用——缺失时能力目录仍可见（`enabled=false`），auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`（details 携带缺失变量名）；
- 变体体系：单接口 Provider，`variants` 与 `default_variant` 均为 `tqsdk`（与 Provider 同名，官方 SDK 包名；背后为天勤 TianQin 行情平台）——缺省请求即可路由，也可显式传 `variant=tqsdk`；
- 权限模型：数据可见性由**天勤平台账户档位**决定（免费版/专业版），与 SDK 及其版本无关。免费版账户仅期货/期权与四大指数（上证50/沪深300/中证500/中证1000）实时行情，A股股票/ETF 实时与历史需专业版账户；当前部署所用账户为专业版（股票/ETF 实时五档、历史日线均实测可用）。能力目录只声明结构性能力，账户权限不足是运行期事实——失败包装为 `UPSTREAM_ERROR`，不作为能力缺口声明；
- 连接模式 `TAPEHUB_TQSDK_CONN_MODE`：默认 `shared`（进程内唯一长连接 Runtime，懒启动）；`per_request` 为回滚配置（每请求在线程池中独立创建并关闭 TqApi 实例——官方约定一个线程一个实例）。两种模式标准化语义完全一致，`shared` 出现问题可一键切回；
- 本文只记录本 Provider 专属行为：跨 Provider 通用 API 约定见 `../api.md`，环境变量全表见 `../operations.md`，缺省回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准。

## 能力与上游映射

映射与能力数据以 `manifest.yaml` 的 capabilities/operations 为准；全部 bars 组合调用同一上游接口 `api.get_kline_serial`，全部 quotes 组合调用同一上游接口 `api.get_quote`（K 线与快照共用同一标的映射）。

| data_type | 资产类型（市场 CN，变体 tqsdk） | 周期 | adjust | history（北京时间，探针二分估计 approx） | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| bars | contract | PT1M/PT5M/PT15M/PT30M/PT1H/P1D | none | 各周期 start 均为 null（随挂牌期限，不编造固定起点） | open/high/low/close/volume |
| bars | continuous | 同上 6 周期 | none | 分钟级滚动保留：1m 14 / 5m 84 / 15m 240 / 30m 450 / 1h 840 天；P1D start 2016-01-05 | 同上 |
| bars | weighted-index | 同上 6 周期 | none | 分钟级滚动保留：1m 14 / 5m 70 / 15m 180 / 30m 390 / 1h 690 天；P1D start 2016-01-05 | 同上 |
| bars | stock（分钟族 ≤PT15M） | PT1M/PT5M/PT15M | none（复权 no-op） | 滚动保留：1m 28 / 5m 150 / 15m 450 天 | 同上 |
| bars | stock（PT30M 起） | PT30M/PT1H/P1D | none/qfq/hfq | 滚动保留：30m 930 / 1h 1500 天；P1D start 2018-01-02 | 同上 |
| bars | index | PT1M/PT5M/PT15M/PT30M/PT1H/P1D | none（adj_type 对指数无效） | 滚动保留：1m 28 / 5m 150 / 15m 450 / 30m 930 / 1h 1500 天；P1D start 2018-01-02 | 同上 |
| bars | etf（分钟族 ≤PT15M） | PT1M/PT5M/PT15M | none（复权 no-op） | 滚动保留：1m 28 / 5m 150 / 15m 450 天 | 同上 |
| bars | etf（PT30M 起） | PT30M/PT1H/P1D | none/qfq/hfq | 滚动保留：30m 930 / 1h 1500 天；P1D start 2018-01-02 | 同上 |
| bars | commodity-option（五所）/ index-option / etf-option | P1D | none（adj_type 对期权无效） | start 均为 null（合约级数据随上市起、随摘牌止） | open/high/low/close/volume |
| quotes | contract/continuous/weighted-index/stock/index/etf/commodity-option/index-option/etf-option | 无周期维度（声明为空） | 无复权维度（声明为空） | 不适用 | timestamp、last_price、五档买卖价量（bid/ask 价量 1..5）、volume、amount |

期权补充口径（asset_type 三分：commodity-option/index-option/etf-option，八所归属见 ../api.md「asset_type 资产类型体系」；2026-11-16 聚焦轮八所探针实证，原始证据本地留档不入库）：

- **统一式入参 → 原生式还原**（结构子类统一式 {品种}{YYMM}{C/P}{行权价} 见 ../api.md「instrument 标准代码」；tqsdk 原生写法为八所中唯一品种小写源，与该所期货品种大小写同系）：SSE/SZSE 数字合约码直拼（`CN-SSE-10012437 → SSE.10012437`）；SHFE/INE 品种小写无连字符（`CN-SHFE-CU2701C102000 → SHFE.cu2701C102000`、`CN-INE-SC2701C550 → INE.sc2701C550`）；DCE/GFEX 品种小写连字符 4 位月（`CN-DCE-M2701C3500 → DCE.m2701-C-3500`；`CN-GFEX-LC2701C90000 → GFEX.lc2701-C-90000`）；CZCE 大写品种 3 位月码（`CN-CZCE-SR2701C5900 → CZCE.SR701C5900`，统一式 4→3 还原与该所期货合约同构）；CFFEX 大写连字符式（`CN-CFFEX-IO2612C4000 → CFFEX.IO2612-C-4000`）；
- 落选候选否定证据（原生式侧，定案时以标准码直测）：INE 大写直通 / DCE 大写直通 / GFEX 无连字符 `lc2701C90000` 均 graphql「查询合约服务报错」快速拒绝——期权写错即快速报错（非期货侧 4 位年月码的挂起行为）；统一式定案后旧连字符/3 位月码写法在域层即拒绝，不再到达上游；
- 权限口径：官方权限模型免费版账户即含期权实时行情（当前账户专业版）；周期仅核实 P1D（`get_kline_serial` 其余周期对期权未核实，不声明），K 线实证 3 根/标的 × 八所、快照与流式走通用 `get_quote` 订阅链路。

- 标准字段口径：上游返回列 `id/datetime/open/high/low/close/volume/open_oi/close_oi`，无成交额列——标准 `amount` 恒为 `null`（不用 0 冒充）；持仓量经受控 `extensions` 透出（见「extensions 白名单」）；
- 分钟级历史不声明固定起点，以 `retention_days` 声明**滚动保留**（覆盖 now − N 天 → now，边界随时间前移）；日线以固定 `start` 声明；`/v1/capabilities` 的 interval history 字段按声明形式原样透出；
- 滚动保留取整规则（向下取，留安全余量）：不足 90 天取 7 的倍数（周）、不小于 90 天取 30 的倍数（月）；六周期折算根数均落在 5000 根量级——与单次上限一致，保留深度即序列存储上限。

## 标的代码映射

标准代码 `{market}-{exchange}-{symbol}` → 上游 `交易所.合约`（交易所段全大写）；标准 symbol 恒为大写 4 位年月（域层校验：contract×CZCE 3 位旧写法构造期拒绝，2026-11-17 定案见 ../api.md「CZCE 期货合约年月形态」），交易所原生写法由适配器还原，还原表只收探针实证条目：

- 期货单一合约（六所写法定案，2026-11-16 第 13 轮期货聚焦轮探针实证；与第 12 轮期权八所品种大小写体系互证一致）：
  - SHFE/INE/DCE/GFEX：品种整段小写——`CN-SHFE-CU2707 → SHFE.cu2707`、`CN-INE-SC2706 → INE.sc2706`、`CN-DCE-M2705 → DCE.m2705`、`CN-GFEX-LC2706 → GFEX.lc2706`（大写不被上游接受：DCE/INE 无响应挂起、GFEX.LC2706 graphql「查询合约服务报错」快速否定）；
  - CZCE：合约 4 位年月码 → 3 位——`CN-CZCE-RM2701 → CZCE.RM701`（上游只认 3 位，4 位写法无响应挂起直至超时；转换只作用于「品种+2 位年+2 位月」形态，主连/加权品种码与不规则代码原样透传；标准式输入全链路——六周期 bars + 快照 `CN-CZCE-MA2701 → CZCE.MA701`——2026-11-17 聚焦复验 confirmed，与同轮 ths 侧 `MA701.CZC` 分时末点 3129.0 同价互证；3 位旧写法已由域层构造期拒绝，不再到达适配器）；
  - CFFEX：大写品种直通——`CN-CFFEX-IF2706 → CFFEX.IF2706`（2026-11-16 实证，不做改写）；
- 主力连续：`KQ.m@<交易所>.<品种>`——`CN-SHFE-CU → KQ.m@SHFE.cu`、`CN-DCE-M → KQ.m@DCE.m`、`CN-INE-SC → KQ.m@INE.sc`、`CN-GFEX-LC → KQ.m@GFEX.lc`、`CN-CZCE-MA → KQ.m@CZCE.MA`、`CN-CFFEX-IF → KQ.m@CFFEX.IF`（五所逐所 2026-11-16 探针实证有数据——金融期货主连存在性定案）；指数连续（加权）：`KQ.i@<交易所>.<品种>`——`CN-SHFE-BU → KQ.i@SHFE.bu`、`CN-INE-SC → KQ.i@INE.sc`、`CN-CFFEX-IF → KQ.i@CFFEX.IF` 等（同轮实证，品种码大小写与合约同系）；
- 无效代码行为：无效订阅无响应挂起直至超时（CZCE 4 位码、DCE/INE 大写品种），个别形态 graphql 快速报错（GFEX 大写）——且无效订阅会把共享 TqApi 运行时堵死、殃及后续一切调用（含合法代码，第 13 轮聚焦轮实证）；还原表只收实证条目正是该行为的防线；
- 股票/指数/ETF（同期货格式，官方文档示例口径）：`CN-SSE-600000 → SSE.600000`、`CN-SZSE-000001 → SZSE.000001`、指数 `CN-SSE-000016 → SSE.000016`、ETF `CN-SSE-510050 → SSE.510050`（指数仅核实 SSE/SZSE 发布的代码）；
- 拒绝范围：资产类型以能力目录为准（上表九类）；BSE 交易所前缀上游不支持（快速报 non-existent instrument）；期权结构子类统一式还原见上方「期权补充口径」（SHFE/INE/DCE/GFEX 品种小写、CZCE 4→3 月码、DCE/GFEX/CFFEX 连字符插入，不沿用期货还原表规则；子类×交易所矛盾组合与非统一式符号在域层即拒绝，见 ../api.md）。

## 周期 / 复权 / 单位映射

- 周期映射：上游接受不超过 1 日的任意秒数——`PT1M→60`、`PT5M→300`、`PT15M→900`、`PT30M→1800`、`PT1H→3600`、`P1D→86400`；适配器只声明这 6 个周期，其余周期抛 `UNSUPPORTED_CAPABILITY`；周期代码之间不做换算（`PT1M` ≠ `P1M`）；
- 复权映射（上游参数 `adj_type` 仅对股票/基金类型有效）：`qfq→F`、`hfq→B`、`none→None`；期货/指数无复权概念恒传 `None`；全部参数显式传递，不依赖上游默认值；
- 复权生效边界（探针实证）：分钟族（≤PT15M）qfq/hfq 与 none 零差异（no-op），能力声明收窄为仅 none；PT30M/PT1H/P1D 复权真实生效（三值声明；2026-11-17 盘中复验确认 etf 同窗 hfq 生效——adjust_diff 窗口须与各周期序列基点（P1D start / 分钟族保留边界）之间跨过至少一个除权事件：旧窗 2024-06~08 紧邻 etf PT30M 保留边界（≈2024-05）且该区间无除权事件，hfq 因子恒 1 曾致零差异伪影 suspect（同窗 P1D/PT1H 因基点更早、中间含分红故有差异），窗口已前移至 2025-01 起）；上游「暂不支持复权获取多合约 K 线」与本适配器的单合约调用方式不冲突；
- 单位换算：期货 `volume` 保持**手**（不换算）；股票/ETF `volume` 经量级对照实证判定为**股**，原样透出；bars 的 `amount` 上游无字段；快照 `amount` 为元。所有数值原值透出，不做二次换算。

## 上游行为定案

- K 线取数语义：序列从“最新一根”往回取 `data_length` 根，**不支持按时间范围请求**——适配器固定传 `data_length=5000`，标准化后按闭区间 `[start, end]` 过滤（返回的是最新 N 根，保尾截头）；翻页上限由此模型决定：`offset ≥ 5000` 的窗口恒为空页且 `meta.partial` 不置位（跨 Provider 分页语义见 [api.md](../api.md)「分页语义」）；
- 单次上限：实测 5000 根（PT1M/PT5M 同时恰好 5000）；官方文档 8000/10000 口径与实测不符（以实测为准），疑受当前套餐限制，升档后需重新实证；返回数量达上限且最早一根仍晚于请求 `start` 时更早数据取不到——响应 `meta.partial=true` 且 `warnings` 提示；
- 时间戳口径：bars 的 `datetime` 为自 epoch 起的**纳秒**时间戳（K 线起点，绝对时间），统一归一为北京时间 `+08:00` 输出；
- 错误指纹：上游 SDK 异常类型未在文档中核实，统一包装为 `UPSTREAM_ERROR`（含无权限请求）；单标的订阅失败（无效合约/权限不足）不向等待方抛错，等待方按首包超时 `QUOTE_NOT_READY` 收场；
- 已知上游缺陷与规避（Runtime 实测）：
  - **peek 门闩**：推送协议要求客户端发出 `peek_message` 后才推送数据，而 `wait_update` 仅在收到数据时复位门闩——静默 tick 会把门闩留在复位态，整个连接变聋（夜盘断流、新订阅挂起）；Runtime 每个 tick 主动复位门闩规避；
  - 盘后时段 `adj_type` 复权（F/B）请求可能无响应直至超时（同标的同周期不复权请求正常返回）——夜间请求股票/ETF 复权序列可能失败；
  - 连续 `wait_update` 异常达到阈值（3 次）触发新一代重建（watchdog），重建前在飞请求显式失败（可快速重试），重建退避 2 秒；
  - 构造失败语义（2026-11 修订）：**暂时性失败**（网络抖动/认证服务暂不可达等 `RuntimeError` 形态）按指数退避重试（2s 起、封顶 60s，成功一代归零），不因单次失败永久不可用——否则每日 04:00 回收 / watchdog 重建窗口的一次抖动即殃及后续全部请求；**致命失败**仅保留 SDK 缺失（ImportError）与驱动线程死亡两类，进入 `fatal` 拒绝后续请求（适配器在替换宽限期后收编重试，见下）；
  - 适配器 Runtime 替换兜底：缓存实例处于 `fatal`，或非 `ready` 且距创建超过替换宽限期（120 秒）时，下一次请求收编旧实例并替换新实例（覆盖构造持续失败与构造永久挂死两类残留态）；
  - `start()` 构造等待超时（`construct_timeout`）不再停止驱动线程——线程继续退避重试，后续构造成功即 `ready`；
- 线程模型：官方约定一个线程一个 TqApi 实例；SDK 延迟导入仅发生在取数/Runtime 线程内，不依赖 FastAPI 请求上下文（后台进程可复用）。

## 快照与流式

参与实时快照（M2，`GET /v1/market/quotes`）与 SSE 快照流（M3，`GET /v1/market/quotes/stream`）；上游接口均为 `api.get_quote` 订阅推送。

- **交付模型（订阅-推送）**：`shared` 模式经进程内唯一 `TqSdkRuntime` 长连接（懒启动，专属线程 tick 驱动 + `create_task` 并发协程 + `wait_update` 维持推送）；首个订阅者 `await get_quote` 等首包，此后 tick 循环以 `is_changing` 判定增量、线程内 `dict` 拷贝发布快照缓存（外部只拿到纯 dict 拷贝）；tick 自适应——有在飞请求或活跃订阅时 50ms、静默期逐步放宽至 2s；
- **按请求租约订阅（M2 快照）**：请求结束（成功/408/客户端断开）即退订本地维护、丢弃缓存，Quote 转入**休眠账本**（上游订阅因协议限制保留）；重复请求经休眠唤醒即时发布 Quote 当前值（上游持续推送，唤醒即新鲜，无陈旧缓存）；休眠回收阀门因此对纯快照流量同样可达；
- **协议不支持取消订阅（诚实边界）**：退订只停止本地维护，休眠订阅的上游推送仍在消耗带宽；休眠订阅累计达 `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT`（默认 64）触发**新一代重建清理**（活跃订阅自动重订；北京时间 04:00 跨交易日回收同理——采纳官方跨交易日重启建议）；
- 首包超时：`TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT`（默认 5 秒）内首包未到达返回 `QUOTE_NOT_READY`（HTTP 408），常见于标的不存在/无权限或非交易时段上游静默；不编造部分字段快照；
- `per_request` 回滚模式：K 线与快照同样可用（每请求独立实例订阅取数/取首包后关闭，无缓存复用）；
- 快照字段语义：`timestamp` 为上游北京时间墙钟（微秒精度），统一 `+08:00` 输出；`meta.received_at` 为网关完成标准化的时刻；指数与加权指数无盘口——`bid/ask` 价格 `null`（不用 0 冒充）、量侧为 `0`；股票/ETF 实时五档需专业版账户（运行期事实）；volume/amount 单位见「周期 / 复权 / 单位映射」；
- **流式（M3）**：仅 `shared` 模式支持（流依赖长连接订阅；`per_request` 返回 `UNSUPPORTED_CAPABILITY` 422）；Runtime 流监听器在 tick `is_changing` 处发布，经 `loop.call_soon_threadsafe` 桥接进每流的有界缓冲（跨线程只传拷贝、不阻塞 tick）；字段白名单与快照一致；注册监听器时已有可用快照则立即补发 `subscribed`（先查快照再开流不静默等待）；
- **代际回收（`recycling` 事件，tqsdk 专属）**：触发条件为北京时间 04:00 跨交易日窗口、watchdog 连续异常、休眠订阅达上限三类；回收时在飞请求显式失败（可快速重试），流客户端先收 `recycling`、重订完成后收 `subscribed` 并恢复 `quote` 推送——绝不静默断流；
- 流关闭（客户端断开/空闲超时/停机）即本地退订（引用计数 −1）并注销监听器；上游订阅保留至代际回收；停机钩子同步 join（上限 10 秒），停机时在飞请求显式失败、流监听器以 `upstream_disconnected` 收尾；
- Runtime 状态经 `/v1/capabilities` 的 `runtime_status` 回填：`idle`（懒启动前）/ `starting` / `ready` / `recycling` / `fatal`；`per_request` 模式无长连接，恒为 `unknown`（不伪造状态）；
- 并发闸门：在飞 K 线请求达 `TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`（默认 16）立即返回 `CONCURRENCY_LIMIT`（429）；
- 运行时推送与历史取数互不干扰（2026-11-16 夜盘盘中实测）：并发 8×5000 根历史大取数期间，活跃订阅相邻推送间隔与空闲基线完全持平（p50 500ms / p95 547ms）——统一实例（历史与行情同连接）无推送抖动；行情新鲜度滞后（墙钟−quote 时间戳 p50 ≈10s）为空闲基线既有、非负载引入。首包延迟盘后退化（3.6×）系盘后上游历史服务慢的伪影（与盘后复权无响应同源），盘中无退化；
- SSE 三类事件帧结构、coalesce 丢旧保新（只约束 quote 帧）、心跳与客户端容量闸门等通用约定见 `../api.md`（心跳/空闲退订由 Application 层流服务统一管理，Provider 无关）。

## 缺省路由参与

- bars：CN `etf`/`contract`/`continuous` 承担缺省；CN `weighted-index` 独占；CN `stock`/`index` 仅声明能力、不参与 auto 回退链（仅显式 `provider=tqsdk` 可用）；CN 期权三子类居链尾（2026-11-16 八所收编并 asset_type 三分，凭据型，ths 缺省首位、tushare 居中）；
- quotes：CN `stock`/`index`/`etf`/`contract` 承担缺省；CN `continuous`/`weighted-index` 独占；CN 期权三子类居链尾（2026-11-16 收编，ths REST 快照居首；期权 SSE 流式经显式 `provider/variant=tqsdk` 可达）；
- 位次依据（一句话）：期货三类与 CN 全资产快照是本 Provider 的原生长连接订阅能力面，故承担缺省/独占；个股/指数 K 线保留显式通道、不进 auto 链是路由决策（回退链上不写即明确不参与）；
- 当前回退链顺序与组合覆盖以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表；凭据未配置时本 Provider 自动跳过，由回退链下一位生效。

## extensions 白名单

- bars：`open_oi` / `close_oi`（持仓量；期货为真实持仓，股票/指数/ETF 列存在但恒为 0——如实透出 0.0，不用 null 冒充）；
- quotes（快照与流帧同口径）：`open` / `highest` / `lowest` / `average` / `pre_settlement` / `pre_close` / `open_interest` / `pre_open_interest` / `upper_limit` / `lower_limit`（均为探针样本中实证有值的行情字段；盘中为 NaN 的字段如 `close`/`settlement` 不透出；合约元数据类字段不透出；缺失/NaN 一律不进 extensions）；
- quotes 另有网关注入字段 `snapshot_age_ms`（2026-11 修订）：Runtime 快照到达时刻距响应生成时刻的毫秒数——断连重建/静默窗口内命中缓存的快照可据此判陈旧（`received_at` 是标准化时刻，不反映数据到达），`per_request` 模式独立实例无缓存复用，不透出该字段。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/tqsdk.yaml`（探针框架规范见 `docs/probes.md`）——覆盖可用性矩阵（CN 证券 SSE/SZSE 逐所，BSE 上游拒绝不上目标）、复权生效性（adjust_diff）、历史深度（history_depth）、单次上限（limit）、错误指纹（raw，含 CZCE 4 位年月码复验；原 BSE 闸门 raw 已随定案退役，拒绝结论由「不设 BSE 目标」承载）、连接时长（connection）；运行时段建议**盘中**（盘后复权 case 会挂满超时，见「上游行为定案」）；
