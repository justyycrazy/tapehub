# 同花顺金融数据 API（ths）

同花顺付费金融数据 REST API 接入：CN 全资产（个股/指数/ETF/期货/期权）日线与实时快照，CN 期权 bars/quotes 的独占 Provider，其余组合居回退链末位。

## 概要

- 接入方式：无官方 Python SDK——httpx 同步客户端直连 REST（GET `https://fuyao.aicubes.cn/...`），认证经 `X-api-key` 请求头（key 不落 URL）；接口签名按官方文档核实（overview、prices、a-share-index、fund-market、futures-prices、options-prices 六页，`https://fuyao.aicubes.cn/docs/api-reference/...`）；
- 依赖：pyproject extras `ths` = `httpx>=0.27`（可选依赖，延迟导入；未安装返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动）；同步调用经 `call_in_thread` 隔离到线程池；
- 凭据：`TAPEHUB_THS_API_KEY`（禁止写入代码/manifest/日志/异常响应）；未配置时不启用（能力目录仍可见 `enabled=false`），显式请求返回 `PROVIDER_DISABLED`；付费凭据 API，缺省路由置各组合回退位末位；环境变量全表见 ../operations.md；
- 变体体系：单体系 Provider，变体与 `default_variant` 均为 `ths`（数据源即上游服务自身）；缺省请求经回退链裁决，也可显式传 `variant=ths`。

## 能力与上游映射

以 `src/tapehub/providers/ths/manifest.yaml` 的 capabilities/operations 为准（全部 CN 市场、P1D 日线、变体 ths）：

| data_type | asset_type | 上游端点 | adjust | history 声明 | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| bars | stock | `GET /api/a-share/prices/historical` | none/qfq/hfq | start ≈ 2005-01-15T00:00:00+08:00 | open/high/low/close/volume/amount |
| bars | index | `GET /api/a-share-index/prices/historical` | none | retention_days=1825（滚动窗口） | 同上 |
| bars | etf | `GET /api/fund/market/historical` | qfq（上游固定前复权，不可选） | retention_days=1825（滚动窗口） | 同上 |
| bars | contract | `GET /api/futures/prices/daily` | none | start=null（随合约轮换漂移，不锚定） | 同上 |
| bars | option | `GET /api/options/prices/daily` | none | start=null（同上） | 同上 |
| quotes | stock | `GET /api/a-share/prices/snapshot` | —（快照无复权） | — | timestamp/last_price/volume/amount |
| quotes | index | `GET /api/a-share-index/prices/snapshot` | — | — | 同上 |
| quotes | etf | `GET /api/fund/market/snapshot` | — | — | 同上 |
| quotes | contract | `GET /api/futures/prices/intraday`（分时末点） | — | — | 同上 |
| quotes | option | `GET /api/options/prices/intraday`（分时末点） | — | — | 同上 |

bars 行字段两种形态统一映射（时间键不同、其余同构）：A 股/指数/ETF 行时间键 `date_ms`，期货/期权日 K 行时间键 `timestamp`；价格键 `open_price/high_price/low_price/close_price`，量额键 `volume`/`turnover`（`turnover` → 标准 `amount`）。stock 历史覆盖为聚焦二分 ±30 天粒度 approx（真实最早可能更早）；指数/ETF 为滚动窗口语义。

## 标的代码映射

标准代码 → thscode，exchange 段按资产类型查后缀表（只收实证条目——文档示例 + 探针价格端点实证，其余 exchange 明确拒绝）：

| asset_type | 后缀映射 | 示例 |
| --- | --- | --- |
| stock / index | SSE→SH、SZSE→SZ、BSE→BJ | `CN-SSE-600519 → 600519.SH`、`CN-SZSE-000001 → 000001.SZ`、`CN-BSE-920000 → 920000.BJ` |
| etf | SSE→SH、SZSE→SZ（北交所无 ETF） | |
| contract | SHFE→SHF、CZCE→CZC、DCE→DCE、INE→INE、GFEX→GFE（五所实证：MA612.CZC / MA701.CZC / EG2701.DCE / BC2612.INE / LC2701.GFE 均 code=0 正常返回） | `CN-SHFE-CU2706 → CU2706.SHF` |
| option | CFFEX→CFE（chain 复验） | `CN-CFFEX-IO2612-C-4000 → IO2612-C-4000.CFE` |

- CZCE 年月码 4→3 还原：`CN-CZCE-MA2701 → MA701.CZC`（去年份十位，与 tqsdk 同构；4 位码 MA2701.CZC 上游 3001 不识别、3 位 MA701.CZC 可用）；仅匹配「品种 + 2 位年 + 2 位月」形态——主连/加权品种码（无年月段）、3 位旧写法与不规则代码原样透传；
- contract/option 的 symbol 段除 CZCE 还原外原样透传（期权行权价等结构不做改写）；
- 查表无果（exchange 段不在该资产类型后缀表）属命名空间外请求，明确抛 `UnsupportedCapabilityError`——不透传上游猜错更隐蔽。

## 周期 / 复权 / 单位映射

- 周期：历史 K 线端外仅日线 `P1D→1d`，其余周期能力目录即不声明（operation 二级路由拒绝）；期货/期权日 K 端外仅 `time_period=day_1`（显式传参，不依赖上游默认值）；时间边界统一毫秒 Unix 时间戳显式传参。
- 复权：
  - A 股个股（唯一三值语义）：`none→none`、`qfq→forward`（前复权）、`hfq→backward`（后复权）；上游 `forward` 缺省恰与网关个股声明默认 `qfq` 对齐，适配器仍始终显式传参；
  - 指数：无复权语义（响应 `data.adjust` 固定 null，不传 adjust 参数）；
  - ETF：上游无 adjust 参数、数据固定前复权口径——受控对比实证（调整生效段 vs 未复权基准偏差中位 9.01%、vs 前复权基准 0.34%）后声明 `[qfq]`；`adjust=none` 的 auto ETF 请求按六维匹配不落 ths（回退链末位，前序 Provider 全失败时诚实返回 `NO_PROVIDER_AVAILABLE`）；
  - 期货/期权：仅声明 none（不传 adjust）。
- 单位：A 股快照/历史 K 线 `volume`=股、`turnover`→amount=元（官方文档明示）；ETF/期货/期权 volume/turnover 单位上游未明示，原值透出（不编造换算）。

## 上游行为定案

- 统一 ApiResponse 信封：业务结果（含错误）HTTP 恒 200，经 `code` 字段表达（0=成功）；`code=4001`（频率超限）映射 `UPSTREAM_RATE_LIMITED`；其余非 0（1001-1004 参数类 / 2001-2003 凭据权限 / 3001-3004 标的类 / 5001-5003 服务端）统一 `UPSTREAM_ERROR`（details 携 `upstream_code`/`upstream_message`）；非 2xx 与 JSON 解析失败为防御性 `UPSTREAM_ERROR`（官方约定业务错误走信封，HTTP 恒 200）；上游异常不穿透 API。
- 时间戳口径：上游统一毫秒 Unix、Asia/Shanghai，适配器负责北京时间归一化；时间戳是 bar 的身份键，行缺失时间键抛 `UPSTREAM_ERROR`（loud 失败优于静默丢行）；bars 合法无数据（`item=[]`）返回空批次，快照/分时无有效 item 属无有效行情、抛 `UPSTREAM_ERROR`。
- 窗口口径（文档与实测不符，以实测为准）：指数文档写「≤10 年」，实测仅支持最近约 5 年滚动窗口（越界 `code=1003 'recent five years only'` 拒绝）；A 股个股窗口跨度 ≤10 年（>10 年窗 `code=1003 'must span at most 10 years'` 拒绝——起点可早于 10 年前，上限约束的是窗口跨度）；ETF ≤5 自然年吻合（越界返空不报错）。
- 分页与截断：historical 的 `offset` 参数被上游静默忽略（全年窗 offset=100 实测返回行数与首根均不变），适配器不启用该参数；行数上限未观测到（10 年窗内完整返回 ≥2417 行、无触顶信号），`meta.partial` 恒 false（触顶判定无观测基础，隐式截断行为待探针核实后补充覆盖判定）。
- 已摘牌合约移出上游目录、仅现活合约可查：已到期文档示例合约（CU2601.SHF / IO2601-C-4000.CFE）存续期窗口返回 3001 'Target not found'，现活同后缀合约 code=0 干净对照、虚构负对照 ZZ9999.SHF 同 3001——这是 contract/option 的 `history.start` 保持 null 的依据（「最早可用」随合约轮换漂移，不支持全量锚定）。
- 已知上游缺陷：期权日 K 以单一日期作 `start` 触发 `code=5003 'daily quote unavailable'`（邻近起点均正常，与调用频率无关）——窗口避开即可；适配器照常上抛 `UPSTREAM_ERROR` 不归一化（单点缺陷而非稳定边界）。
- 上游能力边界：历史 K 线仅日线；同花顺板块/行业指数（88xxxx.TI）与商品指数（850002.TI）属同花顺特有 TI 类指数，本期不纳入；ETF 行情仅支持 ETF（LOF/场外/REITs 上游返回 `code=3004`）。
- 快照 timestamp 口径（探针实证）：`data.timestamp` 为上游响应组装时刻（盘后随墙钟推进、间隔 10s 两次调用精确 +10s），非成交时点；上游快照无独立行情时间字段，按文档「最新上游有效时间」口径透出——盘后请求的快照 timestamp 为请求时刻属预期行为。
- 期货/期权快照取分时末点：`session=intraday`、`trade_date=0` 显式传参（端外仅当前交易日）；`last_price`=点时间升序排序后末点 `price`、`timestamp`=末点时间；非交易日/盘前 `item=[]` 返回 `UPSTREAM_ERROR`；`item[].timestamp` 官方标明可空（盘前等场景），无时间戳的点不参与排序。
- 分时量额口径：`volume`/`amount`=分时全点求和（当日累计）——分时点量额为逐分钟增量（各点 turnover/volume 恰等于该点 price×合约乘数、开盘三点 volume 5→26→31 非累计序列，探针实证）；两字段独立计算：任一点 volume 缺失则 volume 恒 null、任一点 turnover 缺失则 amount 恒 null（部分和不冒充累计）；存在无时间戳点时求和序列不完整，两字段均不计算。
- quote 响应按 operation 显式分派快照/分时两条路径（形态分派会把畸形快照 item 误路由到分时路径、报错语义失真）。
- 客户端超时 20s（应用层整体兜底 `upstream_timeout_seconds` 默认 30s，客户端自身取更小保守值，避免长挂线程占用 worker）。

## 快照与流式

- 快照为纯 REST 单次调用语义：无首包超时/`QUOTE_NOT_READY`（与 alphafeed/tickflow 一致）；
- A 股/指数快照走批量参数 `thscodes`（网关单标的语义传单个）、ETF 走单值参数 `thscode`；`last_price` 直映、`volume`/`turnover`→`amount` 直映；快照不返回中文名 `name`（上游约定，中文名经元信息域端点解析，网关不透出）；
- 期货/期权快照为分时端点末点（口径见「上游行为定案」）；
- 流式：不实现（上游无推送接口，`/v1/market/quotes/stream` 路由到 ths 返回 `UNSUPPORTED_CAPABILITY`）。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars：CN stock / index / etf / contract 居回退位末位（付费凭据 API、仅日线、窗口受限）；CN option 独占（首个期权 Provider）；
- quotes：CN stock / index / etf / contract 居回退位末位（个股/指数/ETF 快照直出，期货/期权经分时端点取末点）；CN option 独占；
- ETF 特例：仅声明 `qfq`——`adjust=none` 的 auto ETF 请求经六维匹配跳过 ths、不参与该请求的回退。

位次依据一句话：付费凭据 API 置各既有链末尾补位；CN 期权为唯一覆盖该组合的 Provider 故独占。

## extensions 白名单

- 快照（A 股/指数/ETF）：`open_price`/`high_price`/`low_price`/`prev_price`/`price_change`/`price_change_ratio_pct`；ETF 另有 `price_amplitude_ratio_pct`/`turnover_ratio_pct`；缺失不透出；
- 期货/期权快照（分时末点）：无 extensions；
- bars：无 extensions。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/ths.yaml`（探针框架规范见 `probes/README.md`）；
- 各轮运行记录：`probes/results/PROBE_LOG.md`（本地文件，不入库）。
