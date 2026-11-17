# Tiingo（tiingo）

聚合行情数据源适配器：GLOBAL 外汇/加密货币的历史 K 线（REST 直连）与实时快照/快照流
（WS 订阅缓存），无官方 Python SDK，凭据经请求头注入，缺省路由置 twelvedata 之后回退位。

## 概要

- 接入方式：无官方 Python SDK——历史 K 线用 `httpx2` 同步客户端直连
  `https://api.tiingo.com`（模块内延迟导入，未安装返回 `PROVIDER_NOT_IMPLEMENTED`），
  同步调用经 `call_in_thread` 隔离到线程池；实时快照/快照流用 `websockets` 库直连
  WS 端点。接口签名按官方文档核实（general/connecting、crypto、forex、
  websockets/crypto、websockets/forex 五页）：
  https://www.tiingo.com/documentation/general/connecting 、
  https://www.tiingo.com/documentation/crypto 、
  https://www.tiingo.com/documentation/forex 、
  https://www.tiingo.com/documentation/websockets/crypto 、
  https://www.tiingo.com/documentation/websockets/forex ；
- SDK 包（pyproject 可选依赖 `tiingo` 组）：`httpx2>=2.16`、`websockets>=14`；
- 凭据与启用条件：`TAPEHUB_TIINGO_API_KEY`（API Key 即账户凭据，经
  `Authorization: Token <key>` 请求头传上游——token 不落 URL，绝不写入代码、
  manifest、日志或异常响应）。变量未配置时不启用：能力目录仍可见（`enabled=false`），
  auto 路由跳过，显式请求返回 `PROVIDER_DISABLED`（details 携带缺失变量名）；
  显式 `TAPEHUB_PROVIDER_ENABLED_OVERRIDES` 优先于凭据推断。相关运行配置
  （订阅上限、首包超时）环境变量全表见 `../operations.md`；
- 变体体系：单接口 Provider，`variants=[tiingo]`、`default_variant=tiingo`
  （数据源即上游服务自身）；显式传 `variant=tiingo` 与缺省解析等价。

## 能力与上游映射

以 `src/tapehub/providers/tiingo/manifest.yaml` 的 capabilities/operations 为准：

| data_type | 市场 | asset_type | 变体 | 上游接口 | 周期 | adjust | 标准字段 | history.start（end 无界） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bars | GLOBAL | forex | tiingo（默认） | `GET /tiingo/fx/<ticker>/prices`（每 ticker 独立路径端点） | PT1H、PT4H、P1D | none | open/high/low/close（无 volume/amount） | 三周期同值 `2020-01-01T08:00:00+08:00`（上游档案硬边界） |
| bars | GLOBAL | crypto | tiingo（默认） | `GET /tiingo/crypto/prices`（ticker 走查询参数） | PT1H、PT4H、P1D | none | open/high/low/close/volume（base 币计；无 amount） | PT1H/PT4H/P1D `2020-01-01T00:00:00+08:00`（R2） |
| quotes | GLOBAL | forex、crypto | tiingo（默认） | WS `wss://api.tiingo.com/fx`（thresholdLevel 5）与 `wss://api.tiingo.com/crypto`（thresholdLevel 2） | —（快照无周期） | —（快照无复权） | timestamp、last_price、bid_price1、ask_price1、bid_volume1、ask_volume1 | 不适用 |

- operations 内部二级路由：`fx_prices`（外汇三周期同端点）、`crypto_prices`
  （加密三周期同端点）、`quote`（forex/crypto 各一条）；全部 match 显式写全
  六个维度，快照 match 对无意义维度（interval/adjust）显式 null；
- history.start 的实证依据见「上游行为定案」历史覆盖条目；
- REST 客户端自身超时 20s（应用层 `upstream_timeout_seconds` 默认 30s 为整体兜底，
  取更小保守值避免长挂线程占用 worker）。

## 标的代码映射

- 外汇：`GLOBAL-OTC-EUR/USD` → `eurusd`（exchange 段白名单 `OTC`，symbol 段
  去斜杠转小写）；
- 加密货币：`GLOBAL-CRYPTO-BTC/USD` → `btcusd`（exchange 段白名单 `CRYPTO`）；
- 官方 ticker 均为小写连写，适配器统一去斜杠转小写；exchange 段命名空间与
  twelvedata 同构（外汇聚合报价 `OTC`、加密聚合对 `CRYPTO`）；
- 拒绝范围：market 固定 GLOBAL；exchange 段或 asset_type 不在白名单时返回
  `UNSUPPORTED_CAPABILITY`。

## 周期 / 复权 / 单位映射

- 周期映射：仅声明 `PT1H/PT4H/P1D`，对应上游 `resampleFreq` 的
  `1hour/4hour/1day`；`PT1M/PT5M/PT15M/PT30M/PT2H` 与 `10min/6hour/12hour/
  24hour` 等其余上游合法格式收窄不声明（日内逐周期深度探测成本/噪声高，
  机制边界不外推为逐周期证据）；`P1W/P1M` 上游 `resampleFreq` 合法格式
  `<n>min|hour|day` **无 week/month**——周期本身不存在、无法声明；
- 适配器内部保留 1min/5min/15min/30min/2hour 等其余合法格式的映射
  （未声明不对外可用；fx PT1M 的 partial 判定依赖该映射）；
- 不依赖上游默认值：`startDate`/`endDate`/`resampleFreq` 全部显式传参
  （`resampleFreq` 缺省时上游返回原始频度而非声明周期口径）；
- 复权映射：外汇/加密现货无复权概念，仅声明 `none`；
- 单位：crypto `volume` 为 base 币计成交量；上游 `volumeNotional`/`tradesDone`
  不透出（标准 `volume` 已承载 base 币口径）；上游无成交额字段，`amount` 恒
  null；forex 无 volume（恒 null）；价格字段原值透出、无单位换算；缺失值与
  NaN 统一映射 null（不用 0 冒充）。

## 上游行为定案

- 时间戳口径：`startDate`/`endDate` 统一传 UTC 墙钟 `YYYY-MM-DDTHH:MM:SS`
  （无时区后缀）；两端点均为 UTC 口径（crypto `date` 带 `+00:00`、forex
  `date` 带 Z，日内分布吻合）；bar/quote 时间戳解析 ISO 8601（裸时间按 UTC
  解释）后统一北京时间归一化；
- REST 认证与错误映射：`Authorization: Token <key>` 请求头；上游 429（限频）
  → `UPSTREAM_RATE_LIMITED`（可重试限流）；其余 ≥400、JSON 解析失败与非数组
  payload → `UPSTREAM_ERROR`（details 携带 status_code）；bar 行 `date` 缺失
  或不可解析属上游契约违背，抛 `UPSTREAM_ERROR`（无时间戳无法标准化）；
- 限频口径（免费档，账户页与官方文档一致）：50 请求/小时、1000 请求/天、
  500 个唯一品种/月、1GB 带宽/月——REST 调用计请求数，WS 不计请求数只计
  带宽；单次返回行数上限官方未标注（受 1GB/月带宽配额约束）；
- 响应形状（探针实证，官方文档未如实反映 crypto 端点嵌套结构）：
  `/tiingo/crypto/prices` 返回按 ticker 分组的嵌套结构
  `{"ticker","baseCurrency","quoteCurrency","priceData":[...]}`（bar 行在
  priceData 内，字段 date/open/high/low/close/volume/volumeNotional/
  tradesDone）；`/tiingo/fx/<ticker>/prices` 为扁平行（date/ticker/
  open/high/low/close，仅 OHLC 无 volume）；适配器按形状统一拍平
  （嵌套/扁平防御兼容），bar 字段名两端点一致；
- fx 档案硬边界：上游档案起点 2020-01-01（UTC 墙钟），越界窗口 400 拒绝而非
  裁剪（两种变体 `End date must be >= 2020-01-01` / `Start date must be >=
  2020-01-01`），按指纹 `must be >= 2020-01-01` 归一化为空结果（防御兜底）；
  主处理路径为适配器钳制——`startDate` 早于档案起点钳到起点（窗口与档案有
  交集则如实返回边界后数据，闭区间复核兜底），完全越界（endDate 亦早于起点）
  直接空批不发请求；
- crypto 历史外窗口：上游优雅返回 200 `[]`（正常空批），无需特殊处理；
- 历史覆盖依据：fx 三周期与档案硬边界相符（越界窗口 400 拒绝经受控复现实证）；crypto 深库实测早于 2020-01-01 → R2 归一（实测细节见探针日志）；
- 分段/截断语义：单次请求仅返回窗口起点首个连续段（保头截尾、静默缺失，
  段后分段查询可行）。fx PT1M 段长按时间 ≈6 自然日（段行数随窗口起点漂移，
  不可硬编码 cap）→ 末根距请求 end 超 7 天（段长 + 周末/假日余量）标记
  `meta.partial=true`（PT1M 已收窄不声明，判定保留为防御）；crypto 段上限
  按行数 ≈5001 行（多次实证一致）→ 返回行数触顶即标 partial，按行数判定
  不限周期（PT1H 实用窗长 ≈208 天、PT4H ≈416 天、P1D 远超实用窗长，不受
  影响）；fx 其余周期一月窗完整实证不受分段影响；网关侧不做多段聚合
  （fx PT1M 一年 ≈61 段请求会打满小时配额），长窗调用方自行分段；
- 日内密度事实：crypto 1min 密集 ≈1440 根/天（年窗 5001 根系分段截断而非
  稀疏）；fx 1min ≈2811 根/日；
- WS 协议形态：订阅帧 `{"eventName":"subscribe","authorization":<token>,
  "eventData":{"thresholdLevel":<2|5>,"tickers":[<ticker>]}}`（token 随帧
  传递、绝不写日志），退订帧同构 `eventName:"unsubscribe"`；下行
  `{service, messageType: A|U|D|I|E|H, data}`——`I` 订阅 ack
  （`data.subscriptionId` 整数下发）、`H` 服务端心跳（订阅后立即回 1 帧，
  此后实测 ~60-90s；客户端不发应用层心跳，静默看门狗 180s 判定僵死重连）、
  `E` 错误帧（`response.code/message`，错误后服务端主动断连，重连状态机
  接管）；
- WS data 元素为位置数组（探针实证，官方文档字段表的对象形态错误）：fx Q
  8 元素 `["Q", ticker, date, bidSize, bidPrice, midPrice, askSize,
  askPrice]`（无 exchange；mid=(bid+ask)/2 经探针自洽验证）；crypto Q 9 元素
  （同上第 4 位多 exchange）；crypto T 6 元素 `["T", ticker, date, exchange,
  lastSize, lastPrice]`；对象形态透传（防御：上游若回归文档形态按字段名
  读取）；标准化见 `response_mapper.ws_element_to_snapshot`；
- tickers 服务端过滤实测生效（客户端兜底过滤保留防御）；
- WS 阈值矩阵：fx 有效值 {2, 5, 省略} 均持续推送 top-of-book；crypto 有效值
  {2 = Q+T 混合、5 = 仅成交 T、省略 = 默认仅 T}。适配器取 fx=5（文档/
  AsyncAPI 口径 7 被上游拒绝 `thresholdLevel not valid` 并断连）、crypto=2
  （top-of-book + 成交，与文档一致）；
- 退订行为：逐 symbol 退订生效且不断连（被退 symbol 经短暂在途竞态后停止）；
  退订最后一个 symbol（空订阅）时服务端关闭连接（重连状态机恢复）；
  `subscriptionId` 增删订阅不可用（增订帧实证触发服务端断连）——维持逐
  symbol 独立订阅帧方案；
- 排序与复核：标准化后统一按 timestamp 升序排序再应用 offset/limit（共享
  分页组件）；fx 起点钳制后按闭区间 [start, end] 精确复核统一时间语义。

## 快照与流式

- 参与 M2 实时快照（`GET /v1/market/quotes`）与 M3 快照流（SSE），两者同源：
  crypto/fx 双端点 WS 共享订阅（WS 不计 REST 请求配额——快照/流统一走 WS
  以保 REST 配额）；
- 冷订阅等待首包：超过 `TAPEHUB_TIINGO_FIRST_SNAPSHOT_TIMEOUT`（默认 5s）返回
  `QUOTE_NOT_READY`（408）；活跃订阅即时读缓存；订阅确认语义为「首包到达」
  ——等首包态收到首条匹配 data 元素即 `subscribed`（与 twelvedata 同口径，
  重连重订后同样触发，订阅确认先于首帧 quote）；
- Runtime：asyncio 原生共享连接，crypto/forex 各一懒启动单连接实例、互不
  干扰；同端点多存活实例会互踢连接（重连死循环），单例由部署构造保证
  （single 模式 → 一 app → 一 registry → 一 adapter → 每端点一 Runtime），
  启动期弱引用存活检测告警；建连超时 20s；建连失败/收包异常固定退避 2s 无限
  重试，重连后对全部活跃引用自动重订；`websockets` 库缺失属致命
  （`PROVIDER_NOT_IMPLEMENTED`，唤醒全部首包等待者后终止）；
- 退订（linger）：per-symbol 引用计数；引用归零延迟 60s（linger，兼顾 M2
  轮询与流快速重连免重订）后向上游发退订并本地释放缓存；linger 窗口内快照
  仍可读（新鲜度由 `received_at` 表达）；探针实证逐 symbol 退订生效且不断连、
  空订阅断连（见上），实现同时保留本地释放兜底——若上游忽略退订，linger
  到期仍完成本地释放，带宽残留由上游侧兜底；
- 无代际回收（不产生 `recycling` 事件）；断连/停机广播 `upstream_disconnected`
  后退避重连自动重订——绝不静默断流；
- 订阅闸门：并发订阅 symbol 数达 `TAPEHUB_TIINGO_SUBSCRIPTION_LIMIT`
  （默认 8，软上限防失控）返回 `UPSTREAM_SUBSCRIPTION_LIMIT`（429，SSE 响应头
  之前）；按端点 crypto/fx 各自计数；活跃口径 = 有缓存快照（活跃或 linger
  中）∪ 等首包；上游未公布并发上限，硬约束为月度 500 个唯一品种——网关不做
  计数，由限额说明与上游超限报错兜底；
- 流受理校验急切执行（operation 路由、凭据、标的映射、容量闸门——错误为
  JSON 结构，在响应头发出前抛出），Runtime 启动与订阅在首次迭代进行；事件经
  共享 StreamBuffer（coalesce 丢旧保新只约束 quote 帧）；迭代器关闭释放引用
  并注销监听器；流关闭即本地退订（上游订阅经 linger 真退订）；M2 快照取数后
  引用归零（延迟退订）；日志统一脱敏（异常文本与订阅帧含 API token）；
- 快照字段口径：`timestamp` 为上游行情时间（UTC 口径归一北京时间），
  `last_price` 取 `lastPrice`、缺失回退 `midPrice`（bid/ask 均非空时
  mid=(bid+ask)/2；两者均缺失属上游契约违背抛 `UPSTREAM_ERROR`）；单档盘口
  `bid_price1/ask_price1/bid_volume1/ask_volume1` 防御映射（缺失 → null），
  五档其余档位恒 null；标准 `volume`/`amount` 恒 null（`lastSize` 为单笔
  成交量的口径未实证，不冒充累计成交量）。

## 缺省路由参与

- bars：GLOBAL forex 与 GLOBAL crypto 组合，回退位（twelvedata 之后）；显式
  `provider=tiingo` 或 auto 链中 twelvedata 缺席/不可解析该请求维度/凭据缺失
  时启用；
- quotes：GLOBAL forex 与 GLOBAL crypto 组合，回退位（同上）；
- 位次依据：免费档配额紧于 twelvedata，且周期面仅为 twelvedata 全周期矩阵的
  子集——置回退位不改变既有缺省行为；周期子集的回退在路由选择阶段按六维
  逐个解析；
- 当前回退链顺序以 `src/tapehub/config/provider_routing.yaml` 为准，本文不
  复述 order 列表。

## extensions 白名单

- quotes：`exchange`（交易所/来源标识字符串；fx 无该字段、crypto 有，缺失
  不透出）；
- quotes 另有网关注入字段 `snapshot_age_ms`（2026-11 修订）：WS Runtime 快照
  到达时刻距响应生成时刻的毫秒数——断连重连与退订 linger（60s）窗口内命中
  缓存的快照可据此判陈旧（`received_at` 是标准化时刻，不反映数据到达）；
- bars：无（crypto 上游的 `volumeNotional`/`tradesDone` 不透出——标准
  `volume` 已承载 base 币成交量口径，`amount` 恒 null）。

## 探针依据

- 能力核实与行为实证用例：`probes/cases/tiingo.yaml`（探针框架规范见
  `docs/probes.md`）；
