# Twelve Data（twelvedata）

全球行情数据商 Twelve Data 的适配器：GLOBAL 外汇/加密货币与美股（个股/ETF）历史 K 线（REST），实时快照与 SSE 快照流（WS 共享订阅）。

## 概要

- 接入方式（接口签名以官方文档为准：<https://twelvedata.com/docs/>）：
  - 历史 K 线走官方 Python 库 `twelvedata`（twelvedata-python）——`TDClient(apikey=...).time_series(...).as_json()`；
  - 实时快照与快照流走 WS 端点 `wss://ws.twelvedata.com/v1/quotes/price`，由 `websockets` 库直连实现共享单连接的动态增删订阅——官方 SDK 的 `td.websocket` 为固定 symbols + 阻塞 keep_alive 模型，不适配共享订阅，故不使用；
  - REST `/quote` 按需路径已退役——快照与流统一 WS 订阅源；
- SDK 包：pyproject extras `twelvedata`（REST）；WS 依赖 `websockets`——两者均为可选依赖、取数/连接路径内延迟导入，未安装返回 `PROVIDER_NOT_IMPLEMENTED`（不影响服务启动）；
- 凭据变量：`TAPEHUB_TWELVEDATA_API_KEY`（历史与实时共用；只经环境变量注入并传入 SDK 与 WS 连接 URL 查询参数，绝不写日志）；未配置不启用——能力目录仍可见（`enabled=false`），auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`；
- 变体体系：单接口 Provider，`variants` 与 `default_variant` 均为 `twelvedata`（数据源即上游服务自身）——缺省请求即可路由，也可显式传 `variant=twelvedata`；
- 连接形态：恒为进程内单条共享 WS 连接（免费版上游限 1 连接；同进程第二个存活 Runtime 实例会互踢连接——启动期经弱引用检测并告警）；无连接模式概念，`runtime_status` 恒为 `unknown`（不伪造状态）；实时能力受全服务单进程部署模式约束；
- 本文只记录本 Provider 专属行为：跨 Provider 通用 API 约定见 `../api.md`，环境变量全表见 `../operations.md`，缺省回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准。

## 能力与上游映射

映射与能力数据以 `manifest.yaml` 的 capabilities/operations 为准；全部 bars 组合调用同一上游接口 `td.time_series`（美股额外显式传 `exchange` 与 `adjust`），全部 quotes 组合的上游接口为 WS 订阅推送（`subscribe` 的 price 事件）。

| data_type | 资产类型（市场，变体 twelvedata） | 周期 | adjust | history.start（北京时间零点，探针二分估计 approx） | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| bars | forex（GLOBAL） | 全 12 周期（见下） | none | 1m 2020-04-08；5m 2020-03-19；15m/30m/45m/1h/2h/4h/8h 2020-01-31；P1D 2003-12-25；P1W/P1M 1975-01-23 | open/high/low/close（无 volume/amount） |
| bars | crypto（GLOBAL） | 全 12 周期 | none | 1m 2020-04-07；5m 2020-03-25；15m 2020-02-19；30m/45m/1h/2h/4h/8h 2020-01-07；P1D/P1W 2017-08-29；P1M 2017-08-02 | open/high/low/close（无 amount） |
| bars | stock（US） | 全 12 周期 | none/qfq | 1m 2020-04-11；5m 2020-02-15；15m/30m/45m 2019-11-06；1h 2019-02-01；2h/4h/8h 2019-07-18；P1D/P1W/P1M 1981-01-18 | OHLCV（amount 恒 null） |
| bars | etf（US） | 全 12 周期 | none/qfq | 1m 2020-04-11；5m/15m/30m/45m/1h/2h/4h/8h 2020-02-15；P1D/P1W 1993-03-17；P1M 1993-01-20 | OHLCV（amount 恒 null） |
| quotes | forex/crypto（GLOBAL） | 无周期维度（声明为空） | 无复权维度（声明为空） | 不适用 | timestamp、last_price |

- 「全 12 周期」＝ `PT1M/PT5M/PT15M/PT30M/PT45M/PT1H/PT2H/PT4H/PT8H/P1D/P1W/P1M`（对应上游完整枚举 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month）；
- 上游 values 元素字段（datetime/open/high/low/close/volume）均为**字符串**，标准化为数值；缺失/NaN → `null`；
- history.start 为全量深度探针的二分估计（approx，回填取估计日零点）；真值与代表性标的互证（US stock ≈ AAPL IPO、US etf ≈ SPY 上市，均落在估计窗口内）；
- crypto 的 volume 上游可能缺失 → `null`（能力 fields 不含 volume）。

## 标的代码映射

- 外汇：`GLOBAL-OTC-EUR/USD → symbol "EUR/USD"`（ISO 4217 货币对，斜杠分隔；exchange 段限定 `OTC`——场外聚合报价）；
- 加密货币：`GLOBAL-CRYPTO-BTC/USD → symbol "BTC/USD"`（聚合对；exchange 段限定 `CRYPTO`；交易所专属对与 `exchange` 筛选参数暂不支持）；
- 美股：`US-NASDAQ-AAPL → symbol=AAPL + exchange=NASDAQ`（交易所原生 ticker 原样透传 + 显式传 `exchange` 消歧同名 ticker）；
  - exchange 段白名单 **NYSE/NASDAQ/CBOE**；OTC 粉单（数据质量差）与 IEX（目录仅 65 个标的）不接入；
- 拒绝范围：
  - 仅支持上表四类资产类型，其余抛 `UNSUPPORTED_CAPABILITY`；exchange 段不在白名单内同样拒绝；
  - **CN/HK 不声明**——上游目录虽有 China/Hong Kong stock/etf，但免费档（Basic）可用标的各仅 1 个（其余为 Pro/Venture 门槛，404 指纹 "This symbol is available starting with the Pro or Venture plan"），且官方交易所页标注 XSHG/XSHE 为 EOD-only（任何档位无日内）；升档后按探针流程实测回填。

## 周期 / 复权 / 单位映射

- 周期映射（12 个全量声明，45min/2h/4h/8h 为实测可用后补充声明）：`PT1M→1min`、`PT5M→5min`、`PT15M→15min`、`PT30M→30min`、`PT45M→45min`、`PT1H→1h`、`PT2H→2h`、`PT4H→4h`、`PT8H→8h`、`P1D→1day`、`P1W→1week`、`P1M→1month`；周期代码之间不做换算（`PT1M` ≠ `P1M`）；
- 复权映射（仅美股资产类型传参；外汇/加密货币无复权概念不传）：
  - `none→"none"`（原始价）、`qfq→"all"`（上游拆股+分红前复权，除息日前历史回溯调整——与国内「前复权含分红」口径一致）；
  - `hfq` 上游无等价物，能力不声明；
- **参数名陷阱**：上游 REST 只认 `adjust`，传 `adj` 会被静默忽略落回上游默认（=splits 仅拆股）；不传时上游默认 splits——因此美股始终显式传参（官方 SDK 参数名即 `adjust`，经 SDK 传递天然正确）；
  - etf 复权同 stock 生效（除息窗口实证，差异为上市以来累计分红回溯量级）；
  - 需要原始价请显式传 `adjust=none`；
- 单位换算：外汇/加密货币无成交量——标准 `volume` 为 `null`（能力 fields 不含 volume）；美股 `volume` 有值；`amount` 上游无字段恒 `null`；上游数值均为字符串，标准化后**原值透出**，不做二次换算。

## 上游行为定案

- 单次上限：`outputsize` 范围 1~5000，适配器显式传最大值 5000（上游默认仅 30）；上游按日期范围过滤，适配器标准化后按闭区间 `[start, end]` 复核；返回数量达上限且最早一根仍晚于请求 `start` 时更早数据取不到——`meta.partial=true` 且 `warnings` 提示；
- 排序：上游 `values` 默认倒序（最新在前），统一升序排序输出；
- bar 时间墙钟口径按资产类型区分（实测 + 官方交易所页）：
  - 外汇按 **Australia/Sydney**（AEDT/AEST 随夏令时切换）；加密货币按 **UTC**；美股按 **America/New_York**（EST/EDT 随夏令时切换）；
  - 日线/周线/月线上游强制返回交易所本地 date-only 时间（零点），归一化北京时间后落在当日 12:00/13:00（随夏令时漂移）；
  - `start_date`/`end_date` 为上游口径墙钟 `YYYY-MM-DDTHH:MM:SS`（无时区后缀），由适配器从北京时间转换后显式传递；
- 已知数据源缺陷：外汇 `1h` 与 `1day` 数据源不一致——任何日切窗口（Sydney/UTC/纽约 17:00 ET）的 1h 聚合都无法精确还原 `1day` 的 OHLC，需要日线请直接请求 `P1D`；加密货币 `1h` 聚合可完美还原 `1day`（UTC 0 点日切）；
- 区间无数据行为：上游返回 400 `BadRequestError`（指纹 "No data is available on the specified dates"）而非空列表——适配器按指纹归一化为**空结果**，不作为错误；
- 错误指纹：
  - 无效 Key → 401 `InvalidApiKeyError`（"apikey parameter is incorrect"）；
  - 无效标的 → 404 `TwelveDataError`（"symbol or figi parameter is missing or invalid"）；
  - 其余 SDK/HTTP 异常类型与限频规则未在所引文档中核实，统一包装为 `UPSTREAM_ERROR`；
- 免费档配额：REST 限频 8 次/分钟、800 次/天（探针 pacing 据此放宽）；WS credit 口径见「快照与流式」；
- 显式传参原则：`interval`/`outputsize`/`start_date`/`end_date`（及美股 `exchange`/`adjust`）全部显式传递，不依赖上游默认值——上游 interval 缺省行为未知、outputsize 默认 30、美股 adjust 缺省 splits。

## 快照与流式

参与实时快照（M2，`GET /v1/market/quotes`）与 SSE 快照流（M3，`GET /v1/market/quotes/stream`），**同源**：均来自 WS 共享单连接的实时 price 事件订阅缓存。

- Runtime 形态：asyncio 原生共享单连接（懒启动，跑在调用方事件循环上），per-symbol 引用计数；
  - 协议为 `{"action": "subscribe|unsubscribe|heartbeat", "params": {...}}`，`subscribe-status`/`unsubscribe-status` 为显式确认（退订与确认间存在在途事件竞态，以确认为准）；
  - 应用层心跳每约 10 秒发送且有应答（应答计入静默看门狗活性口径）；
- 交付模型（WS 订阅缓存）：冷订阅等待首包（超时 `TAPEHUB_TWELVEDATA_FIRST_SNAPSHOT_TIMEOUT` 默认 5 秒 → `QUOTE_NOT_READY` 408，不编造部分字段快照）；活跃订阅即时读缓存；快照新鲜度由 `meta.received_at` 表达；
- **逐 symbol 真退订 + linger**：上游支持真正的逐 symbol 退订；引用归零延迟 60 秒（linger）后向上游发 unsubscribe 真退订并即时释放 WS credit——兼顾快照轮询与快速重连免重订（窗口内引用复活则放弃退订）；linger 窗口内快照仍可读；
  - 流关闭即本地退订（引用计数 −1），上游订阅经 linger 真退订；
  - **无代际回收，不产生 `recycling` 事件**（与 tqsdk 的差异）；
- **容量闸门（上游订阅 credit）**：WS credit = 当前活跃订阅 symbol 数（免费版 8），退订即时释放、重订重连免费、同 symbol 重复订阅不重复计费；
  - 并发活跃 symbol 数达 `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT`（默认 8）时，新 symbol 的流/快照请求在受理阶段返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429），升档后调大配置即可；
  - 活跃口径含 linger 窗口与等首包（Runtime 未启动视为 0）；受理存在并发轻微竞态（软上限），越界订阅由上游 `subscribe-status.fails` 兜底；
- **断线与拒订**：连接断开、静默看门狗超时（45 秒无任何报文，含心跳应答）、上游拒订（`subscribe-status.fails`，仅该 symbol）与停机均以 `upstream_disconnected` 显式事件收尾——绝不静默断流/挂死；
  - 断线后退避重连并对全部活跃引用自动重订（重订免费，首包恢复后广播 `subscribed`）；建连失败同样退避重试（不致命）；`websockets` 库缺失为唯一致命错误（等待者立即失败）；
- 字段口径：
  - `timestamp` 为**分钟桶** Unix 秒（对齐分钟边界，非 tick 时刻；事件到达频率实测约 1 条/秒），按 UTC 解释统一北京时间 `+08:00` 输出；
  - 标准字段仅 `timestamp`/`last_price`——五档盘口、`amount`、`volume` 上游 price 事件无源字段，**恒 null**（文档贵金属示例的 bid/ask 不可推广到货币对）；
  - 缺时间戳/缺价格属上游契约违背，返回 `UPSTREAM_ERROR`（不伪造时间或价格）；
- 受理校验（operation 路由、凭据、标的映射、容量闸门）急切执行——错误在响应头发出前统一 JSON 错误结构；订阅在流首次迭代时进行；迭代器关闭释放引用并注销监听器；停机钩子返回异步停机任务由 lifespan 等待完成（防止 WS 关闭半途被取消）；
- SSE 三类事件帧结构、coalesce 丢旧保新（只约束 quote 帧）、心跳与客户端容量闸门等通用约定见 `../api.md`（twelvedata 无连接模式概念，流不受模式限制）。

## 缺省路由参与

- bars：GLOBAL `forex`/`crypto` 承担缺省（tiingo 回退）；US `etf` 独占；US `stock` 承担缺省（优先位）；
- quotes：GLOBAL `forex`/`crypto` 承担缺省（tiingo 回退，快照/流走 WS 订阅、不占 tiingo REST 配额）；
- 位次依据（一句话）：GLOBAL 外汇/加密货币与美股是该 Provider 的原生能力面（12 周期全量矩阵 + WS 实时），CN/HK 不声明不参与；
- 当前回退链顺序与组合覆盖以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表；凭据未配置时本 Provider 自动跳过，由回退链下一位生效。

## extensions 白名单

- quotes（WS price 事件，快照与流帧同口径）：
  - `currency_base` / `currency_quote` / `exchange` / `type` 四个字符串字段（缺失不透出；实测外汇 EUR/USD 无 `exchange`、加密 BTC/USD 有且为聚合源交易所名）；
  - 数值类扩展字段不透出；
- bars：无 extensions。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/twelvedata.yaml`（探针框架规范见 `probes/README.md`）：
  - 覆盖可用性矩阵、复权生效性（adjust_diff，stock/etf 持续回归入链）、历史深度（history_depth）、单次上限（limit）、错误指纹（raw）；
  - 免费档限频下 pacing 10 秒；
- 各轮运行记录：`probes/results/PROBE_LOG.md`（本地文件，不入库）。
