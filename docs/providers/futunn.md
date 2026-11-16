# 富途 OpenAPI（futunn）

富途 OpenAPI HTTP 直连接入：CN 股票/指数/ETF 与 HK/US 正股 + US/HK ETF 的 8 核心周期历史 K 线 + 同范围实时快照（2026-11-15 扩容），纯 REST、AppKey 签名认证，无官方 Python SDK、无 FutuOpenD 网关，各组合居缺省回退链末位。

## 概要

- 接入方式：HTTP 直连 REST（Base URL `https://webapi.futunn.com`），无官方 Python SDK——httpx2 同步客户端在调用内延迟导入直连（未安装返回 `PROVIDER_NOT_IMPLEMENTED`，不影响服务启动），同步调用经 `call_in_thread` 隔离到线程池；接口签名按官方文档核实（getting-started、api-authority、rate-limit、stock-quote、history-kline 五页，`https://open.futunn.com/zh-cn/api/...`）；
- 认证：「方式二：传统 API Key」——open.futunn.com/dashboard 控制台「AppKey 管理」创建 AppKey 并上传公钥（Ed25519 或 RSA-SHA256 / PKCS#1 v1.5），本地私钥对签名原文签名后经 4 个请求头注入（见「上游行为定案」）；
- 凭据：`TAPEHUB_FUTUNN_APP_KEY`（AppKey ID）+ `TAPEHUB_FUTUNN_PRIVATE_KEY_PATH`（与上传公钥配对的本地私钥 PEM 文件路径）双凭据，**两者全部非空才启用**；缺任一凭据不启用——能力目录仍可见、auto 路由跳过、显式请求返回 `PROVIDER_DISABLED`；凭据只经环境变量注入，AppKey/私钥不落 URL、代码、manifest、日志或异常响应；环境变量全表见 ../operations.md；
- 依赖：pyproject extras `futunn` = `httpx2>=2.16` + `cryptography>=42`（均延迟导入）；
- 变体体系：单体系 Provider，变体与 `default_variant` 均为 `futunn`（数据源即上游服务自身）；缺省请求经缺省路由回退链裁决，也可显式传 `variant=futunn`。

## 能力与上游映射

以 `src/tapehub/providers/futunn/manifest.yaml` 的 capabilities/operations 为准（历史 K 线统一端点 `GET /api/v1.0/quote/{code}/history-kline`，operation=history_kline；快照统一端点 `POST /api/v1.0/quote/stock-quote`，operation=stock_quote；delivery 均 rest，变体均 futunn）。operations 显式枚举全部声明组合（六维全写，bars 共 8 周期 × 各资产类型复权值逐条匹配）；快照对周期/复权等无意义维度显式 null、路由跳过对应维度：

| data_type | 市场 × 资产类型 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- |
| bars | CN stock | PT1M / PT5M / PT15M / PT30M / PT1H / P1D / P1W / P1M | none / qfq / hfq | open/high/low/close/volume/amount |
| bars | CN index | 同上 | none（指数无复权语义） | 同上 |
| bars | CN etf | 同上 | none / qfq / hfq | 同上 |
| bars | HK stock | 同上 | none / qfq / hfq | 同上 |
| bars | HK etf | 同上 | none / qfq / hfq | 同上（2026-11-15 扩容） |
| bars | US stock | 同上 | none / qfq / hfq | 同上 |
| bars | US etf | 同上 | none / qfq / hfq | 同上（2026-11-15 扩容） |
| quotes | CN stock / index / etf、HK stock / etf、US stock / etf | —（快照无周期，match 显式 null） | —（快照无复权，match 显式 null） | timestamp/last_price/volume/amount |

接入范围注记：CN stock/index 覆盖 SSE/SZSE/BSE（上游品类表 SH/SZ/BJ 前缀支持正股/指数）；CN etf 仅 SSE/SZSE（上游品类表 BJ 无 ETF，ETF×BSE 在映射层拒绝）；HK 正股/ETF（窝轮/牛熊证与 HK 指数不接入——指数上游 REST 网关报 invalid symbol，见「上游行为定案」）；US 不支持 OTC（上游不支持获取，exchange 白名单拒绝）；US etf 与 HK etf 2026-11-15 扩容（目录外诊断第 4/5/6/7 轮实证，与 stock 同端点同市场级前缀，映射零分支）。

history.start 历史覆盖声明（逐所/逐周期，均北京时间）：

| 市场 × 资产类型 | 日/周/月（P1D/P1W/P1M） | 分钟族（PT1M~PT1H 五周期） |
| --- | --- | --- |
| CN stock | 1990-12-19T00:00:00+08:00（上市日锚，基准标的 600602） | 2014-01-21T09:45:00+08:00 |
| CN index | ≈1991-01-18T16:52:30+08:00（est 上界，与 CN stock 同库界） | ≈2015-05-15T23:37:30+08:00 |
| CN etf | 2012-05-28T00:00:00+08:00（上市日锚，基准标的 510300） | ≈2015-07-11T08:26:15+08:00 |
| HK stock | ≈1991-01-18T16:52:30+08:00（est 上界） | null（保持未声明） |
| HK etf | null（2026-11-15 扩容，保持未核实） | null（同左） |
| US stock | ≈1997-01-25T00:33:45+08:00（est 上界） | ≈2015-05-15T23:37:30+08:00 |
| US etf | null（2026-11-15 扩容，保持未核实） | null（同左） |

覆盖依据注记（探针实证）：

- 除上市日锚定值外均 approx 90 天粒度（二分收敛）；capability 级声明以基准标的库底为准，SZSE/BSE 交易所未单独探测；
- CN stock 分钟 `2014-01-21` 语义为**连续可查起点**——更早存在稀疏碎片数据（era 宽窗静默返空 + 碎片区使连续性假设失效）；日/周/月锚定值经周线/月线独立实证与上市日精确吻合、日线同库互证；
- CN index/etf 与 US 的分钟族为干净收敛、五周期同值互证；CN index 日/周/月与 CN stock 为同一库界（回溯发布锚起算）；
- **HK stock 分钟族保持 null 待专项方法重探**（碎片区：era 宽窗静默返空 + 稀疏碎片数据使连续性假设失效，二分法不可定案）；HK 日/周/月库底与 alphafeed/tickflow 的 HK 库底（更深）对照差异如实记录；
- US 日/周/月收敛点为真实库底而非上市日伪影（基准标的上市场远早于收敛点）。

## 标的代码映射

标准代码 → 上游前缀代码。前缀按市场段取：HK/US 为市场级（美股前缀不区分交易所、exchange 段仅作上市所白名单——OTC/IEX 上游不支持获取，白名单外拒绝），CN 按资产类型 × 交易所查子表（品类语义真实，只收文档实证条目，白名单外明确拒绝——不透传上游猜错更隐蔽）。market/exchange 组合一致性由域层标准代码词汇表校验（矛盾组合如 `CN-NYSE-…` 在参数校验期即拒绝）：

| 市场/资产类型 | 前缀映射 | 示例 |
| --- | --- | --- |
| CN stock/index | SSE→SH、SZSE→SZ、BSE→BJ | `CN-SSE-600000 → SH.600000`、`CN-SZSE-000001 → SZ.000001`、`CN-BSE-430047 → BJ.430047` |
| CN etf | SSE→SH、SZSE→SZ（上游品类表 BJ 无 ETF） | |
| HK / US | 市场段即前缀（US 限 NYSE/NASDAQ/AMEX） | `HK-HKEX-00700 → HK.00700`、`US-NASDAQ-AAPL → US.AAPL` |

- symbol 段：CN 6 位证券代码原样透传（要求纯数字，非数字拒绝）；港股短码零填充 5 位（`700`→`00700`）；美股 ticker 已由域层大写归一、原样透传（上游美股前缀不区分交易所）；
- 资产类型层面：futunn 仅支持 stock/index/etf 三类，其余 asset_type（contract/期权三子类/forex/crypto 等）在映射层直接拒绝（`UnsupportedCapabilityError`），不透传上游。

## 周期 / 复权 / 单位映射

- 周期（ktype 数字枚举）：`PT1M→1`、`PT5M→6`、`PT15M→7`、`PT30M→8`、`PT1H→9`（上游 60 分）、`P1D→2`、`P1W→3`、`P1M→4`；上游 ktype 文档明示 1/3/5/10/15/30/60/120/180/240 分与日/周/月/季/年枚举，适配器取 8 个核心周期声明，年/季线不进能力目录（周期上界月线）；
- 不声明周期及原因（探针实证）：
  - ktype=10（PT3M）与 ktype=14（PT2H）端点真实可用（与文档 3 分/120 分枚举吻合）；ktype=15（PT3H）与 PT2H 同形态（CN 上午盘约 2 小时，长周期在 CN 会话退化）；PT4H 无可用 ktype 映射（ktype=16 返回 -3 invalid）；
  - **PT3M/PT2H 维持不声明**（可用性证据保留探针 notes；扩容需同步扩 manifest/operations 并另做历史深度专项）；
- 复权（autype）：`none→0`、`qfq→1`、`hfq→2`（上游缺省 1=前复权，适配器始终显式传参、不依赖默认值）；
  - stock：三值直映；
  - index：仅 none（指数无复权语义，与 ths 惯例一致）；
  - etf：三值——**autype 对 ETF 生效**（探针实证：CN 基准 510300 日线全年 none/qfq、none/hfq 各 242/242 根 close 全差异；US etf 目录外诊断同证：SPY 日线两两 250/250 全差异，2026-11-15；HK etf 同证：02800 日线两两 246/246 全差异，第 7 轮）；
- 单位：`volume`=股（上游已按信封 `data.volume_precision` 放大 10^n 倍，适配器逐行除以 10^n 还原；缺失/非法按 0 不缩放——官方默认口径，股票一般 n=0）；`turnover` → `amount`=成交额（各市场原币，官方未标注币种折算，不编造换算）；数值缺失/NaN 统一映射 null（不用 0 冒充）。

## 上游行为定案

- 签名机制（每请求本地计算，AppKey/私钥绝不落 URL、日志、manifest 或异常响应）：
  - 签名原文为 5 个字段以 `\n` 连接：`timestamp_ms`（毫秒 Unix）、大写 HTTP 方法、`request_path`（含路径参数）、`query_string`（与实际请求逐字节一致，无查询参数为空串）、`body_part`（请求体字节的 SHA256 小写十六进制，无请求体为空串）；
  - 私钥签名后 Base64，经 4 头注入：`X-Api-Key`（AppKey ID）、`Authorization`（Base64 签名，**不加 Bearer 前缀**）、`X-Timestamp`（毫秒时间戳，须与签名一致）、`X-Nonce`（1-64 位字母/数字/下划线/连字符随机串，防重放；适配器取 32 位）；POST 另注 `Content-Type: application/json`；
  - 算法按私钥 PEM 类型自动检测：Ed25519 → 原文即消息；RSA → SHA256 + PKCS#1 v1.5（PKCS#1/PKCS#8 私钥均可加载）；
  - 私钥文件缺失/不可读/非有效未加密 PEM/类型不受支持（EC 等）均归运行前提失败（`PROVIDER_NOT_IMPLEMENTED`，错误只定位到设置项、不携带密钥材料）；
- 校时口径：`X-Timestamp` 与服务端偏差超过默认 5 秒报 -12006（HTTP 401）——第一版不做自动校时，依赖宿主机时钟；校时端点 `GET /api/v1.0/server-time` 免签可用已探针实证（HTTP 200，信封 `server_time_ms` 为字符串类型毫秒值），自动校时按需另行实现；
- 统一信封与错误指纹：官方约定业务结果（含错误）HTTP 200，经 `ret_code` 表达（0 成功）；`ret_code` 缺失时防御读 `code` 键——新旧两种封装并存（`ret_code/ret_msg` 与 `code/message`）；`-11`（rate_limited）→ `UPSTREAM_RATE_LIMITED`（429，携上游码与 `Retry-After` 如有）；其余非 0（`-3` invalid_parameter / `-7` invalid_symbol / `-8` unsupported / `-12` history_quota_exceeded / `-13` quota_unavailable / `-12006` 鉴权失败等）→ `UPSTREAM_ERROR` 携 `upstream_code`/`upstream_message`；HTTP >=400 与 JSON 解析失败为防御性 `UPSTREAM_ERROR`（携 `status_code`）；信封结构畸形（data/quote_list/kline_list 非预期形态）同归防御性 `UPSTREAM_ERROR`；SDK 异常不穿透 API；
- 行情权限（api-authority 页）：A 股境内认证客户免费 LV1（国际客户暂不支持）、港股股票境内认证客户免费 LV2、美股股票推广期免费 LV3——未开通对应市场权限时上游报 invalid_symbol/quota 类错误，原样上抛 `UPSTREAM_ERROR`；
- 限频与额度（rate-limit 页）：IP 入口层行情类 100 次/分钟；业务限流走 HTTP 200 + `ret_code=-11`；**历史 K 线独立额度池：每标的首次成功返回非空即占用一个额度（默认 7×24 小时后释放，重复读取不延期）**——分页续读必须用 `page_token`（免计「首请求」；`next_time` 分页仍计入，适配器未使用）；`page_token` 15 分钟有效；
- num 参数失效与单查询行数上限（文档与实测不符（以实测为准））：请求 `num`（文档默认/最大 370，适配器显式传满）被上游忽略，实测单查询恒返回**窗口起点起 ≈1000 行**（CN 精确 1000、US 全年窗实测 911~999）；`pagination.has_more` 恒 False 且无 `next_page_token`——`page_token` 分页实际不可用（适配器分页循环与页数上限 `UPSTREAM_MAX_BARS=370×15=5550` 保留为防御，上游若恢复签发 token 即生效）；
- 隐式截断与 partial：分钟族窗口 >≈1000 根时上游**静默截断**且无截断信号，`meta.partial` 无法可靠判定（实测恒 false；防御性「触页数上限且最早一根晚于 start 判 partial」保留）——分钟族（PT1M~PT30M）建议窄窗口使用（CN PT1M 单查询 ≈4 个交易日），日/周/月线与 CN PT1H 在常规窗口内完整（实测 P1D 243~249、P1W 52、P1M 12、CN PT1H 全年 968 根完整）；
- US ETF 扩容定案（目录外诊断 2026-11-15 第 4/5 轮）：统一 klines 端点对美股 ETF 标的（SPY.US/QQQ.US，交易所段无关）不做证券类型路由拒绝——P1D 2025 全窗 250 根直测放行、none/qfq/hfq 三复权两两全窗差异、P1W 52/P1M 12/PT5M 两周 780 根直测放行；raw（未复权 OHLC/成交量）与 alphafeed 侧逐位一致，qfq 与 alphafeed 吻合至 7 位有效数字（复权因子同源）；**hfq 锚定基准两源不同**（futu ≈1.10× vs alphafeed ≈1.79×，后复权基点各异）——各家语义自洽、hfq 数值不可跨源直比；`amount` 为上游真实成交额（美元原币、量级与 vol×close 吻合，分钟行同样有值），非 alphafeed/tickflow US 日线的原值 0；快照 `US.SPY` stock-quote 直测放行（quotes US etf 同轮扩容）。history 未核实保持 null（chain 二分后回填）；
- HK ETF 扩容定案（目录外诊断 2026-11-15 第 6/7 轮）：统一 klines 端点对港股 ETF 标的（HK-HKEX-02800/02828，5 位零填充数字代码）不做证券类型路由拒绝——P1D 2025 全窗 246 根直测放行、none/qfq/hfq 三复权两两全窗 246/246 根差异（第 7 轮）、P1W 52/P1M 12/PT5M 两周 660 根直测放行（分钟线 bar 终点口径首根 09:35、末根 16:00，与 HK stock 语义一致）；`amount` 为上游真实成交额（02800 日线 ≈145.7 亿 HKD 原币，量级与 vol×close 吻合）——与 alphafeed/tickflow HK 日线「amount 原值 0」口径不同；快照 `HK.02800` stock-quote 直测放行、行结构与 stock 快照同形（quotes HK etf 同轮扩容，ts 为收盘时刻北京时间归一）。history 未核实保持 null（chain 二分后回填）；
- HK/US 指数不支持的定案（目录外诊断 2026-11-15 第 6 轮）：候选指数代码（HK.HSI/HSCEI/HSTECH、US.DJI/SPX/IXIC/NDX）在 history-kline 与 stock-quote 两端点全部 `-7 invalid symbol`/`all input symbols invalid`——socket API 文档虽有同形指数代码约定，但本项目唯一链路 webapi.futunn.com REST 网关不接受（文档与实测不符，以实测为准）；`invalid symbol` 指纹排除了权限缺失解释；对照端点对 HK.02800/HK.00700/US.SPY/SH.000001 正常放行。映射层现状：US 指数代码零改动可达但上游拒绝；HK 指数代码被映射层数字符号约束拒绝（即便放开也撞上游 -7）。HK/US 指数维持不声明；
- time_key 口径：行 `time_key` 为毫秒 Unix 时间戳（绝对时刻），统一北京时间归一化（换算经纪元算术，不用 `datetime.fromtimestamp`——Windows 对早期时间戳受限）；日/周/月 K 线 `time_key` 为标的市场零点墙钟（CN/HK 北京零点、US 美东零点——对应北京时间 13:00 冬令 / 12:00 夏令），绝对时刻口径一致；行内 `time_zone` 为显示用本地时区偏移、不参与标准化（文档口径为相对 UTC 分钟数、实测为小时口径——文档与实测不符（以实测为准），均不影响标准化）；上游排序方向文档未明示，统一由共享分页组件升序归一；
- bars 契约违背拒绝：行缺失 `time_key` 属上游契约违背（bar 无时间戳无法标准化，时间戳是 bar 的身份键）——抛 `UPSTREAM_ERROR`，loud 失败优于静默丢行；合法无数据（如停牌/窗口越界）上游返回 `kline_list=[]`，映射为空批次不报错；
- bars 分页防御：续页仅当 `next_page_token` 为非空字符串且 `has_more` 非 False 才触发（单点提取，畸形分页字段按无令牌处理）。
- 分钟 K 线 end 语义：上游 `end` 以该日 00:00 为截止、**不含当日**——适配器对分钟周期（PT1M~PT1H）end 日期 +1 天补偿；日/周/月 K 线 end 含当日不补偿；请求 `start`/`end` 为标的市场时区日历日期（CN=Asia/Shanghai、HK=Asia/Hong_Kong、US=America/New_York），闭区间 `[start, end]` 精确复核在适配器完成；
- 请求构造约束：查询串由 request_mapper 统一生成（固定键序 start/end/ktype/autype/num/page_token），URL 拼接与签名原文共用同一字符串（签名一致性单点保证；`page_token` 有值时置于末位，续页参数逐字节不变）；GET 显式传 `num`/`autype`，POST 体即签名输入；
- 客户端超时 20s（应用层整体兜底 `upstream_timeout_seconds` 默认 30s，客户端自身取更小保守值，避免长挂线程占用 worker）。
- extensions 盘前/盘后不透出定案（2026-11-14 目录外实证 + 人工拍板）：上游 extensions 段含盘前/盘后/夜盘真实成交数据（US.GE 上周五盘后 60.3 万股），快照 extensions 白名单维持 7 键不透出，盘前盘后扩容搁置；

## 快照与流式

快照为当前时点数据，无周期/时间范围/分页/复权维度（通用 API 约定见 ../api.md，此处只列 futunn 专属差异）。

- 快照为**无状态 REST 单次调用语义**：单标的单次 `POST /api/v1.0/quote/stock-quote`（`code_list` 单次最多 400，本网关单标的传 1 个）——无首包超时/`QUOTE_NOT_READY`（与 ths/alphafeed/tickflow 一致）：要么返回快照，要么返回上游错误；
- 字段直映：`data_time`（毫秒 Unix）→ 行情时间、`last_price` 直映、`volume`/`turnover` → `volume`/`amount`；**无盘口五档**（bid/ask 恒 null，如实透出、不编造）；
- 无有效行情的拒绝口径：`data_time` 为 0/None（官方「不适用品类」语义）或 `last_price` 缺失属无有效行情；上游对 `code_list` 中非法 code 静默丢弃（`ret_code` 仍 0），匹配行缺失属无有效行情——均抛 `UPSTREAM_ERROR`（loud 失败优于静默缺行）；
- **不支持实时快照流（SSE）**：上游 WS 行情推送专区（`wss://webapi-quote.futunn.com/ws`）未接入，第一版不做流式——不实现 stream_quote，流式请求返回 `UNSUPPORTED_CAPABILITY`（422）。

## 缺省路由参与

当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不复述 order 列表。参与组合与角色：

- bars：CN stock / CN index / CN etf / HK stock / US stock / US etf 居各链回退位末位；HK etf 独占（2026-11-15 扩容填补空白，alphafeed/tickflow 无覆盖维持不声明）；
- quotes：CN stock / CN index / CN etf / HK stock / US stock 居各链回退位末位；US etf 独占（2026-11-15 扩容填补空白，此前无 Provider 声明、请求 422）；HK etf 独占（2026-11-15 扩容填补空白，同上）。

位次依据一句话：AppKey 凭据 API 置各既有链末位补位（凭据未配置时 auto 自动跳过），显式 `provider/variant=futunn` 亦可选用；US etf 与 HK etf 快照及 HK ETF K 线为独占组合，其余为既有链扩容补位。

## extensions 白名单

- bars：无 extensions；
- quotes：`open_price` / `high_price` / `low_price` / `prev_close_price` / `turnover_rate` / `amplitude`（% 数值原样透出，缺失不透出）+ `suspension`（布尔停牌标记）；中文名 `name` 不透出。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/futunn.yaml`（探针框架规范见 `docs/probes.md`）；
