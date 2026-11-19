"""Small, deterministic, non-executing Python security checks."""

from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class StaticFinding:
    finding_id: str
    rule_id: str
    title: str
    severity: str
    explanation: str
    affected_file: str
    line_start: int
    line_end: int
    column: int
    code: str
    fingerprint: str
    origin: str = "static"
    issue_type: str = "security"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StaticAnalysisResult:
    status: str  # passed, failed (findings), or errored (input could not be parsed)
    findings: tuple[StaticFinding, ...] = ()
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_line: Optional[int] = None
    error_column: Optional[int] = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["findings"] = [finding.to_dict() for finding in self.findings]
        return result


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


def _rule_for_call(node: ast.Call) -> tuple[str, str, str] | None:
    name = _call_name(node)
    leaf = name.rsplit(".", 1)[-1]
    if leaf in {"eval", "exec"}:
        return (
            "PYSEC001",
            f"Dynamic code execution via {leaf}()",
            f"{leaf}() evaluates dynamically supplied Python code and can execute arbitrary operations.",
        )
    if name in {"pickle.load", "pickle.loads", "_pickle.load", "_pickle.loads",
                "dill.load", "dill.loads", "shelve.open"}:
        return (
            "PYSEC002",
            "Unsafe object deserialization",
            f"{name}() can construct executable objects while loading untrusted serialized data.",
        )
    if leaf == "load" and name in {"yaml.load", "ruamel.yaml.load"}:
        loader = next((kw.value for kw in node.keywords if kw.arg == "Loader"), None)
        if loader is None and len(node.args) >= 2:
            loader = node.args[1]
        unsafe_loader = (
            isinstance(loader, ast.Attribute)
            and loader.attr in {"Loader", "UnsafeLoader", "CLoader"}
        )
        if unsafe_loader:
            return (
                "PYSEC003",
                "YAML loaded with an unsafe loader",
                "This YAML load call explicitly selects an unsafe loader that can construct Python objects.",
            )
    process_calls = {
        "subprocess.run", "subprocess.call", "subprocess.Popen",
        "subprocess.check_call", "subprocess.check_output",
    }
    if name in process_calls and any(
        kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in node.keywords
    ):
        return (
            "PYSEC004",
            "Subprocess invoked with shell=True",
            "shell=True passes the command through a shell and permits shell metacharacter interpretation.",
        )
    if name == "os.system":
        return (
            "PYSEC005",
            "Command executed through os.system()",
            "os.system() invokes a system shell; use an argument vector with subprocess instead.",
        )
    return None


def analyze_python(source: str, *, file_path: str = "submitted.py") -> StaticAnalysisResult:
    """Analyze Python security and narrow formatting rules without executing source."""
    if not isinstance(source, str):
        return StaticAnalysisResult("errored", error_type="TypeError", error_message="source must be a string")
    if not isinstance(file_path, str) or not file_path:
        file_path = "submitted.py"
    try:
        tree = ast.parse(source, filename=file_path, mode="exec")
    except SyntaxError as exc:
        return StaticAnalysisResult(
            "errored", error_type=type(exc).__name__, error_message=exc.msg,
            error_line=exc.lineno, error_column=exc.offset,
        )
    except Exception as exc:
        return StaticAnalysisResult("errored", error_type=type(exc).__name__, error_message=str(exc)[:200])

    counts: Counter[str] = Counter()
    findings: list[StaticFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rule = _rule_for_call(node)
        if rule is None:
            continue
        rule_id, title, explanation = rule
        segment = ast.get_source_segment(source, node) or _call_name(node)
        fingerprint_material = f"{rule_id}\0{ast.dump(node, include_attributes=False)}"
        fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()[:20]
        counts[fingerprint] += 1
        line = int(getattr(node, "lineno", 1))
        column = int(getattr(node, "col_offset", 0))
        stable_material = f"{file_path}\0{fingerprint}\0{counts[fingerprint]}"
        finding_id = "static-" + hashlib.sha256(stable_material.encode("utf-8")).hexdigest()[:20]
        findings.append(StaticFinding(
            finding_id=finding_id, rule_id=rule_id, title=title, severity="high",
            explanation=explanation, affected_file=file_path, line_start=line,
            line_end=int(getattr(node, "end_lineno", line)), column=column,
            code=segment, fingerprint=fingerprint,
        ))
    lines = source.splitlines()
    assignment_regions: list[tuple[int, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign):
            target, value = node.target, node.value
            if (target.end_lineno == value.lineno and target.end_col_offset is not None
                    and value.col_offset is not None):
                assignment_regions.append((target.end_lineno, target.end_col_offset, value.col_offset))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if (target.end_lineno == value.lineno and target.end_col_offset is not None
                    and value.col_offset is not None):
                assignment_regions.append((target.end_lineno, target.end_col_offset, value.col_offset))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            annotation, value = node.annotation, node.value
            if (annotation.end_lineno == value.lineno and annotation.end_col_offset is not None
                    and value.col_offset is not None):
                assignment_regions.append((annotation.end_lineno, annotation.end_col_offset, value.col_offset))

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.OP or token.string not in {"=", "+=", "-=", "*=", "/=", "//=", "%=", "**=", "&=", "|=", "^=", ">>=", "<<=", "@="}:
                continue
            line_number, column = token.start
            end_column = token.end[1]
            byte_column = len(lines[line_number - 1][:column].encode("utf-8"))
            byte_end_column = len(lines[line_number - 1][:end_column].encode("utf-8"))
            if not any(line == line_number and start <= byte_column and byte_end_column <= end
                       for line, start, end in assignment_regions):
                continue
            line_text = lines[line_number - 1]
            if ((column > 0 and not line_text[column - 1].isspace())
                    or (end_column < len(line_text) and not line_text[end_column].isspace())):
                rule_id = "PYFMT001"
                title = "Missing spaces around assignment operator"
                explanation = "PEP 8 requires spaces around assignment and augmented-assignment operators."
                recommended_code = line_text.strip()
                fingerprint = hashlib.sha256(
                    f"{rule_id}\0{token.string}\0{recommended_code}".encode("utf-8")
                ).hexdigest()[:20]
                counts[fingerprint] += 1
                stable_material = f"{file_path}\0{fingerprint}\0{counts[fingerprint]}"
                finding_id = "static-" + hashlib.sha256(stable_material.encode("utf-8")).hexdigest()[:20]
                findings.append(StaticFinding(
                    finding_id=finding_id, rule_id=rule_id, title=title, severity="low",
                    explanation=explanation, affected_file=file_path, line_start=line_number,
                    line_end=line_number, column=column, code=token.string,
                    fingerprint=fingerprint, issue_type="formatting",
                ))
    except (tokenize.TokenError, IndentationError):
        # Valid AST input should tokenize successfully; declining formatting
        # findings on tokenizer anomalies leaves the security AST checks intact.
        pass
    findings.sort(key=lambda item: (item.line_start, item.column, item.rule_id, item.finding_id))
    return StaticAnalysisResult("failed" if findings else "passed", tuple(findings))


def static_fingerprint_counts(result: StaticAnalysisResult) -> Counter[tuple[str, str]]:
    """Return location-independent rule fingerprints for baseline comparison."""
    return Counter((finding.rule_id, finding.fingerprint) for finding in result.findings)
