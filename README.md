# tapehub

通用行情数据网关：对外提供统一的 HTTP RESTful API，内部通过适配器接入多个行情数据源（akshare、tushare、tickflow、alphafeed、tqsdk）。

**当前版本实现「历史 K 线查询」与「tqsdk 实时行情（快照 + SSE 流式，限 `single` 单进程单用户模式）」两条业务链路，不建设行情数据存储平台。**

```text
统一请求参数
→ 参数校验
→ Provider 能力匹配与路由
→ Provider 内部操作路由
→ 转换为 Provider 专用参数
→ 调用 Provider
→ 转换为统一数据模型
→ 排序与分页
→ 返回统一响应
```

## 第一版边界

已实现：

- `GET /health`：健康检查（供其他项目配置 `base_url` 后测试连接）；
- `GET /v1/capabilities`：全部 Provider 能力目录；
- `GET /v1/providers/{provider}/capabilities`：单个 Provider 能力；
- `GET /v1/market/bars`：统一历史 K 线查询；
- `GET /v1/market/quotes`：实时快照（限 `single` 模式）：tqsdk
  （订阅-推送 Runtime）、twelvedata（WS 订阅缓存，GLOBAL 外汇/加密货币）与
  tickflow（无状态 REST 快照，CN/HK/US 股票）；
- `GET /v1/market/quotes/stream`：实时快照 SSE 流（单标的订阅推送、
  心跳与空闲退订、容量闸门，限 `single` 模式）：tqsdk（仅 `shared` 连接模式）
  与 twelvedata（WS 长连接，退订即释放上游订阅额度）；
- `GET /v1/instruments/resolve`：标的模糊解析（宽松输入 → 标准代码候选，
  纯本地规则、零上游调用，见「标的解析」章节）；
- Provider 注册、能力匹配与公共路由；Provider 内部二级操作路由的扩展机制；
- 统一错误处理；
- **akshare Provider 已接入**（接口签名按官方文档核实，见下文）；
- **TickFlow Provider 已接入**（REST API + 官方 Python SDK，API Key 从 `.env` 读取）；
- **AlphaFeed Provider 已接入**（REST API + 官方 Python SDK，API Key 从 `.env` 读取）；
- **Tushare Provider 已接入**（官方 Python SDK `pro_bar`/`fut_daily`，Token 从 `.env` 读取）；
- **TqSdk Provider 已接入**（官方 Python SDK `get_kline_serial`，快期账户凭据从 `.env` 读取）；
- **Twelve Data Provider 已接入**（外汇、加密货币与美股 stock/etf 历史 K 线，官方 Python 库 `twelvedata`，API Key 从 `.env` 读取）；
- 基于 Fake Provider 与 Mock SDK 的完整链路验证与单元/契约测试。

明确不实现（当前版本）：

- 数据库、Redis、行情缓存、CSV/Parquet 等行情落盘
  （实时链路的**进程内最新快照**除外——那是订阅状态，不是持久化）；
- 定时任务、全量转储 Worker、消息队列、双向 WebSocket（行情流式已按
  D8 决策采用 SSE 单向推送，与实时快照同样限 `TAPEHUB_DEPLOYMENT_MODE=single` 模式）；
- 逐笔、标的元数据等其它查询接口；
- 多进程/分布式部署（`TAPEHUB_DEPLOYMENT_MODE` 为守门字段，当前仅支持
  `single`，其他值启动即失败；未来以组件级配置形式提供，不预设）；
- Docker 与前端页面。

## 快速开始

使用 [uv](https://docs.astral.sh/uv/) 管理项目，Python 3.12。

```bash
# 安装 uv（任选其一）
# pipx install uv
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync --dev                    # 同步依赖（自动准备 Python 3.12）
uv sync --dev --extra akshare    # 安装 akshare SDK（可选依赖）
uv sync --dev --extra tickflow   # 安装 TickFlow 官方 SDK（可选依赖）
uv sync --dev --extra alphafeed  # 安装 AlphaFeed 官方 SDK（可选依赖）
uv sync --dev --extra tushare    # 安装 Tushare 官方 SDK（可选依赖）
uv sync --dev --extra tqsdk      # 安装 TqSdk 官方 SDK（可选依赖）
uv sync --dev --extra twelvedata # 安装 Twelve Data 官方库（可选依赖）

uv run pytest          # 运行测试（不依赖任何真实 SDK 与网络）
uv run ruff check .    # 静态检查
uv run uvicorn tapehub.api.app:app --reload   # 启动服务（默认 127.0.0.1:8000）
```

Provider SDK 均为**可选依赖**：不安装时服务正常启动，对应请求返回 `PROVIDER_NOT_IMPLEMENTED`（TickFlow/AlphaFeed 未配置 API Key、Tushare 未配置 Token、TqSdk 未配置快期账户时同样如此，能力目录不受影响）。

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
  "timestamp": "2026-01-01T00:00:00Z"
}
```

调用方可同时校验 **HTTP 200** 与 **`service == "tapehub"`**。该接口不访问任何上游 Provider，也不需要鉴权。

## API 示例

### 能力目录

```http
GET /v1/capabilities?asset_type=stock&interval=P1D
GET /v1/providers/akshare/capabilities
```

只读取本地能力目录，不实时访问上游；支持可选过滤参数 `provider`、`data_type`、`market`、`asset_type`、`interval`、`variant`；每个 Provider 条目携带 `default_variant`（「Provider × 资产类型」级缺省变体，variant 缺省时的解析依据）。`enabled`（是否启用，目录声明）与 `runtime_status`（适配器长连接运行时状态）是两个不同概念：`runtime_status` 经鸭子类型读取适配器属性，仅长连接类 Provider 有意义——tqsdk `shared` 模式回填真实 Runtime 状态（`idle` 懒启动前 / `starting` / `ready` / `recycling` 新一代重建中 / `fatal`），`per_request` 回滚模式与其余无长连接概念的 Provider 恒为 `unknown`（不伪造实时健康状态）。

### 历史 K 线

```http
GET /v1/market/bars?provider=akshare&asset_type=stock&instrument=CN-SSE-600000&interval=P1D&start=2026-01-01&end=2026-01-31&offset=0&limit=100
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
        "timestamp": "2026-01-05T00:00:00Z",
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
    "received_at": "2026-01-01T00:00:00Z",
    "offset": 0, "limit": 100, "count": 20, "partial": false
  },
  "warnings": []
}
```

缺失数值以 `null` 返回，不用 `0` 冒充；Provider 特有字段只能通过 `extensions` 受控透出（如 akshare 的 `pct_change`、`turnover_rate`、`hold`、`settle`）。

### 实时快照（tqsdk / twelvedata / tickflow）

```http
GET /v1/market/quotes?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-au2612
GET /v1/market/quotes?provider=twelvedata&asset_type=forex&instrument=GLOBAL-OTC-EUR/USD
```

参数：`provider`（默认 `auto`；当前 tqsdk 声明 CN 全资产、twelvedata 声明 GLOBAL 外汇/加密货币、tickflow 声明 CN/HK/US 股票的 quotes 能力）、`asset_type`（必填）、`instrument`（必填）、`variant`（可选）。**快照是当前时点的数据：无 `interval`、无时间范围、无分页参数**（`adjust` 同样不接受——复权是历史序列概念）。auto 优先级：由缺省路由配置的完整回退链裁决（CN 股票快照归 tqsdk、tickflow 回退；HK 与 US 股票快照为 tickflow 独占，显式 `provider=tickflow` 亦可选用）；凭据未配置的 Provider 自动跳过。

三类上游的交付模型不同，语义差异如实声明：

- **tqsdk（订阅-推送）**：REST 快照从共享 Runtime 的订阅缓存即时读取（首包超时 `QUOTE_NOT_READY` 408，行为要点见下与 TqSdk 映射表）；
- **twelvedata（WS 订阅缓存）**：快照与流同源，均来自 WS 共享订阅的实时 price 事件（2026-11-05 探针实证），冷订阅等待首包（超时同为 `QUOTE_NOT_READY` 408）、活跃订阅即时读缓存；上游 WS credit 按**并发活跃订阅 symbol** 计数（免费版 8），并发订阅达 `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT` 返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429），映射表见 Twelve Data 章节；
- **tickflow（无状态 REST）**：快照为单标的单次 `tf.quotes.get` 调用，无订阅/缓存语义（不产生 `QUOTE_NOT_READY`——要么返回快照，要么返回上游错误）；限频/无权限等 SDK 异常统一包装为 `UPSTREAM_ERROR`；不支持 SSE 流（M3 流式仍为 tqsdk/twelvedata），映射表见 TickFlow 章节。

以下请求/响应示例与行为要点为 **tqsdk 链路**；twelvedata 链路的字段口径见其映射表。

tqsdk 成功响应（节选，字段白名单以 P5 探针实证为准，见 TqSdk 映射表）：

```json
{
  "data": {
    "instrument": "CN-SHFE-au2612",
    "asset_type": "contract",
    "quote": {
      "timestamp": "2026-11-04T10:14:46.500000+08:00",
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
    "received_at": "2026-11-04T10:14:47.000000+08:00"
  }
}
```

tqsdk 行为要点：

- `data.quote.timestamp` 是行情时间（上游北京时间墙钟，统一 `+08:00` 输出），
  `meta.received_at` 是网关完成标准化的时刻；
- 首次请求某标的会先建立订阅并等待上游首包：未在
  `TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT`（默认 5 秒）内到达则返回
  `QUOTE_NOT_READY`（HTTP 408），常见于标的不存在/无权限或非交易时段上游静默；
- 订阅存活期间由 Runtime tick 循环持续保鲜，后续请求即时返回最新快照
  （M2 快照请求不退订，订阅累积由回收阈值管理，见 TqSdk 映射表）；
- 指数类标的无盘口：`bid/ask` 价格为 `null`（不用 0 冒充）、量为 `0`，如实透出。

### 实时行情流（SSE，tqsdk / twelvedata）

```http
GET /v1/market/quotes/stream?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-au2612
GET /v1/market/quotes/stream?provider=twelvedata&asset_type=forex&instrument=GLOBAL-OTC-EUR/USD
Accept: text/event-stream
```

参数与快照接口一致（`provider`/`asset_type`/`instrument`/`variant`），响应为
`text/event-stream`（HTTP/1.1 chunked 流，零新依赖）。**行情推送是纯单向流，
选 SSE 而非 WebSocket**（D8 决策）：复用 HTTP 鉴权/中间件/request_id，
客户端 `curl -N` 即可消费。仅实现 `stream_quote` 的 Provider 可用
（当前 tqsdk 与 twelvedata）；其余 Provider 在受理阶段即返回
`UNSUPPORTED_CAPABILITY`（422）。

三类事件帧（`event:` + `data:` JSON，帧间空行分隔）：

| event | data 结构 | 语义 |
| --- | --- | --- |
| `status` | `{status, instrument, asset_type, provider}` | 流生命周期：`subscribed`（订阅确认，**首事件**，含已缓存快照场景）/ `upstream_disconnected`（上游连接异常或服务停机；twelvedata 上游拒订同以此收尾）/ `recycling`（tqsdk 专属：Runtime 新一代重建——北京时间 04:00 跨交易日回收 / watchdog / 休眠订阅回收，活跃订阅自动重订，重订完成再发 `subscribed`；twelvedata 无代际回收，不产生此状态）/ `idle_unsubscribed`（流空闲超时，服务端本地退订并结束流） |
| `quote` | `{instrument, asset_type, quote, provider, variant, received_at}` | 与快照响应同构（字段白名单同 P5 实证口径），`quote` 内为标准快照 |
| `heartbeat` | `{type, request_id, ts}` | 空闲保活帧（默认 15s，仅出现在首事件之后），代理/负载均衡不至掐断空闲连接 |

行为要点：

- **断线与回收绝不静默断流**：`upstream_disconnected` / `recycling` /
  `idle_unsubscribed` 都是明确事件；04:00 回收时客户端先收 `recycling`，
  重订完成后收 `subscribed` 并恢复 `quote` 推送；
- **coalesce 丢旧保新（仅 quote 帧）**：慢客户端的消费缓冲有上限
  （`TAPEHUB_STREAM_QUEUE_SIZE`，默认 64），行情 `quote` 帧满时丢弃最旧帧
  ——行情「最新」比「完整」有价值；`status` 生命周期帧稀有且关键，
  **不参与也不被** coalesce 丢弃；
- **退订语义（诚实边界，按 Provider 区分）**：流关闭（客户端断开/空闲超时/停机）
  即本地退订（引用计数 -1、快照停止维护）。**tqsdk 协议不支持取消订阅**，上游
  订阅仍保留至代际回收（休眠订阅累计到 `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT`
  触发新一代重建清理）；**twelvedata 支持逐 symbol 真退订**，引用归零延迟
  60s（linger，兼顾轮询与快速重连免重订）后向上游发退订并即时释放 WS credit
  （探针实证：credit = 并发活跃订阅 symbol 数，退订/断连即释放、重订重连免费）；
- **容量闸门**：并发流数达 `TAPEHUB_STREAM_MAX_CLIENTS`（默认 8）返回
  `STREAM_LIMIT_REACHED`（HTTP 429）；受理阶段（响应头发出前）的错误统一
  JSON 错误结构，流中错误一律事件化收尾；
- **仅 `shared` 连接模式支持**（tqsdk：流依赖长连接订阅；`per_request` 回滚
  模式返回 `UNSUPPORTED_CAPABILITY` 422；twelvedata 恒为单共享 WS 连接，
  无模式概念）；
- **上游订阅容量闸门（twelvedata）**：并发订阅 symbol 数达
  `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT`（默认 8，免费版 WS credit 口径）
  时，新 symbol 的流/快照请求在受理阶段返回 `UPSTREAM_SUBSCRIPTION_LIMIT`
  （HTTP 429）；升档后调大该配置即可；
- **访问日志口径**：SSE 请求的「耗时」= 连接时长（长连接期间不产生新日志行）；
- 心跳与空闲退订由服务端管理：`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`
  （默认 300s）内无行情事件（心跳不算）即 `idle_unsubscribed` 收尾，
  客户端可随时重连。

## 请求规范

### instrument 标准代码

格式为 `{market}-{exchange}-{symbol}`，全部大写，例如 `CN-SSE-600000`、
`US-NASDAQ-AAPL`（美股股票）、`US-NYSE-SPY`（美股 ETF）。美股 exchange 段
取交易所简称（`NYSE`/`NASDAQ`/`CBOE`，twelvedata 白名单），symbol 段为交易所
原生 ticker（如 `AAPL`、`BRK.B`）。

- `market`、`exchange` 由服务端解析，调用方无需重复传递；
- **不接受** `CN.SSE.600000`、`600000` 等非标准格式；Provider 专用代码（如 akshare 的 `sh600000`、`RB0`）由适配器转换；
- 第一版只校验标准格式，不通过标的元数据验证真实资产类型。

### 标的解析（模糊查询）

`GET /v1/instruments/resolve` 把宽松输入解析为标准代码候选，供「先解析、
再查询」使用；bars/quotes 主链路的严格校验不受影响。

```
GET /v1/instruments/resolve?q=600000
GET /v1/instruments/resolve?q=000001&asset_type=index
GET /v1/instruments/resolve?q=AAPL&exchange=NASDAQ
```

参数：`q`（必填，宽松输入）、`asset_type`（可选提示，收窄歧义候选）、
`exchange`（可选提示，按标准代码 exchange 段过滤，如 `NASDAQ`）。
响应恒为 200（含空候选），候选多于一个时 `ambiguous: true`：

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
| 已是标准代码 | 原样回显（market/exchange 大写归一） | symbol 保留大小写（期货品种随交易所） |
| 纯 6 位数字 | A 股：`6/5/0/3/399/4/8/92` 段定交易所与资产类型 | `000xxx` 为指数/个股双关（上证指数系列与深市个股共用代码段），双候选；`001/002/003` 深市个股单候选 |
| 纯数字 1~5 位 | `HK-HKEX-` + 零填充 5 位 | `700` → `HK-HKEX-00700`；港股代码段不判资产类型（`02800` 是 ETF） |
| 纯字母 1~5 位 / 含点字母 | 美股 ticker 三候选（NYSE/NASDAQ/CBOE） | ticker 推不出交易所，可用 `exchange` 提示收窄 |
| 6 字母 3+3 双 ISO 4217 | `GLOBAL-OTC-基准/报价`（外汇） | 前三位为基准货币，`USDJPY` → `USD/JPY` |
| 以 USD/USDT/USDC 结尾的 6~9 字母 | `GLOBAL-CRYPTO-…`（加密货币） | `BTCUSD`、`BTCUSDT` |
| `代码.SH/SZ/BJ/HK/US` 后缀风格 | 对应市场（`600000.SH` → `CN-SSE-600000`） | 美股后缀不区分交易所，三候选 |
| `sh/sz/bj + 代码` 前缀风格 | A 股（`sh600000` → `CN-SSE-600000`） | 前缀已定交易所，`000` 段类型仍不定 |

诚实边界：

- **解析 ≠ 验证存在**：候选是格式级推导，标的是否真实存在由后续
  bars/quotes 请求经上游验证；`asset_type` 为 null 的候选（美股、港股、
  标准代码回显）查询时仍需显式传参（asset_type 必填不推断不变）；
- **期货/期权不解析**：品种代码与交易所无本地映射事实源，返回空候选
  （已是标准代码的期货输入按回显规则处理）；中证指数系列（如 `000905`）
  标准代码为 `CN-CSI-` 前缀，请直接传标准代码；
- 提示参数过滤后可能为空：如实返回空候选，不回退、不猜测；
- 规则依据为公开市场代码约定与 ISO 4217 货币码表，不代表任何上游
  数据源的能力声明。

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
| options 期权 | `option` | 期权 |
| forex 外汇 | `forex` | 外汇 |
| crypto 加密货币 | `crypto` | 加密货币 |

缺失或未知取值返回统一参数错误 `INVALID_REQUEST`。

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

- **变体名反映真实数据源**：仅 akshare 的上游确为东方财富/新浪/腾讯；
  TqSdk、Tushare、AlphaFeed、TickFlow、Twelve Data 与东方财富无关，
  变体与其 Provider 同名（即各自的上游服务/包名）；
- **`variant` 可选，无全局默认值**。未传递时表示不表达数据源偏好：
  该维度不参与能力过滤，由每个 Provider 在 manifest 中**必填声明**的
  `default_variant`（Provider × 资产类型级）具体化，`meta.variant` 回显实际值；
  `default_variant` 不覆盖其余维度时回退到声明顺序第一条匹配能力的第一个变体；
- 显式传递 `variant` 时严格匹配，不会静默降级；Provider 不支持的变体在能力目录即不声明；
- variant 与 capability、内部 operation 一样参与路由维度匹配；
- 每个 Provider 的 `default_variant` 声明见 `/v1/capabilities` 响应的
  `default_variant` 字段（如 akshare：stock/index → `eastmoney`，
  contract/continuous → `sina`；其余单源 Provider → 其自身变体）。

### adjust 复权方式（默认值约定）

`adjust` 取值 `none`（不复权）、`qfq`（前复权）、`hfq`（后复权）。**未传递时的声明默认值按资产类型区分**：

| 资产类型 | 默认 adjust |
| --- | --- |
| `stock` | **`qfq`（前复权）** |
| 其余（index、contract 等） | `none` |

> **美股 qfq 口径说明（twelvedata）**：US stock 的 `qfq` 映射上游 `adjust=all`
> （拆股 + 分红前复权，除息日前历史回溯调整，2026-11-06 探针实证生效）——与国内
> 「前复权含分红」口径**一致**。注意上游参数名只认 `adjust`，传 `adj` 会被静默忽略
> 落回默认（仅拆股）；不传时上游默认仅拆股复权，适配器因此始终显式传参。需要原始价
> 请显式传 `adjust=none`。详见下文 Twelve Data 映射表。

通用参数转换的原则是：**所有相关参数都显式传递给上游，不依赖上游默认值**。例如 akshare `stock_zh_a_hist` 的 `adjust` 默认 `""`（不复权），与 tapehub 的声明默认值（个股 qfq）不一致，因此适配器始终显式传参，保证「接口默认行为」与「上游实际行为」一致。

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

### 时间边界与时区

- 时间范围是**闭区间 `[start, end]`**，`start` 必须早于 `end`；
- 时间边界**必须携带时区偏移**（国内标的推荐 `+08:00`，如
  `2026-01-05T00:00:00+08:00`）；纯日期（`2026-01-01`）与无时区的裸时间
  一律拒绝，不做隐式时区解释；
- **项目内部统一北京时间（全市场）**：服务级时间戳（健康检查 `timestamp`、
  能力目录 `generated_at`）一律北京时间（`+08:00` 后缀）；行情数据时间戳
  亦全市场统一输出北京时间——2026-11-04 数据探针实测确认 twelvedata 上游
  口径（外汇墙钟为 Australia/Sydney、加密货币为 UTC）后，GLOBAL 标的不再
  维持 UTC 输出，与国内（CN）标的一致；美股（US）墙钟为
  America/New_York（EST/EDT，官方
  [交易所文档](https://twelvedata.com/exchanges/stocks)），日线为交易所本地
  date-only 时间（归一化后落北京当日 12:00/13:00 随夏令时）；上游墙钟的
  解析与转换由各适配器负责（见各 Provider 映射表）；
- 上游只支持按日期过滤时，适配器在标准化后按 `[start, end]` 闭区间精确复核。

### 分页语义

| 参数 | 默认 | 约束 |
| --- | --- | --- |
| `offset` | `0` | ≥ 0 |
| `limit` | `500` | ≥ 1，最大值由 `TAPEHUB_MAX_LIMIT` 配置（默认 5000） |

- 结果先按 `timestamp` **升序**排序，再应用 `offset`/`limit`；
- 两者描述**统一接口最终结果**的分页语义；akshare 接口均不支持原生分页，由适配器用共享组件在内存切片（不落盘）；
- 第一版不返回 `total`；响应 `meta` 返回 `offset`、`limit`、`count`。

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
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 |
| `UPSTREAM_RATE_LIMITED` | 429 | 上游限频 |
| `UPSTREAM_ERROR` | 502 | 上游其它错误 |
| `QUOTE_NOT_READY` | 408 | 实时快照首包未就绪：订阅已建立但上游未在首包超时窗口内推送（tqsdk `TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT` / twelvedata `TAPEHUB_TWELVEDATA_FIRST_SNAPSHOT_TIMEOUT`；常见于标的不存在/无权限、非交易时段静默），可稍后重试 |
| `STREAM_LIMIT_REACHED` | 429 | 行情流并发客户端数已达 `TAPEHUB_STREAM_MAX_CLIENTS` 上限（服务容量闸门，可稍后重试或先断开空闲连接） |
| `UPSTREAM_SUBSCRIPTION_LIMIT` | 429 | 上游并发订阅 symbol 数已达计划上限（twelvedata WS credit：`TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT`，默认 8）；空闲退订释放额度后可重试，或升档后调大配置 |

SDK 异常不会穿透 API；日志不记录 Provider 密钥与完整行情响应。

## 架构与扩展

```text
API 层 → Application 层 → Domain 层
                         ↑
        Provider 适配器实现 Domain Port
```

- **能力目录是唯一事实来源**：`/v1/capabilities` 与公共路由复用同一份目录；
- **两级路由**：公共 `ProviderRouter` 按 provider × data_type × market × asset_type × interval × adjust × **variant** 选 Provider；适配器按 manifest 的 `operations` 做内部二级路由，上游函数名不暴露到公共 API；
- **auto 路由优先级（显式声明，非隐式顺序）**：显式 `provider=X` > 缺省路由配置
  `src/tapehub/config/provider_routing.yaml`（可用 `TAPEHUB_ROUTING_CONFIG` 指向
  外部文件整体替换内置版本）的完整回退链。该文件按 `(data_type, market, asset_type)`
  三维全显式声明每组合的 Provider 回退顺序（首个「已注册、已启用且能力可解析」者
  为缺省；未启用条目自动跳过；回退只发生在路由选择阶段，请求执行失败不切换
  Provider）；注册表构建期强制校验**凡能力覆盖的组合必须恰有条目**、order 引用的
  Provider 必须已注册且实际声明该组合能力（缺失/重复/未知引用都拒绝启动）；
  代码内注册顺序不承载路由语义；
- **凭据驱动的注册**：内置 Provider 中需要凭据的（alphafeed/tickflow/tushare/
  tqsdk/twelvedata），`.env` 相应变量全部非空（strip 后）才启用；凭据缺失的
  Provider **能力目录仍可见但 `enabled=false`**，auto 路由跳过，显式请求返回
  `PROVIDER_DISABLED`（details 携带缺失的变量名）。akshare 无凭据概念恒启用。
  启用裁决：显式 `TAPEHUB_PROVIDER_ENABLED_OVERRIDES` > 凭据推断 > manifest
  `enabled`（显式启用可越过凭据推断作运维逃生门，日志告警）。只判非空不做
  格式校验：格式错误的凭据在请求时报 `UPSTREAM_ERROR`，启动不因凭据内容失败；
- **manifest 属于配置**：缺失、格式错误或校验失败直接启动失败（fail-fast）；
  路由配置同上；适配器导入失败（可选 SDK 未安装等）仍单 Provider 隔离，不影响启动；
- **公共能力声明 vs 内部 operation 映射**：manifest 的 `capabilities` 面向目录与公共路由（含 `variants` 声明）；`operations` 的 match 键包含全部六个维度且必须显式声明，面向适配器内部；对请求无意义的维度显式声明 `null`（如 quotes 快照的 interval/adjust，两维度在能力目录同样声明为空、路由时跳过），`null` 只与不携带该维度的请求匹配。

### akshare 已接入范围

接口签名均已按官方文档核实（https://akshare.akfamily.xyz/）：

| 资产类型 | variant | akshare 接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | eastmoney | `stock_zh_a_hist` | P1D/P1W/P1M | none/qfq/hfq | 全部 |
| stock | sina | `stock_zh_a_daily` | P1D | none/qfq/hfq | 全部 |
| stock | tencent | `stock_zh_a_hist_tx` | P1D | none/qfq/hfq | 全部 |
| index | eastmoney | `stock_zh_index_daily_em` | P1D | none | 全部 |
| index | sina | `stock_zh_index_daily` | P1D | none | 无 amount |
| index | tencent | `stock_zh_index_daily_tx` | P1D | none | 无 amount（上游 amount 列语义为成交量） |
| contract / continuous | sina | `futures_zh_daily_sina` | P1D | none | 全部（extensions: hold/settle） |

TickFlow（REST API `GET https://api.tickflow.org/v1/klines` / `GET /v1/quotes`，官方 SDK `tickflow`）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN/HK/US | tickflow（默认） | `tf.klines.get` | PT1M~PT30M/PT1H、P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: prev_close） |
| stock（quotes） | CN/HK/US | tickflow（默认） | `tf.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（extensions 见下） |

- API Key 通过环境变量 `TAPEHUB_TICKFLOW_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 标的代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`、`HK-HKEX-00700 → 00700.HK`、`US-NASDAQ-AAPL → AAPL.US`（美股 NYSE/NASDAQ/CBOE 统一 US 后缀，exchange 段仅作标准代码规范）；
- 周期映射：`PT1M→1m`、`PT5M→5m`、`PT10M→10m`、`PT15M→15m`、`PT30M→30m`、`PT1H→60m`、`P1D→1d`、`P1W→1w`、`P1M→1M`；季线 `1Q` 无法用 ISO 8601 Duration 表达，年线 `1Y` 超出项目「最大支持到月线」的周期范围，均不声明；
- 复权映射：`qfq→forward`（比例前复权）、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；SDK 自身默认 forward，适配器始终显式传参；港股/美股复权语义未经探针实证（美股复权口径可能与 A 股不同），生效性以探针报告为准；
- 快照字段映射：`last_price/volume/amount` 为标准字段（五档盘口上游无源字段恒 null）；`region/name/change_pct/change_amount/amplitude/turnover_rate/prev_close/open/high/low` 经 extensions 白名单透出。SDK 0.1.25 探针实测：返回 `list[dict]`（非文档 data 包装），**无 `session` 字段**（文档声明存在，防御映射存在才透出），`ext.type`（cn_equity 等）不在白名单不透出；比例字段为**小数比例（1% = 0.01，如 change_pct 0.0162 = 1.62%）**；上游支持逗号分隔批量（本项目单标的快照，单标的单次调用）；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 错误指纹（2026-11-07 探针实证）：免费档分钟线无权限为 SDK `PermissionError`「无分钟K线查询权限」（统一包装 `UPSTREAM_ERROR` 告知用户）；限频探针 6.5s 间隔（≈9 次/分钟）全程未触发；`RateLimitError` 类型存在于 SDK，429 指纹待更高频场景核实；
- 日线时间戳口径（探针实测）：CN/HK 日线为**北京时间零点**；US 日线为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移）；HK/US 日线 `amount` 上游原值 0（无成交额数据，按原值透出非缺失 null）；
- 历史深度（history_depth 探针实证，报告 20261107T145801+0800：早上市基准标的 CN 600602（1990-12-19 上交所开业首批老八股）/ HK 00002 / US GE（1892 年上市），二分 90 天粒度收敛 complete=True，日/周/月三周期同值）：CN ≈1991-01-18（不早于上交所开业日 1990-12-19）、HK ≈1980-02-15、US ≈1962-02-17。bars 能力拆 CN/HK/US 三块逐市场声明，`history.start` 取各市场估计窗口右端零点（approx ±90 天，真实最早可能更早；原合并块取跨市场最早 US 值对 CN 过度声明 29 年，已拆块修正）；分钟族（PT1M~PT1H）免费档无分钟权限（403 已三轮实证）无法核实，保持 null。基准标的已永久换为早上市标的：二分收敛点若远离其上市日即为真实库底，后续下探无需再改配置。

AlphaFeed（REST API `GET https://api.alphafeed.org/v1/klines`，官方 SDK `alphafeed`）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN/HK/US | alphafeed（默认） | `af.klines.get` | P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: prev_close） |
| stock（quotes） | CN/HK/US | alphafeed（默认） | `af.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（extensions 见下） |

- API Key 通过环境变量 `TAPEHUB_ALPHAFEED_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 标的代码映射：`CN-SSE-600519 → 600519.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`、`HK-HKEX-00700 → 00700.HK`、`US-NASDAQ-AAPL → AAPL.US`（美股 NYSE/NASDAQ/CBOE 统一 US 后缀；示例已按官方文档核实）；
- 周期映射：`P1D→1d`、`P1W→1w`、`P1M→1M`；分钟级周期（`1m`~`60m`）需上游套餐开通分钟 K 线权限，账户套餐未包含、不声明（alphafeed 是 CN stock bars 缺省 Provider，路由执行失败不回退，声明无权限的周期会 403 打断本可由 tickflow 服务的请求）；**上游实测接受 `10m` 并返回数据（探针 2026-11-07 实证，与官方文档周期枚举不符）**，`PT10M` 维持不声明；季线 `1Q` 无法用 ISO 8601 Duration 表达，年线 `1Y` 超出项目周期上界，均不声明；
- 缺省路由（2026-11-07 拍板）：CN 个股 K 线缺省 alphafeed；HK 个股 K 线缺省 alphafeed（tickflow 回退）；US 个股 K 线缺省 twelvedata（alphafeed 第二候选、tickflow 第三）；快照 HK/US 个股缺省 alphafeed（tickflow 回退）、CN 个股快照缺省 tqsdk（alphafeed 第二候选）；
- 复权映射：`qfq→forward`、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；上游自身默认 forward，适配器始终显式传参；港股/美股复权语义未经探针实证（美股复权口径可能与 A 股不同），生效性以探针报告为准；
- 快照字段映射：`last_price/volume/amount` 为标准字段（五档盘口上游无源字段恒 null）；`region/prev_close/open/high/low/name/change_pct/change_amount/amplitude/turnover_rate` 经 extensions 白名单透出，`ext.type`（cn_equity 等）不在白名单不透出；比例字段为**小数比例（1% = 0.01）**；上游支持批量（5 标的/次）与标的池（universes），本项目单标的快照语义、单元素 symbols 调用（限频 10 次/分钟为实际约束）；
- 快照为纯 REST 单次调用语义：无推送接口，不实现流式（`/v1/market/quotes/stream` 路由到 alphafeed 返回 `UNSUPPORTED_CAPABILITY`，与 tickflow 一致）；无首包超时语义（调用失败统一 `UPSTREAM_ERROR`）；
- 错误码映射：上游 429（限频，响应含 `retry_after_ms`）→ `UPSTREAM_RATE_LIMITED`（HTTP 429，`retry_after_ms` 可得时透传 details；SDK 对 429/5xx 自带最多 3 次退避重试，映射发生在重试耗尽后）；401/403 等其余上游异常统一 `UPSTREAM_ERROR`（SDK 异常类型不穿透 API）；无效 Key 指纹为 SDK `AuthenticationError`「Invalid or expired API key」（2026-11-07 探针实证）；无效标的上游返回 200 空数组（适配器按契约违背抛 `UPSTREAM_ERROR`）；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 历史深度（history_depth 探针二分收敛，报告 20261107T201101+0800 CN / 20261107T203514+0800 HK / 20261107T205210+0800 US，三周期各自口径一致）：CN ≈1991-01-14（真值带 (1990-10-16, 1991-01-14]，含上交所开业日 1990-12-19）、HK ≈1980-02-15、US ≈1962-02-17（HK/US 与 TickFlow 独立探测结果同日一致，两家上游历史库大概率同源）；`history.start` 取估计窗口右端零点（approx ±90 天，真实最早可能更早），bars 能力拆 CN/HK/US 三块逐市场声明；
- 日线时间戳口径（smoke 实测）：CN/HK 日线为**北京时间零点**；US 日线为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移）；HK/US 日线 `amount` 上游原值 0（无成交额数据，按原值透出非缺失 null），与 TickFlow 口径一致。

Tushare（官方 SDK `tushare`：`ts.pro_bar` / `pro.fut_daily`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| stock | tushare（默认） | `ts.pro_bar`（asset='E'） | P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: pre_close/change/pct_chg） |
| contract | tushare（默认） | `pro.fut_daily` | P1D | none | 全部（extensions: pre_close/settle/oi） |

- Token 通过环境变量 `TAPEHUB_TUSHARE_TOKEN` 提供并传入 `ts.pro_api`，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 股票代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`（后缀已按文档示例核实；北交所 .BJ 未在所引文档示例中出现，暂不声明）；
- 期货合约映射：`CN-SHFE-CU2401 → CU2401.SHF`（后缀仅核实上期所，文档示例 CU1811.SHF；DCE/CZCE/INE/GFEX/CFFEX 待逐所核实后补充）；主力连续另有专门接口（doc_id=189），本次未接入；
- 单位换算（按文档）：股票 `vol` 手→股（×100）、`amount` 千元→元（×1000）；期货 `vol` 保持手、`amount` 万元→元（×10000）；
- `fut_daily` 单次上限 2000 条：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；该接口要求上游账户 2000 积分；
- 上游积分限频/错误码未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

TqSdk（官方 SDK `tqsdk`：`TqApi(auth=TqAuth(...)).get_kline_serial(...)`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| contract / continuous / weighted-index | tqsdk（默认） | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none | 无 amount（上游无成交额列；extensions: open_oi/close_oi） |
| stock | tqsdk | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none/qfq/hfq | 无 amount；volume 单位未核实，原样透出 |
| index | tqsdk | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none（adj_type 对指数无效） | 无 amount；无持仓量列 → extensions null |
| etf | tqsdk（默认） | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none/qfq/hfq | 无 amount；volume 单位未核实，原样透出 |

- 变体与 default_variant 均为 `tqsdk`（与 Provider 同名，官方 SDK 包名；背后为天勤行情平台，与东方财富无关）：缺省请求即可路由，也可显式传 `variant=tqsdk`；
- 缺省路由（provider_routing.yaml 裁决）：期货三类与 ETF 归 tqsdk；
  stock/index 仅声明能力、不参与个股/指数 K 线的 auto 回退链
  （provider_routing.yaml 裁决：个股缺省 alphafeed，tqsdk 股票/指数需显式
  `provider=tqsdk`；指数仍归 akshare）；
- **权限说明（usage/mddatas 原文，诚实标注）**：数据可见性由**天勤平台账户
  档位**决定（免费版/专业版），与 SDK 及其版本无关。免费版账户仅期货/期权
  与四大指数（上证50/沪深300/中证500/中证1000）实时行情，A股股票/ETF 实时
  与历史需专业版账户。**当前部署所用快期账户为专业版**（2026-11-04 确认，
  探针盘中实测：股票/ETF 实时五档可用、ETF 历史日线可用）；若换用免费版
  账户，无权限请求失败包装为 `UPSTREAM_ERROR`，不视为能力缺口；
- 复权映射（API 参考 `adj_type` 仅对股票/基金有效）：`qfq→F`、`hfq→B`、`none→None`
  （期货/指数恒为 None，显式传参不依赖上游默认值）；上游「暂不支持复权获取多合约 K 线」
  与本适配器单合约调用不冲突；
- 快期账户凭据（`TqAuth(用户名, 密码)`，必填）通过环境变量 `TAPEHUB_TQSDK_AUTH_USER` / `TAPEHUB_TQSDK_AUTH_PASSWORD` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；任一未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- **连接模式 `TAPEHUB_TQSDK_CONN_MODE`**（默认 `shared`，2026-11-05 15.3h 观察期验证后翻转；`per_request` 为回滚配置）：
  - `shared`：进程内唯一长连接 Runtime（`TqSdkRuntime`，专属线程 tick 驱动 + `create_task` 并发协程 +
    `wait_update` 维持推送，2026-11-04 实验验证），首次请求懒启动，此后每请求节省全部建连开销；
    配套：并发闸门 `TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`（默认 16，超出 429 `CONCURRENCY_LIMIT`）、
    连续 wait_update 异常自动重建新一代、北京时间 04:00 跨交易日回收（采纳官方跨交易日重启建议；
    2026-11-05 15.3h 观察期实证：recycle triggered → gen2 重建 0.94s → 回收后请求全 200、
    活跃订阅自动重订，证据见 probes/results/shared_observation.log 末尾结论段）、
    停机/回收时在飞请求显式失败（可快速重试）。两种模式标准化语义完全一致，`shared` 问题可一键切回；
- 合约代码映射（交易所全大写，品种代码大小写遵从交易所规定）：`CN-SHFE-cu2607 → SHFE.cu2607`、`CN-CZCE-SR609 → CZCE.SR609`；主力连续 `CN-SHFE-cu → KQ.m@SHFE.cu`；指数连续/加权 `CN-SHFE-bu → KQ.i@SHFE.bu`（标准 symbol 允许小写品种代码，如 tqsdk 的小写品种约定）；
- 周期映射（周期为不超过 1 日的任意秒数）：`PT1M→60`、`PT5M→300`、`PT15M→900`、`PT30M→1800`、`PT1H→3600`、`P1D→86400`；
- 上游按“最新 N 根”返回（`data_length`）：**单次上限 2026-11-03 探针实测 5000 根**
  （PT1M/PT5M 同时恰好 5000；文档 8000/10000 口径未复现，疑受当前套餐限制，
  升级套餐后需重新实证）；当返回数量达到上限且最早一根仍晚于请求 `start` 时，
  更早数据取不到，响应 `meta.partial=true` 且 `warnings` 提示；
- **分钟线为滚动保留**（2026-11-03 探针实测边界，精度 ±5 天，随时间前移）：
  `history` 以 `retention_days` 声明——覆盖「now − N 天 → now」，客户端按查询
  时点自行换算。取值向下取整留安全余量：不足 90 天取 7 的倍数（周）、
  不小于 90 天取 30 的倍数（月）。边界按数据类划分——现货三类逐一相同，
  期货两类各自一致：**1m** 现货 35 天、期货 14 天；**5m** 现货 150 天、期货 84 天；
  **15m** 现货 450 天、期货 240 天；**30m** 现货 930 天、期货 450 天、加权 390 天；
  **1H** 现货 1500 天、期货 840 天、加权 690 天。六周期折算根数均落在
  **5000 根量级**，与单次上限（data_length 5000）一致——保留深度即序列存储上限；
- `datetime` 为纳秒时间戳（绝对时间），标准化为北京时间（`+08:00`）；期货/指数无复权概念，`adjust` 仅声明 none；
- **复权实证**（2026-11-03 探针）：hfq 与 none 差异显著（生效）；分钟级 qfq 与 none 相同（天勤特性，如实标注）；日线 qfq/hfq 均有差异；
- **history 回填**（2026-11-03 探针二分估计：首轮粒度 90 天、次轮 5 天）：
  日线以固定起点 `start` 声明——continuous/weighted-index 自 2016-01-05、
  stock/index/etf 自 2018-01-02；contract 随挂牌期限，保持 null；
  分钟级以滚动保留 `retention_days` 声明（见上）；
  `/v1/capabilities` 的 interval history 字段按声明形式原样透出；
- 代码映射（股票/指数/ETF 同期货格式）：`CN-SSE-600000 → SSE.600000`、`CN-SZSE-000001 → SZSE.000001`、`CN-SSE-000016 → SSE.000016`、`CN-SSE-510050 → SSE.510050`（mddatas 文档示例口径；指数仅核实 SSE/SZSE 发布的代码）；
- 连接实例：`shared` 模式由 Runtime 长连接复用（官方推荐的并发用法）；
  `per_request` 回滚模式每次请求在线程池中独立创建并关闭 TqApi 实例（官方约定一个线程一个实例）；
- 上游 SDK 异常类型未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

实时快照（`GET /v1/market/quotes`，上游接口 `api.get_quote`，M2）：

| 资产类型 | 变体 | 上游接口 | 周期/复权 | 标准字段 |
| --- | --- | --- | --- | --- |
| contract / continuous / weighted-index | tqsdk（默认） | `api.get_quote` | 无（不适用） | 全部 + 五档（指数/加权除外，见下） |
| stock / etf | tqsdk | `api.get_quote` | 无（不适用） | 全部 + 五档（需专业版账户，运行期事实） |
| index | tqsdk | `api.get_quote` | 无（不适用） | 无盘口：bid/ask 价格 null、量 0 |

- 字段白名单以 **P5 探针实证**（2026-11-04，probes/results/tqsdk/20261104T012958Z）为准：
  标准字段 `timestamp`/`last_price`/五档买卖价量（`bid|ask_price|volume1..5`）/`volume`/`amount`；
  `extensions` 白名单 `open`/`highest`/`lowest`/`average`/`pre_settlement`/`pre_close`/
  `open_interest`/`pre_open_interest`/`upper_limit`/`lower_limit`（盘中为 NaN 的字段如
  `close`/`settlement` 不透出；合约元数据类字段 M2 不透出）；
- `datetime` 为北京时间墙钟字符串（如 `2026-11-04 10:14:46.500000`），统一输出 `+08:00`；
- 单位：期货 `volume` 保持手、股票/ETF 为股（2026-11-03 探针量级判定，同 K 线口径）、
  `amount` 为元，均原样透出；
- **订阅生命周期**（shared 模式）：首次请求建立订阅（`await get_quote` 等首包，由 Runtime
  tick 循环驱动），此后快照由 tick 循环 `is_changing` 判定增量、线程内拷贝发布缓存，
  后续请求即时返回；M2 快照请求不退订（快照是订阅状态），退订接口为 SSE 流式（M3）预留。
  tqsdk 协议**不支持取消订阅**：退订只停止本地维护，休眠订阅的上游推送仍在消耗带宽，
  累计到 `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT`（默认 64）触发新一代重建清理（仍被引用的
  订阅自动重订；北京时间 04:00 跨交易日回收同理重订）；
- 首包超时 `TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT`（默认 5 秒）→ `QUOTE_NOT_READY` 408；
- `per_request` 回滚模式下快照同样可用（每请求独立实例订阅取首包后关闭，无缓存复用）。

实时行情流（`GET /v1/market/quotes/stream`，上游接口 `api.get_quote` 订阅推送，M3）：

- 仅 `shared` 模式支持（流依赖长连接订阅）：Runtime 流监听器在 tick 循环
  `is_changing` 判定处发布（与快照缓存同源的线程内拷贝），经
  `loop.call_soon_threadsafe` 桥接进每流的有界缓冲——跨线程只传拷贝、
  不阻塞 tick、不引入新的 peek 行为变化；字段白名单与快照一致（P5 实证）；
- **coalesce 丢旧保新只约束 quote 帧**（`TAPEHUB_STREAM_QUEUE_SIZE`，默认 64）；
  `status` 生命周期帧（订阅确认/回收/断连）不参与也不被丢弃；
- 心跳（`TAPEHUB_STREAM_HEARTBEAT_SECONDS`，默认 15s，仅出现在首事件之后）、
  空闲退订（`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`，默认 300s →
  `idle_unsubscribed` 收尾）与容量闸门（`TAPEHUB_STREAM_MAX_CLIENTS`，默认 8 →
  `STREAM_LIMIT_REACHED` 429）由 Application 层流服务统一管理，Provider 无关；
- 流关闭即本地退订（引用计数 -1）；上游订阅保留至代际回收（同快照接口口径）；
- 请求规范、事件帧结构与语义见「实时行情流（SSE）」章节。

单位约定（按官方文档）：

- 个股 `volume` 统一为**股**：东财接口返回手，适配器 ×100 换算；新浪/腾讯为股，原样返回；TqSdk 股票/ETF 的 volume 单位文档未写明，**2026-11-03 探针量级对照判定为股**（600000 日 volume ≈5×10^7），原样透出；
- `amount` 统一为**元**；指数/期货的上游未标注单位时原样返回（见各接口文档）；
- 交易所映射（已核实部分）：个股/指数 `SSE→sh`、`SZSE→sz`；指数东财另支持 `CSI→csi`、`BSE→bj`；期货合约符号原样传递（如 `RB2410`），主力连续为品种代码+`0`（如 `RB`→`RB0`）。

未接入说明（诚实边界，不编造能力）：

- `futures_hist_em`（东财期货）需要**中文合约名**（如“热卷主连”），无法从标准代码映射，第一版不接入；
- 加权（`weighted-index`）在上述接口文档中没有符号说明，暂不声明能力；
- 历史覆盖范围（`history`）已由数据探针实测回填（报告 20261106T211255+0800）：个股各源 `≈1999-12-08`（二分近似，窗口粒度 90 天、complete=True，真值≈标的上市日 1999-11-10；P1W/P1M 与 P1D 同上游端点同数据区间，同值声明）、指数各源 `≈1991-01-01`（探针下限触底）；期货为**全量模式精确值**（新浪期货接口无日期参数，适配器每次拉全量历史后内存过滤，单次调用直接读最早 bar）：单合约 RB2410 实测 `2023-10-17`（各合约以自身上市/摘牌日为准），主力连续 RB 实测 `2009-03-27`（与上期所螺纹钢期货上市日一致，相互印证）；
- **跨源一致性（探针实证，2026-11-06 与 2026-11-04 两轮一致）**：未复权（none）三源完全一致（指数新浪源多一位小数，±0.005 属表示层精度差）；**复权序列跨源不保证一致**——qfq 东财与腾讯一致、新浪复权因子口径不同（±0.24 元），hfq 三源基准各异（最大 ±105 元），**复权序列跨源不可直接混用，跨源对比仅 none 可靠**；探针对复权跨源差异如实判 suspect 留证。

Twelve Data（官方库 `twelvedata`（twelvedata-python）：`TDClient(apikey=...).time_series(...).as_json()`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| forex | twelvedata（默认） | `td.time_series` | PT1M~PT8H（含 PT45M）、P1D/P1W/P1M | none | 无 volume/amount（外汇无成交量） |
| crypto | twelvedata（默认） | `td.time_series` | PT1M~PT8H（含 PT45M）、P1D/P1W/P1M | none | 无 amount（volume 上游可能缺失 → null） |
| US stock | twelvedata（默认） | `td.time_series`（显式传 `exchange` + `adj`） | PT1M~PT8H（含 PT45M）、P1D/P1W/P1M | none、qfq | volume 有值；无 amount |
| US etf | twelvedata（默认） | `td.time_series`（显式传 `exchange` + `adj`） | PT1M~PT8H（含 PT45M）、P1D/P1W/P1M | none | volume 有值；无 amount |

- API Key 通过环境变量 `TAPEHUB_TWELVEDATA_API_KEY` 提供并传入 `TDClient(apikey=...)`，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 标的代码约定（symbol 段允许斜杠，对应上游货币对写法）：外汇 `GLOBAL-OTC-EUR/USD → EUR/USD`（ISO 4217，OTC 场外聚合报价）；加密货币 `GLOBAL-CRYPTO-BTC/USD → BTC/USD`（聚合对；交易所专属对与 `exchange` 筛选参数暂不支持）；美股 `US-NASDAQ-AAPL → symbol=AAPL&exchange=NASDAQ`（原生 ticker + 显式 exchange 消歧，exchange 段白名单 **NYSE/NASDAQ/CBOE**——OTC 粉单与 IEX 不接入，2026-11-05 目录探针实证分布后收敛）；
- **美股 adjust 口径**：`qfq` 映射上游 `adjust=all`——**拆股+分红前复权，与国内口径一致**（2026-11-06 探针实证：`adjust=dividends` 对除息日前历史回溯调整生效，AAPL 2026 年 212 个交易日中 150 天与原始价有差异）；`none` → `adjust=none`（原始价）；**参数名陷阱**：上游 REST 只认 `adjust`，传 `adj` 会被静默忽略落回默认（=splits 仅拆股，AAPL 2006 深度数据实证）；`hfq` 上游无等价物不声明；etf 复权行为未核实，仅声明 `none`；
- interval 枚举（上游完整支持 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month）：本适配器声明全部 12 个周期 `PT1M/PT5M/PT15M/PT30M/PT45M/PT1H/PT2H/PT4H/PT8H/P1D/P1W/P1M`（45min/2h/4h/8h 经数据探针实测可用后补充声明，报告 20261104T061857Z）；
- 历史覆盖范围（`history`）已由全量深度探针实测回填（粒度 90 天，approx）：forex（2026-11-04）日内 `≈2020-01-31~04-08`，日/周/月触底 `2000-01-01`（真实更早、待精化）；crypto（2026-11-04）日内 `≈2020-01-07~04-07`、日/周 `≈2017-08-29`、月 `≈2017-08-02`；美股（2026-11-07，报告 `20261107T111630+0800`，人工确认）stock 日内 `≈2019-02-01~2020-04-11`、etf 日内 `≈2020-02-15~2020-04-11`，日/周/月触底 `2000-01-01`（真实更早、待精化）；
- bar 时间墙钟口径按资产类型区分（外汇/加密 2026-11-04、美股 2026-11-05 实测 + [官方文档](https://twelvedata.com/exchanges/physical_currency)、[美股交易所页](https://twelvedata.com/exchanges/stocks)）：外汇按 **Australia/Sydney**（AEDT/AEST 随夏令时切换，用 `ZoneInfo` 解析）、加密货币按 **UTC**、美股按 **America/New_York**（EST/EDT 随夏令时切换；日线/周线/月线上游强制返回交易所本地 date-only 时间）；适配器将墙钟解析为对应时区的 aware 时间后，由领域层统一归一化为北京时间输出；`start_date`/`end_date` 为上游口径墙钟 `YYYY-MM-DDTHH:MM:SS`，由适配器从北京时间转换后显式传递；`outputsize` 显式传上限 5000（上游默认仅 30）；
- **CN/HK 不支持（诚实边界）**：上游目录虽有 China stock/etf、Hong Kong stock/etf，但免费档 Basic 可用标的仅各 1 个（其余 404 Pro 门槛，指纹 "This symbol is available starting with the Pro or Venture plan"，2026-11-05 目录探针实证），且官方交易所页标注 XSHG/XSHE 为 **EOD-only（任何档位无日内）**；故不声明 CN/HK 能力，升档后按探针流程实测回填；
- 实测注意（2026-11-04）：外汇 `1h` 与 `1day` 数据源不一致——任何日切窗口（Sydney/UTC/纽约 17:00 ET）的 1h 聚合都无法精确还原 `1day` 的 OHLC，需要日线时请直接请求 `P1D`；加密货币 `1h` 聚合可完美还原 `1day`（UTC 0 点日切）；
- 上游 `values` 默认倒序（最新在前），适配器统一按升序排序输出；`values` 字段为字符串，标准化为数值；
- 区间无数据行为（2026-11-04 探针实测核实）：上游返回 400 `BadRequestError` "No data is available on the specified dates" 而非空列表，适配器按指纹归一化为空结果；
- 其余上游 SDK/HTTP 异常类型与限频规则未在所引文档中核实，统一包装为 `UPSTREAM_ERROR`；实测错误指纹（2026-11-05 探针）：无效 Key → 401 `InvalidApiKeyError` "apikey parameter is incorrect"、无效标的 → 404 `TwelveDataError` "symbol or figi parameter is missing or invalid"。

实时快照与快照流（`GET /v1/market/quotes` / `GET /v1/market/quotes/stream`，上游 WS
`wss://ws.twelvedata.com/v1/quotes/price`，共享单连接订阅推送——M2 快照读订阅缓存，
M3 流同源；REST `td.quote` 按需路径已于 2026-11-05 退役）：

| 资产类型 | 变体 | 上游接口 | 周期/复权 | 标准字段 |
| --- | --- | --- | --- | --- |
| forex | twelvedata（默认） | WS `subscribe` price 事件 | 无（不适用） | timestamp/last_price |
| crypto | twelvedata | WS `subscribe` price 事件 | 无（不适用） | timestamp/last_price |

- 上游行为以 **WS 探针实证**（2026-11-05，probes/results/twelvedata/
  ws-behavior-20261105T221000+0800，不入库）为准：credit = **当前活跃订阅
  symbol 数**（免费版 8）——退订即时释放、重订重连免费；退订有
  `unsubscribe-status` 显式确认（与确认间存在在途事件竞态，以确认为准）；
  heartbeat 每 ~10s 有应答；上游拒订（`subscribe-status.fails`）以
  `upstream_disconnected` 显式收尾，绝不静默挂死；
- 字段口径：五档盘口、`amount`、`volume` 上游 price 事件无源字段，**恒 null**
  （不用 0 冒充）；`timestamp` 为**分钟桶** Unix 秒（对齐分钟边界，非 tick
  时刻；事件到达频率实测约 1 条/秒，实时性远超原 REST 分钟聚合快照），
  按 UTC 解释统一北京时间 `+08:00` 输出，快照新鲜度由 `meta.received_at`
  表达；缺时间戳/缺价格属上游契约违背，返回 `UPSTREAM_ERROR`；
- `extensions` 白名单（同一探针实证回填）：`currency_base`/`currency_quote`/
  `exchange`/`type` 四个字符串字段（实测 EUR/USD 无 `exchange`、BTC/USD 有
  且为聚合源交易所如 `"Binance"`；文档 XAU/USD 示例的 bid/ask 不可推广到
  货币对，不透出）；
- Runtime 形态：asyncio 原生共享单连接（免费版限 1 连接），per-symbol 引用
  计数；引用归零延迟 60s（linger）真退订释放 credit；断连广播
  `upstream_disconnected` 后退避重连自动重订（重订免费，探针实证 0.33s 内
  恢复首包）；**无代际回收（不产生 `recycling` 事件）**，与 tqsdk 的差异
  见「实时行情流」一节；
- 并发订阅上限：`TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT`（默认 8，免费版
  WS credit 口径），达限返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429），升档后
  调大即可；冷订阅首包超时 `TAPEHUB_TWELVEDATA_FIRST_SNAPSHOT_TIMEOUT`
  （默认 5s）返回 `QUOTE_NOT_READY`（408），活跃订阅即时读缓存。

六个内置 Provider（akshare、tickflow、alphafeed、tushare、tqsdk、twelvedata）均已接入，接口签名与数据口径按各自官方文档核实；未配置凭据或未安装 SDK 时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`，`provider=auto` 在无可用候选时返回 `NO_PROVIDER_AVAILABLE`，也**绝不返回写死的模拟行情**。

### 新增 / 接入 Provider

1. 新建 `src/tapehub/providers/<name>/`，放置 `manifest.yaml`（能力声明 + operations 映射，match 六维度显式声明）；
2. 实现 `adapter.py`（继承 `BaseProviderAdapter`，`implemented = True`，`fetch_bars` 内延迟导入 SDK 并用 `call_in_thread` 隔离同步调用）；
3. 新增 `request_mapper.py`（所有参数显式传递，不依赖上游默认值）与 `response_mapper.py`（缺失值→null，扩展字段白名单，单位按文档换算）；
4. `pyproject.toml` 以 `[project.optional-dependencies]` 添加该 SDK；
5. 凭据只能通过环境变量提供，禁止写入 manifest、日志或异常响应。

接入一个新 Provider 需要修改的最小文件集合：

```text
src/tapehub/providers/<name>/manifest.yaml
src/tapehub/providers/<name>/adapter.py
src/tapehub/providers/<name>/request_mapper.py   # 新增
src/tapehub/providers/<name>/response_mapper.py  # 新增
pyproject.toml
tests/                                           # 该适配器的 Mock 测试
```

Application/API 层与其它 Provider 不需要任何改动。

### 配置

环境变量前缀 `TAPEHUB_`（见 [.env.example](.env.example)）：`TAPEHUB_VERSION`、`TAPEHUB_API_VERSION`、`TAPEHUB_CATALOG_VERSION`、`TAPEHUB_MAX_LIMIT`、`TAPEHUB_PROVIDER_ENABLED_OVERRIDES`（JSON，如 `{"akshare": false}` 可整体停用 akshare）、`TAPEHUB_TICKFLOW_API_KEY`、`TAPEHUB_ALPHAFEED_API_KEY`、`TAPEHUB_TUSHARE_TOKEN`、`TAPEHUB_TQSDK_AUTH_USER`、`TAPEHUB_TQSDK_AUTH_PASSWORD`、`TAPEHUB_TQSDK_CONN_MODE`（`shared` 默认/`per_request` 回滚）、`TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`、`TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT`（快照首包等待，默认 5 秒）、`TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT`（休眠订阅回收阈值，默认 64）、`TAPEHUB_STREAM_MAX_CLIENTS`（SSE 并发流上限，默认 8）、`TAPEHUB_STREAM_QUEUE_SIZE`（每流 quote 帧缓冲，默认 64）、`TAPEHUB_STREAM_HEARTBEAT_SECONDS`（SSE 心跳间隔，默认 15 秒）、`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`（流空闲退订超时，默认 300 秒）、`TAPEHUB_TWELVEDATA_API_KEY`（Provider 凭据，只在内存中传给 SDK 客户端）。`.env` 不提交版本库。

### 未来扩展边界（现在不实现）

- **定时全量转储**：适配器不依赖 FastAPI 请求上下文，未来 Worker 可复用统一查询与分页模型逐批拉取；
- **新资产类型**：扩展标准枚举、能力清单与对应操作映射即可；
- 第一版不存储任何行情数据。

## 测试

```bash
uv run pytest
```

所有外部 SDK 调用均通过 `sys.modules` 注入的桩 Mock，测试不发起真实网络请求、不落盘、不依赖真实 Token；akshare 的调用参数与响应列均按官方文档构造。
