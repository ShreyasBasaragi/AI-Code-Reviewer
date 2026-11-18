import hashlib
import json

from dataset_pipeline.cli import main


def test_bounded_sample_build_is_reproducible(tmp_path):
    root = tmp_path / "project"
    ms = root / "data" / "msg.jsonl"
    dev = root / "data" / "dev.json"
    ms.parent.mkdir(parents=True)
    ms.write_text("\n".join(json.dumps({"id": i, "oldf": "o", "patch": "p", "msg": f"m{i}", "lang": "py"}) for i in range(20)), encoding="utf8")
    dev.write_text(json.dumps([{"project": "p", "commit_id": str(i), "target": i % 2, "func": f"f{i}"} for i in range(20)]), encoding="utf8")
    cfg = root / "sources.yaml"
    config_text = (
        "project_root: .\n"
        "sample_max_bytes_per_file: 50000\n"
        "microsoft:\n"
        "  - {task: Comment_Generation, split: train, path: data/msg.jsonl}\n"
        "devign_candidate: {path: data/dev.json}\n"
    )
    cfg.write_text(config_text, encoding="utf8")
    out_a, out_b = root / "out-a", root / "out-b"
    args = ["build", "--config", str(cfg), "--sample-per-source", "5", "--seed", "s"]
    assert main(args + ["--output-dir", str(out_a)]) == 0
    assert main(args + ["--output-dir", str(out_b)]) == 0
    for name in ("canonical.jsonl", "quarantine.jsonl", "quality_report.json", "manifest.json"):
        assert hashlib.sha256((out_a / name).read_bytes()).hexdigest() == hashlib.sha256((out_b / name).read_bytes()).hexdigest()
    assert main(["verify", "--input-dir", str(out_a)]) == 0
