# TickFlow（tickflow）

三市场行情 REST API 接入：CN/HK/US 个股 + CN 指数/ETF 与 US ETF 日/周/月 K 线与实时快照（2026-11-15 扩容，无状态 REST 单次调用），凭据型回退源。

## 概要

- 接入方式：REST API（`GET https://api.tickflow.org/v1/klines` / `GET /v1/quotes`），官方文档 <https://docs.tickflow.org/zh-hans/>（接口签名均已按官方文档核实）；
- SDK：官方 Python SDK `tickflow`（pyproject extras `tickflow`，可选依赖）。客户端 `TickFlow(api_key=...)`，K 线方法 `tf.klines.get(...)`、快照方法 `tf.quotes.get(symbols=...)`；SDK 只在适配器内部延迟导入，未安装时返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动；同步调用经线程池隔离，不阻塞事件循环；
- 凭据：`TAPEHUB_TICKFLOW_API_KEY`，只经内存传给 SDK 客户端（禁止写入代码/manifest/日志/异常响应）。未配置时不启用（能力目录仍可见 `enabled=false`），显式请求返回 `PROVIDER_DISABLED`。环境变量全表见 ../operations.md；
- 变体体系：单接口 Provider，变体与 `default_variant` 均为 `tickflow`（数据源即上游服务自身）；缺省请求即可路由，也可显式传 `variant=tickflow`。

## 能力与上游映射

以 `src/tapehub/providers/tickflow/manifest.yaml` 的 capabilities/operations 为准（六维 match 全显式枚举）：

| data_type | 市场 | asset_type | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bars | CN/HK/US | stock | tickflow | `tf.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | CN | index | tickflow | `tf.klines.get` | P1D/P1W/P1M | none | open/high/low/close/volume/amount |
| bars | CN | etf | tickflow | `tf.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | US | etf | tickflow | `tf.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| quotes | CN/HK/US | stock | tickflow | `tf.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount |
| quotes | CN | index/etf | tickflow | `tf.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount |
| quotes | US | etf | tickflow | `tf.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount |

- bars 能力按 CN/HK/US **三市场拆块逐市场声明**（三市场周期集与复权相同、历史深度不同，合并声明会以跨市场最早值对历史较浅的市场过度声明；capabilities 与 history.start 一一对应，operations 本就按市场枚举、不受块拆分影响）；
- quotes 为快照能力：interval/adjust 维度无意义，capabilities 与 operations match 中显式空/null，路由跳过对应维度。

history.start（历史覆盖声明，探针二分收敛，早上市基准标的，90 天窗口粒度、complete=True；取估计窗口右端零点，approx ±90 天——真实最早可能更早；日/周/月三周期同上游端点同数据区间，同值声明）：

| 市场 | history.start（北京时间零点） | 二分真值带 |
| --- | --- | --- |
| CN | 1991-01-18 | [1990-12-19, 1991-01-18]（不早于上交所开业日） |
| HK | 1980-02-15 | (1979-08-19, 1980-02-15] |
| US | 1962-02-17 | (1961-09-23, 1962-02-17] |

基准标的：CN 600602（上交所开业首批）、HK 00002、US GE（1892 年上市）——基准标的为早上市标的，二分收敛点若远离其上市日即为真实库底。

## 标的代码映射

标准代码 `{market}-{exchange}-{symbol}` → 上游 `代码.市场后缀`。后缀按市场段取：HK/US 为市场级（美股多交易所统一 US 后缀，exchange 段不参与上游语义），CN 按交易所段消歧。market/exchange 组合一致性由域层标准代码词汇表校验（矛盾组合如 `CN-NYSE-…` 在参数校验期即拒绝，见 ../api.md「instrument 标准代码」）：

| 市场 | exchange 段 | 后缀 | 示例 |
| --- | --- | --- | --- |
| CN | SSE | SH | CN-SSE-600000 → 600000.SH |
| CN | SZSE | SZ | CN-SZSE-000001 → 000001.SZ |
| CN | BSE | BJ | CN-BSE-920047 → 920047.BJ |
| HK | —（市场段即后缀） | HK | HK-HKEX-00700 → 00700.HK |
| US | —（市场段即后缀） | US | US-NASDAQ-AAPL → AAPL.US |

词汇表内但本 Provider 未接入的组合与未支持市场明确拒绝（`UnsupportedCapabilityError`），不透传上游。

## 周期 / 复权 / 单位映射

- 周期映射：`P1D→1d`、`P1W→1w`、`P1M→1M`；`count` 上游默认 100，适配器恒显式传单次上限 10000；
- 不声明的周期及原因：
  - 分钟族（上游枚举 `1m/5m/10m/15m/30m/60m`）：免费档无分钟 K 线权限；tickflow 是 CN stock bars 的回退候选，声明无权限的周期会 403 打断本可由其他 Provider 服务的请求——去掉声明后 US 分钟 K 由 twelvedata 服务，CN/HK 分钟 K 无人声明、auto 路由诚实返回 `NO_PROVIDER_AVAILABLE`；升档后按探针流程实测再回填；
  - 季线 `1Q`：无法用 ISO 8601 Duration 表达；年线 `1Y`：超出项目「最大支持到月线」的周期上界。均不声明；
- 复权映射：`qfq→forward`（比例前复权）、`hfq→backward`（后复权）、`none→none`；上游另有 `forward_additive`/`backward_additive` 差值加法变体，不对外暴露；SDK 自身默认 forward，适配器始终显式传参；**ETF 复权生效**（探针 2026-11-15 目录外诊断：forward vs none 全窗 360 根差异、与 alphafeed 侧逐根一致；chain adjust_diff 常态复验）；**US ETF 三复权生效**（目录外诊断 2026-11-15：none/qfq/hfq 两两全窗差异、与 alphafeed 侧逐位一致，见「上游行为定案」）；**指数无复权**仅声明 none（复权对指数无意义，与 baostock/tushare/alphafeed 先例同口径）；港股/美股**个股**复权语义未经探针实证，生效性以探针实证为准；
- 单位：无换算，volume/amount 原值透出；HK/US 日线 `amount` 上游原值 0（无成交额数据，按原值透出、非缺失 null）。CN 指数/ETF 与 tushare 同窗互证定案口径：`vol` 手、`amount` 元（与 CN stock 日线一致，探针 2026-11-15 第 7/8 轮）。

## 上游行为定案

- 时间戳口径：K 线与快照 `timestamp` 均为毫秒，输出统一北京时间 `+08:00`；CN/HK 日线为**北京时间零点**，US 日线为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移）；
- K 线响应为列式结构（CompactKlineData，各数组等长、同下标对应同一根 K 线）；缺失值与 NaN 统一映射 null（不用 0 冒充）；
- CN 指数/ETF 扩容定案（探针 2026-11-15 第 7/8 轮）：统一 klines 端点对指数/ETF 代码不做证券类型路由拒绝——000001.SH/510050.SH 日线 118 根、周/月线均有数（与 alphafeed 侧同日同窗互证，两家历史库大概率同源）；代码映射与 stock 完全共用（后缀表零改动），映射层/适配器零分支。指数/ETF history 未核实保持 null（chain 二分后回填）；
- HK/US 指数无覆盖定案（目录外诊断 2026-11-15 第 12 轮，与 alphafeed 同源引擎再证）：10 个候选符号（两套缩写约定）instruments 全未收录，HSI.HK/SPX.US 防御性直测 klines 空、quotes 空 data（如实包装 `UPSTREAM_ERROR`），控制组正常（000001.SH type=index、00700.HK type=stock）；港股段仅收录个股、美股段仅收录可交易证券（stock/etf），CN 指数为唯一指数覆盖，HK/US 指数维持不声明；
- US ETF 扩容定案（目录外诊断 2026-11-15 第 9 轮）：统一 klines 端点对美股 ETF 标的（SPY.US/QQQ.US，交易所段无关）不做证券类型路由拒绝——P1D 2025 全窗 250 根直测放行；none/qfq/hfq 三复权生效且与 alphafeed 侧逐位一致（同源引擎再证）；**复权锚定为上游全序列端点而非查询窗口端点**（hfq 与 qfq 差常数比例 ≈0.5476，同一套分红因子两种锚定，窗口内末根可 ≠ 原始价）；量额同 US stock 日线口径（`amount` 上游原值 0）。history 未核实保持 null（chain 二分后回填）；
- 截断/分页：上游单次上限 **10000 根**，达到上限时无法确认是否被截断，诚实标记 `meta.partial=true` 并经 warnings 提示可能截断；统一分页由共享 `paginate_bars` 完成（跨 Provider 通用分页语义见 ../api.md）；上游按毫秒时间过滤的边界由适配器在标准化后按闭区间 `[start, end]` 精确复核；
- 错误指纹（探针实证）：免费档分钟线无权限为 SDK `PermissionError`「无分钟K线查询权限」（统一包装 `UPSTREAM_ERROR`）；限频实测以 6.5s 间隔（≈9 次/分钟）全程未触发；`RateLimitError` 类型存在于 SDK，429 指纹待更高频场景核实；限频/无权限等 SDK 异常统一包装 `UPSTREAM_ERROR`，不穿透 API；
- 快照响应实测（SDK 0.1.25）：返回 `list[dict]`——文档与实测不符（以实测为准，文档描述为 data 包装结构）；实测未观察到 `session` 字段（文档声明存在；防御性映射，存在才透出）；比例字段为**小数比例（1% = 0.01**，如 change_pct 0.0162 = 1.62%）；上游支持逗号分隔批量（本项目为单标的快照，单标的单次调用）；
- 快照契约违背：`timestamp`（毫秒）缺失/非法、`last_price` 不可解析抛 `UPSTREAM_ERROR`（不伪造时间或价格）；data 数组为空同样抛 `UPSTREAM_ERROR`（不静默返回）。

## 快照与流式

- 快照：无状态 REST 单标的单次 `tf.quotes.get` 调用，无订阅/缓存语义（与订阅-推送类 Provider 不同）；不产生 `QUOTE_NOT_READY`——要么返回快照，要么返回上游错误；
- 快照扩容定案（目录外诊断 2026-11-15 第 11 轮）：统一 quotes 端点对 CN 指数/ETF（SSE/SZSE 两交易所段）与 US ETF（NYSE/NASDAQ 两交易所段）六标的端到端放行，行结构与 stock 快照完全同形（同键集同语义），端到端零改动产出合法标准 Quote（与 alphafeed 侧逐位互证，同源引擎再证）；`ext.type` 维持 `cn_equity`/`us_equity` 不区分证券类型（本就不在白名单）；CN 指数行 `ext` 无 `turnover_rate`（指数无换手）；US ETF 快照 `amount` 为真实成交额（与 US bars 日线 `amount` 原值 0 口径不同）；快照时间戳为最近交易日收盘时刻（CN 15:00、US 美东 16:00，北京时间归一）；
- 标准字段：`timestamp`/`last_price`/`volume`/`amount`；五档盘口上游无源字段，恒 null（不用 0 冒充）；扩展字段见「extensions 白名单」；
- 流式：不支持 SSE 流（不实现 `stream_quote`），`/v1/market/quotes/stream` 受理阶段返回 `UNSUPPORTED_CAPABILITY`。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色（均为回退位）：

- bars CN stock / HK stock / US stock：回退位；
- bars CN index / bars CN etf：参与（2026-11-15 扩容，位次与 CN stock 链同序——凭据型源居免费无凭据源之后）；
- bars US etf：回退位（2026-11-15 扩容，位次与 US stock 链同序——目录外诊断实证后入链，alphafeed 居首）；
- quotes CN stock / HK stock / US stock：回退位；
- quotes CN index / CN etf：回退位（2026-11-15 快照扩容，位次与 quotes CN stock 链同序——ths 居首、alphafeed 居前，目录外诊断实证后入链）；
- quotes US etf：回退位（2026-11-15 快照扩容，位次与 quotes US stock 链同序——alphafeed 居首，futunn 为先入兜底）。

位次依据一句话：凭据型 REST 源，免费无凭据源与先接入源居前；其三市场日/周/月 K 线与快照能力覆盖广、历史库底深（HK/US 深于东财源），居凭据源前列；CN 指数/ETF 与 US ETF 的 K 线与快照扩容位次均与对应 stock 链同序（目录外诊断实证后入链）。

## extensions 白名单

- bars：`prev_close`（前收盘价）、`open_interest`（持仓量）、`settlement_price`（结算价）——均为上游可选字段，存在且非空时透出（个股场景通常仅 `prev_close` 出现，README 映射表亦仅列 prev_close）；
- quotes 顶层：`region`（市场区域，字符串）、`session`（交易时段，字符串；实测未观察到，存在才透出）、`prev_close`/`open`/`high`/`low`（数值）；
- quotes `ext` 块：`name`（标的名称，字符串）、`amplitude`/`change_amount`/`change_pct`/`turnover_rate`（数值，比例为小数）；`ext.type`（如 `cn_equity`）不在白名单、不透出。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/tickflow.yaml`（探针框架规范见 `docs/probes.md`）；
