"""探针文件加载、合法性校验与 chain 矩阵展开。

- 探针文件名必须与 ``BUILTIN_PROVIDERS`` 中的 Provider 同名；
- ``include_declared=true`` 时从 manifest 能力矩阵自动展开可用性 case，
  manifest 变更后探针自动跟随，声明组合不在文件里重复抄写；
- 分析 case（adjust_diff / history_depth / limit_truncation / variant_consistency）
  在声明矩阵上全量展开（``intervals`` 可显式收敛 interval 维度控制预算，
  variant 维度永不收敛）；
- 展开只做静态计算，不访问网络，也不导入任何 SDK。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from probes.analysis import window_back as _window_back
from probes.schema import (
    AdjustDiffSpec,
    ConnectionCaseSpec,
    HistoryDepthSpec,
    LimitTruncationSpec,
    ProbeFile,
    RawCaseSpec,
    TargetRef,
    TargetSpec,
    VariantConsistencySpec,
)
from tapehub.domain.capabilities import Capability
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    CN_TIMEZONE,
    Adjust,
    AssetType,
    DataType,
    DataVariant,
    Instrument,
)
from tapehub.providers.manifest import ProviderManifest, load_builtin_manifest
from tapehub.providers.registry import BUILTIN_PROVIDERS


@dataclass(frozen=True, slots=True)
class ChainCase:
    """展开后的一条 chain 探针（含分析探针），执行前不做任何 IO。"""

    case_id: str
    # availability / quote_availability / adjust_diff / history_depth /
    # limit_truncation / variant_consistency
    kind: str
    asset_type: AssetType
    # quotes 无周期/复权维度（manifest 显式声明 null）：quote_availability
    # case 的 interval/adjust 为 None；bars 各探针恒非 None
    interval: Interval | None
    adjust: Adjust | None
    variant: DataVariant
    instrument: str
    expected_calls: int = 1
    # 窗口是 bars 语义；quote_availability 无窗口，保持 None
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    min_bars: int | None = None
    required_fields: tuple[str, ...] = ()
    declared_history_start: dt.datetime | None = None
    era_step_years: int = 5
    # history_depth 声明值对齐的日历边界粒度（取值经 schema Literal 校验）
    snap: str = "month"
    # history_depth 全量模式：单次全窗口调用直接读最早 bar（上游无日期过滤）
    full_history: bool = False
    # variant_consistency 判定容差（绝对值）；差异幅度始终如实记录
    tolerance: float = 0.0
    # adjust_diff 专属：被对比的复权方式
    compared_adjust: Adjust | None = None
    # adjust_diff 专属：参与对比的价格字段
    compare_fields: tuple[str, ...] = ()
    # variant_consistency 专属：参与对比的全部声明 variant
    variants: tuple[DataVariant, ...] = ()
    # limit_truncation 专属：文档声称的上限
    documented_max: int | None = None
    # 市场限定目标的前缀（如 "stock@HK"）；空串表示不限定（兼容单市场目标）
    target_ref: str = ""
    # 加载期即确定跳过的原因（无 target / 市场不匹配 / 显式 skip / 维度未声明）
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RawPlan:
    """展开后的一条 raw 探针。"""

    spec: RawCaseSpec
    expected_calls: int


@dataclass(frozen=True, slots=True)
class ConnectionPlan:
    """展开后的一条连接探针。

    调用预算 = 每实例（创建 + 首次取数 + 释放）× rounds × concurrent。
    """

    spec: ConnectionCaseSpec
    expected_calls: int


@dataclass(frozen=True, slots=True)
class ExpandedProbe:
    """一个 Provider 的完整展开结果。"""

    probe: ProbeFile
    manifest: ProviderManifest
    chain_cases: list[ChainCase] = field(default_factory=list)
    raw_cases: list[RawPlan] = field(default_factory=list)
    connection_cases: list[ConnectionPlan] = field(default_factory=list)


def load_probe_file(path: Path) -> ProbeFile:
    """加载并校验单个探针文件；文件名必须与内置 Provider 同名。"""
    probe = ProbeFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if probe.probe not in BUILTIN_PROVIDERS:
        raise ValueError(
            f"probe file {path.name!r} declares unknown provider {probe.probe!r}; "
            f"expected one of {BUILTIN_PROVIDERS}"
        )
    return probe


def load_probe_files(cases_dir: Path) -> list[ProbeFile]:
    """加载目录下全部探针文件，按 BUILTIN_PROVIDERS 注册顺序返回。"""
    by_name = {
        path.stem: load_probe_file(path)
        for path in sorted(cases_dir.glob("*.yaml"))
        if path.is_file()
    }
    extra = sorted(set(by_name) - set(BUILTIN_PROVIDERS))
    missing = [name for name in BUILTIN_PROVIDERS if name not in by_name]
    if extra:
        raise ValueError(f"unexpected probe files (no such builtin provider): {extra}")
    if missing:
        raise FileNotFoundError(f"missing probe case files for builtin providers: {missing}")
    return [by_name[name] for name in BUILTIN_PROVIDERS]


def _target_for(probe: ProbeFile, ref: TargetRef) -> tuple[str, TargetSpec] | None:
    """按目标引用解析基准标的：限定键优先，不限定键兜底（市场一致才命中）。

    - ``stock@HK`` 优先命中键 ``stock@HK``；
    - ``stock@CN-SSE``（交易所限定）仅精确命中同名键，不回退——裸键的
      标的属于其他交易所，回退会产生失配证据；
    - 未命中市场限定键时回退键 ``stock``，但仅当其标的的市场与请求市场一致
      （既有单市场文件用不限定键，行为不变；多市场 Provider 需为每个
      要探的市场提供限定键，否则该市场不生成 case）；
    - 不限定引用（market=None）直接命中键 ``stock``，不校验市场
      （分析探针按标的自身市场展开，与既有语义一致）。
    """
    if ref.market is not None:
        qualified = probe.targets.get(ref.key)
        if qualified is not None:
            return (ref.key, qualified)
        if ref.exchange is not None:
            return None
    plain = probe.targets.get(ref.asset_type.value)
    if plain is None:
        return None
    if ref.market is not None and Instrument.parse(plain.instrument).market != ref.market:
        return None
    return (ref.asset_type.value, plain)


def _exchange_targets_for_market(
    probe: ProbeFile, asset_type: AssetType, market: str
) -> list[tuple[str, TargetSpec]]:
    """收集市场内交易所限定目标键（``stock@CN-SSE``），保持声明顺序。

    交易所限定键是市场基准标的（市场限定键/裸键）之外的**追加**目标，
    用于逐所细分探测（availability/adjust_diff 等按同一能力矩阵为每个
    交易所目标独立展开）；键与 instrument 的一致性已在加载期校验
    （schema ``ProbeFile._target_keys``）。"""
    prefix = f"{asset_type.value}@{market}-"
    return [(key, spec) for key, spec in probe.targets.items() if key.startswith(prefix)]


def _validate_target_coverage(probe: ProbeFile, capabilities: list[Capability]) -> None:
    """孤儿限定目标硬失败：限定段必须能被能力目录覆盖（加载期诚实报错）。

    - targets 键：带 market 限定的键，其 market 段不在该 manifest 任何
      capability 的 markets 中 → ValueError（取代展开期的静默忽略；裸键
      豁免，兼容单市场既有语义）；
    - 分析探针 targets 引用：``_target_for`` 解析失败（含键缺失与市场
      不匹配）→ ValueError（取代 ``_base_for`` 的静默 continue）。
    """
    covered = {market for cap in capabilities for market in cap.markets}
    orphan_keys = sorted(
        key
        for key in probe.targets
        if (ref := TargetRef.model_validate(key)).market is not None
        and ref.market not in covered
    )
    if orphan_keys:
        raise ValueError(
            f"target keys reference markets not declared by provider manifest "
            f"{probe.probe!r}: {orphan_keys} (covered markets: {sorted(covered)})"
        )
    chain = probe.chain
    if chain is None:
        return
    unresolved: list[str] = []
    for config in (
        chain.adjust_diff,
        chain.history_depth,
        chain.limit_truncation,
        chain.variant_consistency,
    ):
        if config is None:
            continue
        for ref in config.targets:
            if _target_for(probe, ref) is None:
                unresolved.append(ref.key)
    if unresolved:
        raise ValueError(
            f"analysis probe targets do not resolve to any declared target in "
            f"{probe.probe!r}: {sorted(unresolved)}"
        )


def _id_prefix(base: ChainCase) -> str:
    """case id 的目标前缀：市场限定目标带 @MARKET 段，否则为 asset_type。"""
    return base.target_ref or base.asset_type.value


def _to_beijing(day: dt.date, *, end_of_day: bool = False) -> dt.datetime:
    """裸日期统一按北京时间解释（项目内部时间约定，2026-11-17 口径修订）：
    起点侧取当日零点；终点侧取当日末 23:59:59.999999——裸日期 end 按零点时，
    分钟族 bar（盘中时刻）晚于 end、被适配器闭区间复核排除末个交易日全部
    盘中 bar（2026-11-17 baostock「缺末日」误判的根因，口径详见
    docs/probes.md「决策与变更记录」）。日/周/月线 bar 零点戳在两种口径下
    行为一致。"""
    if end_of_day:
        return dt.datetime(
            day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=CN_TIMEZONE
        )
    return dt.datetime(day.year, day.month, day.day, tzinfo=CN_TIMEZONE)


def capabilities_of(capabilities: list[Capability], asset_type: AssetType) -> list[Capability]:
    """返回声明了该 asset_type 的全部能力（保持声明顺序）。

    同一 asset_type 可能由多条能力覆盖（如 akshare stock 的 eastmoney/sina/tencent
    各为一条能力），分析探针的全量展开必须遍历全部能力。
    """
    return [cap for cap in capabilities if asset_type in cap.asset_types]


def _declared_intervals(capability: Capability, only: list) -> list:
    """该能力声明的全部 interval；``only`` 非空时按其收敛（显式配置，非隐式绑定）。"""
    codes = [entry.code for entry in capability.intervals]
    if not only:
        return codes
    return [code for code in codes if code in only]


def _fallback_adjust(capability: Capability) -> Adjust:
    """无 none 声明时取第一个声明的复权方式（如期货仅有 none 不受影响）。"""
    return Adjust.NONE if Adjust.NONE in capability.adjustments else capability.adjustments[0]


def _adjust_diff_cases(
    base: ChainCase, config: AdjustDiffSpec, caps: list[Capability]
) -> list[ChainCase]:
    """按 (interval × variant × pair) 全量展开复权生效性探针。"""
    cases: list[ChainCase] = []
    seen: set[str] = set()
    for capability in caps:
        for interval in _declared_intervals(capability, config.intervals):
            for variant in capability.variants:
                for baseline, compared in config.pairs:
                    case_id = (
                        f"adjust-diff-{_id_prefix(base)}-{interval.code}-"
                        f"{baseline.value}-vs-{compared.value}-{variant.value}"
                    )
                    if case_id in seen:
                        continue
                    seen.add(case_id)
                    skip_reason: str | None = None
                    if baseline not in capability.adjustments:
                        skip_reason = f"baseline adjust {baseline.value!r} is not declared"
                    elif compared not in capability.adjustments:
                        skip_reason = f"compared adjust {compared.value!r} is not declared"
                    cases.append(
                        dataclasses.replace(
                            base,
                            case_id=case_id,
                            kind="adjust_diff",
                            interval=interval,
                            adjust=baseline,
                            variant=variant,
                            compared_adjust=compared,
                            compare_fields=tuple(config.fields),
                            window_start=_to_beijing(config.window.start),
                            window_end=_to_beijing(config.window.end, end_of_day=True),
                            expected_calls=2,
                            skip_reason=skip_reason,
                        )
                    )
    return cases


def _history_depth_cases(
    base: ChainCase, config: HistoryDepthSpec, caps: list[Capability]
) -> list[ChainCase]:
    """按 (interval × variant) 全量展开历史深度探针。"""
    cases: list[ChainCase] = []
    seen: set[str] = set()
    for capability in caps:
        for interval in _declared_intervals(capability, config.intervals):
            for variant in capability.variants:
                case_id = (
                    f"history-depth-{_id_prefix(base)}-{interval.code}-{variant.value}"
                )
                if case_id in seen:
                    continue
                seen.add(case_id)
                full_history = base.asset_type in config.full_history_targets
                cases.append(
                    dataclasses.replace(
                        base,
                        case_id=case_id,
                        kind="history_depth",
                        interval=interval,
                        adjust=_fallback_adjust(capability),
                        variant=variant,
                        window_start=_to_beijing(config.earliest),
                        era_step_years=config.era_step_years,
                        snap=config.snap,
                        full_history=full_history,
                        # 全量模式单次调用即得精确值，预算声明按实际计
                        expected_calls=1 if full_history else config.max_calls,
                    )
                )
    return cases


def _limit_truncation_cases(
    base: ChainCase, config: LimitTruncationSpec, caps: list[Capability]
) -> list[ChainCase]:
    """按 variant 全量展开单次上限探针。"""
    cases: list[ChainCase] = []
    seen: set[str] = set()
    for capability in caps:
        for variant in capability.variants:
            case_id = f"limit-{_id_prefix(base)}-{config.interval.code}-{variant.value}"
            if case_id in seen:
                continue
            seen.add(case_id)
            skip_reason: str | None = None
            if not any(entry.code == config.interval for entry in capability.intervals):
                skip_reason = (
                    f"interval {config.interval.code} is not declared for {base.asset_type.value}"
                )
            cases.append(
                dataclasses.replace(
                    base,
                    case_id=case_id,
                    kind="limit_truncation",
                    interval=config.interval,
                    adjust=_fallback_adjust(capability),
                    variant=variant,
                    documented_max=config.documented_max,
                    window_start=_window_back(base.window_end, config.max_range),
                    expected_calls=1,
                    skip_reason=skip_reason,
                )
            )
    return cases


def _variant_consistency_cases(
    base: ChainCase, config: VariantConsistencySpec, caps: list[Capability]
) -> list[ChainCase]:
    """按 (interval × adjust) 展开：同一查询在全部声明 variant 下应得到一致序列。

    variant 必须**跨能力聚合**：同一 (asset_type, interval, adjust) 常由多条能力
    覆盖（如 akshare stock 的三个 variant 各为一条能力），一致性对比的正是
    跨能力的同一参数结果。
    """
    grouped: dict[tuple[Interval, Adjust], set[DataVariant]] = {}
    for capability in caps:
        for interval in _declared_intervals(capability, config.intervals):
            for adjust in capability.adjustments:
                grouped.setdefault((interval, adjust), set()).update(capability.variants)

    cases: list[ChainCase] = []
    for (interval, adjust), variant_set in sorted(
        grouped.items(), key=lambda item: (item[0][0].code, item[0][1].value)
    ):
        ordered = tuple(v for v in DataVariant if v in variant_set)
        skip_reason: str | None = None
        if len(ordered) < 2:
            skip_reason = "single declared variant, nothing to compare"
        cases.append(
            dataclasses.replace(
                base,
                case_id=(
                    f"variant-consistency-{_id_prefix(base)}-{interval.code}-{adjust.value}"
                ),
                kind="variant_consistency",
                interval=interval,
                adjust=adjust,
                variant=ordered[0],
                variants=ordered,
                compare_fields=tuple(config.fields),
                tolerance=config.tolerance,
                window_start=_to_beijing(config.window.start),
                window_end=_to_beijing(config.window.end, end_of_day=True),
                expected_calls=len(ordered),
                skip_reason=skip_reason,
            )
        )
    return cases


def _has_target_for(probe: ProbeFile, asset_type: AssetType) -> bool:
    """是否为该 asset_type 声明了任何 target（不限定键或市场限定键）。"""
    prefix = f"{asset_type.value}@"
    return any(
        key == asset_type.value or key.startswith(prefix) for key in probe.targets
    )


def _first_target(probe: ProbeFile, asset_type: AssetType) -> TargetSpec | None:
    """该 asset_type 的首个声明 target（不限定键优先），供错误信息引用。"""
    spec = probe.targets.get(asset_type.value)
    if spec is not None:
        return spec
    prefix = f"{asset_type.value}@"
    for key in sorted(probe.targets):
        if key.startswith(prefix):
            return probe.targets[key]
    return None


def _market_mismatch_case(
    *,
    probe: ProbeFile,
    capability: Capability,
    asset_type: AssetType,
    interval: Interval,
    adjust: Adjust,
    variant: DataVariant,
) -> ChainCase:
    """声明了 target 但其市场不在能力市场内：生成显式跳过的 case（进报告）。"""
    target = _first_target(probe, asset_type)
    market = (
        Instrument.parse(target.instrument).market if target is not None else "unknown"
    )
    case_id = f"{asset_type.value}-{interval.code}-{adjust.value}-{variant.value}"
    for skip in probe.chain.skip if probe.chain else []:
        if skip.case == case_id:
            reason = f"explicit skip: {skip.reason}"
            break
    else:
        reason = (
            f"target instrument market {market!r} not in capability markets "
            f"{capability.markets}"
        )
    return ChainCase(
        case_id=case_id,
        kind="availability",
        asset_type=asset_type,
        interval=interval,
        adjust=adjust,
        variant=variant,
        instrument=target.instrument if target is not None else "",
        window_start=_to_beijing(
            target.window.start
            if target is not None
            else dt.date(2025, 1, 1)
        ),
        window_end=_to_beijing(
            target.window.end if target is not None else dt.date(2025, 12, 31),
            end_of_day=True,
        ),
        skip_reason=reason,
    )


def _availability_case(
    *,
    probe: ProbeFile,
    capability: Capability,
    market: str,
    asset_type: AssetType,
    target_key: str,
    target: TargetSpec,
    interval: Interval,
    adjust: Adjust,
    variant: DataVariant,
) -> ChainCase:
    # 市场一致性由调用方（_target_for）保证；market 参数保留在签名里，
    # 供阅读与后续按市场维度的判定使用。
    del market
    ref_prefix = target_key
    case_id = f"{ref_prefix}-{interval.code}-{adjust.value}-{variant.value}"
    skip_reason: str | None = None
    for skip in probe.chain.skip if probe.chain else []:
        if skip.case == case_id:
            skip_reason = f"explicit skip: {skip.reason}"
    return ChainCase(
        case_id=case_id,
        kind="availability",
        asset_type=asset_type,
        interval=interval,
        adjust=adjust,
        variant=variant,
        instrument=target.instrument,
        window_start=_to_beijing(target.window.start),
        window_end=_to_beijing(target.window.end, end_of_day=True),
        min_bars=target.min_bars,
        required_fields=tuple(target.required_fields),
        declared_history_start=next(
            (entry.history.start for entry in capability.intervals if entry.code == interval),
            None,
        ),
        target_ref=ref_prefix,
        skip_reason=skip_reason,
    )


def _targets_by_market(
    probe: ProbeFile, asset_type: AssetType, markets: list[str]
) -> list[tuple[str, str, TargetSpec]]:
    """按能力市场收集基准与追加目标：(market, target_key, target)。

    - 每个市场先解析市场基准标的（市场限定键或裸键回退）；
    - 交易所限定键是市场基准标的之外的**追加**目标：逐所细分探测
      （键前缀 <asset_type>@<MARKET>-），bars 与 quotes 展开共用。
    """
    targets: list[tuple[str, str, TargetSpec]] = []
    for market in markets:
        found = _target_for(probe, TargetRef(asset_type=asset_type, market=market))
        if found is not None:
            targets.append((market, *found))
        for exch_key, exch_target in _exchange_targets_for_market(probe, asset_type, market):
            targets.append((market, exch_key, exch_target))
    return targets


def _quote_availability_case(
    *,
    probe: ProbeFile,
    target_key: str,
    target: TargetSpec,
    asset_type: AssetType,
    variant: DataVariant,
) -> ChainCase:
    """快照可用性 case：经 adapter.fetch_quote 全链路复验已声明的 quotes 能力。

    快照无周期/复权维度（manifest 显式声明 null），case id 前缀 ``quote-``
    与 bars availability 区分；判定与观测语义见 runner._run_quote_availability。
    """
    case_id = f"quote-{target_key}-{variant.value}"
    skip_reason: str | None = None
    for skip in probe.chain.skip if probe.chain else []:
        if skip.case == case_id:
            skip_reason = f"explicit skip: {skip.reason}"
    return ChainCase(
        case_id=case_id,
        kind="quote_availability",
        asset_type=asset_type,
        interval=None,
        adjust=None,
        variant=variant,
        instrument=target.instrument,
        target_ref=target_key,
        skip_reason=skip_reason,
    )


def _quote_mismatch_case(
    *,
    probe: ProbeFile,
    capability: Capability,
    asset_type: AssetType,
    variant: DataVariant,
) -> ChainCase:
    """声明了 target 但其市场不在 quotes 能力市场内：显式跳过的 case（进报告）。"""
    target = _first_target(probe, asset_type)
    market = (
        Instrument.parse(target.instrument).market if target is not None else "unknown"
    )
    case_id = f"quote-{asset_type.value}-{variant.value}"
    for skip in probe.chain.skip if probe.chain else []:
        if skip.case == case_id:
            reason = f"explicit skip: {skip.reason}"
            break
    else:
        reason = (
            f"target instrument market {market!r} not in capability markets "
            f"{capability.markets}"
        )
    return ChainCase(
        case_id=case_id,
        kind="quote_availability",
        asset_type=asset_type,
        interval=None,
        adjust=None,
        variant=variant,
        instrument=target.instrument if target is not None else "",
        skip_reason=reason,
    )


def _apply_explicit_skips(probe: ProbeFile, chain_cases: list[ChainCase]) -> None:
    """把显式 skip 条目统一应用到未带跳过原因的 chain case。

    availability / 市场不匹配 case 在各自构造器里已优先匹配显式 skip；
    分析探针（adjust_diff / history_depth / limit_truncation /
    variant_consistency）展开时不感知 skip 配置，这里按 case id 补齐，
    使「显式跳过必须给原因（进报告，防止静默漏探）」对全部分析探针生效。
    已带原因（如复权未声明、单 variant 无从对比）的 case 不覆盖。
    """
    if not probe.chain:
        return
    explicit = {skip.case: skip.reason for skip in probe.chain.skip}
    chain_cases[:] = [
        dataclasses.replace(
            case, skip_reason=f"explicit skip: {explicit[case.case_id]}"
        )
        if case.skip_reason is None and case.case_id in explicit
        else case
        for case in chain_cases
    ]


def expand_probe(probe: ProbeFile, manifest: ProviderManifest) -> ExpandedProbe:
    """把探针文件与 manifest 能力矩阵合并展开。"""
    capabilities = manifest.to_domain_capabilities()
    _validate_target_coverage(probe, capabilities)
    chain_cases: list[ChainCase] = []

    if probe.chain and probe.chain.include_declared:
        for capability in capabilities:
            if capability.data_type != DataType.BARS:
                continue
            for entry in capability.intervals:
                for adjust in capability.adjustments:
                    for variant in capability.variants:
                        for asset_type in capability.asset_types:
                            targets_by_market = _targets_by_market(
                                probe, asset_type, capability.markets
                            )
                            if not targets_by_market:
                                # 声明了 target 但市场全不匹配：保留跳过标记进报告
                                # （不静默漏探）；未声明任何 target 的 asset_type 不生成
                                if not _has_target_for(probe, asset_type):
                                    continue
                                chain_cases.append(
                                    _market_mismatch_case(
                                        probe=probe,
                                        capability=capability,
                                        asset_type=asset_type,
                                        interval=entry.code,
                                        adjust=adjust,
                                        variant=variant,
                                    )
                                )
                                continue
                            for market, target_key, target in targets_by_market:
                                chain_cases.append(
                                    _availability_case(
                                        probe=probe,
                                        capability=capability,
                                        market=market,
                                        asset_type=asset_type,
                                        target_key=target_key,
                                        target=target,
                                        interval=entry.code,
                                        adjust=adjust,
                                        variant=variant,
                                    )
                                )

        # 快照可用性：与 bars 同一「声明内不抄写」语义——manifest 声明
        # data_type=quotes 的组合自动展开为 quote_availability case（无周期/
        # 复权维度，每组合 1 次调用），market/交易所限定目标与显式 skip
        # 语义同 bars availability。
        for capability in capabilities:
            if capability.data_type != DataType.QUOTE:
                continue
            for variant in capability.variants:
                for asset_type in capability.asset_types:
                    targets_by_market = _targets_by_market(
                        probe, asset_type, capability.markets
                    )
                    if not targets_by_market:
                        if not _has_target_for(probe, asset_type):
                            continue
                        chain_cases.append(
                            _quote_mismatch_case(
                                probe=probe,
                                capability=capability,
                                asset_type=asset_type,
                                variant=variant,
                            )
                        )
                        continue
                    for target_key, target in (
                        (key, spec) for _, key, spec in targets_by_market
                    ):
                        chain_cases.append(
                            _quote_availability_case(
                                probe=probe,
                                target_key=target_key,
                                target=target,
                                asset_type=asset_type,
                                variant=variant,
                            )
                        )

    if probe.chain:
        chain = probe.chain

        def _base_for(ref: TargetRef) -> tuple[ChainCase, list[Capability]] | None:
            """为分析探针构造共享基底：窗口来自该目标引用的 target 声明。"""
            found = _target_for(probe, ref)
            caps = capabilities_of(capabilities, ref.asset_type)
            if ref.market is not None:
                caps = [cap for cap in caps if ref.market in cap.markets]
            if found is None or not caps:
                return None
            target_key, target = found
            base = ChainCase(
                case_id="",
                kind="",
                asset_type=ref.asset_type,
                interval=Interval.parse("P1D"),
                adjust=Adjust.NONE,
                # 占位值：下游分析探针构造器会按各能力声明的 variants 全量覆写；
                # 取首条能力的第一个声明变体，不假设任何具体数据源
                variant=caps[0].variants[0],
                instrument=target.instrument,
                window_start=_to_beijing(target.window.start),
                window_end=_to_beijing(target.window.end, end_of_day=True),
                target_ref=target_key,
            )
            return base, caps

        for config, builder in (
            (chain.adjust_diff, _adjust_diff_cases),
            (chain.history_depth, _history_depth_cases),
            (chain.limit_truncation, _limit_truncation_cases),
            (chain.variant_consistency, _variant_consistency_cases),
        ):
            if config is None:
                continue
            for ref in config.targets:
                found = _base_for(ref)
                if found is None:
                    continue
                base, caps = found
                chain_cases.extend(builder(base, config, caps))

    _apply_explicit_skips(probe, chain_cases)

    raw_cases = [
        RawPlan(spec=case, expected_calls=len(case.call) + (1 if case.client else 0))
        for case in probe.raw
    ]

    connection_cases = [
        ConnectionPlan(
            spec=case,
            expected_calls=case.rounds * case.concurrent * 3,  # 创建+首调+释放
        )
        for case in probe.connection
    ]

    expanded = ExpandedProbe(
        probe=probe,
        manifest=manifest,
        chain_cases=chain_cases,
        raw_cases=raw_cases,
        connection_cases=connection_cases,
    )
    _ensure_unique_case_ids(expanded)
    return expanded


def expand_builtin(probe: ProbeFile) -> ExpandedProbe:
    """按 Provider 名加载内置 manifest 并展开。"""
    return expand_probe(probe, load_builtin_manifest(probe.probe))


def _ensure_unique_case_ids(expanded: ExpandedProbe) -> None:
    seen: set[str] = set()
    for case in expanded.chain_cases:
        if case.case_id in seen:
            raise ValueError(f"duplicate chain case id: {case.case_id}")
        seen.add(case.case_id)
    for plan in expanded.raw_cases:
        if plan.spec.id in seen:
            raise ValueError(f"raw case id collides with chain case id: {plan.spec.id}")
        seen.add(plan.spec.id)
    for plan in expanded.connection_cases:
        if plan.spec.id in seen:
            raise ValueError(f"connection case id collides with existing id: {plan.spec.id}")
        seen.add(plan.spec.id)
