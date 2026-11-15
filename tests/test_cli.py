from __future__ import annotations

import pytest

from dayloom.cli import main


def test_parser_wires_every_subcommand():
    from dayloom.cli import build_parser

    parser = build_parser()
    for cmd in ("start", "pause", "resume", "status", "report", "analyze", "capture-once",
                "cost", "doctor", "probe-llm", "mock-vlm", "config"):
        args = parser.parse_args([cmd])
        assert callable(args.func)


def test_status_works_on_a_fresh_install(home, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "未运行" in out


def test_status_json_is_valid(home, capsys):
    import json

    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running_pid"] is None
    assert payload["db_exists"] is False


def test_report_without_a_database_says_so(home, capsys):
    assert main(["report"]) == 1
    assert "还没有数据库" in capsys.readouterr().out


def test_cost_without_a_database_says_so(home, capsys):
    assert main(["cost"]) == 1
    assert "还没有数据库" in capsys.readouterr().out


def test_config_example_is_printable(home, capsys):
    assert main(["config", "--example"]) == 0
    out = capsys.readouterr().out
    assert "[capture]" in out
    assert "blacklist_titles" in out


def test_unknown_command_exits(home):
    with pytest.raises(SystemExit):
        main(["definitely-not-a-command"])


def test_no_arguments_explains_the_two_executables(home, capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "dayloom-gui.exe" in out
    assert "dayloom start" in out
    assert "usage:" not in out  # not an argparse dump


def test_analyze_redo_resets_only_that_day(home, repo, cfg, capsys, monkeypatch):
    """--redo rewinds done batches of the day, leaving other days untouched."""
    from conftest import add_frame, at

    for day, hour in (("2026-05-10", 10), ("2026-05-11", 10)):
        for i in range(2):
            add_frame(repo, home, at(hour, 1 + i, day=day), app="code", dhash_value=i)
        repo.ensure_batches(
            day=day, batch_seconds=900, grace_seconds=0,
            now=at(hour, 20, day=day), boundary_hour=4,
        )
        repo.db.execute("UPDATE batches SET status='done' WHERE day = ?", (day,))

    # No model configured: analyze stops early, but the rewind must already
    # have happened — that is the part --redo is responsible for.
    monkeypatch.setattr(cfg.__class__, "home", property(lambda self: home))
    assert main(["analyze", "--date", "2026-05-10", "--redo"]) != 0
    pending = repo.db.scalar(
        "SELECT COUNT(*) FROM batches WHERE day='2026-05-10' AND status='pending'"
    )
    done = repo.db.scalar(
        "SELECT COUNT(*) FROM batches WHERE day='2026-05-11' AND status='done'"
    )
    assert pending >= 1
    assert done >= 1
