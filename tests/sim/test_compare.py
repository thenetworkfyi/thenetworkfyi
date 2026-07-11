from __future__ import annotations

import json

from thenetwork.sim.cli import main
from thenetwork.sim.scoring.compare import (
    compare_runs,
    load_run_metrics,
    render_compare,
)


def _write_events(run_dir, events):
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_load_run_metrics_counts_population_deltas(tmp_path):
    run = tmp_path / "run"
    _write_events(
        run,
        [
            {"event": "sim.process_email_started"},
            {"event": "introduction.sent", "total_tokens": 20, "cost_usd": 0.01},
            {
                "event": "sim.judge.transcript",
                "score": 7,
                "token_usage": 5,
                "cost_usd": 0.02,
            },
            {"event": "sim.judge.transcript", "score": 9},
        ],
    )

    metrics = load_run_metrics(run)

    assert metrics.introductions == 1
    assert metrics.judge_score == 8
    assert metrics.token_usage == 25
    assert metrics.cost_usd == 0.03
    assert metrics.process_email_calls == 1


def test_compare_runs_renders_metric_table(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    _write_events(
        before, [{"event": "sim.judge.transcript", "score": 6, "total_tokens": 10}]
    )
    _write_events(
        after,
        [
            {"event": "introduction.sent", "total_tokens": 15, "cost_usd": 0.02},
            {"event": "sim.judge.transcript", "score": 8},
        ],
    )

    rendered = render_compare(compare_runs(before, after))

    assert "| introductions | 0 | 1 | +1 |" in rendered
    assert "| judge_score | 6.00 | 8.00 | +2.00 |" in rendered
    assert "| token_usage | 10 | 15 | +5 |" in rendered
    assert "| cost_usd | 0.0000 | 0.0200 | +0.0200 |" in rendered


def test_compare_cli_prints_table(tmp_path, capsys):
    before = tmp_path / "before"
    after = tmp_path / "after"
    _write_events(before, [])
    _write_events(after, [{"event": "introduction.sent"}])

    main(["compare", str(before), str(after)])

    assert "| introductions | 0 | 1 | +1 |" in capsys.readouterr().out


def test_load_run_metrics_does_not_count_outbound_email_as_introduction(tmp_path):
    run = tmp_path / "run"
    _write_events(
        run,
        [
            {"event": "agent.dispatch_email.completed"},
            {"event": "agent.tool.completed", "tool_name": "dispatch_email"},
        ],
    )

    assert load_run_metrics(run).introductions == 0
