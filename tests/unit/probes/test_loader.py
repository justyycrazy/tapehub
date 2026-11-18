"""探针文件加载与 chain 矩阵展开测试（离线，使用 Fake Provider manifest）。"""

import datetime as dt
from pathlib import Path

import pytest
from probes.loader import expand_probe, load_probe_files
from probes.schema import ProbeFile

from tapehub.domain.models import Adjust, DataVariant
from tapehub.providers.manifest import load_manifest_file

FIXTURES = Path(__file__).parents[2] / "fixtures"


def _manifest() -> object:
    return load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")


def _probe(**chain: object) -> ProbeFile:
    data = {
        "probe": "fake-provider",
        "version": 2,
        "targets": {
            "stock": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            }
        },
        "chain": {"include_declared": True, **chain},
    }
    return ProbeFile.model_validate(data)


def test_availability_matrix_expansion() -> None:
    expanded = expand_probe(_probe(), _manifest())  # type: ignore[arg-type]
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    # fake manifest：4 个 asset_type × 2 周期 × 1 adjust × 1 variant；仅 stock 有 target
    assert sorted(c.case_id for c in availability) == [
        "stock-P1D-none-eastmoney",
        "stock-PT1M-none-eastmoney",
    ]
    case = availability[0]
    assert case.variant == DataVariant.EASTMONEY
    assert case.adjust == Adjust.NONE
    assert case.skip_reason is None
    assert case.window_start.year == 2025


def test_bare_date_window_normalized_start_midnight_end_end_of_day() -> None:
    """裸日期窗口语义（2026-11-17 口径修订）：start 取当日零点、end 取当日
    末 23:59:59.999999。零点 end 会把末个交易日全部盘中 bar（分钟族时间戳
    为盘中时刻）排除在闭区间外，制造「缺末日」计数错觉（当日历 end 恰为
    交易日时恰差一天，见 docs/probes.md「决策与变更记录」）。"""
    expanded = expand_probe(_probe(), _manifest())  # type: ignore[arg-type]
    case = next(c for c in expanded.chain_cases if c.kind == "availability")
    assert case.window_start == dt.datetime(2025, 1, 1, tzinfo=case.window_start.tzinfo)
    assert case.window_end == dt.datetime(
        2025, 3, 31, 23, 59, 59, 999999, tzinfo=case.window_end.tzinfo
    )
    assert case.window_start.tzinfo is not None and case.window_end.tzinfo is not None


def test_market_mismatch_placeholder_ids_market_qualified(tmp_path: Path) -> None:
    """市场失配占位 case id 带能力市场段限定且互不重复（2026-11-17 修复）。

    复现 akshare 型清单（同变体按市场拆 CN/HK/US 三块）+ 聚焦目录只保留
    交易所限定目标的形态：旧占位 id 裸 asset_type 前缀，HK/US 两块占位互相
    撞 id（duplicate chain case id: stock-P1D-none-eastmoney）致加载失败。
    """
    manifest_yaml = (Path(__file__).parents[2] / "fixtures" / "manifests").joinpath(
        "fake_provider_multi_market.yaml"
    )
    manifest = load_manifest_file(manifest_yaml)
    probe = ProbeFile.model_validate(
        {
            "probe": "fake-provider-multi-market",
            "version": 2,
            "targets": {
                "stock@CN-SZSE": {
                    "instrument": "CN-SZSE-000001",
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                }
            },
            "chain": {"include_declared": True},
        }
    )
    expanded = expand_probe(probe, manifest)
    by_id = {c.case_id: c for c in expanded.chain_cases}
    # 真实限定 case（唯一）+ 两块市场失配占位（id 各带能力市场段，互不重复）
    assert "stock@CN-SZSE-P1D-none-eastmoney" in by_id
    assert by_id["stock@CN-SZSE-P1D-none-eastmoney"].skip_reason is None
    hk = by_id["stock@HK-P1D-none-eastmoney"]
    us = by_id["stock@US-P1D-none-eastmoney"]
    assert hk.kind == "availability" and hk.skip_reason is not None
    assert us.kind == "availability" and us.skip_reason is not None
    assert "not in capability markets ['HK']" in hk.skip_reason
    assert "not in capability markets ['US']" in us.skip_reason


def test_market_mismatch_placeholder_no_collision_with_real_bare_case(
    tmp_path: Path,
) -> None:
    """裸基准真实 case 与失配占位并存时不撞 id（旧占位裸前缀会撞真实
    stock-P1D-none-eastmoney）。"""
    manifest_yaml = (Path(__file__).parents[2] / "fixtures" / "manifests").joinpath(
        "fake_provider_multi_market.yaml"
    )
    manifest = load_manifest_file(manifest_yaml)
    probe = ProbeFile.model_validate(
        {
            "probe": "fake-provider-multi-market",
            "version": 2,
            "targets": {
                "stock": {
                    "instrument": "CN-SSE-600000",
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                }
            },
            "chain": {"include_declared": True},
        }
    )
    expanded = expand_probe(probe, manifest)
    real = next(
        c
        for c in expanded.chain_cases
        if c.case_id == "stock-P1D-none-eastmoney" and c.skip_reason is None
    )
    assert real.instrument == "CN-SSE-600000"
    assert {
        c.case_id
        for c in expanded.chain_cases
        if c.skip_reason is not None and "not in capability markets" in c.skip_reason
    } == {"stock@HK-P1D-none-eastmoney", "stock@US-P1D-none-eastmoney"}


def test_market_mismatch_skipped() -> None:
    data = _probe().model_dump()
    data["targets"]["stock"]["instrument"] = "GLOBAL-OTC-EUR/USD"
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    assert all(
        c.skip_reason is not None and "market" in c.skip_reason
        for c in expanded.chain_cases
        if c.kind == "availability"
    )


def test_explicit_skip_applied() -> None:
    probe = _probe(
        skip=[{"case": "stock-P1D-none-eastmoney", "reason": "合约已到期摘牌"}]
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    skipped = [c for c in expanded.chain_cases if c.case_id == "stock-P1D-none-eastmoney"]
    assert len(skipped) == 1
    assert skipped[0].skip_reason == "explicit skip: 合约已到期摘牌"


def test_explicit_skip_applies_to_analysis_cases() -> None:
    """显式 skip 对分析探针生效：history_depth 命中即跳过，未命中不受影响。"""
    probe = _probe(
        skip=[
            {"case": "history-depth-stock-P1D-eastmoney", "reason": "已收敛，人工确认回填"}
        ],
        history_depth={"targets": ["stock"], "earliest": "2000-01-01", "max_calls": 8},
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    by_id = {c.case_id: c for c in expanded.chain_cases}
    assert (
        by_id["history-depth-stock-P1D-eastmoney"].skip_reason
        == "explicit skip: 已收敛，人工确认回填"
    )
    # 未命中显式条目的分析 case 保持原状（无 skip，不静默漏探）
    assert by_id["history-depth-stock-PT1M-eastmoney"].skip_reason is None


def test_explicit_skip_never_overrides_builder_reason() -> None:
    """显式 skip 不覆盖构造器自带的跳过原因（如复权未声明）。"""
    probe = _probe(
        skip=[
            {
                "case": "adjust-diff-stock-P1D-none-vs-qfq-eastmoney",
                "reason": "无关原因，不应生效",
            }
        ],
        adjust_diff={
            "targets": ["stock"],
            "window": {"start": "2024-01-01", "end": "2024-12-31"},
            "pairs": [["none", "qfq"]],
        },
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    case = next(
        c
        for c in expanded.chain_cases
        if c.case_id == "adjust-diff-stock-P1D-none-vs-qfq-eastmoney"
    )
    # fake manifest 只声明 none：构造器原因保留，不被显式条目覆盖
    assert case.skip_reason is not None
    assert "not declared" in case.skip_reason
    assert "无关原因" not in case.skip_reason


def test_adjust_diff_cases_generated() -> None:
    probe = _probe(
        adjust_diff={
            "targets": ["stock"],
            "window": {"start": "2024-01-01", "end": "2024-12-31"},
            "pairs": [["none", "qfq"], ["none", "hfq"]],
        }
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    diffs = [c for c in expanded.chain_cases if c.kind == "adjust_diff"]
    # 全量展开：2 interval × 1 variant × 2 pairs = 4 条；fake 只声明 none，全部 skip
    assert {c.case_id for c in diffs} == {
        "adjust-diff-stock-P1D-none-vs-qfq-eastmoney",
        "adjust-diff-stock-P1D-none-vs-hfq-eastmoney",
        "adjust-diff-stock-PT1M-none-vs-qfq-eastmoney",
        "adjust-diff-stock-PT1M-none-vs-hfq-eastmoney",
    }
    assert all(c.expected_calls == 2 for c in diffs)
    assert all(c.compared_adjust is not None for c in diffs)
    assert all(c.skip_reason is not None and "not declared" in c.skip_reason for c in diffs)


def test_history_depth_case_generated() -> None:
    probe = _probe(
        history_depth={"targets": ["stock"], "earliest": "2000-01-01", "max_calls": 8}
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    # 全量展开：声明了 P1D 与 PT1M 两个周期就生成两条
    assert {c.case_id for c in cases} == {
        "history-depth-stock-P1D-eastmoney",
        "history-depth-stock-PT1M-eastmoney",
    }
    assert all(c.window_start.year == 2000 for c in cases)
    assert all(c.expected_calls == 8 for c in cases)
    # era/网格粒度参数透传到 case（runner 据此调用分析函数）
    assert all(c.era_step_years == 5 and c.snap == "month" for c in cases)


def test_history_depth_snap_threaded() -> None:
    """snap / era_step_years 从配置透传到展开 case。"""
    probe = _probe(
        history_depth={
            "targets": ["stock"],
            "earliest": "2000-01-01",
            "max_calls": 8,
            "era_step_years": 1,
            "snap": "quarter",
        }
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    assert cases
    assert all(c.era_step_years == 1 for c in cases)
    assert all(c.snap == "quarter" for c in cases)

    # intervals 显式收敛：只保留声明中存在的指定周期
    filtered = expand_probe(
        _probe(
            history_depth={
                "targets": ["stock"],
                "earliest": "2000-01-01",
                "max_calls": 8,
                "intervals": ["PT1M"],
            }
        ),
        _manifest(),  # type: ignore[arg-type]
    )
    cases = [c for c in filtered.chain_cases if c.kind == "history_depth"]
    assert {c.case_id for c in cases} == {"history-depth-stock-PT1M-eastmoney"}


def test_limit_truncation_declared_and_undeclared() -> None:
    declared = expand_probe(
        _probe(
            limit_truncation={
                "targets": ["stock"],
                "interval": "PT1M",
                "max_range": "P1Y",
                "documented_max": 5000,
            }
        ),
        _manifest(),  # type: ignore[arg-type]
    )
    ok = [c for c in declared.chain_cases if c.kind == "limit_truncation"]
    assert len(ok) == 1
    assert ok[0].case_id == "limit-stock-PT1M-eastmoney"
    # window_end 2025-03-31 回溯 P1Y → 2024-03-31
    assert (ok[0].window_start.year, ok[0].window_start.month, ok[0].window_start.day) == (
        2024,
        3,
        31,
    )
    assert ok[0].documented_max == 5000

    undeclared = expand_probe(
        _probe(limit_truncation={"targets": ["stock"], "interval": "PT5M", "max_range": "P1Y"}),
        _manifest(),  # type: ignore[arg-type]
    )
    bad = [c for c in undeclared.chain_cases if c.kind == "limit_truncation"]
    assert len(bad) == 1
    assert bad[0].skip_reason is not None and "not declared" in bad[0].skip_reason


def test_variant_consistency_single_variant_skipped() -> None:
    probe = _probe(
        variant_consistency={
            "targets": ["stock"],
            "window": {"start": "2025-01-01", "end": "2025-03-31"},
        }
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.kind == "variant_consistency"]
    # fake manifest 仅声明 eastmoney：按声明周期全量生成 case 但 skip（无从对比）
    assert {c.case_id for c in cases} == {
        "variant-consistency-stock-P1D-none",
        "variant-consistency-stock-PT1M-none",
    }
    assert cases[0].skip_reason == "single declared variant, nothing to compare"


def test_full_matrix_expansion_multi_variant() -> None:
    """多 variant manifest：分析探针必须在全部 (interval × variant × pair) 上展开。"""
    manifest = load_manifest_file(FIXTURES / "manifests" / "multi_variant.yaml")
    probe = ProbeFile.model_validate(
        {
            "probe": "multi-var",
            "version": 2,
            "targets": {
                "stock": {
                    "instrument": "CN-SSE-600000",
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                }
            },
            "chain": {
                "include_declared": True,
                "adjust_diff": {
                    "targets": ["stock"],
                    "window": {"start": "2024-01-01", "end": "2024-12-31"},
                    "pairs": [["none", "qfq"]],
                },
                "history_depth": {"targets": ["stock"], "earliest": "2000-01-01", "max_calls": 4},
                "variant_consistency": {
                    "targets": ["stock"],
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                },
            },
        }
    )
    expanded = expand_probe(probe, manifest)
    by_kind: dict[str, list] = {}
    for case in expanded.chain_cases:
        by_kind.setdefault(case.kind, []).append(case)

    # adjust_diff：1 interval × 2 variant × 1 pair = 2 条，且均无 skip（none/qfq 均已声明）
    assert {c.case_id for c in by_kind["adjust_diff"]} == {
        "adjust-diff-stock-P1D-none-vs-qfq-eastmoney",
        "adjust-diff-stock-P1D-none-vs-qfq-sina",
    }
    assert all(c.skip_reason is None for c in by_kind["adjust_diff"])
    # history_depth：1 interval × 2 variant = 2 条
    assert {c.case_id for c in by_kind["history_depth"]} == {
        "history-depth-stock-P1D-eastmoney",
        "history-depth-stock-P1D-sina",
    }
    # variant_consistency：1 interval × 2 adjusts = 2 条（每条覆盖全部 2 个 variant）
    consistency = by_kind["variant_consistency"]
    assert {c.case_id for c in consistency} == {
        "variant-consistency-stock-P1D-none",
        "variant-consistency-stock-P1D-qfq",
    }
    assert all(c.variants == ("eastmoney", "sina") for c in consistency)
    assert all(c.expected_calls == 2 for c in consistency)
    assert all(c.skip_reason is None for c in consistency)


def test_load_probe_files_validates_names(tmp_path: Path) -> None:
    # 场景一：probe 名不是内置 Provider → 加载即拒绝
    (tmp_path / "unknown.yaml").write_text(
        "probe: fake-provider\nversion: 2\ntargets: {stock: {instrument: CN-SSE-600000, "
        "window: {start: 2025-01-01, end: 2025-03-31}}}\n"
        "chain: {include_declared: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_probe_files(tmp_path)

    # 场景二：文件名与内置 Provider 不对应（即使内容合法）→ 拒绝
    (tmp_path / "unknown.yaml").unlink()
    (tmp_path / "extra-file.yaml").write_text(
        "probe: akshare\nversion: 2\ntargets: {stock: {instrument: CN-SSE-600000, "
        "window: {start: 2025-01-01, end: 2025-03-31}}}\n"
        "chain: {include_declared: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_probe_files(tmp_path)

    # 场景三：文件名合法但其余 Provider 缺失 → FileNotFoundError
    (tmp_path / "extra-file.yaml").unlink()
    (tmp_path / "akshare.yaml").write_text(
        "probe: akshare\nversion: 2\ntargets: {stock: {instrument: CN-SSE-600000, "
        "window: {start: 2025-01-01, end: 2025-03-31}}}\n"
        "chain: {include_declared: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):  # 其余 Provider 的探针文件缺失
        load_probe_files(tmp_path)


def test_raw_id_collision_rejected() -> None:
    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "stock-P1D-none-eastmoney",
                    "module": "some_sdk",
                    "call": [{"module_func": "fetch"}],
                    "expect": {"status": "ok", "min_rows": 1},
                }
            ]
        }
    )
    with pytest.raises(ValueError):
        expand_probe(probe, _manifest())  # type: ignore[arg-type]


# ── 市场限定目标（asset_type@MARKET）展开 ─────────────────────────────


def _multi_market_manifest() -> object:
    return load_manifest_file(FIXTURES / "manifests" / "multi_market_provider.yaml")


def _multi_market_probe(**chain: object) -> ProbeFile:
    data = {
        "probe": "multi-market-provider",
        "version": 2,
        "targets": {
            "stock@CN": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            },
            "stock@HK": {
                "instrument": "HK-HKEX-00700",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            },
            "stock@US": {
                "instrument": "US-NASDAQ-AAPL",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            },
        },
        "chain": {"include_declared": True, **chain},
    }
    return ProbeFile.model_validate(data)


def test_market_qualified_availability_expansion() -> None:
    """三市场能力 × 三市场限定目标：每市场独立 case，id 带限定前缀。"""
    expanded = expand_probe(
        _multi_market_probe(), _multi_market_manifest()  # type: ignore[arg-type]
    )
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    # 3 市场 × 2 周期 × 2 adjust = 12 条，全部可执行（无 skip）
    assert len(availability) == 12
    assert all(c.skip_reason is None for c in availability)
    by_id = {c.case_id: c for c in availability}
    assert "stock@CN-P1D-none-tickflow" in by_id
    assert "stock@HK-PT1M-qfq-tickflow" in by_id
    assert "stock@US-P1D-none-tickflow" in by_id
    assert by_id["stock@HK-P1D-none-tickflow"].instrument == "HK-HKEX-00700"
    assert by_id["stock@US-PT1M-qfq-tickflow"].instrument == "US-NASDAQ-AAPL"


def test_market_qualified_partial_targets() -> None:
    """只声明部分市场的限定目标：未声明的市场不生成 case（不静默冒用其它市场标的）。"""
    data = _multi_market_probe().model_dump()
    del data["targets"]["stock@US"]
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    assert len(availability) == 8  # CN/HK 各 2 周期 × 2 adjust
    assert all("@US" not in c.case_id for c in availability)


def test_plain_target_fallback_only_matching_market() -> None:
    """不限定键回退：仅当标的市场与请求市场一致时命中（单市场文件行为不变）。"""
    data = _multi_market_probe().model_dump()
    data["targets"] = {
        "stock": {
            "instrument": "CN-SSE-600000",
            "window": {"start": "2025-01-01", "end": "2025-03-31"},
        }
    }
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    # CN 市场命中不限定键（id 不带 @CN），HK/US 无目标不生成
    assert sorted(c.case_id for c in availability) == [
        "stock-P1D-none-tickflow",
        "stock-P1D-qfq-tickflow",
        "stock-PT1M-none-tickflow",
        "stock-PT1M-qfq-tickflow",
    ]


def test_market_qualified_adjust_diff_ids() -> None:
    """分析探针（adjust_diff）接受限定目标：id 与标的按市场区分。"""
    probe = _multi_market_probe(
        adjust_diff={
            "targets": ["stock@HK", "stock@US"],
            "window": {"start": "2024-01-01", "end": "2024-12-31"},
            "pairs": [["none", "qfq"]],
        }
    )
    expanded = expand_probe(
        probe, _multi_market_manifest()  # type: ignore[arg-type]
    )
    diffs = [c for c in expanded.chain_cases if c.kind == "adjust_diff"]
    ids = sorted(c.case_id for c in diffs)
    assert ids == [
        "adjust-diff-stock@HK-P1D-none-vs-qfq-tickflow",
        "adjust-diff-stock@HK-PT1M-none-vs-qfq-tickflow",
        "adjust-diff-stock@US-P1D-none-vs-qfq-tickflow",
        "adjust-diff-stock@US-PT1M-none-vs-qfq-tickflow",
    ]
    instruments = {c.case_id: c.instrument for c in diffs}
    assert instruments["adjust-diff-stock@HK-P1D-none-vs-qfq-tickflow"] == "HK-HKEX-00700"
    assert instruments["adjust-diff-stock@US-P1D-none-vs-qfq-tickflow"] == "US-NASDAQ-AAPL"


def test_market_qualified_history_depth_ids() -> None:
    probe = _multi_market_probe(
        history_depth={
            "targets": ["stock@CN"],
            "earliest": "2000-01-01",
            "intervals": ["P1D"],
        }
    )
    expanded = expand_probe(
        probe, _multi_market_manifest()  # type: ignore[arg-type]
    )
    depth = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    assert [c.case_id for c in depth] == ["history-depth-stock@CN-P1D-tickflow"]
    assert depth[0].instrument == "CN-SSE-600000"


def test_invalid_target_key_rejected() -> None:
    """目标键格式非法（小写市场 / 未知 asset_type）在加载期拒绝。"""
    data = _multi_market_probe().model_dump()
    data["targets"]["stock@hk"] = data["targets"]["stock@HK"]
    del data["targets"]["stock@HK"]
    with pytest.raises(Exception):
        ProbeFile.model_validate(data)

    data2 = _multi_market_probe().model_dump()
    data2["chain"]["adjust_diff"] = {
        "targets": ["bogus@HK"],
        "window": {"start": "2024-01-01", "end": "2024-12-31"},
    }
    with pytest.raises(Exception):
        ProbeFile.model_validate(data2)


# ── 交易所限定目标（asset_type@MARKET-EXCHANGE）展开 ─────────────────────────


def _multi_market_probe_with_exchange_targets() -> ProbeFile:
    """市场限定基准目标 + 交易所限定追加目标：加法式，不抑制基准目标。"""
    data = _multi_market_probe().model_dump()
    data["targets"]["stock@CN-SZSE"] = {
        "instrument": "CN-SZSE-000001",
        "window": {"start": "2025-01-01", "end": "2025-03-31"},
    }
    data["targets"]["stock@CN-BSE"] = {
        "instrument": "CN-BSE-920000",
        "window": {"start": "2025-01-01", "end": "2025-03-31"},
    }
    return ProbeFile.model_validate(data)


def test_exchange_qualified_availability_expansion() -> None:
    """交易所限定目标是市场基准目标之外的追加：CN 基准 + SZSE/BSE 各自成 case。"""
    expanded = expand_probe(
        _multi_market_probe_with_exchange_targets(),
        _multi_market_manifest(),  # type: ignore[arg-type]
    )
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    # 基准 CN/HK/US 各 4 + SZSE/BSE 各 4 = 20 条，全部可执行
    assert len(availability) == 20
    assert all(c.skip_reason is None for c in availability)
    by_id = {c.case_id: c for c in availability}
    assert "stock@CN-P1D-none-tickflow" in by_id  # 基准目标不受抑制
    assert "stock@CN-SZSE-P1D-none-tickflow" in by_id
    assert "stock@CN-BSE-PT1M-qfq-tickflow" in by_id
    assert by_id["stock@CN-SZSE-P1D-none-tickflow"].instrument == "CN-SZSE-000001"
    assert by_id["stock@CN-BSE-PT1M-qfq-tickflow"].instrument == "CN-BSE-920000"


def test_exchange_qualified_only_target_for_market() -> None:
    """市场只声明交易所限定键（无基准键）：该市场按交易所目标展开，不冒用其它标的。"""
    data = _multi_market_probe_with_exchange_targets().model_dump()
    del data["targets"]["stock@CN"]  # CN 仅剩 SZSE/BSE 两个交易所目标
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]
    availability = [c for c in expanded.chain_cases if c.kind == "availability"]
    ids = {c.case_id for c in availability}
    assert "stock@CN-SZSE-P1D-none-tickflow" in ids
    assert "stock@CN-BSE-PT1M-qfq-tickflow" in ids
    assert "stock@CN-P1D-none-tickflow" not in ids
    assert len(ids) == 16  # CN-SZSE/CN-BSE/HK/US 各 4


def test_orphan_market_qualified_key_rejected() -> None:
    """限定键的 market 段不在能力目录覆盖范围：展开期硬报错（不静默忽略）。"""
    data = _multi_market_probe().model_dump()
    data["targets"]["stock@EU"] = {
        "instrument": "EU-XEUR-CA",  # fixture 能力只声明 CN/HK/US
        "window": {"start": "2025-01-01", "end": "2025-03-31"},
    }
    probe = ProbeFile.model_validate(data)
    with pytest.raises(ValueError, match="markets not declared"):
        expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]


def test_analysis_targets_with_exchange_qualifier() -> None:
    """分析探针 targets 支持交易所限定引用：id 前缀带交易所段。"""
    data = _multi_market_probe_with_exchange_targets().model_dump()
    data["chain"]["adjust_diff"] = {
        "targets": ["stock@CN", "stock@CN-SZSE"],
        "window": {"start": "2024-06-01", "end": "2024-08-31"},
    }
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]
    ids = {c.case_id for c in expanded.chain_cases if c.kind == "adjust_diff"}
    assert "adjust-diff-stock@CN-P1D-none-vs-qfq-tickflow" in ids
    assert "adjust-diff-stock@CN-SZSE-P1D-none-vs-qfq-tickflow" in ids
    assert "adjust-diff-stock@CN-SZSE-PT1M-none-vs-qfq-tickflow" in ids


def test_unresolved_analysis_target_rejected() -> None:
    """分析探针引用无法解析的限定目标：展开期硬报错（取代静默忽略）。"""
    data = _multi_market_probe().model_dump()
    data["chain"]["adjust_diff"] = {
        "targets": ["stock@CN-SZX"],  # 不存在的键（CN 市场内无 SZX 交易所目标）
        "window": {"start": "2024-06-01", "end": "2024-08-31"},
    }
    probe = ProbeFile.model_validate(data)
    with pytest.raises(ValueError, match="do not resolve"):
        expand_probe(probe, _multi_market_manifest())  # type: ignore[arg-type]


def test_quote_availability_expansion() -> None:
    """quotes 声明随 include_declared 自动展开：无周期/复权维度，quote- 前缀 id。"""
    expanded = expand_probe(_probe(), _manifest())  # type: ignore[arg-type]
    quotes = [c for c in expanded.chain_cases if c.kind == "quote_availability"]
    # fake manifest quotes 声明：CN stock/index；仅 stock 有 target，index 不生成
    assert [c.case_id for c in quotes] == ["quote-stock-eastmoney"]
    case = quotes[0]
    assert case.interval is None
    assert case.adjust is None
    assert case.instrument == "CN-SSE-600000"
    assert case.window_start is None and case.window_end is None
    assert case.expected_calls == 1
    assert case.skip_reason is None


def test_quote_availability_mismatch_skipped() -> None:
    """target 市场不在 quotes 能力市场内：quote case 显式跳过（进报告）。"""
    data = _probe().model_dump()
    data["targets"]["stock"]["instrument"] = "GLOBAL-OTC-EUR/USD"
    probe = ProbeFile.model_validate(data)
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    quotes = [c for c in expanded.chain_cases if c.kind == "quote_availability"]
    assert [c.case_id for c in quotes] == ["quote-stock@CN-eastmoney"]
    assert quotes[0].skip_reason is not None and "market" in quotes[0].skip_reason


def test_quote_availability_explicit_skip() -> None:
    probe = _probe(skip=[{"case": "quote-stock-eastmoney", "reason": "快照停机维护"}])
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    quotes = [c for c in expanded.chain_cases if c.kind == "quote_availability"]
    assert quotes[0].skip_reason == "explicit skip: 快照停机维护"


def test_history_depth_case_carries_declared_start() -> None:
    """depth case 携带 manifest 声明值（声明复核输入，2026-11-18 裁决）；
    fake manifest 声明 start=null → 字段为 None（null 也是合法输入）。"""
    expanded = expand_probe(
        _probe(history_depth={"targets": ["stock"], "earliest": "2020-01-01", "max_calls": 4}),
        _manifest(),  # type: ignore[arg-type]
    )
    cases = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    assert cases
    assert all(c.declared_history_start is None for c in cases)


def test_target_expired_skips_all_cases() -> None:
    """合约目标 expires 已过：该目标全部 chain case 加载期 skip（带原因进
    报告，防静默漏探；2026-11-18 决议——探针非巡检，不硬错误拒绝运行）。"""
    probe = _probe()
    data = probe.model_dump(mode="json")
    data["targets"]["stock"]["expires"] = "2026-01-01"
    expanded = expand_probe(ProbeFile.model_validate(data), _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.target_ref == "stock"]
    assert cases
    for case in cases:
        assert case.skip_reason is not None
        assert "target expired" in case.skip_reason
        assert "2026-01-01" in case.skip_reason
        assert "rotate the target" in case.skip_reason


def test_target_expiry_future_no_skip() -> None:
    """expires 未到：不跳过、不留 skip 原因。"""
    probe = _probe()
    data = probe.model_dump(mode="json")
    data["targets"]["stock"]["expires"] = "2099-12-31"
    expanded = expand_probe(ProbeFile.model_validate(data), _manifest())  # type: ignore[arg-type]
    for case in expanded.chain_cases:
        assert case.skip_reason is None


def test_target_expiry_near_warns(caplog: pytest.LogCaptureFixture) -> None:
    """临期（≤30 天）→ 运行前告警，提示按轮换流程策划换码。"""
    import logging

    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(days=10)).date().isoformat()
    probe = _probe()
    data = probe.model_dump(mode="json")
    data["targets"]["stock"]["expires"] = soon
    with caplog.at_level(logging.WARNING, logger="probes.loader"):
        expand_probe(ProbeFile.model_validate(data), _manifest())  # type: ignore[arg-type]
    assert any("near expiry" in r.message for r in caplog.records)


def test_target_expiry_never_overrides_other_skip_reasons() -> None:
    """显式 skip 优先于到期 skip（到期后处理只补未带原因的 case）。"""
    probe = _probe(skip=[{"case": "stock-P1D-none-eastmoney", "reason": "旧码退役"}])
    data = probe.model_dump(mode="json")
    data["targets"]["stock"]["expires"] = "2026-01-01"
    expanded = expand_probe(ProbeFile.model_validate(data), _manifest())  # type: ignore[arg-type]
    case = next(c for c in expanded.chain_cases if c.case_id == "stock-P1D-none-eastmoney")
    assert case.skip_reason == "explicit skip: 旧码退役"
