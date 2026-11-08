"""探针文件加载与 chain 矩阵展开测试（离线，使用 Fake Provider manifest）。"""

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
        "version": 1,
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
    # era/二分粒度参数透传到 case（runner 据此调用分析函数）
    assert all(c.era_step_years == 5 and c.probe_span_days == 90 for c in cases)


def test_history_depth_probe_span_days_threaded() -> None:
    """probe_span_days / era_step_years 从配置透传到展开 case。"""
    probe = _probe(
        history_depth={
            "targets": ["stock"],
            "earliest": "2000-01-01",
            "max_calls": 8,
            "era_step_years": 1,
            "probe_span_days": 5,
        }
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    assert cases
    assert all(c.era_step_years == 1 for c in cases)
    assert all(c.probe_span_days == 5 for c in cases)

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
            "version": 1,
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
        "probe: fake-provider\nversion: 1\ntargets: {stock: {instrument: CN-SSE-600000, "
        "window: {start: 2025-01-01, end: 2025-03-31}}}\n"
        "chain: {include_declared: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_probe_files(tmp_path)

    # 场景二：文件名与内置 Provider 不对应（即使内容合法）→ 拒绝
    (tmp_path / "unknown.yaml").unlink()
    (tmp_path / "extra-file.yaml").write_text(
        "probe: akshare\nversion: 1\ntargets: {stock: {instrument: CN-SSE-600000, "
        "window: {start: 2025-01-01, end: 2025-03-31}}}\n"
        "chain: {include_declared: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_probe_files(tmp_path)

    # 场景三：文件名合法但其余 Provider 缺失 → FileNotFoundError
    (tmp_path / "extra-file.yaml").unlink()
    (tmp_path / "akshare.yaml").write_text(
        "probe: akshare\nversion: 1\ntargets: {stock: {instrument: CN-SSE-600000, "
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
        "version": 1,
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
