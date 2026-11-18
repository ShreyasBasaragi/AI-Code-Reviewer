import json
import zipfile
from pathlib import Path

from rag.adapters.base import CanonicalRecord
from rag.ingest import chunk_record as v2_chunk_record
from rag.v3.chunking import assert_lossless_spans, chunk_source_text
from rag.v3.language import classify_language
from rag.v3.prepare import PreparationConfig, prepare_local_sources
from rag.v3.schema import V3EmbeddingChunk, V3ParentRecord, make_parent_record, parent_upsert_action
from rag.v3.sources import load_all_local_sources
from rag.prepare_v3 import main as prepare_v3_main, run_preparation


class CharTokenizer:
    """Deterministic fixture tokenizer: one token per Unicode code point."""

    def encode(self, text, add_special_tokens=False):
        return list(text)


def _write_fixture_sources(root: Path):
    microsoft = root / "microsoft_codereviewer" / "Comment_Generation"
    microsoft.mkdir(parents=True, exist_ok=True)
    records = [
        {"id": "cpp-1", "msg": "Keep this template", "patch": "template <class T>\nT ZeroVector(3) {\n    return array_1d<T, 3>();\n}\n// \"quoted\" λ\n", "y": 1},
        {"id": "java-1", "msg": "Use the interface", "patch": "package demo;\nimport java.util.List;\n@Override\npublic void run() {}\n"},
    ]
    (microsoft / "msg-train-selected.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in [*records, records[0]]) + "\nnot-json\n" +
        json.dumps({"id": "bad", "msg": "missing patch"}) + "\n", encoding="utf-8"
    )

    cwe_dir = root / "cwe"
    cwe_dir.mkdir(exist_ok=True)
    xml = '''<?xml version="1.0"?><Weaknesses>
      <Weakness ID="78" Name="Improper Neutralization">
        <Description>Application input is used in a command without safe neutralization.</Description>
        <Extended_Description>Longer CWE description for the fixture record.</Extended_Description>
        <Potential_Mitigations><Mitigation>Use safe APIs and validate arguments.</Mitigation></Potential_Mitigations>
      </Weakness>
    </Weaknesses>'''
    with zipfile.ZipFile(cwe_dir / "cwec_latest.xml.zip", "w") as archive:
        archive.writestr("cwec_v4.15.xml", xml)

    owasp = root / "owasp_cheatsheets" / "CheatSheetSeries-master" / "cheatsheets"
    owasp.mkdir(parents=True, exist_ok=True)
    (owasp / "SQL_Injection_Prevention_Cheat_Sheet.md").write_text(
        "# SQL Injection Prevention\n\nUse parameterized queries for all user controlled data. "
        "Avoid building SQL statements by concatenating strings from untrusted input.\n",
        encoding="utf-8",
    )

    plugins = root / "bandit_rules" / "bandit-main" / "bandit" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "injection_shell.py").write_text('''
@test.test_id("B602")
def subprocess_popen_with_shell_equals_true(context):
    """This rule detects subprocess calls that enable a shell and can interpret unsafe command content."""

@test.test_id("B603")
def subprocess_without_shell_equals_true(context):
    """This rule detects subprocess calls that do not enable a shell and have different applicability."""
''', encoding="utf-8")

    (root / "pep-0008.html").write_text(
        "<h2>Whitespace in Expressions</h2><p>Use consistent whitespace around operators in expressions and statements.</p>"
        "<pre>result = left + right\n</pre>",
        encoding="utf-8",
    )
    (root / "google_pyguide.md").write_text(
        "# Naming\n\nUse descriptive names that communicate the purpose of variables, functions, and classes. "
        "Keep naming conventions consistent across the codebase.\n\n```python\nuser_name = 'Ada'\n```\n",
        encoding="utf-8",
    )


def test_v3_schema_metadata_is_scalar_and_optional_values_are_omitted():
    parent = make_parent_record(
        source_document_id="doc", record_id="record", source_family="bandit",
        source_category="style_static", record_type="static_analysis_rule",
        source_revision="sha256:abc", language="python", language_origin="source_language",
        language_confidence=1.0, fields={"rule": "B602"},
    )
    chunk = V3EmbeddingChunk(
        chunk_id="chunk", source_document_id="doc", record_id="record", source_family="bandit",
        source_category="style_static", record_type="static_analysis_rule", source_revision="sha256:abc",
        chunk_index=0, chunk_count=1, content_hash="hash", language="python",
        language_origin="source_language", language_confidence=1.0, embedding_text="rule text",
        token_count=2,
    )
    assert parent.validate() is None
    metadata = chunk.metadata()
    assert "source_url" not in metadata
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())
    assert parent_upsert_action(None, parent) == "insert"
    assert parent_upsert_action(parent, parent) == "unchanged"
    changed = make_parent_record(
        source_document_id="doc", record_id="record", source_family="bandit",
        source_category="style_static", record_type="static_analysis_rule",
        source_revision="sha256:def", language="python", language_origin="source_language",
        language_confidence=1.0, fields={"rule": "B602"},
    )
    assert parent_upsert_action(parent, changed) == "update"
    tombstone = V3ParentRecord(**{**parent.to_dict(), "source_status": "tombstone"})
    assert parent_upsert_action(parent, tombstone) == "tombstone"


def test_lossless_chunking_preserves_source_code_and_diff_hunks():
    code = (
        "template <class T>\nT ZeroVector(3) {\n    return array_1d<T, 3>();\n}\n"
        "String quoted = \"ZeroVector(3)\"; // café λ\n"
        "void f() {\n    if (true) {\n        run();\n    }\n}\n"
    )
    spans = chunk_source_text(code, CharTokenizer(), max_tokens=45, kind="code")
    assert_lossless_spans(code, spans)
    assert "ZeroVector(3)" in "".join(span.text for span in spans)
    assert "array_1d" in "".join(span.text for span in spans)
    assert "    return" in "".join(span.text for span in spans)
    assert "café λ" in "".join(span.text for span in spans)
    assert all(span.text == code[span.start:span.end] for span in spans)

    java = (
        "package demo;\nclass Example {\n    @Override\n"
        "    public String toString() {\n        return \"λ\";\n    }\n}\n"
    )
    java_spans = chunk_source_text(java, CharTokenizer(), max_tokens=38, kind="code")
    assert_lossless_spans(java, java_spans)
    assert "public String toString()" in "".join(span.text for span in java_spans)

    diff = "diff --git a/x b/x\n@@ -1,2 +1,3 @@\n-a\n+b\n+c\n@@ -8 +9 @@\n-old\n+new\n"
    diff_spans = chunk_source_text(diff, CharTokenizer(), max_tokens=50, kind="diff")
    assert_lossless_spans(diff, diff_spans)
    assert len(diff_spans) == 2
    assert all(span.text.startswith(("diff --git", "@@")) for span in diff_spans)


def test_long_patch_and_documentation_have_deterministic_line_safe_chunks():
    patch = "@@ -1 +1 @@\n" + "".join(f"+value_{index} = {index}\n" for index in range(40))
    docs = "# Long section\n" + "".join(f"Guidance line {index} explains safe behavior.\n" for index in range(35))
    for text, kind in ((patch, "diff"), (docs, "documentation")):
        chunks_a = chunk_source_text(text, CharTokenizer(), max_tokens=65, kind=kind)
        chunks_b = chunk_source_text(text, CharTokenizer(), max_tokens=65, kind=kind)
        assert chunks_a == chunks_b
        assert_lossless_spans(text, chunks_a)
        assert len(chunks_a) > 1
        assert all(span.semantic_unit_split for span in chunks_a)
        assert all(span.text.endswith("\n") or span.end == len(text) for span in chunks_a)

    long_line = "quoted = \"" + ("λvalue_" * 80) + "\"\n"
    line_chunks = chunk_source_text(long_line, CharTokenizer(), max_tokens=50)
    assert_lossless_spans(long_line, line_chunks)
    assert "".join(span.text for span in line_chunks) == long_line
    assert all(span.token_count <= 50 for span in line_chunks)
    assert all(span.split_line_fallback for span in line_chunks)


def test_language_provenance_is_conservative_and_deterministic():
    cases = [
        ("def run(value):\n    return value\n", "python"),
        ("package example;\nimport java.util.List;\n@Override\n", "java"),
        ("const value = 1;\nexport default value;", "javascript"),
        ("template <class T>\nstd::vector<T> values;", "cpp"),
        ("package main\nfunc run() {}", "go"),
        ("require 'rails'\nattr_accessor :name", "ruby"),
        ("if value then\n  value\nend", "unknown"),
        ("value = 1", "unknown"),
    ]
    for text, expected in cases:
        first = classify_language(text)
        assert first == classify_language(text)
        assert first.language == expected
        assert first.confidence in {0.0, 0.7, 0.9, 1.0}
    assert classify_language("class JavaThing { }", file_path="src/Thing.java").language == "java"


def test_source_readers_keep_source_families_and_genuine_metadata_separate(tmp_path):
    _write_fixture_sources(tmp_path)
    parents, inputs, warnings, rejections = load_all_local_sources(tmp_path)
    by_family = {}
    for parent in parents:
        by_family.setdefault(parent.source_family, []).append(parent)
    assert set(by_family) == {"microsoft_historical_reviews", "mitre_cwe", "owasp_cheatsheets", "bandit", "pep8", "google_style"}
    assert by_family["mitre_cwe"][0].fields["cwe_id"] == "CWE-78"
    assert "Use safe APIs" in by_family["mitre_cwe"][0].fields["mitigations"][0]
    assert "applicable_conditions" not in by_family["mitre_cwe"][0].fields
    assert by_family["owasp_cheatsheets"][0].fields["cheatsheet_id"] == "SQL_Injection_Prevention_Cheat_Sheet"
    bandit = {row.fields["rule_function_name"]: row.fields for row in by_family["bandit"]}
    assert bandit["subprocess_popen_with_shell_equals_true"]["rule_id"] == "B602"
    assert bandit["subprocess_without_shell_equals_true"]["rule_id"] == "B603"
    assert by_family["pep8"][0].fields["examples"][0]["kind"] == "example"
    assert by_family["google_style"][0].fields["examples"][0]["language_hint"] == "python"
    ms = {row.fields["original_record_id"]: row for row in by_family["microsoft_historical_reviews"]}
    assert ms["cpp-1"].fields["review_status"] == "historical_unverified"
    assert ms["cpp-1"].fields["source_label_y_uninterpreted"] == 1
    assert ms["cpp-1"].language == "cpp"
    assert ms["java-1"].language == "java"
    assert any("msg-train-selected.jsonl" in source_path for source_path in inputs)
    assert {row["reason"] for row in rejections} == {
        "malformed_json", "missing_required_fields", "duplicate_parent_identity",
    }
    assert not warnings


def test_parent_aware_chunks_and_parent_limit_before_expansion(tmp_path):
    _write_fixture_sources(tmp_path)
    tokenizer = CharTokenizer()
    corpus = prepare_local_sources(
        input_root=tmp_path, tokenizer=tokenizer,
        config=PreparationConfig(max_chunk_tokens=90,
                                 parent_limits={"microsoft_historical_reviews": 1},
                                 tokenizer_name="fixture-char-tokenizer"),
    )
    microsoft_parents = [p for p in corpus.parents if p.source_family == "microsoft_historical_reviews"]
    microsoft_chunks = [c for c in corpus.chunks if c.source_family == "microsoft_historical_reviews"]
    assert len(microsoft_parents) == 1
    assert len(microsoft_chunks) > 1
    assert {chunk.record_id for chunk in microsoft_chunks} == {microsoft_parents[0].record_id}
    assert sorted(chunk.chunk_index for chunk in microsoft_chunks) == list(range(len(microsoft_chunks)))
    assert {chunk.chunk_count for chunk in microsoft_chunks} == {len(microsoft_chunks)}
    assert corpus.manifest["parent_counts_before_allocation"]["microsoft_historical_reviews"] == 2
    assert corpus.manifest["parent_counts_by_source"]["microsoft_historical_reviews"] == 1
    assert corpus.manifest["chunk_counts_by_source"]["microsoft_historical_reviews"] > 1
    comment_chunks = [c for c in microsoft_chunks if c.role == "review_comment"]
    patch_chunks = [c for c in microsoft_chunks if c.role == "patch_hunk"]
    assert comment_chunks and patch_chunks
    assert set(patch_chunks[0].related_chunk_ids) == {c.chunk_id for c in comment_chunks}
    parent = microsoft_parents[0]
    assert parent.fields["reviewer_comment"]
    assert parent.fields["patch"]


def test_ids_hashes_and_manifest_artifacts_are_reproducible(tmp_path):
    _write_fixture_sources(tmp_path / "sources")
    source = tmp_path / "sources"
    config = PreparationConfig(max_chunk_tokens=120, tokenizer_name="fixture-char-tokenizer")
    first = prepare_local_sources(input_root=source, tokenizer=CharTokenizer(), config=config)
    second = prepare_local_sources(input_root=source, tokenizer=CharTokenizer(), config=config)
    assert [row.record_id for row in first.parents] == [row.record_id for row in second.parents]
    assert [row.chunk_id for row in first.chunks] == [row.chunk_id for row in second.chunks]
    assert first.manifest == second.manifest
    assert first.manifest["chromadb_touched"] is False
    assert first.manifest["embedding_generation_performed"] is False
    assert first.manifest["embedding_token_length_statistics"]["max"] is not None

    out_a, out_b = tmp_path / "out-a", tmp_path / "out-b"
    run_preparation(input_root=source, output_dir=out_a, tokenizer=CharTokenizer(), config=config)
    run_preparation(input_root=source, output_dir=out_b, tokenizer=CharTokenizer(), config=config)
    for artifact in sorted(out_a.iterdir()):
        assert artifact.read_bytes() == (out_b / artifact.name).read_bytes()


def test_dry_run_does_not_write_and_missing_sources_are_reported(tmp_path, monkeypatch, capsys):
    _write_fixture_sources(tmp_path / "fixture")
    output = tmp_path / "should-not-exist"
    monkeypatch.setattr("rag.prepare_v3._load_tokenizer", lambda _name: CharTokenizer())
    assert prepare_v3_main([
        "--input-root", str(tmp_path / "fixture"), "--output-dir", str(output), "--dry-run",
        "--tokenizer-name", "fixture-char-tokenizer",
    ]) == 0
    assert not output.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["chromadb_touched"] is False
    assert report["parent_count"] > 0
    assert prepare_v3_main([
        "--input-root", str(tmp_path / "fixture"), "--output-dir", str(output),
        "--tokenizer-name", "fixture-char-tokenizer",
    ]) == 0
    assert {path.name for path in output.iterdir()} == {
        "canonical_parents_v3.jsonl", "embedding_chunks_v3.jsonl",
        "preparation_manifest_v3.json", "rejected_records_v3.jsonl",
        "duplicate_candidates_v3.jsonl",
    }
    assert json.loads((output / "preparation_manifest_v3.json").read_text())[
        "chromadb_touched"
    ] is False

    empty_parents, _inputs, warnings, rejections = load_all_local_sources(tmp_path / "empty")
    assert not empty_parents and not rejections
    assert len(warnings) == 6


def test_v2_record_and_chunking_contract_remains_available():
    tokenizer = CharTokenizer()
    record = CanonicalRecord(
        id="v2", text="short source text", source="microsoft_codereviewer",
        language="python", type="review_comment",
    )
    chunks = v2_chunk_record(record, tokenizer, max_tokens=200)
    assert chunks[0].id == "v2_chunk_0"
    assert chunks[0].text == record.text
