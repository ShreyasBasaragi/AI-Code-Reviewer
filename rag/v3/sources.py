"""Offline v3 source readers with stable identities and structured parent fields."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from rag.adapters.reference_rules import _TextBlocks, _local_name, _xml_text
from rag.v3.language import classify_language
from rag.v3.schema import V3ParentRecord, content_hash, stable_id

SOURCE_FAMILIES = (
    "microsoft_historical_reviews", "mitre_cwe", "owasp_cheatsheets",
    "bandit", "pep8", "google_style",
)


@dataclass(frozen=True)
class SourceLayout:
    microsoft: Path
    cwe_zip: Path
    owasp_root: Path
    bandit_root: Path
    pep8_html: Path
    google_markdown: Path

    @classmethod
    def from_root(cls, root: Path) -> "SourceLayout":
        raw = root.resolve()
        return cls(
            microsoft=raw / "microsoft_codereviewer" / "Comment_Generation",
            cwe_zip=raw / "cwe" / "cwec_latest.xml.zip",
            owasp_root=raw / "owasp_cheatsheets",
            bandit_root=raw / "bandit_rules",
            pep8_html=raw / "pep-0008.html",
            google_markdown=raw / "google_pyguide.md",
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_revision(path: Path, project_root: Path) -> tuple[str, str] | None:
    if path.is_file():
        return _as_input(path, project_root), _revision(path)
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        return None
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(item)))
    return _as_input(path, project_root), "sha256:" + digest.hexdigest()


def _revision(path: Path) -> str:
    return "sha256:" + sha256_file(path)


def _source_doc_id(family: str, revision: str, identity: str) -> str:
    return stable_id("document", family, revision, identity)


def _make_parent(
    *, family: str, category: str, record_type: str, revision: str,
    identity: str, document_id: str, fields: dict[str, Any],
    language: str = "unknown", language_origin: str = "unknown",
    language_confidence: float = 0.0, source_url: str | None = None,
) -> V3ParentRecord:
    record = V3ParentRecord(
        source_document_id=document_id,
        record_id=stable_id("record", family, revision, identity),
        source_family=family,
        source_category=category,
        record_type=record_type,
        source_revision=revision,
        content_hash=content_hash(fields),
        language=language,
        language_origin=language_origin,
        language_confidence=language_confidence,
        source_url=source_url,
        source_identity=identity,
        fields=fields,
    )
    record.validate()
    return record


def load_microsoft_parents(
    layout: SourceLayout, *, project_root: Path, max_records: int | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> tuple[list[V3ParentRecord], dict[str, str]]:
    selected = layout.microsoft / "msg-train-selected.jsonl"
    source_file = selected if selected.is_file() else layout.microsoft / "msg-train.jsonl"
    if not source_file.is_file():
        return [], {}
    revision = _revision(source_file)
    relative = source_file.relative_to(project_root).as_posix()
    document_id = _source_doc_id("microsoft_historical_reviews", revision, relative)
    parents: list[V3ParentRecord] = []
    with source_file.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, line in enumerate(stream, start=1):
            if max_records is not None and len(parents) >= max_records:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if rejected is not None:
                    rejected.append({"source_path": relative, "line_number": line_number,
                                     "reason": "malformed_json"})
                continue
            if not isinstance(row, dict):
                if rejected is not None:
                    rejected.append({"source_path": relative, "line_number": line_number,
                                     "reason": "record_not_object"})
                continue
            comment, patch = row.get("msg"), row.get("patch")
            if not isinstance(comment, str) or not comment.strip() or not isinstance(patch, str) or not patch.strip():
                if rejected is not None:
                    missing = []
                    if not isinstance(comment, str) or not comment.strip():
                        missing.append("msg")
                    if not isinstance(patch, str) or not patch.strip():
                        missing.append("patch")
                    rejected.append({"source_path": relative, "line_number": line_number,
                                     "reason": "missing_required_fields", "fields": missing})
                continue
            old_code = row.get("oldf")
            raw_id = row.get("id")
            original_identity = str(raw_id) if raw_id is not None else "content:" + content_hash({
                "msg": comment, "patch": patch, "oldf": old_code if isinstance(old_code, str) else "",
            })
            identity = original_identity + ":content:" + content_hash({
                "msg": comment, "patch": patch, "oldf": old_code if isinstance(old_code, str) else "",
            })
            fields: dict[str, Any] = {
                "original_record_id": original_identity,
                "task_name": "Comment_Generation",
                "reviewer_comment": comment,
                "patch": patch,
                "review_status": "historical_unverified",
            }
            if isinstance(old_code, str) and old_code:
                fields["old_code"] = old_code
            # Preserve y without assigning it a meaning; absent values are omitted.
            if row.get("y") is not None:
                fields["source_label_y_uninterpreted"] = row["y"]
            language = classify_language(
                patch + ("\n" + old_code if isinstance(old_code, str) else ""),
                explicit_language=row.get("lang"), file_path=row.get("path"),
            )
            parents.append(_make_parent(
                family="microsoft_historical_reviews", category="historical_pr",
                record_type="historical_review", revision=revision, identity=identity,
                document_id=document_id, fields=fields, language=language.language,
                language_origin=language.origin, language_confidence=language.confidence,
            ))
    parents.sort(key=lambda item: item.record_id)
    return parents, {relative: revision}


def _markdown_sections(path: Path) -> list[tuple[str, str, int]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text))
    if not headings:
        return [(path.stem, text, 0)] if text.strip() else []
    result = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end]
        if body.strip():
            result.append((heading.group(1).strip(), body, index))
    return result


def _markdown_examples(text: str) -> list[dict[str, str]]:
    examples = []
    pattern = re.compile(r"(?ms)^```([^\n`]*)\n(.*?)^```\s*$")
    for match in pattern.finditer(text):
        example: dict[str, str] = {"kind": "example", "text": match.group(2)}
        language = match.group(1).strip()
        if language:
            example["language_hint"] = language
        examples.append(example)
    return examples


def _example_kind(section_title: str) -> str:
    normalized = section_title.casefold()
    if re.search(r"\b(correct|good|preferred|recommended)\b", normalized):
        return "correct"
    if re.search(r"\b(incorrect|wrong|bad|avoid)\b", normalized):
        return "incorrect"
    return "example"


def _as_input(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name


def load_cwe_parents(layout: SourceLayout, *, project_root: Path) -> tuple[list[V3ParentRecord], dict[str, str]]:
    source = layout.cwe_zip
    if not source.is_file():
        return [], {}
    revision = _revision(source)
    relative = _as_input(source, project_root)
    document_id = _source_doc_id("mitre_cwe", revision, relative)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            names = sorted(name for name in archive.namelist() if name.lower().endswith(".xml"))
            if not names:
                return [], {relative: revision}
            with archive.open(names[0]) as stream:
                root = ET.parse(stream).getroot()
    elif source.suffix.lower() == ".xml":
        root = ET.parse(source).getroot()
    else:
        return [], {relative: revision}
    parents = []
    for weakness in (node for node in root.iter() if _local_name(node.tag) == "Weakness"):
        number = weakness.attrib.get("ID")
        name = weakness.attrib.get("Name", "")
        description, extended = "", ""
        mitigations, conditions, consequences = [], [], []
        for child in weakness.iter():
            tag = _local_name(child.tag)
            if tag == "Description" and not description:
                description = _xml_text(child)
            elif tag == "Extended_Description" and not extended:
                extended = _xml_text(child)
            elif tag == "Mitigation":
                value = _xml_text(child)
                if value:
                    mitigations.append(value)
            elif tag in {"Applicable_Platforms", "Modes_Of_Introduction"}:
                value = _xml_text(child)
                if value:
                    conditions.append(value)
            elif tag == "Common_Consequences":
                value = _xml_text(child)
                if value:
                    consequences.append(value)
        if not number or not name or not description:
            continue
        cwe_id = f"CWE-{number}"
        fields: dict[str, Any] = {
            "cwe_id": cwe_id,
            "weakness_name": name,
            "description": description,
            "language_applicability": "language_agnostic",
        }
        if mitigations:
            fields["mitigations"] = mitigations
        if conditions:
            fields["applicable_conditions"] = conditions
        if consequences:
            fields["common_consequences"] = consequences
        if extended:
            fields["extended_description"] = extended
        parents.append(_make_parent(
            family="mitre_cwe", category="security", record_type="security_pattern",
            revision=revision, identity=cwe_id, document_id=document_id, fields=fields,
            language="language_agnostic", language_origin="source_semantics",
            language_confidence=1.0,
            source_url=f"https://cwe.mitre.org/data/definitions/{number}.html",
        ))
    parents.sort(key=lambda item: item.record_id)
    return parents, {relative: revision}


_IGNORED_OWASP = {
    "table of contents", "introduction", "authors", "acknowledgements", "references",
    "related articles", "license", "contributors", "overview",
}


def load_owasp_parents(layout: SourceLayout, *, project_root: Path) -> tuple[list[V3ParentRecord], dict[str, str]]:
    root = layout.owasp_root
    if not root.is_dir():
        return [], {}
    series = root / "CheatSheetSeries-master"
    cheatsheets = series / "cheatsheets" if (series / "cheatsheets").is_dir() else root
    files = sorted(path for path in cheatsheets.rglob("*.md")
                   if not any(part.startswith(".") for part in path.relative_to(cheatsheets).parts))
    if not files:
        return [], {}
    archive = root.parent / "owasp_cheatsheets.zip"
    if archive.is_file():
        revision = _revision(archive)
        revision_path = _as_input(archive, project_root)
    else:
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(cheatsheets).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(sha256_file(path)))
        revision = "sha256:" + digest.hexdigest()
        revision_path = _as_input(root, project_root)
    parents = []
    for path in files:
        relative_path = path.relative_to(cheatsheets).as_posix()
        document_id = _source_doc_id("owasp_cheatsheets", revision, relative_path)
        title = path.stem.replace("_", " ").replace("-", " ")
        for section, body, section_index in _markdown_sections(path):
            if section.casefold() in _IGNORED_OWASP or len(body.strip()) < 40:
                continue
            identity = relative_path + "#" + section + ":" + str(section_index)
            fields = {
                "cheatsheet_id": path.stem,
                "cheatsheet_title": title,
                "section_id": stable_id("section", "owasp_cheatsheets", revision, identity),
                "section_title": section,
                "security_topic": path.stem,
                "section_text": body,
                "language_applicability": "unknown",
            }
            url = "https://cheatsheetseries.owasp.org/" + relative_path.removesuffix(".md") + ".html"
            parents.append(_make_parent(
                family="owasp_cheatsheets", category="security", record_type="security_pattern",
                revision=revision, identity=identity, document_id=document_id, fields=fields,
                language="language_agnostic", language_origin="source_semantics",
                language_confidence=1.0, source_url=url,
            ))
    parents.sort(key=lambda item: item.record_id)
    return parents, {revision_path: revision}


def _bandit_rule_id(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr != "test_id":
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and re.fullmatch(r"B\d{3}", first.value):
            return first.value
    return None


def _documented_apis(text: str) -> list[str]:
    return sorted(set(re.findall(
        r"\b(?:subprocess|os|pickle|yaml|ssl|hashlib)\.[A-Za-z_]\w*", text
    )))


def load_bandit_parents(layout: SourceLayout, *, project_root: Path) -> tuple[list[V3ParentRecord], dict[str, str]]:
    root = layout.bandit_root
    plugin_root = root / "bandit-main" / "bandit" / "plugins"
    if not plugin_root.is_dir():
        plugin_root = root / "bandit" / "plugins"
    if not plugin_root.is_dir():
        return [], {}
    files = sorted(plugin_root.rglob("*.py"))
    archive = root.parent / "bandit_rules.zip"
    if archive.is_file():
        revision = _revision(archive)
        revision_path = _as_input(archive, project_root)
    else:
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(sha256_file(path)))
        revision = "sha256:" + digest.hexdigest()
        revision_path = _as_input(root, project_root)
    parents = []
    for path in files:
        try:
            source_text = path.read_text(encoding="utf-8", errors="strict")
            tree = ast.parse(source_text)
        except (SyntaxError, OSError, UnicodeError):
            continue
        relative = path.relative_to(root).as_posix()
        document_id = _source_doc_id("bandit", revision, relative)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            description = ast.get_docstring(node)
            if not description or len(description.strip()) < 45:
                continue
            identity = relative + "::" + node.name
            fields: dict[str, Any] = {
                "rule_function_name": node.name,
                "rule_condition_and_description": description,
                "applicable_apis": _documented_apis(description),
                "source_file": relative,
                "language_applicability": "python",
            }
            rule_id = _bandit_rule_id(node)
            if rule_id:
                fields["rule_id"] = rule_id
            documented_cwe = sorted(set(re.findall(r"\bCWE-\d{1,5}\b", description)))
            if documented_cwe:
                fields["documented_cwe_ids"] = documented_cwe
            parents.append(_make_parent(
                family="bandit", category="style_static", record_type="static_analysis_rule",
                revision=revision, identity=identity, document_id=document_id, fields=fields,
                language="python", language_origin="source_language", language_confidence=1.0,
                source_url="https://github.com/PyCQA/bandit",
            ))
    parents.sort(key=lambda item: item.record_id)
    return parents, {revision_path: revision}


def load_style_parents(layout: SourceLayout, *, project_root: Path) -> tuple[list[V3ParentRecord], dict[str, str]]:
    parents: list[V3ParentRecord] = []
    inputs: dict[str, str] = {}
    for family, path, guide, url in (
        ("pep8", layout.pep8_html, "PEP 8", "https://peps.python.org/pep-0008/"),
        ("google_style", layout.google_markdown, "Google Python Style Guide", "https://google.github.io/styleguide/pyguide.html"),
    ):
        if not path.is_file():
            continue
        revision = _revision(path)
        relative = _as_input(path, project_root)
        inputs[relative] = revision
        document_id = _source_doc_id(family, revision, relative)
        if family == "google_style":
            sections = [
                (title, body, index, _markdown_examples(body))
                for title, body, index in _markdown_sections(path)
            ]
        else:
            parser = _TextBlocks()
            parser.feed(path.read_text(encoding="utf-8", errors="strict"))
            heading = "PEP 8"
            section_bodies: dict[str, list[str]] = {}
            section_examples: dict[str, list[dict[str, str]]] = {}
            section_order: list[str] = []
            for index, (tag, value) in enumerate(parser.blocks):
                if tag in {"h1", "h2", "h3", "h4"}:
                    heading = value
                    if heading not in section_bodies:
                        section_bodies[heading] = []
                        section_examples[heading] = []
                        section_order.append(heading)
                elif tag == "pre" and value.strip():
                    if heading not in section_bodies:
                        section_bodies[heading] = []
                        section_examples[heading] = []
                        section_order.append(heading)
                    section_examples[heading].append({"kind": _example_kind(heading), "text": value})
                elif value.strip():
                    if heading not in section_bodies:
                        section_bodies[heading] = []
                        section_examples[heading] = []
                        section_order.append(heading)
                    section_bodies[heading].append(value)
            sections = [
                (title, "\n\n".join(section_bodies[title]), index, section_examples[title])
                for index, title in enumerate(section_order)
            ]
        for section, body, _index, examples in sections:
            if len(body.strip()) < 35 and not examples:
                continue
            rule_text = body if body.strip() else "\n\n".join(item["text"] for item in examples)
            identity = section + ":" + content_hash({"rule_text": rule_text, "examples": examples})
            fields = {
                "guide_id": guide,
                "section_id": stable_id("section", family, revision, identity),
                "section_title": section,
                "rule_text": rule_text,
                "applicability": "python",
                "guideline_kind": "style_or_convention",
            }
            if examples:
                fields["examples"] = examples
            parents.append(_make_parent(
                family=family, category="style_static", record_type="style_rule",
                revision=revision, identity=identity, document_id=document_id, fields=fields,
                language="python", language_origin="source_identity", language_confidence=1.0,
                source_url=url,
            ))
    parents.sort(key=lambda item: item.record_id)
    return parents, inputs


def load_all_local_sources(
    input_root: Path, *, max_microsoft_records: int | None = None,
) -> tuple[list[V3ParentRecord], dict[str, str], list[str], list[dict[str, Any]]]:
    """Load available sources only; never downloads or extracts archives."""
    root = input_root.resolve()
    layout = SourceLayout.from_root(root)
    loaders = (
        ("microsoft_historical_reviews", load_microsoft_parents),
        ("mitre_cwe", load_cwe_parents),
        ("owasp_cheatsheets", load_owasp_parents),
        ("bandit", load_bandit_parents),
        ("pep8", load_style_parents),
    )
    parents: list[V3ParentRecord] = []
    inputs: dict[str, str] = {}
    rejections: list[dict[str, Any]] = []
    family_path_markers = {
        "microsoft_historical_reviews": "microsoft_codereviewer/",
        "mitre_cwe": "cwe/",
        "owasp_cheatsheets": "owasp_cheatsheets",
        "bandit": "bandit_rules",
        "pep8": "pep-0008.html",
        "google_style": "google_pyguide.md",
    }
    family_paths = {
        "microsoft_historical_reviews": (layout.microsoft / "msg-train-selected.jsonl"
                                          if (layout.microsoft / "msg-train-selected.jsonl").is_file()
                                          else layout.microsoft / "msg-train.jsonl"),
        "mitre_cwe": layout.cwe_zip,
        "owasp_cheatsheets": layout.owasp_root,
        "bandit": layout.bandit_root,
        "pep8": layout.pep8_html,
        "google_style": layout.google_markdown,
    }
    for family, loader in loaders:
        try:
            if loader is load_microsoft_parents:
                records, used = loader(layout, project_root=root, max_records=max_microsoft_records,
                                       rejected=rejections)
            else:
                records, used = loader(layout, project_root=root)
        except (OSError, UnicodeError, ValueError, SyntaxError, zipfile.BadZipFile, ET.ParseError) as exc:
            rejections.append({"source_family": family, "reason": "source_parse_error",
                               "error_type": type(exc).__name__})
            source_revision = _path_revision(family_paths[family], root)
            if source_revision:
                inputs[source_revision[0]] = source_revision[1]
            continue
        parents.extend(records)
        inputs.update(used)
    unique_parents: dict[str, V3ParentRecord] = {}
    for parent in sorted(parents, key=lambda item: (item.source_family, item.record_id,
                                                     item.content_hash)):
        existing = unique_parents.get(parent.record_id)
        if existing is None:
            unique_parents[parent.record_id] = parent
            continue
        if existing.content_hash != parent.content_hash:
            raise ValueError(f"source record ID collision with different content: {parent.record_id}")
        rejections.append({
            "source_family": parent.source_family,
            "record_id": parent.record_id,
            "reason": "duplicate_parent_identity",
            "duplicate_of": parent.record_id,
        })
    parents = list(unique_parents.values())
    input_names = tuple(inputs)
    available = {
        family for family, marker in family_path_markers.items()
        if any(marker in input_name for input_name in input_names)
    }
    missing = [family for family in SOURCE_FAMILIES if family not in available]
    parents.sort(key=lambda item: (item.source_family, item.record_id))
    return (parents, dict(sorted(inputs.items())),
            [f"missing local source: {family}" for family in missing], rejections)
