# API 与数据约定

本文是 tapehub 对外 API 与跨 Provider 数据约定的唯一出处：接口示例、请求规范、单位换算总则与错误码表。项目说明与总体设计见 [README](../README.md)；运维与配置见 [operations.md](operations.md)；各 Provider 的上游映射、标的代码转换与行为差异见 [providers/](providers/)。

## 连接测试（base_url + /health）

```http
GET http://tapehub:8000/health
```

正常返回 HTTP 200：

```json
{
  "status": "ok",
  "service": "tapehub",
  "version": "0.1.0",
  "api_version": "v1",
  "timestamp": "2026-01-01T00:00:00+08:00"
}
```

调用方可同时校验 **HTTP 200** 与 **`service == "tapehub"`**。该接口不访问任何上游 Provider，也不需要鉴权。

## 认证

- 当前版本（`TAPEHUB_DEPLOYMENT_MODE=single`，单进程单用户最小运行模式）**默认无入站鉴权**：全部端点对本机/可信网络内的任何调用方开放。服务不强制回环绑定——**对外（非回环）暴露前必须配置入站认证或置于可信网络**，否则任何可达主体都可无门槛消耗已配置 Provider 的付费配额与数据（部署警示见 [operations.md](operations.md)「安全边界」）。
- 可选入站 API Key 认证（`TAPEHUB_API_KEYS`，请求头 `X-Api-Key`）随安全加固提交提供；未配置时行为与上述默认语义完全一致。

## 能力目录

```http
GET /v1/capabilities?asset_type=stock&interval=P1D
GET /v1/providers/akshare/capabilities
```

只读取本地能力目录，不实时访问上游；支持可选过滤参数 `provider`、`data_type`、`market`、`asset_type`、`interval`、`variant`；每个 Provider 条目携带 `default_variant`（「Provider × 资产类型」级缺省变体，variant 缺省时的解析依据）。

`enabled`（是否启用，目录声明）与 `runtime_status`（适配器长连接运行时状态）是两个不同概念：`runtime_status` 经鸭子类型读取适配器属性，仅长连接类 Provider 有意义——tqsdk `shared` 模式回填真实 Runtime 状态（`idle` 懒启动前 / `starting` / `ready` / `recycling` 新一代重建中 / `fatal`），`per_request` 回滚模式与其余无长连接概念的 Provider 恒为 `unknown`（不伪造实时健康状态）。

## 历史 K 线

```http
GET /v1/market/bars?provider=akshare&asset_type=stock&instrument=CN-SSE-600000&interval=P1D&start=2026-01-01T00:00:00+08:00&end=2026-01-31T00:00:00+08:00&offset=0&limit=100
```

参数：`provider`（默认 `auto`）、`asset_type`（必填）、`instrument`（必填）、`interval`（必填）、`start`/`end`（必填）、`adjust`（可选）、`variant`（可选，缺省按 Provider 声明的 `default_variant` 解析，`meta.variant` 回显实际值）、`offset`（默认 0）、`limit`（默认 500）。

成功响应（节选）：

```json
{
  "data": {
    "instrument": "CN-SSE-600000",
    "asset_type": "stock",
    "interval": "P1D",
    "bars": [
      {
        "timestamp": "2026-01-05T00:00:00+08:00",
        "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05,
        "volume": 1000000.0, "amount": 10050000.0,
        "extensions": {"pct_change": 0.5, "turnover_rate": 0.8}
      }
    ]
  },
  "meta": {
    "request_id": "req_...",
    "provider": "akshare",
    "source_time": null,
    "received_at": "2026-01-01T00:00:00+08:00",
    "offset": 0, "limit": 100, "count": 20, "partial": false
  },
  "warnings": []
}
```

缺失数值以 `null` 返回，不用 `0` 冒充；Provider 特有字段只能通过 `extensions` 受控透出（各 Provider 的白名单见对应文档）。

## 实时快照

```http
GET /v1/market/quotes?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-AU2612
GET /v1/market/quotes?provider=twelvedata&asset_type=forex&instrument=GLOBAL-OTC-EUR/USD
```

参数：`provider`（默认 `auto`）、`asset_type`（必填）、`instrument`（必填）、`variant`（可选）。**快照是当前时点的数据：无 `interval`、无时间范围、无分页参数**（`adjust` 同样不接受——复权是历史序列概念）。`provider=auto` 时由缺省路由配置的完整回退链裁决（见 `src/tapehub/config/provider_routing.yaml`）；凭据未配置的 Provider 自动跳过。

各 Provider 的交付模型不同，语义差异如实声明（细节见对应 Provider 文档）：

| Provider | 交付模型 | 冷订阅首包超时 | SSE 流 |
| --- | --- | --- | --- |
| tqsdk | 订阅-推送（共享 Runtime 订阅缓存） | `QUOTE_NOT_READY` 408 | 支持（仅 `shared` 模式） |
| twelvedata | WS 订阅缓存（快照与流同源） | `QUOTE_NOT_READY` 408 | 支持 |
| tiingo | WS 订阅缓存（crypto/fx 双端点共享订阅） | `QUOTE_NOT_READY` 408 | 支持 |
| tickflow | 无状态 REST 单次调用 | 不产生（要么快照要么上游错误） | 不支持 |
| ths | 无状态 REST 单次调用 | 不产生（同上） | 不支持 |
| futunn | 无状态 REST 单次调用（AppKey 签名） | 不产生（同上） | 不支持 |

快照成功响应示例（以 tqsdk 为例；各 Provider 的字段口径见对应文档）：

```json
{
  "data": {
    "instrument": "CN-SHFE-AU2612",
    "asset_type": "contract",
    "quote": {
      "timestamp": "2026-01-05T10:14:46.500000+08:00",
      "last_price": 848.24,
      "bid_price1": 848.26, "bid_volume1": 2,
      "ask_price1": 848.3, "ask_volume1": 2,
      "volume": 200250.0, "amount": 169906871060.0,
      "extensions": {"open_interest": 160306.0, "pre_settlement": 846.12}
    }
  },
  "meta": {
    "request_id": "req_...",
    "provider": "tqsdk",
    "variant": "tqsdk",
    "received_at": "2026-01-05T10:14:47.000000+08:00"
  }
}
```

## 实时行情流（SSE）

```http
GET /v1/market/quotes/stream?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-AU2612
Accept: text/event-stream
```

参数与快照接口一致（`provider`/`asset_type`/`instrument`/`variant`），响应为 `text/event-stream`（HTTP/1.1 chunked 流，零新依赖）。**行情推送是纯单向流，选 SSE 而非 WebSocket**：复用 HTTP 中间件/request_id（入站鉴权语义与全部接口一致，见「认证」节），客户端 `curl -N` 即可消费。仅实现 `stream_quote` 的 Provider 可用（当前 tqsdk、twelvedata、tiingo）；其余 Provider 在受理阶段即返回 `UNSUPPORTED_CAPABILITY`（422）。

三类事件帧（`event:` + `data:` JSON，帧间空行分隔）：

| event | data 结构 | 语义 |
| --- | --- | --- |
| `status` | `{status, instrument, asset_type, provider}` | 流生命周期：`subscribed`（订阅确认，**首个业务事件**；已缓存快照场景立即补发）/ `upstream_disconnected`（上游连接异常或服务停机）/ `recycling`（tqsdk 专属：Runtime 新一代重建——跨交易日回收 / watchdog / 休眠订阅回收，活跃订阅自动重订，重订完成再发 `subscribed`）/ `idle_unsubscribed`（流空闲超时，服务端本地退订并结束流） |
| `quote` | `{instrument, asset_type, quote, provider, variant, received_at}` | 与快照响应同构，`quote` 内为标准快照 |
| `heartbeat` | `{type, request_id, ts}` | 空闲保活帧（默认 15s，含订阅确认之前的冷启动等待期），代理/负载均衡不至掐断空闲连接——冷订阅上游静默时连接不再最长静默 `TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS` 无任何字节 |

行为要点：

- **断线与回收绝不静默断流**：`upstream_disconnected` / `recycling` / `idle_unsubscribed` 都是明确事件；
- **coalesce 丢旧保新（仅 quote 帧）**：慢客户端的消费缓冲有上限（`TAPEHUB_STREAM_QUEUE_SIZE`，默认 64），行情 `quote` 帧满时丢弃最旧帧——行情「最新」比「完整」有价值；`status` 生命周期帧稀有且关键，**不参与也不被** coalesce 丢弃；
- **退订语义**：流关闭（客户端断开/空闲超时/停机）即本地退订（引用计数 -1、快照停止维护）。上游订阅的释放语义按 Provider 区分（tqsdk 协议不支持取消订阅、twelvedata/tiingo 经 linger 真退订），见对应 Provider 文档；
- **容量闸门**：并发流数达 `TAPEHUB_STREAM_MAX_CLIENTS`（默认 8）返回 `STREAM_LIMIT_REACHED`（HTTP 429）；受理阶段（响应头发出前）的错误统一 JSON 错误结构，流中错误一律事件化收尾；
- **上游订阅容量闸门**：并发订阅 symbol 数达 Provider 计划上限时，新 symbol 的流/快照请求在受理阶段返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（HTTP 429）；上限值与配置见 [operations.md](operations.md) 与对应 Provider 文档；
- **访问日志口径**：SSE 请求的「耗时」= 连接时长（长连接期间不产生新日志行）；
- 心跳与空闲退订由服务端管理：`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`（默认 300s）内无行情事件（心跳不算）即 `idle_unsubscribed` 收尾，客户端可随时重连。

## 请求规范

### instrument 标准代码

格式为 `{market}-{exchange}-{symbol}`，规范形式全大写，例如 `CN-SSE-600000`、`US-NASDAQ-AAPL`（美股股票）、`US-NYSE-SPY`（美股 ETF）。**输入大小写不敏感**（`cn-shfe-cu2607` 同样受理），服务端统一归一为全大写并以规范形式回显；期货品种的交易所原生大小写与年月码形态由 Provider 适配器还原（见对应 Provider 文档）。

- `market`、`exchange` 由服务端解析，调用方无需重复传递；
- **不接受** `CN.SSE.600000`、`600000` 等非标准格式；Provider 专用代码由适配器转换；
- **market/exchange 组合一致性**：exchange 段必须属于 market 段的规范词汇（CN：SSE/SZSE/BSE/SHFE/DCE/CZCE/INE/GFEX/CFFEX/CSI；HK：HKEX；US：NYSE/NASDAQ/CBOE/AMEX/OTC/IEX；GLOBAL：OTC/CRYPTO），矛盾组合（如 `CN-NYSE-…`）在参数校验期拒绝（`INVALID_REQUEST`），不会透传到 Provider 映射层；各 Provider 的接入范围是词汇表的更窄子集，由映射层白名单自行收窄；
- **期权子类 × 交易所归属**：`commodity-option` 仅适用于 SHFE/INE/DCE/CZCE/GFEX、`index-option` 仅 CFFEX、`etf-option` 仅 SSE/SZSE（CN 期权八所，2026-11 探针实证）；矛盾组合（如 `commodity-option` + `CN-SSE-…`）与 market/exchange 矛盾同层在参数校验期拒绝（`INVALID_REQUEST`）；
- **期权结构子类统一符号式**：`commodity-option` / `index-option` 的 symbol 段为 `{品种}{4位年月YYMM}{C/P}{行权价}`（如 `CN-SHFE-CU2701C102000`、`CN-CFFEX-IO2612C4000`、`CN-CZCE-SR2701C5900`），在参数校验期严格校验（`INVALID_REQUEST`）；各交易所原生写法（SHFE/INE 品种小写无连字符、DCE/GFEX/CFFEX 连字符式、CZCE 3 位月码）由 Provider 适配器统一还原。`etf-option`（SSE/SZSE）为交易所指派数字合约码（如 `CN-SSE-10012437`），无结构不受本式约束。诚实边界：行权价样本全为整数（八所探针实证），小数未观测但放行不拒绝；P 方向码式未直测、与 C 同构处理。历史连字符式（`M2701-C-3500`）与 3 位月码写法自 2026-11-16 统一式定案起不再合法；
- 第一版不通过标的元数据验证真实资产类型（「解析 ≠ 验证存在」：标的是否真实存在由 bars/quotes 请求经上游验证）。

### 标的解析（模糊查询）

`GET /v1/instruments/resolve` 把宽松输入解析为标准代码候选，供「先解析、再查询」使用；bars/quotes 主链路的严格校验不受影响。

```
GET /v1/instruments/resolve?q=600000
GET /v1/instruments/resolve?q=000001&asset_type=index
GET /v1/instruments/resolve?q=AAPL&exchange=NASDAQ
```

参数：`q`（必填，宽松输入）、`asset_type`（可选提示，收窄歧义候选）、`exchange`（可选提示，按标准代码 exchange 段过滤，如 `NASDAQ`）。响应恒为 200（含空候选），候选多于一个时 `ambiguous: true`：

```json
{
  "data": {
    "query": "000001",
    "ambiguous": true,
    "candidates": [
      {"instrument": "CN-SSE-000001", "market": "CN", "exchange": "SSE", "symbol": "000001", "asset_type": "index"},
      {"instrument": "CN-SZSE-000001", "market": "CN", "exchange": "SZSE", "symbol": "000001", "asset_type": "stock"}
    ]
  },
  "meta": {"request_id": "req_…", "generated_at": "…"}
}
```

解析规则（公开市场代码约定，纯本地推导、零上游调用）：

| 输入模式 | 解析结果 | 说明 |
| --- | --- | --- |
| 已是标准代码 | 大写归一回显（三段统一大写） | 输入大小写不敏感，期货品种的交易所原生大小写由适配器还原 |
| 纯 6 位数字 | A 股：`6/5/0/3/399/4/8/92` 段定交易所与资产类型 | `000xxx` 为指数/个股双关（上证指数系列与深市个股共用代码段），双候选；`001/002/003` 深市个股单候选 |
| 纯数字 1~5 位 | `HK-HKEX-` + 零填充 5 位 | `700` → `HK-HKEX-00700`；港股代码段不判资产类型（`02800` 是 ETF） |
| 纯字母 1~5 位 / 含点字母 | 美股 ticker 三候选（NYSE/NASDAQ/CBOE） | ticker 推不出交易所，可用 `exchange` 提示收窄 |
| 6 字母 3+3 双 ISO 4217 | `GLOBAL-OTC-基准/报价`（外汇） | 前三位为基准货币，`USDJPY` → `USD/JPY` |
| 以 USD/USDT/USDC 结尾的 6~9 字母 | `GLOBAL-CRYPTO-…`（加密货币） | `BTCUSD`、`BTCUSDT` |
| `代码.SH/SZ/BJ/HK/US` 后缀风格 | 对应市场（`600000.SH` → `CN-SSE-600000`） | 美股后缀不区分交易所，三候选 |
| `sh/sz/bj + 代码` 前缀风格 | A 股（`sh600000` → `CN-SSE-600000`） | 前缀已定交易所，`000` 段类型仍不定 |

诚实边界：

- **解析 ≠ 验证存在**：候选是格式级推导，标的是否真实存在由后续 bars/quotes 请求经上游验证；`asset_type` 为 null 的候选（美股、港股、标准代码回显）查询时仍需显式传参（asset_type 必填不推断不变）；
- **期货/期权不解析**：品种代码与交易所无本地映射事实源，返回空候选（已是标准代码的期货输入按回显规则处理）；中证指数系列（如 `000905`）标准代码为 `CN-CSI-` 前缀，请直接传标准代码；
- 提示参数过滤后可能为空：如实返回空候选，不回退、不猜测；
- 规则依据为公开市场代码约定与 ISO 4217 货币码表，不代表任何上游数据源的能力声明。

### asset_type 资产类型体系

`asset_type` 是**必填**字段，不做默认推断，也不根据代码格式猜测。取值按以下分类体系（category → asset_type）：

| 分类 | asset_type | 说明 |
| --- | --- | --- |
| securities 证券 | `stock` | 个股 |
| securities 证券 | `index` | 指数 |
| securities 证券 | `etf` | ETF |
| futures 期货 | `contract` | 单一合约 |
| futures 期货 | `continuous` | 主力连续 |
| futures 期货 | `weighted-index` | 加权 |
| options 期权 | `commodity-option` | 商品期权（SHFE/INE/DCE/CZCE/GFEX） |
| options 期权 | `index-option` | 股指期权（CFFEX） |
| options 期权 | `etf-option` | ETF 期权（SSE/SZSE） |
| forex 外汇 | `forex` | 外汇 |
| crypto 加密货币 | `crypto` | 加密货币 |

缺失或未知取值返回统一参数错误 `INVALID_REQUEST`。

期权三子类与交易所存在契约级归属约束（CN 期权八所，2026-11 探针实证）：矛盾组合（如 `commodity-option` + `CN-SSE-…`）在请求构造期拒绝，返回 `INVALID_REQUEST`。旧取值 `option` 已于 2026-11-16 拆分为上述三子类，不再合法（返回 `INVALID_REQUEST`）。

### variant 数据源变体

同一 Provider 下同一资产类型往往有多个上游接口（如 akshare 个股有东方财富/新浪/腾讯三个源）。统一请求通过可选参数 `variant` 指定，取值为**数据源名**而非上游函数名：

| variant | 含义 |
| --- | --- |
| `eastmoney` | 东方财富（仅 akshare 声明） |
| `sina` | 新浪（仅 akshare 声明） |
| `tencent` | 腾讯（仅 akshare 声明） |
| `tqsdk` | TqSdk（快期 Shinny Tech 官方 SDK 包名，背后为天勤行情平台） |
| `tushare` | Tushare（独立数据服务体系） |
| `alphafeed` | AlphaFeed（独立数据服务体系） |
| `tickflow` | TickFlow（独立数据服务体系） |
| `twelvedata` | Twelve Data（外汇/加密货币/美股数据服务） |
| `tiingo` | Tiingo（外汇/加密货币数据服务） |
| `baostock` | Baostock 证券宝（免费 A 股数据平台，与东方财富无关） |
| `ths` | 同花顺金融数据 API（独立数据服务体系） |
| `futunn` | 富途 OpenAPI（独立数据服务体系） |

- **变体名反映真实数据源**：仅 akshare 的上游确为东方财富/新浪/腾讯；其余 Provider 与东方财富无关，变体与其 Provider 同名（即各自的上游服务/包名）；
- **`variant` 可选，无全局默认值**。未传递时表示不表达数据源偏好：该维度不参与能力过滤，由每个 Provider 在 manifest 中**必填声明**的 `default_variant`（Provider × 资产类型级）具体化，`meta.variant` 回显实际值；`default_variant` 不覆盖其余维度时回退到声明顺序第一条匹配能力的第一个变体；
- 显式传递 `variant` 时严格匹配，不会静默降级；Provider 不支持的变体在能力目录即不声明；
- variant 与 capability、内部 operation 一样参与路由维度匹配；
- 每个 Provider 的 `default_variant` 声明见 `/v1/capabilities` 响应的 `default_variant` 字段。

### adjust 复权方式（默认值约定）

`adjust` 取值 `none`（不复权）、`qfq`（前复权）、`hfq`（后复权）。**未传递时的声明默认值按资产类型区分**：

| 资产类型 | 默认 adjust |
| --- | --- |
| `stock` | **`qfq`（前复权）** |
| `etf` | **`qfq`（前复权）** |
| 其余（index、contract、期权三子类等） | `none` |

> **决策记录（2026-11-15）——ETF 缺省复权调整为 qfq**：ETF 原与 index/contract 同组缺省 `none`；调整为与 stock 同组缺省 `qfq`。动机：场内基金与个股同样存在分红除权，ETF 作为可复权场内证券，缺省「前复权」才能给出跨分红时点可比的价格序列（与个股缺省口径对齐）；指数是人为构造的点位序列、期货无复权概念，维持 `none`。影响面：仅声明 `none` 的 Provider（如 tushare/tqsdk 的 ETF 能力）在缺省请求下被能力过滤出候选集——显式传 `adjust=none` 仍可达；支持 `qfq` 的 Provider（baostock/ths/futunn/akshare/alphafeed/tickflow/twelvedata US）承接缺省请求。实现：`api/schemas/requests.py` 的 `effective_adjust` 按 `(stock, etf) → qfq` 填充声明默认值。

各 Provider 的复权参数映射与上游生效性（含美股等特殊口径）见对应 Provider 文档。通用参数转换的原则是：**所有相关参数都显式传递给上游，不依赖上游默认值**——保证「接口默认行为」与「上游实际行为」一致。

### interval：ISO 8601 Duration

周期直接使用 ISO 8601 Duration：

| 周期 | 取值 | | 周期 | 取值 |
| --- | --- | --- | --- | --- |
| 1 秒 | `PT1S` | | 1 日 | `P1D` |
| 1 分钟 | `PT1M` | | 1 周 | `P1W` |
| 5 分钟 | `PT5M` | | 1 月 | `P1M` |
| 1 小时 | `PT1H` | | 1 年 | `P1Y` |

- **`PT1M`（1 分钟）与 `P1M`（1 个月）是不同周期**；
- 不把 `P1M` 换算为固定 30 天，也不把周期统一换算为秒数；`P1W` 与 `P7D`、`PT1H` 与 `PT60M` 视为**不同**周期代码；
- **项目能力目录的周期上界为月线（`P1M`）**：manifest 只声明**单分量**且不超过月线的周期——年分量（`P1Y`）、月分量 > 1（季线的 `P3M`/`P13W` 映射）、一切组合周期（`P1M2W`、`P1W2D`、`PT1H30M`）均被架构测试静态拒绝；
- `tick` 不是 ISO 8601 周期；manifest 中出现非法周期会在加载时被拒绝。

各 Provider 支持的周期子集见对应文档（不支持的周期在能力目录即不声明，路由会落到支持它的 Provider）。

### 时间边界与时区

- 时间范围是**闭区间 `[start, end]`**，`start` 必须早于 `end`；
- 时间边界**必须携带时区偏移**（国内标的推荐 `+08:00`，如 `2026-01-05T00:00:00+08:00`）；纯日期（`2026-01-01`）与无时区的裸时间一律拒绝，不做隐式时区解释；
- **项目内部统一北京时间（全市场）**：服务级时间戳（健康检查 `timestamp`、能力目录 `generated_at`）一律北京时间（`+08:00` 后缀）；行情数据时间戳亦全市场统一输出北京时间，与国内标的一致。各上游的墙钟口径（如外汇/加密货币/美股的本地时区）由适配器解析转换，口径见对应 Provider 文档；
- 上游只支持按日期过滤时，适配器在标准化后按 `[start, end]` 闭区间精确复核。

### 分页语义

| 参数 | 默认 | 约束 |
| --- | --- | --- |
| `offset` | `0` | ≥ 0 |
| `limit` | `500` | ≥ 1，最大值由 `TAPEHUB_MAX_LIMIT` 配置（默认 5000） |

- 结果先按 `timestamp` **升序**排序，再应用 `offset`/`limit`；
- 两者描述**统一接口最终结果**的分页语义；不支持原生分页的 Provider 由适配器用共享组件在内存切片（不落盘）；
- 第一版不返回 `total`；响应 `meta` 返回 `offset`、`limit`、`count`。

## 单位换算总则

标准字段的单位约定（各 Provider 的具体换算与例外见对应文档）：

- `volume` 单位为**股**（个股/ETF）；期货类由适配器按官方文档换算或原样透出；
- `amount` 单位为**元**；
- 上游单位与标准单位不同时，适配器按官方文档换算（换算依据经探针交叉验证）；
- 上游未标注单位时**原值透出，不编造换算**（缺失值 → `null`，不用 0 冒充）。

## 错误处理

```json
{
  "error": {
    "code": "UNSUPPORTED_CAPABILITY",
    "message": "Requested capability is not supported by the provider.",
    "request_id": "req_...",
    "details": {}
  }
}
```

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 参数校验失败 |
| `PROVIDER_NOT_FOUND` | 404 | 指定的 Provider 不存在 |
| `PROVIDER_DISABLED` | 409 | 指定的 Provider 已禁用 |
| `PROVIDER_NOT_IMPLEMENTED` | 501 | Provider 已启用但未实现 / SDK 未安装 |
| `UNSUPPORTED_CAPABILITY` | 422 | 能力目录或内部操作不支持该请求 |
| `NO_PROVIDER_AVAILABLE` | 409 | `auto` 路由无可用候选 |
| `CONCURRENCY_LIMIT` | 429 | 网关侧并发闸门：shared 模式在飞取数已达上限（`TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`），可稍后重试 |
| `UPSTREAM_TIMEOUT` | 504 | 上游调用整体超时（`TAPEHUB_UPSTREAM_TIMEOUT_SECONDS`，默认 30 秒；bars/quotes 单次请求上限，SSE 流不适用），可稍后重试或调大阈值 |
| `UPSTREAM_RATE_LIMITED` | 429 | 上游限频 |
| `UPSTREAM_ERROR` | 502 | 上游其它错误（details 可携带 `upstream_code`/`upstream_message`） |
| `QUOTE_NOT_READY` | 408 | 实时快照首包未就绪：订阅已建立但上游未在首包超时窗口内推送（各 Provider 的超时配置见 [operations.md](operations.md)；常见于标的不存在/无权限、非交易时段静默），可稍后重试 |
| `STREAM_LIMIT_REACHED` | 429 | 行情流并发客户端数已达 `TAPEHUB_STREAM_MAX_CLIENTS` 上限（服务容量闸门，可稍后重试或先断开空闲连接） |
| `UPSTREAM_SUBSCRIPTION_LIMIT` | 429 | 上游并发订阅 symbol 数已达计划上限（twelvedata WS credit / tiingo 软上限，配置见 [operations.md](operations.md)）；空闲退订释放额度后可重试，或升档后调大配置 |

SDK 异常不会穿透 API；日志不记录 Provider 密钥与完整行情响应。
