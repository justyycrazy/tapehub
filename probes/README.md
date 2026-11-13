# probes — Provider 数据探针规范

用真实上游调用实证核实能力目录声明（及声明之外的边界），把 README 中
「未核实 / 保持 null / 待核实」的诚实边界变成**有据可查的时点证据**。

设计动机与决策记录见 [PLAN.md](PLAN.md)。**探针不是服务运行时的一部分，
也不进入 CI 测试**：`uv run pytest` 保持零网络、零真实凭据。

## 目录结构

```text
probes/
  PLAN.md                # 设计决策（批准时的方案全文）
  README.md              # 本文件：文件格式、类型语义、运行方式、结果解读
  schema.py / loader.py / runner.py / analysis.py / report.py / cli.py / logging_setup.py
  cases/                 # 探针文件：每 Provider 一个，与 BUILTIN_PROVIDERS 同名
  results/               # 运行产出（.gitignore 整目录，不入库）
tests/unit/probes/       # 框架自身离线测试
```

## 运行方式

```bash
uv run python -m probes.cli list                              # 展开 case 清单（零网络）
uv run python -m probes.cli run --provider tushare --dry-run  # 打印计划调用，不发请求
uv run python -m probes.cli run --provider tushare --yes      # 真实运行（消耗上游配额）
uv run python -m probes.cli run --yes                         # 全部 Provider
uv run python -m probes.cli run --provider ths --yes -v --log-file probes/results/run.log
```

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
  `era` / `bisect` / `verify`（全量模式为 `full`）同时写入 JSON 证据的
  probes 条目，Markdown history 表汇总为「阶段」列；
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
version: 1                         # 探针文件格式版本（当前 1）

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

chain:                             # ── 统一链路探针（经 adapter.fetch_bars）──
  include_declared: true           # 从 manifest 自动展开全部声明组合
  skip:                            # 显式跳过必须给原因（进报告）
    - {case: contract-P1D-none-eastmoney, reason: 合约已到期摘牌}
  adjust_diff:                     # 复权生效性：同窗口对比价格序列
    targets: [stock]
    window: {start: 2024-01-01, end: 2024-12-31}
    pairs: [[none, qfq], [none, hfq]]
    fields: [close]
  history_depth:                   # 历史深度：纪元回溯 + 二分
    targets: [stock]
    earliest: 1991-01-01
    era_step_years: 5
    max_calls: 24
    full_history_targets: []       # 可选：这些目标单次全窗口调用直接读最早 bar
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

### 规范化规则

| 规则 | 说明 |
| --- | --- |
| 文件名 = Provider 名 | loader 只认 `BUILTIN_PROVIDERS` 同名文件，缺失/多余即报错 |
| case ID | chain 自动生成 `{asset_type}-{interval}-{adjust}-{variant}`；分析类为 `adjust-diff-<at>-<interval>-<a>-vs-<b>-<variant>`、`history-depth-<at>-<interval>-<variant>`、`limit-<at>-<interval>-<variant>`、`variant-consistency-<at>-<interval>-<adjust>`；raw 自定义 `^[A-Za-z][A-Za-z0-9._-]*$` 且全局唯一 |
| 声明内不抄写 | `include_declared: true` 从 manifest 矩阵展开，manifest 改动探针自动跟随 |
| **市场限定目标** | `targets` 键与各分析探针的 `targets` 列表项支持 `asset_type@MARKET` 与 `asset_type@MARKET-EXCHANGE` 形式；availability 按 (market × asset_type × interval × adjust × variant) 展开，每市场用对应限定键的标的（无限定键时回退不限定键且标的市场一致才命中）。市场限定目标的 case id 带前缀（如 `stock@HK-P1D-none-tickflow`、`history-depth-stock@US-P1D-tickflow`）；不限定键的 id 不变（向后兼容既有 skip 条目与历史报告） |
| **交易所限定目标** | `asset_type@MARKET-EXCHANGE` 是市场基准标的之外的**追加**目标（不抑制基准目标、不回退裸键），每个交易所目标按同一能力矩阵独立展开全部 case（availability/adjust_diff 等），id 前缀带交易所段（如 `stock@CN-SZSE-P1D-none-tickflow`）；键的 market/exchange 段必须与标的限定段一致（加载期拒绝）；限定键的 market 段不被能力目录覆盖、或分析探针引用无法解析时，展开期硬报错（孤儿目标不静默忽略） |
| **分析探针全量展开** | adjust_diff 按 (interval × variant × pair)、history_depth / limit_truncation 按 (interval × variant) 在声明矩阵上**全量展开**，不与「声明顺序第一组维度」绑定；`intervals` 字段可显式收敛 interval 维度以控制预算（variant 维度永不收敛） |
| **跨 variant 一致性** | `variant_consistency` 对同一 (asset_type, interval, adjust) 在**跨能力聚合的全部声明 variant** 下重放同一查询；任何不一致（数值差异 / 无重叠 / 预算中断）都判 suspect，并记录差异幅度与首个分歧时间戳 |
| skip 必须带 reason | 防止静默漏探 |
| kwargs 引用 | `$TAPEHUB_*` 环境变量（raw/connection 的 kwargs）；`{"$ctor": {attr, module?, kwargs}}` 轻量 SDK 对象构造（如认证对象，仅本地构造不做网络调用，module 缺省继承所在 case）；`{"$deadline_s": 秒数}` 相对当前时刻 → unix epoch。运行时注入，凭据值绝不落盘 |
| raw call 作用对象 | 首条作用于 client、后续作用于上一条结果（链式）；`target: client` 显式强制回到客户端 |
| raw close | 客户端型 raw case（声明 `client`）应声明 `close: {method: 客户端释放方法}`——与 connection 探针的 close 必填同款设计，case 结束（含调用失败/预算耗尽）后尽力释放客户端并记录 `closed`；close 是本地释放动作，不消耗调用预算，失败不影响判定 |
| 跨 kind id 唯一 | raw / connection 与 chain 的 case id 全局唯一；重复在加载期拒绝 |

## 探针类型语义

| 类型 | 链路 | 做什么 | 判定 |
| --- | --- | --- | --- |
| availability | chain | 对每个声明组合查一次 | 0 bars / 低于 min_bars / required 字段全 null → failed；否则 confirmed |
| adjust_diff | chain | 同窗口 none vs qfq/hfq 对比（按 interval × variant × pair 全量展开） | 序列完全一致 → **suspect**（adjust_no_op_suspect）；有差异 → confirmed；无重叠 → suspect |
| history_depth | chain | 5 年档回溯 + 90 天窗口二分（按 interval × variant 全量展开）；`full_history_targets` 中的目标改走全量模式：单次全窗口调用直接读最早 bar，适用于上游无日期参数（每次返回全量历史，适配器内存过滤）或全量拉取代价可忽略的接口 | 恒为 observe；二分模式报告 `earliest_estimate`（**近似值**，粒度 = 窗口宽度）；全量模式给出**精确**最早 bar（approx=False） |
| limit_truncation | chain | 最小周期 + 超长窗口数 bar（按 variant 全量展开） | 实测 = documented_max → confirmed；不等 → suspect；未设 → observe |
| variant_consistency | chain | 同一 (interval, adjust) 查询在跨能力聚合的全部 variant 下重放并两两对比 | 差异 ≤ `tolerance` → confirmed（差异幅度如实记录）；超出 → suspect，记录 max_abs_diff 与 first_divergence；无重叠 / 预算中断 → suspect |
| raw | SDK 反射 | 声明外能力探测、错误指纹、快照字段矩阵（`min_fields` 验证 mapping 字段存在性） | 期望匹配 → confirmed / 不匹配 → failed；`assert: false` → observe |
| connection | SDK 反射 | 客户端「创建 → 首次取数 → 释放」生命周期计时与并发共存验证（rounds × concurrent） | 全部实例创建/首调/释放成功 → confirmed；任一异常 → failed；`assert: false` → observe。观测值：每实例 create/first_call/close 毫秒 |

判定枚举：`confirmed` / `failed` / `suspect` / `skipped` / `observe`。

## 结果解读

- `results/<provider>/<时间戳>.json`：完整证据（case + 观测 + 判定 + 环境布尔）；
- `results/report-<provider>-<时间戳>.md`：人读汇总，含「已确认能力 /
  声明未通过 / suspect+observe / history.start 建议回填值 / skipped」五张表；
- 结果是**时点证据**，报告带时间戳，不代表上游当前状态；
- 探针工具及其报告的时间口径统一北京时间（+08:00）：运行时间戳、结果
  文件名（`YYYYMMDDTHHMMSS+0800`）与观测中的 bar 时间戳均为北京时间；
- 样本数据每 case 最多 3 根 bar；凭据只记录「是否已配置」布尔。

**manifest 回填流程**：报告 → 人工确认 → 修订 manifest（`history.start`、
复权声明收敛、新增周期/后缀声明）与 README → 单独提交。探针永不自动改 manifest。

## 安全纪律

- Provider 凭据只经环境变量注入；探针文件与结果文件中禁止出现任何凭据字面量
  （错误指纹探针可使用明显的字面量假 Key，如 `probe-invalid-key-not-a-credential`）；
- pacing + max_calls 预算必须保守设置；探针消耗的是真实上游配额；
- 日志与结果不记录凭据值、完整行情响应。

## 新增 Provider 时

1. `cases/<name>.yaml` 按 Provider 要核实的问题编写（参考现有八份文件）；
2. 新增分析探针时先在 `tests/unit/probes/` 补离线测试（合成 fetch 闭包）；
3. 框架代码保持 Provider 无关：Provider 专属事实只允许出现在 `cases/*.yaml`。
