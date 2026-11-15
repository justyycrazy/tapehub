# AlphaFeed（alphafeed）

海外市场行情 REST API 接入：CN/HK/US 三市场个股 + CN 指数/ETF 与 US ETF 日/周/月 K 线（2026-11-15 扩容）+ 实时快照，HK 个股 K 线、HK/US 个股快照与 US ETF K 线的缺省源。

## 概要

- 接入方式：官方 REST API（`GET https://api.alphafeed.org/v1/klines` / `/v1/quotes`），官方文档 <https://docs.alphafeed.org/zh-hans/>（接口签名均已按官方文档核实）；
- SDK：官方 Python SDK `alphafeed`（pyproject extras `alphafeed`，可选依赖）。客户端 `AlphaFeed(api_key=...)`，K 线方法 `af.klines.get(...)`、快照方法 `af.quotes.get(symbols=...)`；SDK 只在适配器内部延迟导入，未安装时返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动；
- 凭据：`TAPEHUB_ALPHAFEED_API_KEY`，只经内存传给 SDK 客户端（禁止写入代码/manifest/日志/异常响应）。未配置时不启用（能力目录仍可见 `enabled=false`），显式请求返回 `PROVIDER_DISABLED`；环境变量全表见 ../operations.md；
- 变体体系：单接口 Provider，变体与 `default_variant` 均为 `alphafeed`（数据源即上游服务自身）；缺省请求即可路由，也可显式传 `variant=alphafeed`。

## 能力与上游映射

以 `src/tapehub/providers/alphafeed/manifest.yaml` 的 capabilities/operations 为准（六维 match 全显式枚举）：

| data_type | 市场 | asset_type | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bars | CN/HK/US | stock | alphafeed | `af.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | CN | index | alphafeed | `af.klines.get` | P1D/P1W/P1M | none | open/high/low/close/volume/amount |
| bars | CN | etf | alphafeed | `af.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | US | etf | alphafeed | `af.klines.get` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| quotes | CN/HK/US | stock | alphafeed | `af.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount |

历史覆盖（history.start，探针二分收敛，取估计窗口右端零点，approx ±90 天、真实最早可能更早；三市场周期集相同但历史深度不同，故 bars 能力拆 CN/HK/US 三块逐市场声明，operations 不受块拆分影响）：

| 市场 | history.start（北京时间零点） | 二分真值带 | 备注 |
| --- | --- | --- | --- |
| CN | 1991-01-14 | (1990-10-16, 1991-01-14] | 含上交所开业日 1990-12-19 |
| HK | 1980-02-15 | (1979-11-17, 1980-02-15] | 与 tickflow 独立探测结果同日一致，两家上游历史库大概率同源 |
| US | 1962-02-17 | (1961-11-19, 1962-02-17] | 同上 |

quotes 为快照能力：interval/adjust 维度无意义，capabilities 与 operations match 中显式空/null，路由跳过对应维度。

## 标的代码映射

标准代码 `{market}-{exchange}-{symbol}` → 上游 `代码.市场后缀`（示例均已按官方文档核实）：

| exchange 段 | 后缀 | 示例 |
| --- | --- | --- |
| SSE | SH | CN-SSE-600519 → 600519.SH |
| SZSE | SZ | CN-SZSE-000001 → 000001.SZ |
| BSE | BJ | CN-BSE-920047 → 920047.BJ |
| HKEX | HK | HK-HKEX-00700 → 00700.HK |
| NYSE / NASDAQ / CBOE | US（美股多交易所统一后缀） | US-NASDAQ-AAPL → AAPL.US |

查表无果的 exchange 明确拒绝（`UnsupportedCapabilityError`），不透传上游。

## 周期 / 复权 / 单位映射

- 周期映射：`P1D→1d`、`P1W→1w`、`P1M→1M`；上游按毫秒时间戳过滤（`start_time`/`end_time` 含端点），`count` 恒传单次上限 10000。
- 不声明的周期及原因：
  - 分钟族（上游枚举 1m~60m）：需上游套餐单独开通「分钟K线权限」，账户套餐未包含；且 alphafeed 是 CN stock bars 的缺省 Provider，路由执行失败不回退，声明无权限的周期会 403 打断本可由其他 Provider 服务的请求；
  - `10m`：官方文档周期枚举无 10m，但上游实测接受并返回数据（文档与实测不符，以实测为准），仍维持不声明；
  - 季线 `1Q`：无法用 ISO 8601 Duration 表达；年线 `1Y`：超出项目「最大支持到月线」的周期上界。均不声明。
- 复权映射：`qfq→forward`（前复权）、`hfq→backward`（后复权）、`none→none`；上游另有 `forward_additive`/`backward_additive` 差值加法变体，不对外暴露。上游自身默认 forward、period 默认 1d，均与网关声明默认值无关，适配器始终显式传参。**ETF 复权生效**（探针 2026-11-15 目录外诊断：forward vs none 全窗 360 根差异、与 tickflow 侧逐根一致；chain adjust_diff 常态复验）；**US ETF 三复权生效**（目录外诊断 2026-11-15：none/qfq/hfq 两两全窗差异、与 tickflow 侧逐位一致，见「上游行为定案」）；**指数无复权**仅声明 none（复权对指数无意义，与 baostock/tushare 先例同口径）。港股/美股**个股**复权语义尚未探针实证。
- 单位：无换算，上游 volume/amount 原值透出。CN 指数/ETF 与 tushare 同窗互证定案口径：`vol` 手、`amount` 元（与 CN stock 日线一致，探针 2026-11-15 第 7/8 轮）。

## 上游行为定案

- 时间戳口径：K 线 `timestamp` 为毫秒；CN/HK 日线为北京时间零点，US 日线为美东零点（归一化北京时间随夏令时在 12:00/13:00 漂移）；输出统一北京时间 `+08:00`。
- HK/US 日线 `amount` 上游原值 0（无成交额数据，按原值透出、非缺失 null），与 tickflow 口径一致。
- K 线响应为列式结构（各数组等长、同下标对应同一根 K 线）；缺失值与 NaN 统一映射 null（不用 0 冒充）。
- CN 指数/ETF 扩容定案（探针 2026-11-15 第 7/8 轮）：统一 klines 端点对指数/ETF 代码不做证券类型路由拒绝——000001.SH/510050.SH 日线 118 根、周/月线均有数；代码映射与 stock 完全共用（后缀表零改动），映射层/适配器零分支。指数/ETF history 未核实保持 null（chain 二分后回填）。
- US ETF 扩容定案（目录外诊断 2026-11-15 第 9 轮）：统一 klines 端点对美股 ETF 标的（SPY.US/QQQ.US，交易所段无关）不做证券类型路由拒绝——P1D 2025 全窗 250 根直测放行；none/qfq/hfq 三复权生效且与 tickflow 侧逐位一致（同源引擎再证）；**复权锚定为上游全序列端点而非查询窗口端点**（hfq 与 qfq 差常数比例 ≈0.5476，同一套分红因子两种锚定，窗口内末根可 ≠ 原始价）；量额同 US stock 日线口径（`amount` 上游原值 0）。history 未核实保持 null（chain 二分后回填）。
- 截断/分页：上游单次上限 10000 根，达到上限时无法确认是否被截断，诚实标记 `meta.partial=true` 并经 warnings 提示可能截断；统一分页由共享 `paginate_bars` 完成，闭区间 `[start, end]` 在标准化后精确复核（上游按毫秒时间过滤，网关再做区间复核）。
- 错误指纹：
  - 上游 429（限频，响应含 `retry_after_ms`）→ `UPSTREAM_RATE_LIMITED`（HTTP 429，`retry_after_ms` 可得时透传 details）；SDK 对 429/5xx 自带最多 3 次退避重试，映射发生在重试耗尽后；
  - 401/403 等其余上游异常统一 `UPSTREAM_ERROR`（SDK 异常类型不穿透 API）；无效 Key 指纹为 SDK `AuthenticationError`「Invalid or expired API key」（探针实证）；
  - 无效标的上游返回 200 空数组，适配器按契约违背抛 `UPSTREAM_ERROR`（不静默返回空结果）。
- 快照契约违背：`timestamp`（毫秒）缺失/非法、`last_price` 不可解析抛 `UPSTREAM_ERROR`（不伪造时间或价格）；批量包装形态含多个 symbol 条目时无法判定唯一标的，显式拒绝（不静默取第一个）。

## 快照与流式

- 快照（`af.quotes.get(symbols=[...])`）：上游支持 5 标的/次批量与标的池（universes），网关为单标的快照语义、单元素 symbols 调用（套餐限频 10 次/分钟才是实际约束）；
- 标准字段：`timestamp`/`last_price`/`volume`/`amount`；五档盘口上游无源字段恒 null（不用 0 冒充）；比例类扩展字段为小数比例（1% = 0.01）；
- 交付模型：纯 REST 单次调用语义——无推送接口，无首包超时/`QUOTE_NOT_READY`（调用失败统一 `UPSTREAM_ERROR`）；
- 流式：不实现，`/v1/market/quotes/stream` 路由到 alphafeed 返回 `UNSUPPORTED_CAPABILITY`（与 tickflow 一致）。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars CN stock：回退位（免费无凭据的 baostock 居首）；
- bars CN index / bars CN etf：参与（2026-11-15 扩容，位次与 CN stock 链同序——凭据型源居免费无凭据源之后）；
- bars HK stock：缺省（官方文档核实港股日/周/月线后接管）；
- bars US stock：缺省（居首，twelvedata/futunn/akshare 依次回退）；
- bars US etf：缺省（2026-11-15 扩容，位次与 US stock 链同序——目录外诊断实证后入链，分钟级请求经能力过滤落到 twelvedata）；
- quotes CN stock：回退位（ths 居首）；
- quotes HK stock / US stock：缺省。

位次依据一句话：官方文档核实的三市场日/周/月 K 线与三市场个股快照能力，凭据型源居免费无凭据源之后；US ETF 缺省与 US stock 链同序（目录外诊断实证后入链）。

## extensions 白名单

- bars：`prev_close`（前收盘价，上游可选字段，存在且非空时透出）；
- quotes 顶层：`region`（市场区域，字符串）、`prev_close`/`open`/`high`/`low`（数值）；
- quotes `ext` 块：`name`（标的名称，字符串）、`amplitude`/`change_amount`/`change_pct`/`turnover_rate`（数值，比例为小数）；`ext.type`（如 `cn_equity`）不在白名单、不透出。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/alphafeed.yaml`（探针框架规范见 `probes/README.md`）；
- 各轮运行记录：`probes/results/PROBE_LOG.md`（本地文件，不入库）。
