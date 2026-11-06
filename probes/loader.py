"""探针文件加载、合法性校验与 chain 矩阵展开。

- 探针文件名必须与 ``BUILTIN_PROVIDERS`` 中的 Provider 同名；
- ``include_declared=true`` 时从 manifest 能力矩阵自动展开可用性 case，
  manifest 变更后探针自动跟随，声明组合不在文件里重复抄写；
- 分析 case（adjust_diff / history_depth / limit_truncation）按每个 asset_type
  的「声明顺序第一组维度」生成，控制调用预算；
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
    kind: str  # availability / adjust_diff / history_depth / limit_truncation
    asset_type: AssetType
    interval: Interval
    adjust: Adjust
    variant: DataVariant
    instrument: str
    window_start: dt.datetime
    window_end: dt.datetime
    expected_calls: int = 1
    min_bars: int | None = None
    required_fields: tuple[str, ...] = ()
    declared_history_start: dt.datetime | None = None
    era_step_years: int = 5
    probe_span_days: int = 90
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


def _to_beijing(day: dt.date) -> dt.datetime:
    """裸日期统一按北京时间零点解释（项目内部时间约定）。"""
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
                        f"adjust-diff-{base.asset_type.value}-{interval.code}-"
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
                            window_end=_to_beijing(config.window.end),
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
                    f"history-depth-{base.asset_type.value}-{interval.code}-{variant.value}"
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
                        probe_span_days=config.probe_span_days,
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
            case_id = f"limit-{base.asset_type.value}-{config.interval.code}-{variant.value}"
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
                    f"variant-consistency-{base.asset_type.value}-{interval.code}-{adjust.value}"
                ),
                kind="variant_consistency",
                interval=interval,
                adjust=adjust,
                variant=ordered[0],
                variants=ordered,
                compare_fields=tuple(config.fields),
                tolerance=config.tolerance,
                window_start=_to_beijing(config.window.start),
                window_end=_to_beijing(config.window.end),
                expected_calls=len(ordered),
                skip_reason=skip_reason,
            )
        )
    return cases


def _availability_case(
    *,
    probe: ProbeFile,
    capability: Capability,
    asset_type: AssetType,
    interval: Interval,
    adjust: Adjust,
    variant: DataVariant,
) -> ChainCase:
    target = probe.targets[asset_type]
    case_id = f"{asset_type.value}-{interval.code}-{adjust.value}-{variant.value}"
    skip_reason: str | None = None
    market = Instrument.parse(target.instrument).market
    if market not in capability.markets:
        skip_reason = (
            f"target instrument market {market!r} not in capability markets {capability.markets}"
        )
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
        window_end=_to_beijing(target.window.end),
        min_bars=target.min_bars,
        required_fields=tuple(target.required_fields),
        declared_history_start=next(
            (entry.history.start for entry in capability.intervals if entry.code == interval),
            None,
        ),
        skip_reason=skip_reason,
    )


def expand_probe(probe: ProbeFile, manifest: ProviderManifest) -> ExpandedProbe:
    """把探针文件与 manifest 能力矩阵合并展开。"""
    capabilities = manifest.to_domain_capabilities()
    chain_cases: list[ChainCase] = []

    if probe.chain and probe.chain.include_declared:
        for capability in capabilities:
            if capability.data_type != DataType.BARS:
                continue
            for entry in capability.intervals:
                for adjust in capability.adjustments:
                    for variant in capability.variants:
                        for asset_type in capability.asset_types:
                            if asset_type not in probe.targets:
                                # 无 target 的 asset_type 不生成 case，报告层面提示即可
                                continue
                            chain_cases.append(
                                _availability_case(
                                    probe=probe,
                                    capability=capability,
                                    asset_type=asset_type,
                                    interval=entry.code,
                                    adjust=adjust,
                                    variant=variant,
                                )
                            )

    if probe.chain:
        chain = probe.chain

        def _base_for(asset_type: AssetType) -> tuple[ChainCase, list[Capability]] | None:
            """为分析探针构造共享基底：窗口来自该 asset_type 的 target 声明。"""
            target = probe.targets.get(asset_type)
            caps = capabilities_of(capabilities, asset_type)
            if target is None or not caps:
                return None
            base = ChainCase(
                case_id="",
                kind="",
                asset_type=asset_type,
                interval=Interval.parse("P1D"),
                adjust=Adjust.NONE,
                # 占位值：下游分析探针构造器会按各能力声明的 variants 全量覆写；
                # 取首条能力的第一个声明变体，不假设任何具体数据源
                variant=caps[0].variants[0],
                instrument=target.instrument,
                window_start=_to_beijing(target.window.start),
                window_end=_to_beijing(target.window.end),
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
            for asset_type in config.targets:
                found = _base_for(asset_type)
                if found is None:
                    continue
                base, caps = found
                chain_cases.extend(builder(base, config, caps))

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
