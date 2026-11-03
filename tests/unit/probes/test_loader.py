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
    assert {c.case_id for c in diffs} == {
        "adjust-diff-stock-none-vs-qfq",
        "adjust-diff-stock-none-vs-hfq",
    }
    assert all(c.expected_calls == 2 for c in diffs)
    assert all(c.compared_adjust is not None for c in diffs)


def test_history_depth_case_generated() -> None:
    probe = _probe(
        history_depth={"targets": ["stock"], "earliest": "2000-01-01", "max_calls": 8}
    )
    expanded = expand_probe(probe, _manifest())  # type: ignore[arg-type]
    cases = [c for c in expanded.chain_cases if c.kind == "history_depth"]
    assert len(cases) == 1
    assert cases[0].case_id == "history-depth-stock"
    assert cases[0].window_start.year == 2000
    assert cases[0].expected_calls == 8


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
    assert ok[0].case_id == "limit-stock-PT1M"
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
