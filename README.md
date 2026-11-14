# tapehub

通用行情数据网关：对外提供统一的 HTTP RESTful API，内部通过适配器接入多个行情数据源（akshare、tushare、tickflow、alphafeed、tqsdk、tiingo、twelvedata）。

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
  （订阅-推送 Runtime）、twelvedata（WS 订阅缓存，GLOBAL 外汇/加密货币）、
  tiingo（WS 订阅缓存，GLOBAL 外汇/加密货币）与
  tickflow（无状态 REST 快照，CN/HK/US 股票）；
- `GET /v1/market/quotes/stream`：实时快照 SSE 流（单标的订阅推送、
  心跳与空闲退订、容量闸门，限 `single` 模式）：tqsdk（仅 `shared` 连接模式）、
  twelvedata（WS 长连接，退订即释放上游订阅额度）与
  tiingo（WS 长连接，crypto/fx 双端点共享订阅）；
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
- **Tiingo Provider 已接入**（外汇与加密货币历史 K 线 httpx 直连 + 实时快照/快照流 WS 直连——无官方 Python SDK，API Key 从 `.env` 读取；免费档配额紧，缺省路由置 twelvedata 之后回退位）；
- **Futunn Provider 已接入**（CN stock/index/etf 与 HK/US stock 的 bars + quotes，富途 OpenAPI HTTP 直连——无官方 Python SDK，「方式二：传统 API Key」AppKey 签名认证，凭据从 `.env` 读取；缺省路由置各组合回退链最末位，第一版不做流式）；
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

uv sync --dev --all-extras       # 同步依赖 + 全部 Provider SDK（推荐日常使用）
                                 # 注意：uv 默认「精确同步」——venv 中不在当前选中
                                 # 集合里的包会被移除。uv sync 只选部分 extra 时，
                                 # 其余 Provider SDK 会被卸载；Provider SDK 之间
                                 # 解析无冲突，务必用 --all-extras 共存
uv sync --dev --extra akshare    # 仅装单个 SDK 的写法（会移除其它 Provider SDK）

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

### 实时快照（tqsdk / twelvedata / tiingo / tickflow / ths / futunn）

```http
GET /v1/market/quotes?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-AU2612
GET /v1/market/quotes?provider=twelvedata&asset_type=forex&instrument=GLOBAL-OTC-EUR/USD
```

参数：`provider`（默认 `auto`；当前 tqsdk 声明 CN 全资产、twelvedata 与 tiingo 声明 GLOBAL 外汇/加密货币、tickflow 声明 CN/HK/US 股票、ths 声明 CN 股票/指数/ETF/期货/期权的 quotes 能力、futunn 声明 CN stock/index/etf 与 HK/US stock 的 quotes 能力）、`asset_type`（必填）、`instrument`（必填）、`variant`（可选）。**快照是当前时点的数据：无 `interval`、无时间范围、无分页参数**（`adjust` 同样不接受——复权是历史序列概念）。auto 优先级：由缺省路由配置的完整回退链裁决（CN 股票快照归 tqsdk、tickflow 回退、ths 末位；CN 期权快照为 ths 独占；HK 与 US 股票快照为 tickflow 独占，显式 `provider=tickflow` 亦可选用；GLOBAL 外汇/加密货币快照缺省 twelvedata、tiingo 回退；futunn 置上述各组合回退链最末位）；凭据未配置的 Provider 自动跳过。

四类上游的交付模型不同，语义差异如实声明：

- **tqsdk（订阅-推送）**：REST 快照从共享 Runtime 的订阅缓存即时读取（首包超时 `QUOTE_NOT_READY` 408，行为要点见下与 TqSdk 映射表）；
- **twelvedata（WS 订阅缓存）**：快照与流同源，均来自 WS 共享订阅的实时 price 事件（2026-11-05 探针实证），冷订阅等待首包（超时同为 `QUOTE_NOT_READY` 408）、活跃订阅即时读缓存；上游 WS credit 按**并发活跃订阅 symbol** 计数（免费版 8），并发订阅达 `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT` 返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429），映射表见 Twelve Data 章节；
- **tiingo（WS 订阅缓存）**：快照与流同源（crypto/fx 双端点共享订阅，WS 不计 REST 请求配额），冷订阅等待首包（超时同为 `QUOTE_NOT_READY` 408）、活跃订阅即时读缓存；并发订阅达 `TAPEHUB_TIINGO_SUBSCRIPTION_LIMIT` 返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429），映射表见 Tiingo 章节；
- **tickflow（无状态 REST）**：快照为单标的单次 `tf.quotes.get` 调用，无订阅/缓存语义（不产生 `QUOTE_NOT_READY`——要么返回快照，要么返回上游错误）；限频/无权限等 SDK 异常统一包装为 `UPSTREAM_ERROR`；不支持 SSE 流（M3 流式仍为 tqsdk/twelvedata/tiingo），映射表见 TickFlow 章节；
- **futunn（无状态 REST）**：快照为单标的单次 `POST /api/v1.0/quote/stock-quote` 调用（与 tickflow/ths 同语义，AppKey 签名认证），无盘口五档（bid/ask 恒 null）；不支持 SSE 流（第一版不做流式——WS 行情推送专区未接入，用户拍板 2026-11-14），映射表见富途章节。

以下请求/响应示例与行为要点为 **tqsdk 链路**；twelvedata 与 tiingo 链路的字段口径见各自映射表。

tqsdk 成功响应（节选，字段白名单以 P5 探针实证为准，见 TqSdk 映射表）：

```json
{
  "data": {
    "instrument": "CN-SHFE-AU2612",
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
- M2 快照为**按请求租约订阅**：请求结束（成功/408/客户端断开）即退订本地
  维护，Quote 转入休眠账本（上游订阅因协议限制保留）——重复请求经休眠
  唤醒即时返回新鲜快照（唤醒时发布 Quote 当前值，无陈旧缓存）；休眠订阅
  累计到 `TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT` 触发新一代重建清理；
- 指数类标的无盘口：`bid/ask` 价格为 `null`（不用 0 冒充）、量为 `0`，如实透出。

### 实时行情流（SSE，tqsdk / twelvedata / tiingo）

```http
GET /v1/market/quotes/stream?provider=tqsdk&asset_type=contract&instrument=CN-SHFE-AU2612
GET /v1/market/quotes/stream?provider=twelvedata&asset_type=forex&instrument=GLOBAL-OTC-EUR/USD
Accept: text/event-stream
```

参数与快照接口一致（`provider`/`asset_type`/`instrument`/`variant`），响应为
`text/event-stream`（HTTP/1.1 chunked 流，零新依赖）。**行情推送是纯单向流，
选 SSE 而非 WebSocket**（D8 决策）：复用 HTTP 鉴权/中间件/request_id，
客户端 `curl -N` 即可消费。仅实现 `stream_quote` 的 Provider 可用
（当前 tqsdk、twelvedata 与 tiingo）；其余 Provider 在受理阶段即返回
`UNSUPPORTED_CAPABILITY`（422）。

三类事件帧（`event:` + `data:` JSON，帧间空行分隔）：

| event | data 结构 | 语义 |
| --- | --- | --- |
| `status` | `{status, instrument, asset_type, provider}` | 流生命周期：`subscribed`（订阅确认，**首事件**，含已缓存快照场景）/ `upstream_disconnected`（上游连接异常或服务停机；twelvedata 上游拒订同以此收尾）/ `recycling`（tqsdk 专属：Runtime 新一代重建——北京时间 04:00 跨交易日回收 / watchdog / 休眠订阅回收，活跃订阅自动重订，重订完成再发 `subscribed`；twelvedata/tiingo 无代际回收，不产生此状态）/ `idle_unsubscribed`（流空闲超时，服务端本地退订并结束流） |
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
  **tiingo 走同款 linger 语义**（上游退订生效性待探针实证：若上游忽略退订，
  linger 到期仍完成本地释放，带宽残留由上游侧兜底）；
- **容量闸门**：并发流数达 `TAPEHUB_STREAM_MAX_CLIENTS`（默认 8）返回
  `STREAM_LIMIT_REACHED`（HTTP 429）；受理阶段（响应头发出前）的错误统一
  JSON 错误结构，流中错误一律事件化收尾；
- **仅 `shared` 连接模式支持**（tqsdk：流依赖长连接订阅；`per_request` 回滚
  模式返回 `UNSUPPORTED_CAPABILITY` 422；twelvedata 恒为单共享 WS 连接，
  无模式概念）；
- **上游订阅容量闸门（twelvedata / tiingo）**：并发订阅 symbol 数达
  `TAPEHUB_TWELVEDATA_SUBSCRIPTION_LIMIT`（默认 8，免费版 WS credit 口径）或
  `TAPEHUB_TIINGO_SUBSCRIPTION_LIMIT`（默认 8，软上限防失控；tiingo 按端点
  crypto/fx 各自计数）时，新 symbol 的流/快照请求在受理阶段返回
  `UPSTREAM_SUBSCRIPTION_LIMIT`（HTTP 429）；升档后调大该配置即可；
- **访问日志口径**：SSE 请求的「耗时」= 连接时长（长连接期间不产生新日志行）；
- 心跳与空闲退订由服务端管理：`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`
  （默认 300s）内无行情事件（心跳不算）即 `idle_unsubscribed` 收尾，
  客户端可随时重连。

## 请求规范

### instrument 标准代码

格式为 `{market}-{exchange}-{symbol}`，规范形式全大写，例如 `CN-SSE-600000`、
`US-NASDAQ-AAPL`（美股股票）、`US-NYSE-SPY`（美股 ETF）。**输入大小写不敏感**
（`cn-shfe-cu2607` 同样受理），服务端统一归一为全大写并以规范形式回显；
期货品种的交易所原生大小写由 Provider 适配器还原（如 tqsdk 的 `SHFE.cu2607`）。
美股 exchange 段取交易所简称（`NYSE`/`NASDAQ`/`CBOE`，twelvedata 白名单），
symbol 段为交易所原生 ticker（如 `AAPL`、`BRK.B`）。

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
| 已是标准代码 | 大写归一回显（三段统一大写） | 输入大小写不敏感，期货品种的交易所原生大小写由适配器还原 |
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
| `tiingo` | Tiingo（外汇/加密货币数据服务） |
| `baostock` | Baostock 证券宝（免费 A 股数据平台，与东方财富无关） |
| `ths` | 同花顺金融数据 API（独立数据服务体系） |
| `futunn` | 富途 OpenAPI（独立数据服务体系） |

- **变体名反映真实数据源**：仅 akshare 的上游确为东方财富/新浪/腾讯；
  TqSdk、Tushare、AlphaFeed、TickFlow、Twelve Data、Tiingo、Baostock 与东方财富无关，
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
| `UPSTREAM_TIMEOUT` | 504 | 上游调用整体超时（`TAPEHUB_UPSTREAM_TIMEOUT_SECONDS`，默认 30 秒；bars/quotes 单次请求从发起到返回的上限，SSE 流不适用），可稍后重试或调大阈值 |
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
- **凭据驱动的注册**：内置 Provider 中需要凭据的（alphafeed/tickflow/ths/tiingo/
  tushare/tqsdk/twelvedata/futunn），`.env` 相应变量全部非空（strip 后）才启用；凭据缺失的
  Provider **能力目录仍可见但 `enabled=false`**，auto 路由跳过，显式请求返回
  `PROVIDER_DISABLED`（details 携带缺失的变量名）。akshare/baostock 无凭据
  概念恒启用。
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
| index | eastmoney | `index_zh_a_hist` | P1D/P1W/P1M | none | 全部 |
| index | sina | `stock_zh_index_daily` | P1D | none | 无 amount |
| index | tencent | `stock_zh_index_daily_tx` | P1D | none | 无 amount（上游 amount 列语义为成交量） |
| etf | eastmoney | `fund_etf_hist_em` | P1D/P1W/P1M | none/qfq/hfq | 全部 |
| stock（HK） | eastmoney | `stock_hk_hist` | P1D/P1W/P1M | none/qfq/hfq | 全部 |
| stock（US） | eastmoney | `stock_us_hist` | P1D/P1W/P1M | none/qfq/hfq | 全部 |
| contract / continuous | sina | `futures_zh_daily_sina` | P1D | none | 全部（extensions: hold/settle） |

TickFlow（REST API `GET https://api.tickflow.org/v1/klines` / `GET /v1/quotes`，官方 SDK `tickflow`）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN/HK/US | tickflow（默认） | `tf.klines.get` | P1D/P1W/P1M | none/qfq/hfq | 全部（extensions: prev_close） |
| stock（quotes） | CN/HK/US | tickflow（默认） | `tf.quotes.get(symbols=[...])` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（extensions 见下） |

- API Key 通过环境变量 `TAPEHUB_TICKFLOW_API_KEY` 提供并传入 SDK 客户端，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 标的代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`、`CN-BSE-… → .BJ`、`HK-HKEX-00700 → 00700.HK`、`US-NASDAQ-AAPL → AAPL.US`（美股 NYSE/NASDAQ/CBOE 统一 US 后缀，exchange 段仅作标准代码规范）；
- 周期映射：`P1D→1d`、`P1W→1w`、`P1M→1M`；分钟族（`1m`~`60m`，含 `10m`）不声明（2026-11-07 拍板，与 alphafeed 同口径收敛）：免费档无分钟 K 线权限（403 已三轮实证），tickflow 又是 CN stock bars 第二候选，声明无权限的周期会 403 打断本可由其他 Provider 服务的请求——去掉声明后 US 分钟 K 由 twelvedata 服务，CN/HK 分钟 K 无人声明、auto 路由诚实返回 `NO_PROVIDER_AVAILABLE`；季线 `1Q` 无法用 ISO 8601 Duration 表达，年线 `1Y` 超出项目「最大支持到月线」的周期范围，均不声明；
- 复权映射：`qfq→forward`（比例前复权）、`hfq→backward`、`none→none`（差值加法变体不对外暴露）；SDK 自身默认 forward，适配器始终显式传参；港股/美股复权语义未经探针实证（美股复权口径可能与 A 股不同），生效性以探针报告为准；
- 快照字段映射：`last_price/volume/amount` 为标准字段（五档盘口上游无源字段恒 null）；`region/name/change_pct/change_amount/amplitude/turnover_rate/prev_close/open/high/low` 经 extensions 白名单透出。SDK 0.1.25 探针实测：返回 `list[dict]`（非文档 data 包装），**无 `session` 字段**（文档声明存在，防御映射存在才透出），`ext.type`（cn_equity 等）不在白名单不透出；比例字段为**小数比例（1% = 0.01，如 change_pct 0.0162 = 1.62%）**；上游支持逗号分隔批量（本项目单标的快照，单标的单次调用）；
- 上游单次上限 10000 根：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；
- 错误指纹（2026-11-07 探针实证）：免费档分钟线无权限为 SDK `PermissionError`「无分钟K线查询权限」（统一包装 `UPSTREAM_ERROR` 告知用户）；限频探针 6.5s 间隔（≈9 次/分钟）全程未触发；`RateLimitError` 类型存在于 SDK，429 指纹待更高频场景核实；
- 日线时间戳口径（探针实测）：CN/HK 日线为**北京时间零点**；US 日线为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移）；HK/US 日线 `amount` 上游原值 0（无成交额数据，按原值透出非缺失 null）；
- 历史深度（history_depth 探针实证，报告 20261107T145801+0800：早上市基准标的 CN 600602（1990-12-19 上交所开业首批老八股）/ HK 00002 / US GE（1892 年上市），二分 90 天粒度收敛 complete=True，日/周/月三周期同值）：CN ≈1991-01-18（不早于上交所开业日 1990-12-19）、HK ≈1980-02-15、US ≈1962-02-17。bars 能力拆 CN/HK/US 三块逐市场声明，`history.start` 取各市场估计窗口右端零点（approx ±90 天，真实最早可能更早；原合并块取跨市场最早 US 值对 CN 过度声明 29 年，已拆块修正）；分钟族（PT1M~PT1H）经拍板不再声明（免费档无权限，见周期映射 bullet）。基准标的已永久换为早上市标的：二分收敛点若远离其上市日即为真实库底，后续下探无需再改配置。

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
| continuous | tushare（默认） | `pro.fut_daily` | P1D | none | 全部（extensions: pre_close/settle/oi） |

- Token 通过环境变量 `TAPEHUB_TUSHARE_TOKEN` 提供并传入 `ts.pro_api`，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），显式请求返回 `PROVIDER_DISABLED`；
- 股票代码映射：`CN-SSE-600000 → 600000.SH`、`CN-SZSE-… → .SZ`（后缀已按文档示例核实）；北交所 `CN-BSE-920000 → 920000.BJ`（文档 doc_id=14 后缀表列明 + 探针实证，报告 20261112T172921+0800：目录 355 在市标的全部 920 段、920000.BJ daily/adj_factor 双证；83/87/43 段老代码已被 920 段替换，不再有效）；
- 期货合约映射（五所后缀探针实证，报告 20261112T124404+0800）：`CN-SHFE-CU2401 → CU2401.SHF`、`CN-DCE-V2601 → V2601.DCE`、`CN-INE-SC2409 → SC2409.INE`、`CN-CFFEX-IF2409 → IF2409.CFX`；CZCE 在 tushare 为 4 位年月码，与统一标准码同形直通（`CN-CZCE-RM2701 → RM2701.ZCE`；实证 4 位可用、3 位旧写法全空，旧写法输入如实透传不猜年份；广期所 .GFE 未核实不声明）；
- 主力连续映射（总览页连续合约格式 XX=主力 / XXL=主连；探针实证报告 20261112T133250+0800）：品种+L 主连——`CN-SHFE-CU → CUL.SHF`、`CN-CZCE-SR → SRL.ZCE`、`CN-DCE-M → ML.DCE`、`CN-INE-SC → SCL.INE`；CFFEX 例外直拼主力 `CN-CFFEX-IF → IF.CFX`（该所 IFL/IFL1-3 为当月/次月/当季/下季月份系列非主连）；不参与 CN contract/continuous 的 auto 路由（仅显式 `provider=tushare`）；
- 单位换算（按文档）：股票 `vol` 手→股（×100）、`amount` 千元→元（×1000）；期货 `vol` 保持手、`amount` 万元→元（×10000）；
- `fut_daily` 单次上限 2000 条（主连 CUL.SHF 16 年窗口实测恰 2000，报告 20261112T124404+0800）：达到上限时响应 `meta.partial=true` 且 `warnings` 提示可能截断；上游截断为「保尾截头」（保留最新 2000 根，partial 时缺窗口头部更早数据）；该接口要求上游账户 2000 积分；
- 历史 K 线起点（history.start 探针二分，报告 20261112T124404+0800 / 20261112T135507+0800）：stock 日/周/月 ≈1999-12-08（approx ±90 天，真值≈标的上市日 1999-11-10 落在窗口内，与 akshare 个股口径同日互证）；contract = CU2401 上市首日 2023-01-17（全量模式精确定值；capability 为 asset_type 级声明，各合约以自身上市日起、随摘牌止）；continuous 主连 ≈1991-01-01（二分下限触底，真实可能更早）。
- 上游积分限频/错误码未在文档中核实，统一包装为 `UPSTREAM_ERROR`。

TqSdk（官方 SDK `tqsdk`：`TqApi(auth=TqAuth(...)).get_kline_serial(...)`）：

| 资产类型 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- |
| contract / continuous / weighted-index | tqsdk（默认） | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none | 无 amount（上游无成交额列；extensions: open_oi/close_oi） |
| stock | tqsdk | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M | none（复权 no-op：2026-11-13 探针实证 qfq/hfq 与 none 零差异，收窄声明） | 无 amount；volume 单位未核实，原样透出 |
| stock | tqsdk | `api.get_kline_serial`（adj_type） | PT30M/PT1H、P1D | none/qfq/hfq（复权生效实证） | 无 amount；volume 单位未核实，原样透出 |
| index | tqsdk | `api.get_kline_serial` | PT1M/PT5M/PT15M/PT30M/PT1H、P1D | none（adj_type 对指数无效） | 无 amount；无持仓量列 → extensions null |
| etf | tqsdk（默认） | `api.get_kline_serial`（adj_type） | PT1M/PT5M/PT15M | none（复权 no-op，同 stock） | 无 amount；volume 单位未核实，原样透出 |
| etf | tqsdk（默认） | `api.get_kline_serial`（adj_type） | PT30M/PT1H、P1D | none/qfq/hfq（复权生效实证） | 无 amount；volume 单位未核实，原样透出 |

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
- 合约代码映射（交易所全大写；标准 symbol 恒为大写 4 位年月，交易所原生写法由适配器还原，还原表只收探针实证条目）：`CN-SHFE-CU2607 → SHFE.cu2607`（SHFE 品种小写，2026-11-03/04 探针 cu/au/bu 实证；2026-11-12 大写 SHFE.AU2612 实证失败）、`CN-CZCE-RM2701 → CZCE.RM701`（CZCE 合约 4→3 位年月码，2026-11-12 探针实证 TianQin 只认 3 位、4 位无响应挂起；主连品种码无年月段直通）；主力连续 `CN-SHFE-CU → KQ.m@SHFE.cu`；指数连续/加权 `CN-SHFE-BU → KQ.i@SHFE.bu`；DCE/INE/GFEX 写法约定未核实，暂按大写直通，探针实证后回填；
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
  tick 循环驱动），订阅存活期间快照由 tick 循环 `is_changing` 判定增量、线程内拷贝发布缓存。
  **M2 快照为按请求租约订阅**：请求结束（成功/408/客户端断开）即退订本地维护、丢弃缓存
  （`latest` 返回 None），Quote 转入休眠账本；重复请求经休眠唤醒即时发布 Quote 当前值
  （上游持续推送，唤醒即新鲜，无需重新等首包）。退订接口为 SSE 流式（M3）与 M2 租约共用。
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

- 个股/ETF `volume` 统一为**股**：东财接口（个股与 ETF 同端点同口径）返回手，适配器 ×100 换算；新浪/腾讯为股，原样返回；TqSdk 股票/ETF 的 volume 单位文档未写明，**2026-11-03 探针量级对照判定为股**（600000 日 volume ≈5×10^7），原样透出；
- 指数 `volume` **原样透出**：东财（`index_zh_a_hist`）与腾讯上游单位为**手**（东财 2026-11-08 探针交叉验证：换算 ×100 后成交额÷成交量≈10.7 元/股，为上证成分股均价量级；用户决策不换算，保持指数三源口径一致），新浪未核实；
- `amount` 统一为**元**；期货的上游未标注单位时原样返回（见各接口文档）；
- 交易所映射（已核实部分）：个股/指数 `SSE→sh`、`SZSE→sz`（指数东财为**纯 6 位代码**，`CN-SSE-000001 → 000001`，上游按指数代码映射表自动判定市场，SSE/SZSE/CSI/BSE 探针实测覆盖）；指数新浪/腾讯为带前缀符号（`sh000001`），指数东财之外另支持 `CSI→csi`、`BSE→bj`（新浪/腾讯仅 SSE/SZSE）；期货合约符号原样传递（如 `RB2410`），主力连续为品种代码+`0`（如 `RB`→`RB0`）；ETF（东财）为纯 6 位代码原样传递（`CN-SSE-510050 → 510050`、`CN-SZSE-159915 → 159915`），上游按代码首位自动判断沪深（5/6 开头→沪、其余→深）；港股（东财）为 5 位数字代码，短代码零填充归一（`HK-HKEX-700 → 00700`）；美股（东财）为**市场号.代码**（`US-NASDAQ-AAPL → 105.AAPL`、`US-NYSE-GE → 106.GE`，ticker 大写归一、含点 ticker 如 `BRK.B` 原样透传），**CBOE/OTC 不支持**（CBOE 在东财的市场号归属未核实，探针后议）；
- 港股/美股日线时间戳口径（探针实证，报告 20261108T150042+0800）：港股交易日=北京同日（北京时间零点）；美股为**美东零点**（归一化北京时间随夏令时在 12:00/13:00 漂移；实测 1 月 EST 首末根均 13:00，EDT 12:00 由同构时区逻辑与单测覆盖）；
- 港股/美股成交量单位（探针实证，成交额÷成交量≈价格交叉验证）：**均为股，原样透出正确、无需换算**（00002：196,936,517 ÷ 3,065,640 ≈ 64.24 ≈ close 64.15 HKD；GE：701,270,320 ÷ 4,149,470 ≈ 168.99 ≈ close 168.59 USD）；同轮实证 ETF 手→股 ×100 换算成立（换算后 2.69 ≈ close 2.667 元）；`stock_hk_hist`/`stock_us_hist` 上游忽略 beg 日期参数（`lmt=1000000` 全量拉取客户端切片），每次调用拉全量历史的性能成本与新浪指数/期货接口相同，窗口过滤由适配器闭区间内存复核兜底；
- 历史覆盖范围回填（报告 20261108T150042+0800，人工确认；approx ±90 天、窗口粒度 90 天、complete=True）：ETF ≈2005-03-20（真值≈510050 上市日 2005-02-23，落在窗口内）、港股（00002 基准）≈1989-01-27、美股（GE 基准）≈1986-02-21（浅于 tickflow/alphafeed 独立探测的 ≈1962——东财美股库底确实较浅，两口径互证）；P1W/P1M 与 P1D 同上游端点同数据区间，同值声明；CN 个股本轮重估 ≈2000-01-01 为 era 步长 5→10 年的网格效应，保留既有更细粒度声明 ≈1999-12-08；
- 缺省路由：CN ETF bars 为 `tqsdk → baostock → akshare`（akshare 末位兜底）；HK/US 个股 bars 为 `… → akshare` 末位免凭据兜底（2026-11-08 接入，未核实项已由探针实证回填）。

未接入说明（诚实边界，不编造能力）：

- `fund_etf_hist_min_em`（东财 ETF 分钟线）不接入：1 分钟走 trends2 仅近 5 个交易日、5/15/30/60 分钟 kline 分支无服务端日期过滤（每次全量拉取客户端切片）；CN ETF 分钟族已由 Baostock 声明（2026-01-05 起，探针实证），重叠价值低（2026-11-08 拍板）；
- ETF（`fund_etf_hist_em`，2026-11-08 接入）`history.start` 已由探针实证回填（见下方历史覆盖范围回填 bullet）；
- `futures_hist_em`（东财期货）需要**中文合约名**（如“热卷主连”），无法从标准代码映射，第一版不接入；
- 加权（`weighted-index`）在上述接口文档中没有符号说明，暂不声明能力；
- 历史覆盖范围（`history`）已由数据探针实测回填（报告 20261106T211255+0800）：个股各源 `≈1999-12-08`（二分近似，窗口粒度 90 天、complete=True，真值≈标的上市日 1999-11-10；P1W/P1M 与 P1D 同上游端点同数据区间，同值声明）、指数各源 `≈1991-01-01`（探针下限触底；指数东财 P1W/P1M 与 P1D 同端点同数据区间同值声明，20261108T162519+0800 周期探测 + 1991 年周线 52 根实测确认）；期货为**全量模式精确值**（新浪期货接口无日期参数，适配器每次拉全量历史后内存过滤，单次调用直接读最早 bar）：单合约 RB2410 实测 `2023-10-17`（各合约以自身上市/摘牌日为准），主力连续 RB 实测 `2009-03-27`（与上期所螺纹钢期货上市日一致，相互印证）；
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
- **美股 adjust 口径**：`qfq` 映射上游 `adjust=all`——**拆股+分红前复权，与国内口径一致**（2026-11-06 探针实证：`adjust=dividends` 对除息日前历史回溯调整生效，AAPL 2026 年 212 个交易日中 150 天与原始价有差异）；`none` → `adjust=none`（原始价）；**参数名陷阱**：上游 REST 只认 `adjust`，传 `adj` 会被静默忽略落回默认（=splits 仅拆股，AAPL 2006 深度数据实证）；`hfq` 上游无等价物不声明；etf 复权经 2026-11-12 探针实证生效（SPY 2024-06 除息窗口 `adjust=all` 与 `none` 全 19 个交易日差异 ~2.9%，为上市以来累计分红回溯量级，报告 20261112T143502+0800），etf 声明扩容为 `[none, qfq]`；复权生效性已纳入探针 chain `adjust_diff` 持续回归（stock/etf × P1D/PT1H）；
- interval 枚举（上游完整支持 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month）：本适配器声明全部 12 个周期 `PT1M/PT5M/PT15M/PT30M/PT45M/PT1H/PT2H/PT4H/PT8H/P1D/P1W/P1M`（45min/2h/4h/8h 经数据探针实测可用后补充声明，报告 20261104T061857Z）；
- 历史覆盖范围（`history`）已由全量深度探针实测回填（粒度 90 天，approx）：forex（2026-11-04）日内 `≈2020-01-31~04-08`；crypto（2026-11-04）日内 `≈2020-01-07~04-07`、日/周 `≈2017-08-29`、月 `≈2017-08-02`；美股（2026-11-07，报告 `20261107T111630+0800`）stock/etf 日内 `≈2019-02-01~2020-04-11` / `≈2020-02-15~2020-04-11`；**粗周期 2026-11-12 精化收敛**（下限自 2000-01-01 外推，报告 `20261112T143502+0800` / `20261112T155931+0800`，人工确认）：forex `P1D ≈2003-12-25`、`P1W/P1M ≈1975-01-23`（下限 1950 起二分收敛；1970-01-01 处为无证据下限值已废弃）、US stock 三周期 `≈1981-01-18`（真值≈AAPL IPO 1980-12-12 落窗口内）、US etf `P1D/P1W ≈1993-03-17`、`P1M ≈1993-01-20`（真值≈SPY 上市 1993-01-22，月线桶 1993-01）；
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

Tiingo（无官方 Python SDK：REST 历史 K 线 httpx 同步客户端直连
`GET https://api.tiingo.com/...`，认证经 `Authorization: Token <key>` 请求头——
token 不落 URL；实时快照/快照流 websockets 库直连 WS，2026-11-13 接入，
接口签名按官方文档核实：general/connecting、crypto、forex、websockets/crypto、
websockets/forex 五页）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| forex（bars） | GLOBAL | tiingo（默认） | `GET /tiingo/fx/<ticker>/prices` | PT1M/PT5M/PT15M/PT30M/PT1H/PT2H/PT4H、P1D | none | 无 volume/amount（外汇文档仅 OHLC） |
| crypto（bars） | GLOBAL | tiingo（默认） | `GET /tiingo/crypto/prices` | PT1M/PT5M/PT15M/PT30M/PT1H/PT2H/PT4H、P1D | none | volume 有值（base 币计）；无 amount |
| forex/crypto（quotes） | GLOBAL | tiingo | WS crypto/fx 端点（thresholdLevel 2/5） | —（快照无周期） | —（快照无复权） | timestamp/last_price + 单档 bid/ask（WS 探针实证后声明，见下） |

- **免费档限额（账户页与官方文档口径，2026-11-13 接入依据）**：50 请求/小时、
  1000 请求/天、500 个唯一品种/月、1GB 带宽/月——REST 调用计请求数，WS 不计
  请求数只计带宽（快照/快照流统一走 WS 共享订阅以保 REST 配额）。因配额紧于
  twelvedata，缺省路由置**回退位**（`provider_routing.yaml`：GLOBAL forex/
  crypto 的 bars 与 quotes 均为 `[twelvedata, tiingo]`——不改变既有缺省行为，
  显式 `provider=tiingo` 或 twelvedata 缺席/不可解析该请求维度时启用）；
- API Key 通过环境变量 `TAPEHUB_TIINGO_API_KEY` 提供并经请求头传给上游，
  禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，`enabled=false`），
  显式请求返回 `PROVIDER_DISABLED`；
- 标的代码映射：外汇 `GLOBAL-OTC-EUR/USD → eurusd`、加密货币
  `GLOBAL-CRYPTO-BTC/USD → btcusd`（官方 ticker 均为小写连写，适配器去斜杠
  统一小写；exchange 段白名单 forex=`OTC`、crypto=`CRYPTO`）；
- **周期面收窄（2026-11-14 人工拍板）**：仅声明 `PT1H/PT4H/P1D`——
  `PT1M/PT5M/PT15M/PT30M/PT2H` 与 `10min/6hour/12hour/24hour` 等其余格式
  收窄不声明（`2hour`/`4hour` 曾经 2026-11-13 两轮探针实证可用后扩容，
  随收窄一并退出声明）；`P1W/P1M` 上游 `resampleFreq` 合法格式
  `<n>min|hour|day` **无 week/month**，周期本身不存在、无法声明；
  `startDate`/`endDate`/`resampleFreq` 显式
  传参（不依赖上游默认值：上游缺省返回原始频度而非声明周期口径）；
- **crypto 日内分钟族收窄（2026-11-14 裁决，推翻 2026-11-13「恢复
  声明」）**：原恢复基于「1min 稀疏 ≈14 根/天」误判纠正（limit 专项
  实证实际密集 ≈1440 根/天），但日内逐周期 depth 探测成本/噪声高
  （分段 ≈5001 行/段），与周期面收窄裁决合并处理——分钟族退出声明；
  分段语义作为上游行为事实保留记录（见下「日内周期单次请求分段」）；
- **响应形状（2026-11-13 探针实证，官方文档未如实反映）**：crypto 端点
  返回按 ticker 分组的嵌套结构 `{"ticker", "baseCurrency", "quoteCurrency",
  "priceData": [...]}`（bar 行在 `priceData` 内，字段 date/open/high/low/
  close/volume/volumeNotional/tradesDone）；forex 端点为扁平行（仅 OHLC，
  无 volume）。适配器按形状统一拍平（嵌套/扁平防御兼容）；
- **历史覆盖范围（history.start）已按 2026-11-13 四轮 + 2026-11-14 逐周期
  探针回填（人工确认）**：fx 三周期同值 = 上游档案硬边界 2020-01-01（UTC
  墙钟）换算北京时间 `2020-01-01T08:00:00+08:00`——`PT1H/PT4H/P1D` 各自
  depth 独立收敛一致 ≈2020-02-15 与边界相符（受控复现实证：越界窗口 400
  拒绝而非裁剪，两种变体 `End date` / `Start date must be >= …`；适配器对
  fx 请求钳制到档案起点、完全越界窗口直接空批）；crypto P1D = depth 二分
  收敛 `2011-08-25T00:00:00+08:00`（窗口右端零点，90 天粒度 approx、真值
  可能更早，三轮一致，显著深于 twelvedata crypto ≈2017-08）；**crypto
  PT1H/PT4H = `2018-01-01T00:00:00+08:00`**（2026-11-14 逐周期探测独立
  收敛 ≈2017-12-28、两周期一致——**比日线浅 6.4 年**，原 P1D 继承值已证
  过度声明，保守取整季初）；单次返回
  行数上限官方未标注（受 1GB/月带宽配额约束）；
- 时间边界口径（2026-11-13 探针实证）：`startDate`/`endDate` 统一传 UTC
  墙钟 `YYYY-MM-DDTHH:MM:SS`——两端点均为 UTC（crypto `date` 带 `+00:00`、
  forex `date` 带 Z，日内分布吻合）；bar 时间戳解析 ISO 8601 后统一北京
  时间归一化；
- **日内周期单次请求分段（2026-11-13 limit 专项实证，报告
  limit-truncation-20261113T2130+0800）**：单次请求仅返回窗口起点首个
  连续段（保头截尾、静默缺失；段后分段查询可行——E7/E9 实证）。
  **fx PT1M 段长按时间 ≈6 自然日**（段行数随窗口起点漂移
  5671/6136/7028/7051，不可硬编码 cap）→ 适配器按「末根距请求 end
  >7 天（段长 + 周末/假日余量）」标记 `meta.partial=true` 提示尾部
  截断；**crypto 段上限按行数 ≈5001 行**（三次一致）→ 返回行数触顶即
  标记 partial（PT1M ≈3.5 天/段、PT1H ≈208 天、PT2H ≈416 天、PT4H/
  P1D 远超实用窗长）；fx PT5M/PT1H 一月窗完整实证不受分段影响；
  网关侧不做多段聚合（fx PT1M 一年 ≈61 段请求会打满小时配额），长窗
  调用方自行分段；
- **纠正密度误判**：crypto 1min 实际密集 ≈1440 根/天（年窗 5001 根系
  分段截断而非稀疏；fx 1min 密度充足 ≈2811 根/日）——日内分钟族恢复
  声明的前提纠正记录见上；
- 区间无数据行为：crypto 端点对历史外窗口返回 200 `[]`（正常空批）；
  fx 端点的档案边界 400 按指纹 `"End date must be >="` 归一化为空结果
  （上游权威边界结论，twelvedata 空窗指纹同款先例）；
- 上游 429（限频）映射 `UPSTREAM_RATE_LIMITED`；其余非 2xx 与 JSON 解析失败
  统一 `UPSTREAM_ERROR`（details 携带 status_code）；
- 实时快照与快照流（WS 端点 `wss://api.tiingo.com/crypto` 与
  `wss://api.tiingo.com/fx`，按资产类型分流两个懒启动 Runtime）：订阅帧
  `{"eventName": "subscribe", "authorization": <token>, "eventData":
  {"thresholdLevel": <2|5>, "tickers": [<ticker>]}}`——阈值按 2026-11-13
  WS 行为探针实证（报告 ws-behavior-20261113T2130+0800）：forex=**5**
  （文档/AsyncAPI 口径 7 被上游拒绝「thresholdLevel not valid」并断连）、
  crypto=2（top-of-book + 成交）；下行 `{service, messageType:
  A|U|D|I|E|H, data}`：I = 订阅 ack（`data.subscriptionId` 整数下发）、
  H = 心跳（订阅后立即回 1 帧，此后实测 ~60-90s——看门狗 180s）、
  E = 错误帧（错误后服务端主动断连，重连状态机接管）；
- **data 元素为位置数组（WS 探针实证，官方文档字段表的对象形态错误，
  标准化见 response_mapper.ws_element_to_snapshot）**：fx Q 8 元素
  `["Q", ticker, date, bidSize, bidPrice, midPrice, askSize, askPrice]`
  （mid 自洽验证）、crypto Q 9 元素（+exchange）/ T 6 元素
  `[type, ticker, date, exchange, lastSize, lastPrice]`；**tickers 服务端
  过滤实测生效**（fx 147/147、crypto 206/206——客户端兜底过滤保留
  防御）；**阈值全值矩阵（二次探针附录）**：fx 有效 {2, 5, 省略} 均持续
  推 TOP、crypto 有效 {2 = Q+T 混合，5 = 仅成交 T，省略 = 默认仅 T}；
  **退订定案**：逐 symbol 退订生效且不断连（被退 symbol 经短暂在途
  竞态后停止，与 twelvedata 同款）、退订最后一个 symbol（空订阅）服务
  端关闭连接（重连状态机恢复，linger 语义成立）；**subscriptionId 增删
  订阅不可用**（增订帧两次独立实证触发服务端断连）——维持逐 symbol
  独立订阅帧方案；
- 快照字段口径：`last_price` 取 `lastPrice`、缺失回退 `midPrice`（文档：
  bid/ask 均非空时 mid=(bid+ask)/2；两者均缺失属上游契约违背返回
  `UPSTREAM_ERROR`）；**fields 已按 WS 探针实证扩容**：单档
  `bid_price1/ask_price1/bid_volume1/ask_volume1` 声明（fx Q 数组含
  bidSize/bidPrice/midPrice/askSize/askPrice；crypto 同 + exchange），
  五档其余恒 null；标准 `volume`/`amount` 恒 null（`lastSize` 为单笔
  成交量的口径未实证，不冒充累计成交量）；`extensions` 白名单：
  `exchange`（fx 无、crypto 有，缺失不透出）；
- Runtime 形态：asyncio 原生共享连接（crypto/fx 各一单连接，同端点多实例
  启动告警），per-symbol 引用计数；引用归零延迟 60s（linger）向上游发
  退订并本地释放缓存；断连广播 `upstream_disconnected` 后退避重连自动重订；
  **无代际回收（不产生 `recycling` 事件）**；冷订阅首包超时
  `TAPEHUB_TIINGO_FIRST_SNAPSHOT_TIMEOUT`（默认 5s）返回 `QUOTE_NOT_READY`
  （408），活跃订阅即时读缓存；并发订阅软上限
  `TAPEHUB_TIINGO_SUBSCRIPTION_LIMIT`（默认 8，按端点各自计数；上游未公布
  并发上限，硬约束为月度 500 个唯一品种——网关不做计数，由限额说明与
  上游超限报错兜底），达限返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429）。

Baostock（证券宝，官方 SDK `baostock`：`bs.login()` → `bs.query_history_k_data_plus()` → `bs.logout()`）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN | baostock（默认） | `bs.query_history_k_data_plus` | P1D/P1W/P1M、PT5M/PT15M/PT30M/PT1H | none/qfq/hfq | 全部（extensions 见下） |
| index（bars） | CN | baostock（默认） | 同上 | P1D/P1W/P1M | none | 全部（extensions: prev_close/pct_change） |
| etf（bars） | CN | baostock（默认） | 同上 | P1D/P1W/P1M、PT5M/PT15M/PT30M/PT1H | none/qfq/hfq | 全部（extensions 见下） |

- 免费无凭据数据平台（无需注册）：**恒启用**（与 akshare 同类），`TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 可显式禁用；未安装 SDK 时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`；
- 会话模型：`login()/logout()` 为**模块级全局单连接**（单例 socket、非线程安全），适配器以模块级锁串行化「登录→查询→取数」，**懒登录 + 进程内复用**（首次请求登录，此后复用）；查询返回未登录（10001001）/网络类（10002xxx）错误码时**重登一次并重试一次**；不主动 logout（进程退出即断，baostock ≥0.8.9 已修复退出 socket 泄漏）；
- 标的代码映射：`CN-SSE-600000 → sh.600000`、`CN-SZSE-000001 → sz.000001`（沪深两市统一「前缀.6 位代码」，stock/index/etf 同格式；北交所未支持不声明）；
- 周期映射：`P1D→d`、`P1W→w`、`P1M→m`、`PT5M→5`、`PT15M→15`、`PT30M→30`、`PT1H→60`（上游 60 分钟线）；**分钟族仅个股与 ETF 声明**（指数无分钟线——探针实证传分钟 frequency 返回空结果不报错；ETF 分钟 2026-01-05 起，探针实证四周期齐备、形态与个股分钟同构、hfq 复权管线生效，官方口径一致，20261108 补探）；周/月线仅每周/月最后一个交易日有数据（探针实证：周二单日窗口查周线返回空）；
- fields 三套按官方「历史行情指标参数」显式写全：日线含 `preclose/turn/tradestatus/pctChg/isST`（估值四项 `peTTM/pbMRQ/psTTM/pcfNcfTTM` 仅个股）、周月线无 `preclose/tradestatus/isST/估值`、分钟线仅 `date/time + OHLC + 量额`；extensions 白名单：个股日线 `prev_close/turnover_rate/pct_change/trade_status/is_st/pe_ttm/pb_mrq/ps_ttm/pcf_ncf_ttm`，指数日线 `prev_close/pct_change`，周月线 `turnover_rate/pct_change`，分钟线无；
- 复权映射：`none→3`、`qfq→2`、`hfq→1`（上游默认 "3"，适配器始终显式传参）；上游为**涨跌幅复权法**（官方声明与同花顺/通达信口径不同）——复权序列与其他 CN Provider 不保证一致，**跨源对比仅 none 可靠**；**复权生效性（探针实证，报告 20261108T002941+0800）**：个股日线 adjust_diff confirmed、分钟族真生效（PT5M 3072 行全差异、首分歧 2024-06-03 除权日）、**ETF qfq/hfq 生效**（2024 全年 242 行全差异，回填三复权）、**指数为 no-op**（qfq 与 none 零差异，维持 [none]）；
- 单位：`volume` 上游已是**股**（无需换算，与 akshare 东财「手」不同）、`amount` 为元；上游行值全为字符串、缺失为空串 `""`（→ null，不用 0 冒充）；
- 时间戳口径（探针实证）：日/周/月线为交易日 00:00 北京时间；分钟线由 `date + time`（YYYYMMDDHHMMSSsss）解析为北京时间，**time 为 bar 终点时刻**（5/15/30/60 分钟首根 time 分别为 0935/0945/1000/1030，全吻合）；
- 停牌日（探针实证 147 行，乐视网 2017 停牌期）：OHLC=前收盘价、成交量/额=0（原值透出非缺失）、换手率空串 → null，与官方口径一致；
- 数据更新时间（官方）：日K 交易日 17:30、复权因子 18:00、分钟K 20:00、周K 周六 17:30、月K 每月 1 号 17:30——当日 17:30 前请求当日日线为空属上游入库节奏；
- **单次返回上限 5000 根**（探针实证：PT5M 2025 全年窗口恰返回最早 5000 根、尾部截断；PT15M/PT30M/PT1H 全年 3872/1936/968 根均完整）：达到上限时响应 `meta.partial=true` 且 `warnings` 提示（窗口起点侧完整、尾部取不到，调用方需缩小窗口）；上游无条数上限参数（服务端分批经 `rs.next()` 对调用者透明）；PT5M 大窗口时延 22~49s（重查询，timeout 需留余量）；
- 错误经字符串 `error_code/error_msg` 返回（"0"=成功，不抛异常），非零统一包装 `UPSTREAM_ERROR`（details 透出 `upstream_error_code`/`upstream_error_msg`）；**空结果语义（探针实证）**：指数 + 分钟 frequency、无效代码、超范围日期、无效周期均返回空结果集不报错（错误码仅 SDK stderr 打印），适配器按空批次如实透出；有 IP 黑名单机制（10001011，访问规则见 baostock.com/blacklist，具体限频未公布，探针 pacing 2s 保守节流）；
- 历史覆盖范围（history.start 探针实测回填，报告 20261108T002941+0800，人工确认；二分 90 天粒度 approx 者真实最早可能更早）：个股日/周/月 ≈**1991-01-18**（官方口径 1990-12-19 偏乐观；与 tickflow CN 收敛点同日、alphafeed ≈1991-01-14 接近，几家 CN 库大概率同源）、个股分钟族 **2020-01-02**（精确，官方 2020-01-03 差一天）、指数 ≈**1991-01-18**（官方口径 2006-01-01 显著偏保守）、ETF **2005-02-23**（精确，2004 全年空；官方口径 2026-01-05 仅指 V0.9.3 新增每日更新 API）、**ETF 分钟族 2026-01-05**（精确，官方口径一致；2025Q4 全空、四周期首根同日）；
- 缺省路由（2026-11-08 接入拍板）：CN 个股 K 线缺省 **baostock**（回退链首位：免费无凭据、个股官方口径 1990-12-19 起深于既有各源、分钟族免费有权限）；CN 指数 K 线缺省 akshare（库底 ≈1991-01-01 深于 baostock 官方口径 2006-01-01，浅库底源置首位会在路由选择阶段截胡更早区间——回退只在路由选择阶段发生），baostock 末位补位；CN ETF 缺省 tqsdk，baostock 回退（tqsdk 凭据未配置时自动跳过）；
- 文档矛盾点（探针实证定案，报告 20261108T002941+0800）：参数说明写 `start/end`、示例代码写 `start_date/end_date`——**SDK 函数签名只接受 `start_date/end_date`**（传 `start` 报 TypeError，适配器按示例风格实现正确）；`end` 闭/开区间两页文档矛盾——**闭区间（包含）**（stock/index 单日窗口各返回 1 行，适配器按闭区间实现正确）。

同花顺金融数据 API（无官方 Python SDK：REST 用 httpx 同步客户端直连
`GET https://fuyao.aicubes.cn/...`，认证经 `X-api-key` 请求头——key 不落 URL；
2026-11-13 接入，接口签名按官方文档核实：overview、prices、a-share-index、
fund-market、futures-prices、options-prices 六页）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN | ths（默认） | `GET /api/a-share/prices/historical` | P1D | none/qfq/hfq | 全部 |
| index（bars） | CN | ths（默认） | `GET /api/a-share-index/prices/historical` | P1D | none | 全部 |
| etf（bars） | CN | ths（默认） | `GET /api/fund/market/historical` | P1D | qfq（上游固定前复权，不可选） | 全部 |
| contract（bars） | CN | ths（默认） | `GET /api/futures/prices/daily` | P1D | none | 全部（量额单位上游未明示，原值透出） |
| option（bars） | CN | ths（默认） | `GET /api/options/prices/daily` | P1D | none | 全部（量额单位上游未明示，原值透出） |
| stock/index/etf（quotes） | CN | ths | `GET /api/a-share|a-share-index|fund/market*/snapshot` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（extensions 见下） |
| contract/option（quotes） | CN | ths | `GET /api/futures|options/prices/intraday`（分时末点） | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（量额=分时全点求和，见下） |

- **凭据与启用**：API Key 经环境变量 `TAPEHUB_THS_API_KEY` 提供并经请求头传给
  上游，禁止写入代码/manifest/日志；未配置时不启用（能力目录仍可见，
  `enabled=false`），显式请求返回 `PROVIDER_DISABLED`；付费凭据 API，缺省路由
  置**回退位末位**（`provider_routing.yaml`：CN stock/index/etf/contract 的
  bars 与 CN stock/index/etf/contract 的 quotes 均在既有链末尾追加；CN option
  的 bars/quotes 为 ths 独占组合）；
- **统一 ApiResponse 信封**：业务结果（含错误）HTTP 恒 200，经 `code` 字段
  表达（0=成功）——`code=4001`（频率超限）映射 `UPSTREAM_RATE_LIMITED`；
  其余非 0（1001-1004 参数类/2001-2003 凭据权限/3001-3004 标的类/
  5001-5003 服务端）统一 `UPSTREAM_ERROR`（details 携带 `upstream_code`/
  `upstream_message`）；非 2xx 与 JSON 解析失败为防御性 `UPSTREAM_ERROR`
  （官方约定业务错误走信封，HTTP 恒 200）；时间戳统一毫秒 Unix、
  Asia/Shanghai，适配器负责北京时间归一化；
- 标的代码映射（只收文档实证条目，其余 exchange 明确拒绝、探针后回填）：
  stock/index/etf `CN-SSE-600519 → 600519.SH`、`CN-SZSE-000001 → 000001.SZ`
  （BSE→BJ 仅元信息域实证、价格端点未实证，不收）；contract
  `CN-SHFE-CU2601 → CU2601.SHF`（仅 SHFE 后缀文档实证）；option
  `CN-CFFEX-IO2601-C-4000 → IO2601-C-4000.CFE`（仅 CFFEX 后缀文档实证），
  期货/期权 symbol 段原样透传（不做行权价等结构改写）；
- **上游能力边界（端外，文档明示 + 2026-11-13 三轮探针实证）**：历史 K 线仅
  日线 `1d`；**窗口口径实测与文档不符**——指数文档写「≤10 年」实测仅支持
  **最近约 5 年滚动窗口**（越界 `code=1003 'recent five years only'` 拒绝）、
  A 股个股实测无滚动上限（越界软返空）、ETF ≤5 自然年吻合（越界**返空不报
  错**）；期货/期权日 K 端外仅 `time_period=day_1`（显式传参）；已知上游缺陷：
  期权日 K `start=2026-10-01`（单一日期）触发 `code=5003 'daily quote
  unavailable'`（邻近起点均正常，与调用频率无关——窗口避开即可，适配器照常
  上抛 `UPSTREAM_ERROR` 不归一化：单点缺陷而非稳定边界）；同花顺板块/行业
  指数（`88xxxx.TI`）与商品指数（`850002.TI`）属同花顺特有 TI 类指数，本期
  不纳入；ETF 行情仅支持 ETF（LOF/场外/REITs 上游返回 `code=3004`）；
- 复权映射（A 股历史 K 线三值语义）：`none→none`、`qfq→forward`
  （前复权）、`hfq→backward`（后复权）；指数无复权语义（响应 `data.adjust`
  固定 null）、期货/期权仅声明 none；**ETF 上游无 adjust 参数、数据固定
  前复权口径**——受控对比实证（2026-11-14，报告 20261114T080423+0800：
  510880 调整生效段 vs baostock none 偏差中位 9.01% / vs qfq 0.34%，除息日
  2023-01-16/2024-01-23/2025-01-21）后人工裁决声明 `[qfq]`，`adjust=none`
  的 auto ETF 请求不落 ths（回退链末位，前序 Provider 全失败时诚实返回
  `NO_PROVIDER_AVAILABLE`）；`ths` 的 `forward` 缺省恰与网关
  个股声明默认 `qfq` 对齐，适配器仍始终显式传参；
- **单位**：A 股快照/历史 K 线 `volume`=股、`turnover`=元（官方文档明示）；
  ETF/期货/期权 volume/turnover 单位上游未明示，原值透出（不编造换算）；
- **期货/期权实时快照取分时末点**（端外仅当前交易日，`session=intraday`、
  `trade_date=0` 显式传参）：`last_price`=末点 `price`、`timestamp`=末点时间
  （防御升序排后取末点；非交易日/盘前 `item=[]` 返回 `UPSTREAM_ERROR`）；
  `volume`/`amount`=**分时全点求和（当日累计）**——分时点量额为逐分钟增量
  （2026-11-13 探针实证：各点 `turnover/volume` 恰等于该点 `price`×合约
  乘数、夜盘开盘三点 volume 5→26→31 非累计序列）；两字段独立计算（任一点
  volume 缺失则 volume 恒 null、任一点 turnover 缺失则 amount 恒 null，
  部分和不冒充累计）；存在无时间戳点时求和序列不完整，两字段均不计算；
- **快照 timestamp 口径（探针实证）**：`data.timestamp` 为上游**响应组装
  时刻**（盘后随墙钟推进、间隔 10s 两次调用精确 +10s），非成交时点；上游
  快照无独立行情时间字段，按文档「最新上游有效时间」口径透出，盘后请求
  的快照 timestamp 为请求时刻属预期行为；
- 快照字段口径（A 股/指数/ETF）：`last_price`/`volume`/`turnover→amount`
  直映；`extensions` 白名单：`open_price/high_price/low_price/prev_price/
  price_change/price_change_ratio_pct`（ETF 另有
  `price_amplitude_ratio_pct`/`turnover_ratio_pct`）；快照不返回中文名
  `name`（上游约定，经元信息域端点解析，网关不透出）；
- **历史覆盖范围（2026-11-13 探针实证回填，报告 20261113T212034/213321/
  214147+0800）**：个股日线 `history.start ≈ 2005-02-14`（二分 90 天粒度
  approx，真实最早可能更早，[2005,2010] era 1153 行完整）；指数/ETF 为
  **滚动窗口语义**，按 `retention_days=1825` 声明（指数错误消息自述
  'recent five years only'、ETF 文档 5 自然年吻合；[2022-01-01] 起可达性
  实证 969 行，边界粗粒度）；单次返回行数上限官方未标注（`offset`
  分页参数行为未核实、适配器未启用），`meta.partial` 恒 false（隐式截断
  行为待探针核实后补充覆盖判定）；
- 快照为纯 REST 单次调用语义：无首包超时/`QUOTE_NOT_READY`（与
  alphafeed/tickflow 一致），上游无推送接口，不实现快照流（流式请求返回
  `UNSUPPORTED_CAPABILITY`）。

富途 OpenAPI（无官方 Python SDK：REST 用 httpx 同步客户端直连
`https://webapi.futunn.com`，认证采用「方式二：传统 API Key」——AppKey +
本地私钥签名（Ed25519/RSA-SHA256 自动检测），AppKey ID 与私钥路径从 `.env` 读取；
2026-11-14 接入，接口签名按官方文档核实：getting-started、api-authority、
rate-limit、stock-quote、history-kline 五页）：

| 资产类型 | 市场 | 变体 | 上游接口 | 周期 | adjust | 标准字段 |
| --- | --- | --- | --- | --- | --- | --- |
| stock（bars） | CN | futunn（默认） | `GET /api/v1.0/quote/{code}/history-kline` | PT1M~PT1H/P1D/P1W/P1M | none/qfq/hfq | 全部 |
| index（bars） | CN | futunn（默认） | 同上 | 同上 | none（指数无复权语义） | 全部 |
| etf（bars） | CN | futunn（默认） | 同上 | 同上 | none（autype 对 ETF 生效性未实证） | 全部 |
| stock（bars） | HK | futunn（默认） | 同上 | 同上 | none/qfq/hfq | 全部 |
| stock（bars） | US | futunn（默认） | 同上 | 同上 | none/qfq/hfq | 全部 |
| stock/index/etf（quotes） | CN/HK/US | futunn | `POST /api/v1.0/quote/stock-quote` | —（快照无周期） | —（快照无复权） | timestamp/last_price/volume/amount（extensions 见下） |

- **凭据与启用（方式二：传统 API Key）**：`TAPEHUB_FUTUNN_APP_KEY`（AppKey ID，
  open.futunn.com/dashboard 控制台「AppKey 管理」创建并上传公钥）与
  `TAPEHUB_FUTUNN_PRIVATE_KEY_PATH`（与其公钥 Ed25519/RSA-SHA256 配对的本地
  私钥 PEM 文件路径）**两者全部非空才启用**（凭据驱动的注册语义同上）；签名
  每请求本地计算（原文 `timestamp_ms\nMETHOD\nrequest_path\nquery_string\nbody_sha256`
  以 `\n` 连接，签名 Base64 后经 `X-Api-Key`/`Authorization`（无 Bearer 前缀）/
  `X-Timestamp`/`X-Nonce` 四头注入），算法按私钥 PEM 类型自动检测；
  X-Timestamp 与服务端偏差超过默认 5 秒报 -12006（HTTP 401）——第一版不做
  自动校时，依赖宿主机时钟（校时端点 `GET /api/v1.0/server-time` 待探针实证
  后按需补充）；AppKey/私钥不落 URL、日志、manifest 或异常响应；缺省路由置
  各组合回退链最末位（2026-11-14 接入拍板：AppKey 凭据 API、历史覆盖未探针
  实证），显式 `provider/variant=futunn` 亦可选用；
- 行情权限（api-authority 页）：A 股境内认证客户免费 LV1（国际客户暂不支持）、
  港股股票境内认证客户免费 LV2、美股股票推广期免费 LV3——未开通对应市场权限
  时上游报 invalid_symbol/quota 类错误，适配器原样上抛 `UPSTREAM_ERROR`；
- 限频与历史额度（rate-limit 页）：IP 入口层行情类 100 次/分钟；业务限流走
  HTTP 200 + `ret_code=-11`（新旧两种封装并存，`ret_code/ret_msg` 与
  `code/message`）→ `UPSTREAM_RATE_LIMITED`（429，携上游码与 Retry-After 如有）；
  其余非 0（-3 invalid_parameter/-7 invalid_symbol/-8 unsupported/
  -12 history_quota_exceeded/-13 quota_unavailable）→ `UPSTREAM_ERROR` 携
  `upstream_code/upstream_message`；**历史 K 线独立额度池：每标的首次成功
  返回非空即占用一个额度（默认 7×24 小时后释放，重复读取不延期）**——分页
  续读必须用 `page_token`（免计「首请求」；`next_time` 分页仍计入，适配器
  未使用）；
- 标的代码映射（只收文档实证条目，越界明确拒绝）：`CN-SSE-600000 → SH.600000`、
  `CN-SZSE-000001 → SZ.000001`、`CN-BSE-430047 → BJ.430047`（上游品类表
  SH/SZ/BJ 支持正股/指数）、`HK-HKEX-00700 → HK.00700`（短码零填充 5 位）、
  `US-NASDAQ-AAPL → US.AAPL`（上游美股前缀不区分交易所，NASDAQ/NYSE/AMEX
  统一 US；**OTC 上游不支持获取**，exchange 白名单拒绝）；上游品类表 BJ 无
  ETF（仅正股/指数），ETF×BSE 组合在映射层拒绝；
- 周期映射（ktype 数字枚举）：`PT1M→1`、`PT5M→6`、`PT15M→7`、`PT30M→8`、
  `PT1H→9`（上游 60 分）、`P1D→2`、`P1W→3`、`P1M→4`；上游另有 3/10/120/180/240
  分与季/年线（文档明示），对应 ISO 周期不在声明面（PT3M/PT2H/PT4H 与季/年线
  待探针实证后回填；年/季线不进能力目录——周期上界月线）；
- 复权映射（autype）：`none→0`、`qfq→1`、`hfq→2`（上游缺省 1=前复权，适配器
  始终显式传参）；stock 三值声明；指数无复权语义仅 none（与 ths 惯例一致）；
  ETF autype 是否生效**未实证**（先声明 none，探针复权生效性观察后回填）；
- 边界与分页：`start`/`end` 为标的市场时区日期（CN=Asia/Shanghai、
  HK=Asia/Hong_Kong、US=America/New_York；北京时间边界先转标的市场墙钟取
  日历日期）；**分钟 K 线 `end` 以该日 00:00 为截止、不含当日**——适配器对
  分钟周期 end 日期 +1 天补偿，闭区间 `[start, end]` 精确复核在适配器完成
  （日/周/月 K 线 end 含当日，不补偿）；单次 `num` 默认与最大 **370**（显式
  传满），`page_token` 续页（15 分钟有效，续页参数不变原则），页数上限防御
  15 页（=5550 根）——触上限且最早一根晚于 start 时 `meta.partial=true`
  （从最新往回取型的隐式截断，与 twelvedata 同语义）；
- 单位与时间戳：`volume`=股（按信封 `data.volume_precision` 还原——上游已放大
  10^n 倍，股票一般 n=0）、`turnover→amount`=成交额（各市场原币，官方未标注
  币种折算不编造换算）；行 `time_key` 为毫秒 Unix 时间戳（绝对时刻）→ 统一
  北京时间归一化（行内 `time_zone` 为显示用本地时区偏移，相对 UTC 分钟数，
  不参与标准化）；日/周/月 K 线 `time_key` 墙钟口径（市场零点还是北京零点）
  待探针核实；
- 快照（`POST /api/v1.0/quote/stock-quote`，`code_list` 单次最多 400、本网关
  单标的传 1 个）：`data_time`（毫秒 Unix）→ 行情时间、`last_price`/OHLC/
  `prev_close_price`/`volume`/`turnover→amount` 直映，**无盘口五档**（bid/ask
  恒 null，如实透出）；`extensions` 白名单：`open_price/high_price/low_price/
  prev_close_price/turnover_rate/amplitude`（% 数值原样透出，缺失不透出）+
  `suspension`（布尔，停牌标记）；中文名 `name` 不透出；上游对 `code_list`
  中非法 code 静默丢弃（ret_code 仍 0），匹配行缺失属无有效行情
  （`UPSTREAM_ERROR`）；快照为纯 REST 单次调用语义：无首包超时/
  `QUOTE_NOT_READY`（与 ths/alphafeed/tickflow 一致）；WS 行情推送专区
  （`wss://webapi-quote.futunn.com/ws`）本期不接入——**第一版不做流式**
  （用户拍板 2026-11-14），流式请求返回 `UNSUPPORTED_CAPABILITY`；
- **历史覆盖范围：全部周期 history 保持 null（未核实）**——官方文档未标注
  最早可取时间，且历史额度池（每标的 7×24h 占用、容量未公布）使 depth 探针
  成本高，回填待额度核实后单独立项（探针流水见 probes/cases/futunn.yaml）。

十个内置 Provider（akshare、alphafeed、baostock、tickflow、ths、tiingo、tushare、tqsdk、twelvedata、futunn）均已接入，接口签名与数据口径按各自官方文档核实（tiingo 的 WS 消息形态细节、futunn 的各周期历史覆盖与 K 线时间口径待数据探针实证回填，已在上述章节如实标注）；未配置凭据或未安装 SDK 时数据请求返回 `PROVIDER_NOT_IMPLEMENTED`，`provider=auto` 在无可用候选时返回 `NO_PROVIDER_AVAILABLE`，也**绝不返回写死的模拟行情**。

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

环境变量前缀 `TAPEHUB_`（见 [.env.example](.env.example)）：`TAPEHUB_VERSION`、`TAPEHUB_API_VERSION`、`TAPEHUB_CATALOG_VERSION`、`TAPEHUB_MAX_LIMIT`、`TAPEHUB_PROVIDER_ENABLED_OVERRIDES`（JSON，如 `{"akshare": false}` 可整体停用 akshare）、`TAPEHUB_TICKFLOW_API_KEY`、`TAPEHUB_ALPHAFEED_API_KEY`、`TAPEHUB_TUSHARE_TOKEN`、`TAPEHUB_TQSDK_AUTH_USER`、`TAPEHUB_TQSDK_AUTH_PASSWORD`、`TAPEHUB_TQSDK_CONN_MODE`（`shared` 默认/`per_request` 回滚）、`TAPEHUB_TQSDK_MAX_INFLIGHT_FETCH`、`TAPEHUB_TQSDK_FIRST_SNAPSHOT_TIMEOUT`（快照首包等待，默认 5 秒）、`TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT`（休眠订阅回收阈值，默认 64）、`TAPEHUB_STREAM_MAX_CLIENTS`（SSE 并发流上限，默认 8）、`TAPEHUB_STREAM_QUEUE_SIZE`（每流 quote 帧缓冲，默认 64）、`TAPEHUB_STREAM_HEARTBEAT_SECONDS`（SSE 心跳间隔，默认 15 秒）、`TAPEHUB_STREAM_IDLE_TIMEOUT_SECONDS`（流空闲退订超时，默认 300 秒）、`TAPEHUB_UPSTREAM_TIMEOUT_SECONDS`（bars/quotes 上游调用整体超时，默认 30 秒，超时返回 504；SSE 流不适用）、`TAPEHUB_TWELVEDATA_API_KEY`（Provider 凭据，只在内存中传给 SDK 客户端）、`TAPEHUB_FUTUNN_APP_KEY` 与 `TAPEHUB_FUTUNN_PRIVATE_KEY_PATH`（富途「方式二：传统 API Key」凭据——AppKey ID 与本地私钥 PEM 路径，两者全部非空才启用）。`.env` 不提交版本库。

### 未来扩展边界（现在不实现）

- **定时全量转储**：适配器不依赖 FastAPI 请求上下文，未来 Worker 可复用统一查询与分页模型逐批拉取；
- **新资产类型**：扩展标准枚举、能力清单与对应操作映射即可；
- 第一版不存储任何行情数据。

## 测试

```bash
uv run pytest
```

所有外部 SDK 调用均通过 `sys.modules` 注入的桩 Mock，测试不发起真实网络请求、不落盘、不依赖真实 Token；akshare 的调用参数与响应列均按官方文档构造。
