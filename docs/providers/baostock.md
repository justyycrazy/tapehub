# Baostock（baostock）

证券宝免费数据平台接入：CN 个股/指数/ETF 的日/周/月 K 线与个股、ETF 分钟族，免费无凭据恒启用，CN 个股 K 线缺省源。

## 概要

- 接入方式：官方 Python SDK `baostock`（`bs.login()` → `bs.query_history_k_data_plus()` → `bs.logout()`），接口签名均已按官方文档核实：<https://baostock.com/mainContent?file=pythonAPI.md>；
- SDK：pyproject extras `baostock`（`baostock>=0.9.3`——多节点与 ETF 数据版本，可选依赖）。SDK 只在适配器内部延迟导入，未安装时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动；同步阻塞调用经线程池隔离，不阻塞事件循环；
- 凭据：免费无凭据数据平台（无需注册），恒启用（registry 不登记凭据，凭据推断恒通过）；`TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 可显式禁用。环境变量全表见 ../operations.md；
- 变体体系：单上游函数 Provider，变体与 `default_variant` 均为 `baostock`（stock/index/etf 同名）。

## 能力与上游映射

以 `src/tapehub/providers/baostock/manifest.yaml` 的 capabilities/operations 为准（六维 match 全显式枚举；单一上游函数覆盖全部 K 线查询，按 资产类型 × 周期 × 复权 显式展开）：

| data_type | 市场 | asset_type | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bars | CN | stock | baostock | `bs.query_history_k_data_plus`（内部 operation `history_k_data_plus`） | P1D/P1W/P1M、PT5M/PT15M/PT30M/PT1H | none/qfq/hfq | open/high/low/close/volume/amount |
| bars | CN | index | baostock | 同上 | P1D/P1W/P1M | none（复权 no-op） | open/high/low/close/volume/amount |
| bars | CN | etf | baostock | 同上 | P1D/P1W/P1M、PT5M/PT15M/PT30M/PT1H | none/qfq/hfq | open/high/low/close/volume/amount |

history.start（历史覆盖声明，全局统一口径——四条规则见 docs/probes.md「规范化规则」；R=规则分类，实测细节见探针日志）：

| 资产类型 | 周期 | history.start（北京时间零点） | 说明 |
| --- | --- | --- | --- |
| stock | 日/周/月 | 1991-01-01 | R4 |
| stock | 分钟族（PT5M~PT1H） | 2020-01-01 | R4 |
| index | 日/周/月 | 1991-01-01 | R4 |
| etf | 日/周/月 | 2005-02-23 | R3（实测首根=上市日） |
| etf | 分钟族 | 2026-01-01 | R4 |

## 标的代码映射

- 沪深两市统一「前缀.6 位代码」：`CN-SSE-600000 → sh.600000`、`CN-SZSE-000001 → sz.000001`；stock/index/etf 同格式；
- **北交所（BSE）未支持，不声明**；查表无果的 exchange 明确拒绝（`UnsupportedCapabilityError`），不透传上游。

## 周期 / 复权 / 单位映射

- 周期映射：`P1D→d`、`P1W→w`、`P1M→m`、`PT5M→5`、`PT15M→15`、`PT30M→30`、`PT1H→60`（上游 60 分钟线；项目周期代码 `PT1H ≠ PT60M`，不做等价换算）；
- 分钟族仅个股与 ETF 声明：指数无分钟线（传分钟 frequency 返回空结果不报错）；ETF 分钟族数据自 2026-01-05 起（四周期齐备、形态与个股分钟同构、hfq 复权管线生效，官方口径一致）；
- 周/月线仅每周/月最后一个交易日有数据（探针实证：单日窗口查周线返回空）；
- 复权映射：`none→3`、`qfq→2`、`hfq→1`（上游 adjustflag 默认 "3"，适配器始终显式传参）；上游为**涨跌幅复权法**（官方声明与同花顺/通达信口径不同），复权序列与其他 CN Provider 不保证一致，**跨源对比仅 none 可靠**；
- 复权生效性（探针实证）：个股日线复权差异逐行确认、分钟族真生效、ETF qfq/hfq 生效（跨除息逐行差异）；指数为 no-op（qfq 与 none 零差异，维持 [none]）；
- 单位：volume 上游已是**股**（无需换算，与 akshare 东财「手」×100 不同）；amount 为人民币元；上游行值全为字符串，缺失为空串 `""`（→ null，不用 0 冒充）。

## 上游行为定案

- 会话模型：`login()/logout()` 为模块级全局单连接（单例 socket、非线程安全），适配器以模块级锁串行化「登录→查询→行收集」全程；**懒登录 + 进程内复用**（首次请求登录，此后复用）；查询返回未登录（10001001）或网络类（10002xxx 前缀）错误码时**重登一次并重试一次**，仍失败如实上报；不主动 logout（进程退出即断；baostock ≥0.8.9 已修复退出 socket 未关闭问题）；
- fields 三套按官方「历史行情指标参数」显式写全：日线含 `preclose/turn/tradestatus/pctChg/isST`（估值四项 `peTTM/pbMRQ/psTTM/pcfNcfTTM` 仅个股日线——指数/ETF 请求它们只得空串，按资产类型拆分）；周月线无 `preclose/tradestatus/isST/估值`；分钟线仅 `date/time` + OHLC + 量额；
- 时间戳口径（探针实证）：日/周/月线为交易日 00:00 北京时间；分钟线由 `date + time`（YYYYMMDDHHMMSSsss，交易所行情时间）解析为北京时间，**time 为 bar 终点时刻**（5/15/30/60 分钟首根 time 分别为 0935/0945/1000/1030）；
- 停牌日（探针实证）：OHLC=前收盘价、成交量/额=0（原值透出非缺失）、换手率空串 → null，与官方口径一致；
- 数据更新时间（官方）：日K 交易日 17:30、复权因子 18:00、分钟K 20:00、周K 周六 17:30、月K 每月 1 号 17:30——当日 17:30 前请求当日日线为空属上游入库节奏；
- 单次返回上限 **5000 根**（探针实证：PT5M 全年窗口恰返回最早 5000 根、尾部截断；PT15M/PT30M/PT1H 全年根数均完整）：返回行数达 5000 时响应 `meta.partial=true` 且 `warnings` 提示（窗口起点侧完整、尾部取不到，调用方需缩小窗口）；上游无条数上限参数（服务端分批经 `rs.next()` 对调用者透明，分批装配完整性经 2026-11-17 复核实证，见下条）；
- **「宽窗口分钟查询末日前日截断」——2026-11-17 下午复核推翻，上游无此缺陷（第 1 轮误判纠正）**：上午轮据「全年窗口（裸日期）PT15M 3872=242 天×16 < 243 天×16、PT30M/PT1H 同构」判定上游分批交付丢尾批末日；下午四轮聚焦探针 + 目录外专项复验**推翻该结论**：
  - **误判根因**：探针 targets 窗口为裸日期，链路按项目时间约定以**零点**解释（`end=2025-12-31T00:00:00+08:00`，`probes/loader.py` `_to_beijing`）；分钟 bar 时间戳在当日 09:35~15:00、全部晚于零点 end，被适配器闭区间复核**正确**过滤——3872/1936/968 恰为「完整数据 − 末日当日 bar」（3888−16 / 1944−8 / 972−4，242=243−1 个交易日），日线 243 根齐全（日线 bar 时间戳为零点、恰等于 end 不被过滤）；
  - **决定性反证**：直连 raw 各跨度（1/2/3/4/6/9/12 个月 × 四分钟周期，含年中锚点与未来端对照）经 `get_data` 计数全部完整（PT30M 全年 1944、PT1H 972，末根 12-31T15:00）；目录外专项以全日时域 end（`2025-12-31T23:59:59`）经原始适配器单次调用查询 PT15M 全年：**3888 根、末根 2025-12-31T15:00、`partial=false`**——上游单次调用（含跨 2000 行的服务端分批、SDK `next()` 逐行消费拼装）交付完整，无需任何适配器侧分片/重登/核验防线（曾按上午结论实现的防线已全部回退）；
  - 保留的真实事实：① SDK `get_data()` 对超 2000 行结果在 pandas 2 下抛 `DataFrame.append` AttributeError（pandas 2 已移除该方法）——**仅影响 `get_data` 取数路径**（探针 raw 链等直连场景 >2000 行时无法计数），适配器 `next()/get_row_data()` 逐行消费不受影响且完整；SDK 翻页线 `BAOSTOCK_PER_PAGE_COUNT=2000`（某页恰 2000 行时客户端才请求下一页，翻页为客户端驱动、服务端单页亦可超 2000 行）；② SDK `login()` 每次调用新建 socket 且不关闭旧连接（适配器懒登录复用单连接，符合既有会话模型定案）；
  - **探针解读教训**：分钟族 availability 的裸日期窗口计数天然不含 end 当日盘中 bar（日线族含零点 bar 故含当日），跨族对比窗口覆盖时必须以有效时域折算期望值——本教训与证据链记录于 `probes/results/PROBE_LOG.md` 2026-11-17 第 2 轮；上午轮第 1 轮条目中「上游分批交付丢失尾批末日」的机理表述作废，以本条为准。同日晚探针 loader 裸日期 end 归一改为当日末 23:59:59.999999（docs/probes.md「决策与变更记录」），该陷阱自口径层面消除，此后分钟族窗口计数含 end 当日；
- 分钟族重查询时延波动大（探针实证 2026-11-17：同 case 轮间 8.4s→62s→7.5s，PT5M 12~38s，单轮 5 次贴 60s 线超时——既往 22~49s 口径的波动范围已被突破）——探针与调用方超时阈值需大幅留余量（探针 canonical pacing 超时已放宽至 120s）；
- 错误模型：上游经字符串 `error_code/error_msg` 返回（"0"=成功，不抛异常），非零统一包装 `UPSTREAM_ERROR`（details 透出 `upstream_error_code`/`upstream_error_msg`；本 Provider 无凭据可泄露）；**空结果语义（探针实证）**：指数 + 分钟 frequency、无效代码、超范围日期、无效周期均返回空结果集不报错（错误码仅 SDK stderr 打印），适配器按空批次如实透出；
- IP 黑名单机制（错误码 10001011，访问规则见 baostock.com/blacklist；具体限频未公布，探针以 2s pacing 保守节流）；
- 上游文档矛盾点（探针实证）：参数说明章节写 `start/end`、示例代码写 `start_date/end_date`——SDK 函数签名只接受 `start_date/end_date`（传 `start` 报 TypeError），适配器按示例风格实现；`end` 闭/开区间两页文档矛盾——**闭区间（包含）**（单日窗口各返回 1 行），适配器按闭区间实现；
- 分页：上游不支持原生分页，统一分页由共享 `paginate_bars` 完成（跨 Provider 通用分页语义见 ../api.md）；上游按日期过滤的边界由适配器在标准化后按闭区间 `[start, end]` 精确复核。

## 快照与流式

不参与实时快照/流式：manifest 仅声明 bars 能力，适配器不实现 `fetch_quote`/`stream_quote`；`/v1/market/quotes` 与 `/v1/market/quotes/stream` 均不会路由到 baostock。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars CN stock：**缺省**（回退链首位）；
- bars CN index：回退位补位（指数官方口径浅于缺省源——浅库底源置首位会在路由选择阶段截胡更早区间，回退只在路由选择阶段发生）；
- bars CN etf：回退位（缺省源凭据未配置时自动跳过，本 Provider 即为生效缺省）；
- quotes：不参与任何快照组合。

位次依据一句话：免费无凭据、个股库底深（官方口径 1990-12-19 起深于既有各源）且分钟族免费有权限，CN 个股缺省。

## extensions 白名单

按 (资产类型, 周期族) 选择（值为空转换后 null，不写入）：

| 场景 | extensions 字段 |
| --- | --- |
| 个股日线 | `prev_close`（前收盘）、`turnover_rate`（换手率）、`pct_change`（涨跌幅）、`trade_status`（交易状态）、`is_st`（ST 标记）、`pe_ttm`/`pb_mrq`/`ps_ttm`/`pcf_ncf_ttm`（估值四项） |
| 指数日线 | `prev_close`、`pct_change` |
| 周月线（个股/指数/ETF） | `turnover_rate`、`pct_change` |
| 分钟线 | 无 |

## 探针依据

- 能力核实与行为实证用例：`probes/cases/baostock.yaml`（探针框架规范见 `docs/probes.md`）；
