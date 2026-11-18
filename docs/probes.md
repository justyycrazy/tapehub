# Provider 数据探针框架

`probes/` 是 Provider 数据探针子系统：用真实上游调用实证核实能力目录声明
（及声明之外的边界），把各 Provider 文档中「未核实 / 保持 null / 待核实」的
诚实边界变成**有据可查的时点证据**。各轮运行记录见
`probes/results/PROBE_LOG.md`（不入库，见「结果记录」一节）。

本文是探针框架的唯一文档：**运行规范**（探针文件格式、类型语义、运行方式、
结果解读）与**设计动机与决策记录**的唯一出处。各 Provider 探针覆盖范围与
定案结论的事实记录见 [providers/](providers/)<name>.md「探针依据」与
`probes/cases/<name>.yaml`。**探针不是服务运行时的一部分，也不进入 CI
测试**：`uv run pytest` 保持零网络、零真实凭据。

## 设计原则

核心原则（与整体架构对齐，立项即定）：

1. **探针不是服务运行时的一部分**：独立目录、显式命令运行，绝不进入
   pytest/CI——测试仍然全部 Mock、零网络；
2. **声明内探针走统一链路**（`adapter.fetch_bars` / `adapter.fetch_quote`），
   框架完全通用、零 Provider 专属代码；声明外探索与错误指纹走**声明式
   原始调用**（YAML 里写 SDK 调用，框架反射执行），客户端生命周期用
   connection 探针表达；
3. **能力目录仍是唯一事实来源**：探针不自动改 manifest，只产出报告，
   回填由人工确认后单独提交；
4. **凭据纪律不破例**：只从环境变量注入，结果文件只记录「凭据是否已
   配置」（布尔），绝不记录值；行情样本每 case 最多 3 根 bar；
5. **框架自身的测试同样全部离线**：schema 校验、manifest 矩阵展开、
   分析算法（合成 fetch 闭包构造已知数据）、raw/connection 执行器
   （`sys.modules` 注入假 SDK 模块）均在 `tests/unit/probes/`，
   `uv run pytest` 保持零网络、零真实凭据。

## 目录结构

```text
probes/
  schema.py / loader.py / runner.py / analysis.py / report.py / cli.py / logging_setup.py
  cases/                 # 探针文件：每 Provider 一个，与 BUILTIN_PROVIDERS 同名
  results/               # 运行产出（.gitignore 整目录，不入库）
    PROBE_LOG.md         # 各轮运行记录（人工追加，见「结果记录」一节）
tests/unit/probes/       # 框架自身离线测试
```

框架文档即本文（`docs/probes.md`）。

## 运行方式

```bash
uv run python -m probes.cli list                              # 展开 case 清单（零网络）
uv run python -m probes.cli run --provider tushare --dry-run  # 打印计划调用，不发请求
uv run python -m probes.cli run --provider tushare --yes      # 真实运行（消耗上游配额）
uv run python -m probes.cli run --yes                         # 全部 Provider
uv run python -m probes.cli run --provider ths --yes -v --log-file probes/results/run.log
```

主题聚焦验证（只探某资产类型/某几所等）用 `--cases-dir` 指向聚焦目录
`probes/results/_cases-focus-<主题>/`：目录须含全部 Provider 同名文件
（loader 强制），聚焦 Provider 自规范文件裁剪 targets/raw——完整方案见
[AGENTS.md](../AGENTS.md)「数据探针」节「聚焦目录方案」。

真实运行必须显式 `--yes`；退出码：0 正常（允许 skipped/observe）、
1 存在 failed、2 参数或前提问题。凭据经 `.env` / 环境变量（前缀 `TAPEHUB_`）
注入，运行前确认 `requires.env` 列出的变量已配置。

## 日志与输出

运行输出统一走标准库 `logging`（`probes.*` 命名空间，`run` 子命令入口经
`probes/logging_setup.py` 的 `setup_logging` 配置）；`list` 子命令与缺
`--yes` 的用法提示是普通 print，不属于运行日志。

级别语义：

| 级别 | 内容 | 默认控制台 |
| --- | --- | --- |
| INFO | case 生命周期（`[RUN]` / 判定行）、**每次真实上游调用的子步骤行**（`[step]`）、段头/产物路径/判定统计 | 可见 |
| DEBUG | 纯计算细节（预算耗尽中止调用等）；`-v/--verbose` 打开 | 隐藏 |
| ERROR / WARNING | 段级异常中止、failed case 汇总、停机钩子异常 | 可见 |

子步骤行（每次真实上游调用一行，与 pacing 节流天然对齐，噪声有界）：

- `history_depth` 二分模式：每轮探测一行，带阶段标签与预算水位
  （`era#1 [2021-01-01, 2025-12-31] count=0 (预算 2/24)`）；阶段
  `era` / `bisect` / `verify`（全量模式为 `full`；`bisect` 为日历边界
  网格二分）同时写入 JSON 证据的 probes 条目，Markdown history 表汇总
  为「阶段」列；
- `adjust_diff`：baseline / compared 每侧一行（adjust 值 + bar 数）；
- `variant_consistency`：每个 variant 抓取一行（variant 名 + bar 数）；
- `raw`：client 构造、每条 call（序号 + 方法名 + 行数或异常）、close 各一行；
- `connection`：每实例 created/first_call 计时一行 + closed 计时一行。

控制台 handler 挂 **stdout**（进度行自描述、每次 emit 自动 flush）：
`> out.txt` 重定向后仍实时可见；卡住的 case 通过「最后一条 RUN/step 行」
直接定位。

`--log-file PATH` 追加文件 handler，**恒为 DEBUG 全量**（无视控制台级别，
时间戳统一北京时间，追加写入）：非 verbose 运行也能留下完整时间线供事后取证。

脱敏纪律：子步骤行只含序号/方法名/行数/耗时/异常类型与截断消息，绝不打印
kwargs 值（raw 探针 kwargs 经 `$ENV` 注入凭据）；错误文本截断后与证据
observation 同口径。

## 探针文件格式

文件名 = Provider 名（`cases/<provider>.yaml`）。所有模型 `extra="forbid"`，
未知键一律拒绝；维度取值复用 domain 枚举与 ISO 8601 Duration。

```yaml
probe: tushare                     # 必须等于 BUILTIN_PROVIDERS 中的名称
version: 2                         # 探针文件格式版本（当前 2，迁移说明见下）

requires:                          # 运行前提；缺失时整文件 skip 并写入报告
  extras: [tushare]                # SDK 模块名（同时是 pyproject 可选依赖组名）
  env: [TAPEHUB_TUSHARE_TOKEN]     # 必需环境变量

pacing:                            # 节流与预算（探针消耗真实配额，默认保守）
  call_interval_s: 1.5             # 相邻上游调用最小间隔
  timeout_s: 30                    # 单次调用超时
  max_calls: 120                   # 本文件调用预算，超出即中止

targets:                           # 每个 asset_type 的基准标的 + 默认窗口
  stock:                           # 键为 asset_type、asset_type@MARKET（市场限定）
    instrument: CN-SSE-600000      # 或 asset_type@MARKET-EXCHANGE（交易所限定）
    window: {start: 2025-01-01, end: 2025-12-31}
    min_bars: 100                  # 可选：count 低于该值判 failed
    required_fields: [close]       # 可选：字段全 null 判 failed
    expires: 2026-12-23            # 可选（合约/期权目标）：到期边界（YYYY-MM-DD）——
                                   # 过期该目标全部 case 加载期 skip（带原因进报告）、
                                   # 临期 30 天内运行前告警；取值=上游不可查的保守月末
                                   #（注释已知摘牌日用精确日）
  stock@HK:                        # 同一 asset_type 跨多市场声明能力时（如
    instrument: HK-HKEX-00700      # tickflow 的 CN/HK/US stock），每个要探的
    window: {start: 2025-01-01, end: 2025-12-31}   # 市场各给一个限定键；case id
                                   # 以限定键为前缀（stock@HK-P1D-...）
  stock@CN-SZSE:                   # 交易所限定键：市场基准标的之外的追加目标，
    instrument: CN-SZSE-000001     # 逐所细分探测（availability/adjust_diff 按同一
    window: {start: 2025-01-01, end: 2025-12-31}   # 能力矩阵独立展开）；标的的
                                   # market/exchange 段必须与键限定一致（加载期
                                   # 校验）；限定键的 market 段不在能力覆盖范围
                                   # 时展开期硬报错（孤儿键不静默忽略）

chain:                             # ── 统一链路探针 ──
  include_declared: true           # 从 manifest 自动展开全部声明组合：
                                   # bars → availability（fetch_bars），
                                   # quotes → quote_availability（fetch_quote，
                                   # 无周期/复权维度，每组合 1 次调用）
  skip:                            # 显式跳过必须给原因（进报告）
    - {case: contract-P1D-none-eastmoney, reason: 合约已到期摘牌}
  adjust_diff:                     # 复权生效性：同窗口对比价格序列
    targets: [stock]
    window: {start: 2024-01-01, end: 2024-12-31}
    pairs: [[none, qfq], [none, hfq]]
    fields: [close]
  history_depth:                   # 历史深度：纪元回溯 + 日历边界网格二分
    targets: [stock]               # 合约类有时限标的（contract/期权三子类）不入列（见规范化规则）
    earliest: 1991-01-01
    era_step_years: 5
    max_calls: 24
    snap: month                    # 可选：声明值对齐的日历边界粒度
                                   #（month=每月 1 日 / quarter=季初 / half_year=半年初，默认 month）
  variant_consistency:             # 跨 variant 一致性
    targets: [stock, index]
    window: {start: 2025-01-01, end: 2025-12-31}
    fields: [close]
    tolerance: 0.0                 # 可选：绝对容差，差异 ≤ 容差不判不一致（仍如实记录）
  limit_truncation:                # 单次上限：最小周期 + 超长窗口
    targets: [contract]
    interval: P1D
    max_range: P1Y
    documented_max: 2000           # 可选：不写则只观察

connection:                        # ── 连接生命周期：创建→首调→释放 计时与共存 ──
  - id: connect-latency
    purpose: 建连+鉴权+首数据耗时（重复计时）
    module: tqsdk
    client: {attr: TqApi, kwargs: {auth: {"$ctor": {attr: TqAuth, kwargs: {user: "$U", password: "$P"}}}}}
    first_call: {method: get_kline_serial, kwargs: {symbol: "SHFE.au2612", duration_seconds: 86400, data_length: 2}}
    close: {method: close}         # 必填：连接类探针必须显式声明释放方式
    rounds: 3                      # 顺序轮数
    concurrent: 4                  # 每轮同时创建的实例数（共存验证）

raw:                               # ── 声明外探索 / 错误指纹（SDK 反射）──
  - id: fut-dce-suffix
    purpose: 探测大商所后缀（声明外）
    module: tushare                # SDK 根模块
    client: {attr: pro_api, kwargs: {token: "$TAPEHUB_TUSHARE_TOKEN"}}
    call:                          # 首条作用于 client，后续条作用于上一条结果（链式）
      - {method: fut_daily, kwargs: {ts_code: "M2409.DCE", ...}}
    close: {method: close}         # 客户端释放方法（可选但建议）：case 结束后
                                   # 显式释放第三方 SDK 客户端（不消耗调用预算），
                                   # 防止连接/asyncgen 跨段泄漏
    expect: {status: ok, min_rows: 1}   # 或 {status: error, error_contains: "..."}（空串=任意错误）
    assert: true                   # false = 仅观察（observe），不判定

notes: 本探针要回答的问题与已知存疑点。
```

### 格式版本迁移

- **v2（当前）**：`history_depth` 删除 `probe_span_days`，新增 `snap`
  （声明值对齐的日历边界粒度：`month` / `quarter` / `half_year`，默认
  `month`）——二分搜索空间从连续日期改为日历边界网格；声明口径变为
  **保守上界**（首个 ≥ 首根实测的日历边界），首根实测另记 `first_bar`
  证据；迁移方式：删除 case 文件中的 `probe_span_days` 键、`version: 2`；
- **v1**：二分以 `probe_span_days` 天为窗口粒度，`earliest_estimate` 为
  上界估计（粒度 = 窗口宽度，收敛点为任意日期）。

### 规范化规则

| 规则 | 说明 |
| --- | --- |
| **换基准必复探** | history.depth 的上市/基日锚随基准标的走：R3 锚定基准标的发生变更 → **必须重新探测**该资产历史深度，以新实测为准后方可回填/维持锚定值（2026-11-18 定案；存量待办：twelvedata US stock 换 GE 后未复探，声明暂为 R1 归一口径） |
| **复探差异处置** | 重新探测值与已记录声明值不同 → **按正常流程报告并等待人工确认是否回填**，不得自动回填（2026-11-18 定案；同源 Provider 库界漂移同此处置） |
| **碎片区判据（history_depth 可回填条件）** | 稀疏碎片档案的处置（2026-11-18 定案）：**多周期互证干净收敛 → 可回填**（各声明周期独立探测收敛于同一 floor、complete）；bisect 出现全空窗/无法收敛 → 不可定案，维持 null 或按全局统一口径归一，不得以单周期观测回填 |
| **探测目标轮换优先** | 探测目标因档案过浅/数据缺口/到期失效时，**轮换成熟标的优先**（目录外直测定案后换码；期权按现价锚定 ATM、ETF 选成熟标的），**不为个别目标放宽 min_bars**（2026-11-18 定案） |
| **合约目标到期自动 skip** | 合约/期权目标的 targets 键可声明可选 `expires`（到期边界）：已过 → 该目标全部 chain case 加载期 skip（带原因进报告，与「skip 必须带 reason」同款防静默漏探；不硬错误拒绝运行——探针为开发服务、非巡检，且避免单 Provider 过期目标阻断其余 Provider）；临期 30 天内 → 运行前警告；报告 failed 的合约/期权 case 标注「疑似到期/摘牌或上游逐合约缺口，按目标轮换流程核实」（只标疑似不断言——上游缺口形态多样）；未声明视为不受限（证券类目标不设该字段）（2026-11-18 定案） |
| **周线/月线键位跨源差异** | 周线根标注（周首日 vs 周内最后交易日）、月线根标注（自然月首日 vs 月末交易日）跨源不同——已在各 Provider docs「上游行为定案」记录（futunn/alphafeed/tickflow 等），跨源周/月线根日期不可直比；探针框架 bisect 月窗键位校准**待单独立项**（2026-11-18） |
| **depth 声明复核** | history_depth 展开时从 manifest 读该能力该周期的 `history.start` 声明，判定后与实测首根对照——**相容性判定只看 first_bar（实测首根），earliest_estimate 二分估计不参与**（2026-11-18 裁决）：日历月粒度方向判定——相同月=相容、首根月>声明月=over-claim（声明承诺区间内实测无数据，报告加粗标注、走「复探差异处置」人工确认）、首根月<声明月=under-claim（保守成立）；判定恒不自动改 manifest，verdict 维持 observe |
| **history.start 全局统一口径** | 探针实测/二分估计回填 `history.start` 按四条规则归一（2026-11-17 定案并经七条补充裁决细化，全 Provider 统一遵守）：① GLOBAL forex 与 US/HK stock/etf 实测早于 2000-01-01 → 统一声明 2000-01-01（HK 同 US）；② GLOBAL crypto 实测早于 2020-01-01 → 统一声明 2020-01-01；③ CN 实测首根**精确等于基日或上市日** → 按该日期锚回填（approx 估计不适用；同源实测互证不可作为他家 R3 依据；基日/上市日核实源=人工提供，实测首根必须与其一致方可锚定）；④ 其余取实测估计月份的 1 日零点（月首对齐）。统一北京时间零点（T00:00:00+08:00）；null（未核实）不适用；**开始时间之前的数据状态不入 docs、只入探针日志**（裁决 2），实测细节见 `probes/results/PROBE_LOG.md` |
| 文件名 = Provider 名 | loader 只认 `BUILTIN_PROVIDERS` 同名文件，缺失/多余即报错 |
| case ID | bars 自动生成 `{asset_type}-{interval}-{adjust}-{variant}`（快照为 `quote-{asset_type}-{variant}`，市场/交易所限定键带前缀，如 `quote-stock@HK-tickflow`）；分析类为 `adjust-diff-<at>-<interval>-<a>-vs-<b>-<variant>`、`history-depth-<at>-<interval>-<variant>`、`limit-<at>-<interval>-<variant>`、`variant-consistency-<at>-<interval>-<adjust>`；raw 自定义 `^[A-Za-z][A-Za-z0-9._-]*$` 且全局唯一 |
| 声明内不抄写 | `include_declared: true` 从 manifest 矩阵展开，manifest 改动探针自动跟随 |
| **市场限定目标** | `targets` 键与各分析探针的 `targets` 列表项支持 `asset_type@MARKET` 与 `asset_type@MARKET-EXCHANGE` 形式；availability 按 (market × asset_type × interval × adjust × variant) 展开，每市场用对应限定键的标的（无限定键时回退不限定键且标的市场一致才命中）。市场限定目标的 case id 带前缀（如 `stock@HK-P1D-none-tickflow`、`history-depth-stock@US-P1D-tickflow`）；不限定键的 id 不变（向后兼容既有 skip 条目与历史报告） |
| **交易所限定目标** | `asset_type@MARKET-EXCHANGE` 是市场基准标的之外的**追加**目标（不抑制基准目标、不回退裸键），每个交易所目标按同一能力矩阵独立展开全部 case（availability/adjust_diff 等），id 前缀带交易所段（如 `stock@CN-SZSE-P1D-none-tickflow`）；键的 market/exchange 段必须与标的限定段一致（加载期拒绝）；限定键的 market 段不被能力目录覆盖、或分析探针引用无法解析时，展开期硬报错（孤儿目标不静默忽略） |
| **分析探针全量展开** | adjust_diff 按 (interval × variant × pair)、history_depth / limit_truncation 按 (interval × variant) 在声明矩阵上**全量展开**，不与「声明顺序第一组维度」绑定；`intervals` 字段可显式收敛 interval 维度以控制预算（variant 维度永不收敛） |
| **合约类不统计历史深度** | 有时间期限的标的合约（`contract`、期权三子类 `commodity-option`/`index-option`/`etf-option` 等合约级目标）不入 `history_depth.targets`（2026-11-16 决议）：合约级「最早可用」随挂牌/摘牌轮换漂移，深度是资产类级结论，由存续性目标（`continuous` 主连、证券类等）承载；亦不得为维持深度锚而以已摘牌合约作目标（原「已摘牌稳定锚」机制随之废止，SHFE contract 基准随之统一为现活合约） |
| **跨 variant 一致性** | `variant_consistency` 对同一 (asset_type, interval, adjust) 在**跨能力聚合的全部声明 variant** 下重放同一查询；任何不一致（数值差异 / 无重叠 / 预算中断）都判 suspect，并记录差异幅度与首个分歧时间戳 |
| skip 必须带 reason | 防止静默漏探 |
| kwargs 引用 | `$TAPEHUB_*` 环境变量（raw/connection 的 kwargs）；`{"$ctor": {attr, module?, kwargs}}` 轻量 SDK 对象构造（如认证对象，仅本地构造不做网络调用，module 缺省继承所在 case）；`{"$deadline_s": 秒数}` 相对当前时刻 → unix epoch。运行时注入，凭据值绝不落盘 |
| raw call 作用对象 | 首条作用于 client、后续作用于上一条结果（链式）；`target: client` 显式强制回到客户端 |
| raw close | 客户端型 raw case（声明 `client`）应声明 `close: {method: 客户端释放方法}`——与 connection 探针的 close 必填同款设计，case 结束（含调用失败/预算耗尽）后尽力释放客户端并记录 `closed`；close 是本地释放动作，不消耗调用预算，失败不影响判定 |
| 跨 kind id 唯一 | raw / connection 与 chain 的 case id 全局唯一；重复在加载期拒绝 |

## 探针类型语义

| 类型 | 链路 | 做什么 | 判定 |
| --- | --- | --- | --- |
| availability | chain | 对 bars 每个声明组合查一次（fetch_bars） | 0 bars / 低于 min_bars / required 字段全 null → failed；否则 confirmed |
| quote_availability | chain | 对 quotes 每个声明组合查一次（fetch_quote，全链路二级路由→上游→标准化）；无周期/复权维度，`include_declared: true` 自动展开，`quote-` 前缀 case id | 适配器失败（含上游「无有效行情」语义）→ failed；last/bid1/ask1 全 null → failed（捕捉订阅未就绪 NaN 置空、权限静默降级）；否则 confirmed。volume/amount 的 null（如外汇无量）不判定、如实记录进 observation |
| adjust_diff | chain | 同窗口 none vs qfq/hfq 对比（按 interval × variant × pair 全量展开） | 序列完全一致 → **suspect**（adjust_no_op_suspect）；有差异 → confirmed；无重叠 → suspect |
| history_depth | chain | 纪元回溯 + 日历边界网格二分（`snap` 粒度，默认月边界；按 interval × variant 全量展开）：收敛到最小非空边界 B*，B* 之前逐窗实测无数据 + B* 窗口实测有数据；声明非 null 时走**声明抽验快路径**（单次调用读 [earliest, 声明月末] 最早 bar，月粒度相容判定通过即收工——2026-11-18），否则二分 | 恒为 observe；`first_bar` 为首根实测（二分模式 = 验证窗返回首根；全量模式 = 精确最早 bar，approx=False）；**声明复核**（2026-11-18）：判定只看 first_bar，与 manifest `history.start` 声明做日历月粒度方向判定（相容/over-claim/under-claim/unknown），结果入 observation 与报告复核表；`earliest_estimate` 仅为收敛机制中间产物、不参与判定；触底/预算中断/未收敛一律不回填 |
| limit_truncation | chain | 最小周期 + 超长窗口数 bar（按 variant 全量展开） | 实测 = documented_max → confirmed；不等 → suspect；未设 → observe |
| variant_consistency | chain | 同一 (interval, adjust) 查询在跨能力聚合的全部 variant 下重放并两两对比 | 差异 ≤ `tolerance` → confirmed（差异幅度如实记录）；超出 → suspect，记录 max_abs_diff 与 first_divergence；无重叠 / 预算中断 → suspect |
| raw | SDK 反射 | 声明外能力探测与错误指纹（`min_fields` 验证 mapping 字段存在性；已声明能力的快照字段矩阵自 quote_availability 入链后不再经 raw） | 期望匹配 → confirmed / 不匹配 → failed；`assert: false` → observe |
| connection | SDK 反射 | 客户端「创建 → 首次取数 → 释放」生命周期计时与并发共存验证（rounds × concurrent） | 全部实例创建/首调/释放成功 → confirmed；任一异常 → failed；`assert: false` → observe。观测值：每实例 create/first_call/close 毫秒 |

判定枚举：`confirmed` / `failed` / `suspect` / `skipped` / `observe`。

## 结果解读

- `probes/results/<provider>/<时间戳>.json`：完整证据（case + 观测 + 判定 +
  环境布尔）；
- `probes/results/report-<provider>-<时间戳>.md`：人读汇总，含「已确认能力 /
  声明未通过 / suspect+observe / history.start 建议回填值 / skipped」五张表；
- 结果是**时点证据**，报告带时间戳，不代表上游当前状态；
- 探针工具及其报告的时间口径统一北京时间（+08:00）：运行时间戳、结果
  文件名（`YYYYMMDDTHHMMSS+0800`）与观测中的 bar 时间戳均为北京时间；
- 样本数据每 case 最多 3 根 bar；凭据只记录「是否已配置」布尔。

**manifest 回填流程**：报告 → 人工确认 → 修订 manifest（`history.start`、
复权声明收敛、新增周期/后缀声明）与 `docs/providers/<name>.md` → 单独提交。
探针永不自动改 manifest。`history.start` 回填口径（2026-11-17 全局统一口径
+ 2026-11-18 裁决）：以 **first_bar 实测首根**为唯一判定证据——声明复核
（月粒度方向判定）相容则维持，over-claim 走「复探差异处置」人工确认，
统一规则（R1~R4/R6）由人工定案推导，`earliest_estimate` 不参与判定。

## 结果记录

- `probes/results/PROBE_LOG.md` 是全部轮次运行记录的唯一汇总文档（不入库）：
  **每轮跑完后，把该轮条目追加到对应 Provider 小节最上方**（节内按时间倒序）；
- 条目格式（见该文件「记录格式」）：日期与轮次、报告产物文件名、
  confirmed/failed/observe 统计与预算、关键结论（保留关键数字与根因结论）、
  后续待办；
- 能力结论的规范出处是 `docs/providers/<name>.md`；PROBE_LOG 只记录运行
  事实与报告产物位置，不复述规范；
- 原始 JSON 证据与 `report-*.md` 报告同目录留存（时点证据，不入库）。

## 安全纪律

- Provider 凭据只经环境变量注入；探针文件与结果文件中禁止出现任何凭据字面量
  （错误指纹探针可使用明显的字面量假 Key，如 `probe-invalid-key-not-a-credential`）；
- pacing + max_calls 预算必须保守设置；探针消耗的是真实上游配额；
- 日志与结果不记录凭据值、完整行情响应。

## 各 Provider 探针要点

设计层面每个 Provider 探针必须回答的问题（定案结论的规范出处是各
Provider 文档「上游行为定案」节；覆盖范围与证据出处见「探针依据」节）：

| Provider | 探针焦点 |
| --- | --- |
| akshare | 三源（eastmoney/sina/tencent）可用性与同标的一致性、CN 证券逐所（SZSE/BSE）、期货逐所扩容、复权生效性、证券类历史深度（continuous 全量）、variant_consistency、BSE 指数字段语义指纹；无凭据但东财 push2 站点限流敏感 |
| tushare | 周/月线复权生效性、期货六所合约/主连与单次上限（交易所限定 chain 目标）、北交所接入复验、期权八所日 K、积分/限频错误指纹 |
| tickflow | 三市场（CN/HK/US）全声明维度可用性、复权生效性、历史深度、单次上限、quotes 字段矩阵 |
| alphafeed | 同 tickflow 焦点（三市场日线全声明维度 + quotes 字段矩阵）+ 错误指纹 |
| tqsdk | 合约/主连/加权可用性、data_length 单次上限裁决（定案 5000 根）、复权生效边界（分钟族 no-op）、CZCE 4 位年月码等错误指纹、连接时长（connection） |
| twelvedata | 外汇/加密货币/美股可用性、历史深度、单次上限、错误指纹；免费档 8 次/分钟限频 |
| tiingo | 外汇/加密货币可用性、历史深度、单次上限观察、墙钟口径、扩容周期探测、错误指纹；免费档 50 次/小时限额 |
| ths | 历史 K 线可用性、复权生效性、历史深度收敛、隐式截断常态复验、快照/分时形态（量额口径核实） |
| baostock | 全声明维度可用性、复权生效性、历史深度、「空结果不报错、错误码仅 SDK stderr 打印」语义的适配器分类依据 |
| futunn | 三市场全声明维度可用性、逐所细分（SZSE/BSE）、复权生效性、逐周期深度（era 二分）、无签名错误指纹、校时端点形态；历史 K 线每标的额度池特性 |

## 新增 Provider 时

1. `cases/<name>.yaml` 按 Provider 要核实的问题编写（参考现有十份文件）；
2. 新增分析探针时先在 `tests/unit/probes/` 补离线测试（合成 fetch 闭包）；
3. 框架代码保持 Provider 无关：Provider 专属事实只允许出现在 `cases/*.yaml`。

## 关键设计决策

| 决策 | 内容与动机 |
| --- | --- |
| 声明内不抄写 | `include_declared: true` 从 manifest 能力矩阵自动展开全部声明组合；manifest 改动探针自动跟随，杜绝规范文件与能力目录的双抄漂移 |
| 分析探针全量展开（2026-11-03 v2 修订） | 分析探针不再与「声明顺序第一组维度」绑定——adjust_diff 按 (interval × variant × pair)、history_depth / limit_truncation 按 (interval × variant) 在声明矩阵上全量展开；`intervals` 字段可显式收敛 interval 维度以控制预算，variant 维度永不收敛 |
| variant_consistency（2026-11-03 引入） | 对同一 (interval, adjust) 在跨能力聚合的全部声明 variant 下重放同一查询并两两对比；任何不一致（数值差异 / 无重叠 / 预算中断）判 suspect，并记录 max_abs_diff 与 first_divergence——捕捉跨源/跨 variant 数值漂移的常态化手段 |
| history_depth 口径演进（2026-11-15 v2 格式） | v1 以 `probe_span_days` 天窗口二分（收敛点为任意日期）→ v2 改**日历边界网格二分**（`snap` 粒度：月/季/半年边界），声明口径变为**保守上界**（首个 ≥ 首根实测的日历边界，查询从声明起点起保证有数据），首根实测另记 `first_bar` 证据；全量模式（单次全窗口调用读最早 bar）此后被声明抽验快路径吸收（2026-11-18 退役 full_history_targets）；`max_calls` 语义为**每条 case** 的预算 |
| 合约类不统计历史深度（2026-11-16 决议） | 有时间期限的合约级标的（contract、期权三子类）不入 `history_depth.targets`：合约级「最早可用」随挂牌/摘牌轮换漂移，深度是资产类级结论，由存续性目标（`continuous` 主连、证券类）承载；亦不得为维持深度锚而以已摘牌合约作目标（原「已摘牌稳定锚」机制随之废止） |
| 早上市基准标的 | 历史深度二分的收敛点可能是标的自身上市日伪影（如 600000 收敛点即其上市月）；各 Provider 的证券类基准统一换用早上市标的，收敛点远离上市日即真实库底而非标的伪影。具体标的选择是 Provider 专属事实，只记录在各 `cases/*.yaml` |
| 市场/交易所限定目标 | 同一 asset_type 跨多市场声明能力的 Provider（如 tickflow/alphafeed/futunn 的 CN/HK/US stock）用 `asset_type@MARKET` 为每个市场提供独立基准；`asset_type@MARKET-EXCHANGE` 是市场基准之外的**追加**目标，用于逐所细分探测适配器交易所映射分支；限定键与标的失配、市场不被能力覆盖等孤儿键一律展开期硬报错，不静默忽略 |
| 快照可用性入链（2026-11-15） | quotes 声明组合经 `adapter.fetch_quote` 统一链路探测（quote_availability，全链路二级路由→上游→标准化），快照字段矩阵证据由 raw 探针移交链路；捕捉订阅未就绪 NaN 置空、权限静默降级等失败形态 |
| connection 探针与 raw close | 客户端型 SDK 资源用 connection 探针表达「创建 → 首次取数 → 释放」的计时与并发共存（rounds × concurrent）；`close` 在 connection 探针必填、客户端型 raw case 建议——case 结束（含调用失败/预算耗尽）后显式释放客户端，防止连接/asyncgen 跨段泄漏；close 是本地释放动作，不消耗调用预算 |
| limit_truncation 超大 limit | 框架以 100 000 的超大查询 limit 抵消统一分页的切片干扰，让上游/适配器自身的单次上限原样可见 |
| 证据序列化兜底 | JSON 证据写盘做浅层遍历 + 未识别对象字符串化兜底：观测一旦混入 SDK 内部句柄等不可拷贝对象，证据绝不因序列化失败而丢失整轮运行 |
| 执行器缺陷修复记录（2026-11-03 v2 实施期） | 离线测试发现并修复两处执行器缺陷：adjust_diff 对比序列曾未使用 compared_adjust（实际抓了两次 baseline）；分析 fetch 闭包未兜底未识别的 SDK 异常（直连适配器绕过了 BarQueryService 的统一包装，已在 fetcher 侧折算为 `UpstreamError`） |

## 主要风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 上游配额消耗 | pacing + `max_calls` 预算双闸（探针消耗真实上游配额）；`--dry-run` 预检后再真跑；聚焦目录方案（`--cases-dir`）把一轮运行收窄到单一主题 |
| 结果时效性 | 报告带时间戳，明示为**时点证据**、不代表上游当前状态；各轮运行事实记录于 `probes/results/PROBE_LOG.md` |
| 慢实例 Provider（tqsdk） | pacing 显著放宽（数值记录在 `cases/tqsdk.yaml`） |
| 站点限流脆弱（东财 push2 等） | Provider 专属节流事实只记录在各自 `cases/*.yaml`；限流期内每轮失败调用会延长封禁（越试越封），重试规程见 `cases/akshare.yaml` |

## 决策与变更记录

- **2026-11-18（history_depth 两段式：声明抽验快路径 + full_history_targets
  退役）**：抽验通过（compatible/under_claim）→ 单调用收工不跑二分
  （depth 常驻成本降至 1 调/条，「收敛后移除」不再必要，声明每轮都有
  抽验守护）；抽验不通过（over_claim）或窗口倒置/空结果/调用异常 →
  降级二分辨明真实库底（最终判定以二分为准）；声明 null 直接二分
  （收敛即首次回填证据）。full_history_targets 退役（快路径吸收其
  场景）；判定矩阵与快/慢路径语义见「规范化规则·depth 声明复核」与
  「探针类型语义·history_depth」；
- **2026-11-18（depth 声明复核：判定只看 first_bar）**：history_depth
  展开时从 manifest 读 `history.start` 声明，判定后与实测首根做日历月
  粒度方向判定（相容/over-claim/under-claim/unknown）；**earliest_estimate
  二分估计不参与判定**（仅为收敛机制中间产物）——over-claim 报告加粗
  标注并走「复探差异处置」，verdict 恒 observe 不自动改；报告「建议回填
  值」表重构为「depth 实测首根与声明复核」（first_bar/现声明/相容性/
  complete 四列，estimate/approx/粒度/阶段列退役入 JSON 证据）；
- **2026-11-18（合约目标到期自动 skip + 临期告警 + failed 标注）**：运行时
  询问是否换码不成立（换码=运行前策划流程：选码+直测+人工确认；CLI 全
  flag 非交互设计 + 后台运行场景 stdin 不可用）——改为目标级 `expires`
  字段（加载期检查）：过期自动 skip 带原因、临期 30 天告警、failed 合约/
  期权 case 标注疑似到期/缺口；48 个合约/期权目标一次性补齐该字段；
- **2026-11-18（换基准必复探 + 复探差异处置）**：基准标的变更必须重新
  探测历史深度；重新探测值与记录值不同按正常流程报告并等待人工确认
  是否回填，不得自动回填（同源 Provider 库界漂移同此处置）；
- **2026-11-18（回填规则三条补充成文）**：碎片区判据（多周期互证干净收敛
  → 可回填）、探测目标轮换优先（成熟标的优先、不为个别目标放宽
  min_bars）、周/月线键位跨源差异（已记录 docs；框架校准待单独立项）
  成文入「规范化规则」；
- **2026-11-17（history.start 全局统一口径 + 七条补充裁决）**：回填规则从
  「按 Provider 各自实测首根精化」改为四条统一规则（2000/2020 深库归一、
  CN 上市/基日锚、其余月首对齐），全 Provider 90+ 处落地；补充裁决：统一
  北京时间零点、开始时间之前的数据状态不入 docs、R3 判定=实测首根精确
  等于基日/上市日、同源互证不可作他家 R3 依据、index 同款基日/上市日锚、
  HK 同 US 归一、alphafeed/tickflow 算法统一——alphafeed CN stock 首根
  自证聚焦补测（1990-12-19=开业日，与 tickflow 一致）；证据留档探针日志；
- **2026-11-17（窗口 end 时域口径修订）**：探针 targets/分析探针的裸日期
  `end` 归一由「当日零点」改为「当日末 23:59:59.999999」（`start` 仍取
  零点）。原因：分钟族 bar 时间戳为盘中时刻，零点 end 经适配器闭区间复核
  会排除末个交易日全部盘中 bar——日历 end 恰为交易日时分钟族计数恰差一天
  （baostock「缺末日」误判的根因）；日/周/月线零点戳 bar 两种口径行为
  一致，daily 族证据不受影响。证据可比性注记：口径变更前产生的分钟族
  availability 计数比新口径少一个交易日（当 end 为交易日时）。月/周线
  bar 日期在周期末尾，窗口未覆盖周期末尾时同样查空——与 end 时域无关，
  属查询设计纪律（窗口须含周期末尾）。适配器闭区间复核语义不变（本修
  仅改探针层喂入的窗口边界）；API 面不受影响（纯日期在 API 边界即拒绝）；
- **2026-11-03**：探针子系统立项（原 `probes/PLAN.md` v1 批准）；同日 v2
  修订——分析探针全量展开、variant_consistency 引入、执行器两缺陷修复、
  history_depth `max_calls` 改为每条 case 预算；
- **2026-11-05**：时间口径统一北京时间（运行时间戳、结果文件名、观测
  bar 时间戳均为 +08:00）；
- **2026-11-14**：文档体系重构，探针运行规范定稿于 `probes/README.md`；
- **2026-11-15**：history_depth 升级 v2 文件格式（日历边界网格二分、
  snap 粒度、保守上界口径 + `first_bar` 精确实测、全量模式）；quotes
  快照入链（quote_availability 展开，raw 字段矩阵退役）；
- **2026-11-16**：聚焦目录方案入档（AGENTS.md「数据探针」节）；
  「合约类不统计历史深度」决议与探针目标码跨源统一落地；
- **2026-11-16（文档统一）**：`probes/PLAN.md`（设计动机）与
  `probes/README.md`（运行规范）先后退役并入本文——探针框架文档统一为
  `docs/probes.md` 单一出处；后续设计决策与规范变更增量记录于此
  （探针文件格式版本迁移说明见「格式版本迁移」节）。
