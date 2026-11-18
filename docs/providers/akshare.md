# AkShare（akshare）

国内公开数据聚合库接入：CN 个股/指数/ETF、HK/US 个股与 CN 期货的历史 K 线，多数据源变体（东方财富/新浪/腾讯），免凭据恒启用。

## 概要

- 接入方式：Python 聚合库 `akshare`（内部聚合东方财富、新浪、腾讯等公开数据源），接口签名均已按官方文档核实：<https://akshare.akfamily.xyz/>；
- SDK：pyproject extras `akshare`（`akshare>=1.14`，可选依赖）。SDK 只在适配器内部延迟导入，未安装时返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动；同步阻塞调用经线程池隔离，不阻塞事件循环；
- 凭据：无凭据概念，恒启用（registry 不登记凭据，凭据推断恒通过）；`TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 可显式禁用。环境变量全表见 ../operations.md；
- 变体体系：变体为上游数据源（`eastmoney` 东方财富 / `sina` 新浪 / `tencent` 腾讯）；`default_variant` 按资产类型声明：stock/index/etf → `eastmoney`，contract/continuous → `sina`。

## 能力与上游映射

以 `src/tapehub/providers/akshare/manifest.yaml` 的 capabilities/operations 为准（六维 match 全显式枚举；上游接口名仅存在于本适配器与 manifest，不暴露到公共 API）：

| data_type | 市场 | asset_type | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bars | CN | stock | eastmoney | `stock_zh_a_hist` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | CN | stock（BSE） | eastmoney | `stock_zh_a_hist` | P1D/P1W/P1M | none/qfq/hfq | 同上 |
| bars | CN | stock | sina | `stock_zh_a_daily` | P1D | none/qfq/hfq | 同上 |
| bars | CN | stock | tencent | `stock_zh_a_hist_tx` | P1D | none/qfq/hfq | 同上 |
| bars | CN | index | eastmoney | `index_zh_a_hist` | P1D/P1W/P1M | none | open/high/low/close/volume/amount |
| bars | CN | index | sina | `stock_zh_index_daily` | P1D | none | 无 amount |
| bars | CN | index | tencent | `stock_zh_index_daily_tx` | P1D | none | 无 amount（上游 amount 列语义为成交量，映射到 volume） |
| bars | CN | etf | eastmoney | `fund_etf_hist_em` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | HK | stock | eastmoney | `stock_hk_hist` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | US | stock | eastmoney | `stock_us_hist` | P1D/P1W/P1M | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | CN | contract | sina | `futures_zh_daily_sina` | P1D | none | 无 amount（extensions: hold/settle） |
| bars | CN | continuous | sina | `futures_zh_daily_sina` | P1D | none | 无 amount（extensions: hold/settle） |

- 东财 kline 族接口（个股/ETF/指数/港美股）同上游 kline 端点、同中文列：指数 `klt` 101/102/103 对应日/周/月；ETF 无「股票代码」列（dict 按列名取值天然兼容，列映射与量纲换算复用个股配置）；港美股同端点同列。

history.start（历史覆盖声明，探针实测回填；二分近似窗口粒度 90 天、complete=True，approx ±90 天——真实最早可能更早；同上游端点同数据区间的周期与 P1D 同值声明）：

| 资产类型（市场 / 变体） | 周期 | history.start（北京时间零点） | 说明 |
| --- | --- | --- | --- |
| stock（CN，三源） | eastmoney P1D/P1W/P1M；sina/tencent P1D | 1990-12-19 | R3（实测首根=上市/开业日） |
| index（CN，三源） | eastmoney P1D/P1W/P1M；sina/tencent P1D | 1991-01-01 | R4 |
| etf（CN，eastmoney） | P1D/P1W/P1M | 2005-03-01 | R4 |
| stock（HK，eastmoney） | P1D/P1W/P1M | 2000-01-01 | R6（HK 同 US 归一） |
| stock（US，eastmoney） | P1D/P1W/P1M | 2000-01-01 | R1 |
| contract（CN，sina） | P1D | null（2026-11-16 决议） | 合约级数据随挂牌起、随摘牌止，无资产类级固定起点——原 RB2410 全量锚定值 2023-10-17 随「合约类不统计历史深度」决议废止 |
| stock（CN，BSE） | eastmoney P1D/P1W/P1M | 1990-12-19 | R3（库级口径沿用 CN stock 声明；BSE 标的均 2020 年后上市，各标的数据以自身上市日起。仅 eastmoney 变体，sina/tencent 未探针不声明） |
| continuous（CN，sina） | P1D | 2009-03-01 | R4 |

## 标的代码映射

标准代码 `{market}-{exchange}-{symbol}` → 上游 symbol：

- 个股：eastmoney 用纯 6 位代码（SSE/SZSE/BSE——BSE 920 段 2026-11-17 探针实证已收录，纯 6 位直通上游按市场判定）；sina/tencent 用带市场前缀符号（`SSE→sh`、`SZSE→sz`，如 `sh600000`；仅 SSE/SZSE，BSE 未探针不声明）；
- 指数：eastmoney（`index_zh_a_hist`）为**纯 6 位代码**（`CN-SSE-000001 → 000001`），上游按指数代码映射表自动判定市场（SSE/SZSE/CSI/BSE 探针实测覆盖；映射表首次调用拉取，进程内缓存）；sina/tencent 为带前缀符号（如 `sh000001`），仅支持 SSE/SZSE；
- ETF（eastmoney）：纯 6 位代码原样传递（`CN-SSE-510050 → 510050`、`CN-SZSE-159915 → 159915`），上游按代码首位自动判断沪深（5/6 开头→沪、其余→深）；
- 港股（eastmoney）：5 位数字代码，短代码零填充归一（`HK-HKEX-700 → 00700`，上游 secid `116.{symbol}`）；非 HKEX 或非纯数字代码拒绝；
- 美股（eastmoney）：东财市场号.代码（`NASDAQ→105`、`NYSE→106`，如 `US-NASDAQ-AAPL → 105.AAPL`、`US-NYSE-GE → 106.GE`）；ticker 大写归一，含点 ticker（如 `BRK.B`）原样透传；**CBOE/OTC 不支持**（市场号归属未核实，不声明不映射）；
- 期货（新浪）：单一合约符号原样传递（如 `RB2410`）；主力连续为品种代码 + `0`（如 `RB → RB0`）。CZCE 实证双形态直通：标准式 4 位 `MA2701`（2026-11-17 聚焦轮，93 根真实日线）与 3 位旧写法 `MA701`（2026-11-16 聚焦轮）新浪均收、无需还原——与 tushare（只认 4 位）、tqsdk/ths（只认 3 位，适配器 4→3 还原）形成三家三形态对照；域层自 2026-11-17 起 3 位旧写法构造期拒绝（见 ../api.md「CZCE 期货合约年月形态」），网关输入恒为 4 位；
- 查表无果的 exchange 明确拒绝（`UnsupportedCapabilityError`），不透传上游。

## 周期 / 复权 / 单位映射

- 周期映射（东财 kline 族通用，个股/ETF/指数/港美股）：`P1D→daily`、`P1W→weekly`、`P1M→monthly`；新浪/腾讯个股与指数接口、新浪期货均为仅日线（上游无周期参数）；声明之外的周期显式拒绝；
- 复权映射：`none→""`、`qfq→qfq`、`hfq→hfq`。上游 `stock_zh_a_hist` 的 `adjust` 默认 `""`（不复权），与 tapehub 声明默认值（个股 qfq）不一致，适配器始终显式传参；指数三接口均无复权参数（仅声明 none）；期货仅 none；
- 单位：
  - A 股个股与 ETF（东财）：volume 上游单位为**手**，标准化为**股**（×100；换算后成交额÷成交量≈价格，探针实证）；新浪/腾讯个股为股，原样返回；
  - 指数：东财/腾讯上游单位为**手**，**原样透出不换算**（×100 交叉验证成立，保持指数三源口径一致）；新浪指数 volume 单位未核实，原样透出；
  - 港股/美股（东财）：volume 单位经探针实证为**股**（成交额÷成交量≈价格），原样透出、无需换算；
  - 期货（新浪）：上游未标注单位，volume/hold/settle 原样返回；`amount` 统一为元（本 Provider 期货无 amount 标准字段）。

## 上游行为定案

- 时间戳口径：日/周/月线时间戳为交易日 00:00；港股交易日=北京同日（北京时间零点）；美股为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移，与 twelvedata 同口径）；输出统一北京时间 `+08:00`；
- 分页：akshare 接口均不支持原生分页，由适配器用共享组件在内存切片（不落盘）；统一分页由共享 `paginate_bars` 完成（跨 Provider 通用分页语义见 ../api.md）；上游按日期过滤的边界由适配器在标准化后按闭区间 `[start, end]` 精确复核；
- 全量拉取接口（适配器闭区间内存复核兜底）：
  - 新浪指数（`stock_zh_index_daily`）、新浪期货（`futures_zh_daily_sina`）：上游无日期参数，返回全量历史；
  - 腾讯指数（`stock_zh_index_daily_tx`）：支持日期参数（start_date/end_date 显式传递）；
  - 东财港股/美股（`stock_hk_hist`/`stock_us_hist`）：上游忽略 beg 日期参数（`lmt=1000000` 全量拉取客户端切片），日期参数仍显式传递；每次调用拉全量历史的性能成本与新浪指数/期货接口相同；
- 错误处理：SDK 异常统一包装为 `UPSTREAM_ERROR`，不穿透 API（错误码表见 ../api.md）；
- 复权跨源一致性（探针实证）：未复权（none）三源完全一致（指数新浪源多一位小数，±0.005 属表示层精度差）；复权序列跨源不保证一致——qfq 东财与腾讯一致、新浪复权因子口径不同（约 ±0.24 元）；hfq 三源基准各异（最大约 ±105 元）。**复权序列跨源不可直接混用，跨源对比仅 none 可靠**；
- 未接入范围（诚实边界，不编造能力）：
  - `futures_hist_em`（东财期货）需要**中文合约名**（如「热卷主连」），无法从标准标的代码映射，不接入；
  - `fund_etf_hist_min_em`（东财 ETF 分钟线）不接入：1 分钟走 trends2 仅近 5 个交易日、5/15/30/60 分钟 kline 分支无服务端日期过滤（每次全量拉取客户端切片）；CN ETF 分钟族已由 Baostock 声明，重叠价值低；
  - 加权（`weighted-index`）在上列接口文档中没有符号说明，暂不声明能力。

## 快照与流式

不参与实时快照/流式：manifest 无 quotes 能力，适配器不实现 `fetch_quote`/`stream_quote`；`/v1/market/quotes` 与 `/v1/market/quotes/stream` 均不会路由到 akshare。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色（**仅 CN bars 五链尾位**，不参与 HK/US 与任何快照组合）：

- bars CN stock / index / etf：尾位（免凭据兜底）；
- bars CN contract / CN continuous：尾位（新浪源）；
- quotes：不参与任何快照组合。

位次依据一句话：免凭据恒可用，居回退链兜底位（在前排免费同侪——baostock/ths——全部不可用时才兜底）。

**成本警示**（审查项 M5）：部分 operation 为**全量拉取型**——`stock_zh_index_daily`（新浪指数）、`futures_zh_daily_sina`（新浪期货）、`stock_hk_hist`（港股，上游忽略日期参数）上游不支持时间过滤，每次请求传输标的**完整历史**并由适配器内存复核窗口：单请求的上游流量/延迟/CPU 与查询窗口大小无关（新浪指数约 9,000+ 行起）。且 akshare 背后为东财/新浪非官方接口（无 SLA），高频调用有触发上游 IP 风控的实际风险——生产不建议将其配置为缺省数据源（当前 yaml 尾位安排即为该结论的落地）。

## extensions 白名单

| 上游接口族 | extensions 字段 |
| --- | --- |
| 东财 kline 族（个股/ETF/指数/港美股） | `amplitude`（振幅）、`pct_change`（涨跌幅）、`change`（涨跌额）、`turnover_rate`（换手率） |
| 新浪个股（`stock_zh_a_daily`） | `outstanding_share`（流通股）、`turnover`（换手率） |
| 腾讯个股（`stock_zh_a_hist_tx`） | `turnover`（换手率） |
| 新浪期货（`futures_zh_daily_sina`） | `hold`（持仓量）、`settle`（结算价） |
| 新浪指数 / 腾讯指数 | 无 |

白名单之外的 Provider 特有列一律不透出；缺失值与 NaN 统一映射为 null（不用 0 冒充），extensions 值转换后为 null 不写入。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/akshare.yaml`（探针框架规范见 `docs/probes.md`）；
