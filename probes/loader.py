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
    HistoryDepthSpec,
    LimitTruncationSpec,
    ProbeFile,
    RawCaseSpec,
)
from tapehub.domain.capabilities import Capability
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, DataType, DataVariant, Instrument
from tapehub.providers.manifest import ProviderManifest, load_builtin_manifest
from tapehub.providers.registry import BUILTIN_PROVIDERS

UTC = dt.timezone.utc


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
    # adjust_diff 专属：被对比的复权方式
    compared_adjust: Adjust | None = None
    # adjust_diff 专属：参与对比的价格字段
    compare_fields: tuple[str, ...] = ()
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
class ExpandedProbe:
    """一个 Provider 的完整展开结果。"""

    probe: ProbeFile
    manifest: ProviderManifest
    chain_cases: list[ChainCase] = field(default_factory=list)
    raw_cases: list[RawPlan] = field(default_factory=list)


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


def _to_utc(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=UTC)


def capability_of(capabilities: list[Capability], asset_type: AssetType) -> Capability | None:
    """返回声明了该 asset_type 的第一条能力（声明顺序优先）。"""
    for cap in capabilities:
        if asset_type in cap.asset_types:
            return cap
    return None


def primary_dims(capability: Capability) -> tuple[Interval, Adjust, DataVariant]:
    """分析探针的「声明顺序第一组维度」，保证确定性并控制预算。"""
    adjust = Adjust.NONE if Adjust.NONE in capability.adjustments else capability.adjustments[0]
    return capability.intervals[0].code, adjust, capability.variants[0]


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
        window_start=_to_utc(target.window.start),
        window_end=_to_utc(target.window.end),
        min_bars=target.min_bars,
        required_fields=tuple(target.required_fields),
        declared_history_start=next(
            (entry.history.start for entry in capability.intervals if entry.code == interval),
            None,
        ),
        skip_reason=skip_reason,
    )


def _adjust_diff_case(
    base: ChainCase,
    config: AdjustDiffSpec,
    pair: tuple[Adjust, Adjust],
    capability: Capability,
) -> ChainCase:
    interval, _, variant = primary_dims(capability)
    baseline, compared = pair
    case_id = f"adjust-diff-{base.asset_type.value}-{baseline.value}-vs-{compared.value}"
    skip_reason: str | None = None
    if Adjust.NONE not in capability.adjustments:
        skip_reason = "baseline adjust 'none' is not declared"
    elif compared not in capability.adjustments:
        skip_reason = f"compared adjust {compared.value!r} is not declared"
    return dataclasses.replace(
        base,
        case_id=case_id,
        kind="adjust_diff",
        interval=interval,
        adjust=baseline,
        variant=variant,
        compared_adjust=compared,
        compare_fields=tuple(config.fields),
        window_start=_to_utc(config.window.start),
        window_end=_to_utc(config.window.end),
        expected_calls=2,
        skip_reason=skip_reason,
    )


def _history_depth_case(
    base: ChainCase, spec: "HistoryDepthSpec", capability: Capability
) -> ChainCase:
    interval, adjust, variant = primary_dims(capability)
    return dataclasses.replace(
        base,
        case_id=f"history-depth-{base.asset_type.value}",
        kind="history_depth",
        interval=interval,
        adjust=adjust,
        variant=variant,
        window_start=_to_utc(spec.earliest),
        expected_calls=spec.max_calls,
    )


def _limit_truncation_case(
    base: ChainCase, spec: "LimitTruncationSpec", capability: Capability
) -> ChainCase:
    _, adjust, variant = primary_dims(capability)
    case_id = f"limit-{base.asset_type.value}-{spec.interval.code}"
    skip_reason: str | None = None
    if not any(entry.code == spec.interval for entry in capability.intervals):
        skip_reason = f"interval {spec.interval.code} is not declared for {base.asset_type.value}"
    return dataclasses.replace(
        base,
        case_id=case_id,
        kind="limit_truncation",
        interval=spec.interval,
        adjust=adjust,
        variant=variant,
        documented_max=spec.documented_max,
        window_start=_window_back(base.window_end, spec.max_range),
        expected_calls=1,
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
        for asset_type in chain.adjust_diff.targets if chain.adjust_diff else []:
            target = probe.targets.get(asset_type)
            capability = capability_of(capabilities, asset_type)
            if target is None or capability is None or chain.adjust_diff is None:
                continue
            base = ChainCase(
                case_id="",
                kind="",
                asset_type=asset_type,
                interval=Interval.parse("P1D"),
                adjust=Adjust.NONE,
                variant=DataVariant.EASTMONEY,
                instrument=target.instrument,
                window_start=_to_utc(target.window.start),
                window_end=_to_utc(target.window.end),
            )
            for pair in chain.adjust_diff.pairs:
                chain_cases.append(
                    _adjust_diff_case(base, chain.adjust_diff, pair, capability)
                )
        for spec_name in ("history_depth", "limit_truncation"):
            config = getattr(chain, spec_name)
            if config is None:
                continue
            builder = (
                _history_depth_case
                if spec_name == "history_depth"
                else _limit_truncation_case
            )
            for asset_type in config.targets:
                target = probe.targets.get(asset_type)
                capability = capability_of(capabilities, asset_type)
                if target is None or capability is None:
                    continue
                base = ChainCase(
                    case_id="",
                    kind="",
                    asset_type=asset_type,
                    interval=Interval.parse("P1D"),
                    adjust=Adjust.NONE,
                    variant=DataVariant.EASTMONEY,
                    instrument=target.instrument,
                    window_start=_to_utc(target.window.start),
                    window_end=_to_utc(target.window.end),
                )
                chain_cases.append(builder(base, config, capability))

    raw_cases = [
        RawPlan(spec=case, expected_calls=len(case.call) + (1 if case.client else 0))
        for case in probe.raw
    ]

    expanded = ExpandedProbe(
        probe=probe, manifest=manifest, chain_cases=chain_cases, raw_cases=raw_cases
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
