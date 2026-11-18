"""探针报告渲染与落盘测试（离线）。"""

from datetime import datetime
from pathlib import Path

from probes.report import CaseResult, RunResult, render_markdown, write_results

from tapehub.domain.models import CN_TIMEZONE


def _run() -> RunResult:
    started = datetime(2026, 1, 1, tzinfo=CN_TIMEZONE)
    return RunResult(
        provider="fake-provider",
        started_at=started,
        finished_at=started,
        environment={"extras": {"some_sdk": True}, "env": {"TAPEHUB_X": True}},
        call_budget={"max": 120, "used": 42},
        notes="时点证据说明",
        cases=[
            CaseResult(
                case_id="stock-P1D-none-eastmoney",
                kind="availability",
                verdict="confirmed",
                reason="availability confirmed",
                dims={"asset_type": "stock"},
                observation={
                    "count": 55,
                    "first_timestamp": "2025-01-02T00:00:00+08:00",
                    "last_timestamp": "2025-03-31T00:00:00+08:00",
                    "null_rates": {"amount": 0.2},
                },
                duration_ms=12.3,
            ),
            CaseResult(
                case_id="stock-P1M-none-eastmoney",
                kind="availability",
                verdict="failed",
                reason="0 bars in declared capability window",
            ),
            CaseResult(
                case_id="adjust-diff-stock-none-vs-qfq",
                kind="adjust_diff",
                verdict="suspect",
                reason="adjust_no_op_suspect: adjusted series identical to unadjusted",
            ),
            CaseResult(
                case_id="history-depth-stock",
                kind="history_depth",
                verdict="observe",
                reason="calendar-boundary declaration (conservative upper bound; informational)",
                observation={
                    "earliest_estimate": "2003-05-01T00:00:00+08:00",
                    "first_bar": "2003-04-10T00:00:00+08:00",
                    "approx": True,
                    "snap": "month",
                    "calls_used": 12,
                    "complete": True,
                },
            ),
            CaseResult(
                case_id="stock-PT5M-none-eastmoney",
                kind="availability",
                verdict="skipped",
                reason="explicit skip: 测试原因",
            ),
        ],
    )


def test_render_markdown_sections() -> None:
    text = render_markdown(_run())
    assert "# tapehub Provider 探针报告 — fake-provider" in text
    assert "## 已确认能力（confirmed）" in text
    assert "## 声明未通过（failed）" in text
    assert "## suspect / observe" in text
    assert "## depth 实测首根与声明复核（判定只看 first_bar；人工确认后才改 manifest）" in text
    assert "## skipped" in text
    assert "stock-P1D-none-eastmoney" in text
    assert "adjust_no_op_suspect" in text
    assert "运行时间（北京时间 +08:00）：2026-01-01T00:00:00+08:00" in text
    assert "2003-05-01T00:00:00+08:00" in text
    assert "2003-04-10T00:00:00+08:00" in text
    assert "测试原因" in text


def test_write_results_creates_files(tmp_path: Path) -> None:
    json_path, md_path = write_results(_run(), tmp_path)
    assert json_path.exists() and md_path.exists()
    assert json_path.parent.name == "fake-provider"
    assert "20260101T000000+0800" in json_path.name
    content = json_path.read_text(encoding="utf-8")
    assert '"provider": "fake-provider"' in content
    assert "2026-01-01T00:00:00+08:00" in content


def test_render_quote_availability_section() -> None:
    """快照可用性单列小节；bars 表不混入 quote case。"""
    run = _run()
    run.cases.append(
        CaseResult(
            case_id="quote-stock-eastmoney",
            kind="quote_availability",
            verdict="confirmed",
            reason="quote availability confirmed",
            dims={"asset_type": "stock", "variant": "eastmoney"},
            observation={
                "last_price": 10.0,
                "bid_price1": None,
                "ask_price1": None,
                "volume": 1000.0,
                "amount": 10000.0,
            },
            duration_ms=8.1,
        )
    )
    content = render_markdown(run)
    assert "## 快照可用性（quotes，confirmed）" in content
    assert "| quote-stock-eastmoney | 10.0 | None/None | 1000.0 | 10000.0 | 8.1 |" in content
    # bars confirmed 表不含 quote case
    bars_section = content.split("## 已确认能力")[1].split("## 快照可用性")[0]
    assert "quote-stock-eastmoney" not in bars_section


def test_failed_contract_case_annotated_with_rotation_hint() -> None:
    """合约/期权目标 failed：报告行标注疑似到期/摘牌或逐合约缺口（2026-11-18
    决议；只标疑似不断言——LC2701-C-88000 前例证明缺口形态多样）。"""
    started = datetime(2026, 11, 17, tzinfo=CN_TIMEZONE)
    run = RunResult(
        provider="fake-provider",
        started_at=started,
        finished_at=started,
        environment={},
        call_budget={"max": 10, "used": 2},
        notes="",
        cases=[
            CaseResult(
                case_id="contract-P1D-none-eastmoney",
                kind="availability",
                verdict="failed",
                reason="adapter call failed: UPSTREAM_ERROR",
                dims={"asset_type": "contract", "instrument": "CN-SHFE-CU2706"},
            ),
            CaseResult(
                case_id="stock-P1D-none-eastmoney",
                kind="availability",
                verdict="failed",
                reason="adapter call failed: UPSTREAM_ERROR",
                dims={"asset_type": "stock"},
            ),
        ],
    )
    text = render_markdown(run)
    assert "疑似到期/摘牌或上游逐合约缺口" in text
    # stock 失败不标注
    contract_row = next(line for line in text.splitlines() if "contract-P1D-none" in line)
    stock_row = next(
        line
        for line in text.splitlines()
        if "stock-P1D-none" in line
    )
    assert "疑似到期" in contract_row
    assert "疑似到期" not in stock_row


def test_depth_review_table_renders_compat_and_highlights_over_claim() -> None:
    """depth 复核表（2026-11-18 裁决：判定只看 first_bar）：展示实测首根/
    现声明/相容性列，over_claim 行加粗高亮；estimate 列退役。"""
    started = datetime(2026, 11, 18, tzinfo=CN_TIMEZONE)

    def _depth(case_id: str, first_bar: str, declared: str | None, compat: str) -> CaseResult:
        return CaseResult(
            case_id=case_id,
            kind="history_depth",
            verdict="observe",
            reason="first-bar floor evidence; declaration cross-check",
            dims={"asset_type": "stock"},
            observation={
                "first_bar": first_bar,
                "declared_start": declared,
                "declaration_compat": compat,
                "complete": True,
            },
        )

    run = RunResult(
        provider="fake-provider",
        started_at=started,
        finished_at=started,
        environment={},
        call_budget={"max": 10, "used": 3},
        notes="",
        cases=[
            _depth("history-depth-a", "1991-01-02T00:00:00+08:00",
                   "1991-01-01T00:00:00+08:00", "compatible"),
            _depth("history-depth-b", "1991-01-02T00:00:00+08:00",
                   "1990-12-19T00:00:00+08:00", "over_claim"),
            _depth("history-depth-c", "1962-01-02T00:00:00+08:00",
                   "2000-01-01T00:00:00+08:00", "under_claim"),
            _depth("history-depth-d", None, None, "unknown"),
        ],
    )
    text = render_markdown(run)
    assert "depth 实测首根与声明复核" in text
    assert "**over-claim（需人工确认）**" in text
    assert "under-claim（保守成立）" in text
    assert "✓ 相容" in text
    # estimate 列退役：报告不再出现 earliest_estimate 列头
    assert "earliest_estimate" not in text
