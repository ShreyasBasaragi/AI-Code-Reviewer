from dataset_pipeline.cli import main


def test_inspect_reports_missing_source(tmp_path, capsys):
    cfg = tmp_path / "sources.yaml"
    cfg.write_text("project_root: .\nmicrosoft:\n  - {task: Comment_Generation, split: train, path: absent.jsonl}\n", encoding="utf8")
    result = main(["inspect", "--config", str(cfg)])
    assert result == 2
    assert '"exists": false' in capsys.readouterr().out
