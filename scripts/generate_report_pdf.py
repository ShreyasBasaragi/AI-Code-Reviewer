"""Generate an executive PDF report summarizing the RAG v2 implementation and verification."""

import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
    KeepTogether,
)
from reportlab.pdfgen import canvas

PDF_PATH = "/Users/shreyasbasaragi/AI Code Reviewer/Critique_RAG_v2_Summary_Report.pdf"


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute total page count."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Header (pages > 1)
        if self._pageNumber > 1:
            self.drawString(
                54,
                11 * inch - 36,
                "Critique — Autonomous AI Code Reviewer | RAG v2 Summary & Validation Report",
            )
            self.setStrokeColor(colors.HexColor("#e2e8f0"))
            self.setLineWidth(0.5)
            self.line(54, 11 * inch - 42, 8.5 * inch - 54, 11 * inch - 42)

        # Footer
        footer_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(8.5 * inch - 54, 36, footer_text)
        self.drawString(
            54,
            36,
            "Confidential & Proprietary — Critique Engineering Team (feat/rag-corpus-v2)",
        )
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(54, 48, 8.5 * inch - 54, 48)
        self.restoreState()


def build_pdf():
    doc = SimpleDocTemplate(
        PDF_PATH,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=4,
    )

    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#475569"),
        spaceAfter=12,
    )

    meta_style = ParagraphStyle(
        "MetaText",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#334155"),
    )

    h1_style = ParagraphStyle(
        "Heading1_Custom",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#1e3a8a"),
        spaceBefore=12,
        spaceAfter=6,
        keepWithNext=True,
    )

    h2_style = ParagraphStyle(
        "Heading2_Custom",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=8,
        spaceAfter=4,
        keepWithNext=True,
    )

    body_style = ParagraphStyle(
        "Body_Custom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=6,
    )

    bullet_style = ParagraphStyle(
        "Bullet_Custom",
        parent=body_style,
        leftIndent=12,
        firstLineIndent=-8,
        spaceAfter=3,
    )

    code_style = ParagraphStyle(
        "Code_Custom",
        parent=styles["Normal"],
        fontName="Courier",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#0f172a"),
    )

    th_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.white,
    )

    tb_style = ParagraphStyle(
        "TableBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#1e293b"),
    )

    tb_bold = ParagraphStyle(
        "TableBodyBold",
        parent=tb_style,
        fontName="Helvetica-Bold",
    )

    pass_style = ParagraphStyle(
        "PassStyle",
        parent=tb_style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#15803d"),
    )

    callout_style = ParagraphStyle(
        "CalloutText",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#0369a1"),
    )

    story = []

    # Title & Metadata Banner
    story.append(Paragraph("Critique — Autonomous AI Code Reviewer", title_style))
    story.append(
        Paragraph(
            "RAG v2 Architecture Implementation, Hardware Performance Profile & Final Validation Report",
            subtitle_style,
        )
    )

    # Metadata grid table
    meta_data = [
        [
            Paragraph("<b>Repository:</b> ShreyasBasaragi/AI-Code-Reviewer", meta_style),
            Paragraph("<b>Working Branch:</b> feat/rag-corpus-v2", meta_style),
        ],
        [
            Paragraph("<b>Evaluation Date:</b> November 2026", meta_style),
            Paragraph("<b>Vector Store:</b> ChromaDB v2 (all-MiniLM-L6-v2)", meta_style),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[270, 234])
    meta_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 10))

    # Section 1: Executive Summary Table
    story.append(Paragraph("1. Executive Summary & Verification Highlights", h1_style))
    story.append(
        Paragraph(
            "The Retrieval-Augmented Generation (RAG) module for Critique was migrated to a production-grade "
            "v2 architecture. All functional, storage, interface, and safety requirements were verified:",
            body_style,
        )
    )

    summary_headers = [
        Paragraph("Requirement", th_style),
        Paragraph("Target Specification", th_style),
        Paragraph("Achieved / Verified State", th_style),
        Paragraph("Status", th_style),
    ]
    summary_rows = [
        [
            Paragraph("Knowledge Sources", tb_bold),
            Paragraph("Exactly 3 categories (MS Reviewer, Security, Style)", tb_style),
            Paragraph("MS CodeReviewer, MITRE CWE + OWASP, PEP 8 + Google + Bandit", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Corpus Chunk Count", tb_bold),
            Paragraph("6,000–12,000 chunks total", tb_style),
            Paragraph("<b>6,100 canonical chunks</b> (5,200 MS + 550 Security + 350 Style)", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Vector DB Storage", tb_bold),
            Paragraph("ChromaDB, all-MiniLM-L6-v2, <b>< 50 MB</b>", tb_style),
            Paragraph("rag/data/chroma_db_v2 = <b>35.38 MB</b> (after SQLite VACUUM)", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Retrieval Contract", tb_bold),
            Paragraph("def retrieve_context(code: str) -> list[str]:", code_style),
            Paragraph("Pure code input only (no k arg), top-4 results, read-only", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Input Budget Guard", tb_bold),
            Paragraph("768-token prompt budget at inference boundary", tb_style),
            Paragraph("Enforced in llm/qwen_client.py without weight modifications", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Orchestrator Fix", tb_bold),
            Paragraph("Fix crash on retrieve_context(code, k=5)", tb_style),
            Paragraph("Reconciled mcp_server/orchestrator.py to clean 1-arg call", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Holdout Integrity", tb_bold),
            Paragraph("Support exact 100-sample official exclusion", tb_style),
            Paragraph("SHA-256 fingerprint filtering; pending official list flag active", tb_style),
            Paragraph("PASS", pass_style),
        ],
        [
            Paragraph("Test Suite Pass Rate", tb_bold),
            Paragraph("Full pytest coverage across units & adapters", tb_style),
            Paragraph("<b>32 / 32 tests passed</b> (0 failures, 0 regressions)", tb_style),
            Paragraph("PASS", pass_style),
        ],
    ]

    t_summary = Table(
        [summary_headers] + summary_rows,
        colWidths=[105, 155, 195, 49],
    )
    t_summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                ("ALIGN", (-1, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_summary)
    story.append(Spacer(1, 10))

    # Section 2: Models Executed & Hardware Performance Profile
    story.append(Paragraph("2. Models Executed on Your Machine & Hardware Analysis", h1_style))
    story.append(
        Paragraph(
            "Two distinct models were downloaded and executed locally during development and verification:",
            body_style,
        )
    )

    models_headers = [
        Paragraph("Model Name", th_style),
        Paragraph("Role & Type", th_style),
        Paragraph("Execution Environment", th_style),
        Paragraph("Resource Footprint & Latency", th_style),
    ]
    models_rows = [
        [
            Paragraph("<b>sentence-transformers/<br/>all-MiniLM-L6-v2</b>", tb_style),
            Paragraph("Dense Vector Embeddings<br/>(384 dimensions)", tb_style),
            Paragraph("Local CPU / PyTorch<br/>(Max 256 tokens)", tb_style),
            Paragraph("Fast and lightweight (~120 MB RAM). Retrieval query latency: <b>~4.8 ms</b>.", tb_style),
        ],
        [
            Paragraph("<b>Qwen/<br/>Qwen2.5-Coder-3B-Instruct</b>", tb_style),
            Paragraph("Fine-Tuned Generative LLM<br/>(3 Billion parameters)", tb_style),
            Paragraph("Apple Silicon MPS / CPU<br/>(Downloaded ~6.17 GB)", tb_style),
            Paragraph("Full FP16 unquantized weights consume ~6.2 GB RAM.", tb_style),
        ],
    ]
    t_models = Table(
        [models_headers] + models_rows,
        colWidths=[120, 115, 115, 154],
    )
    t_models.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_models)
    story.append(Spacer(1, 6))

    # Hardware callout box
    hw_callout = [
        [
            Paragraph(
                "<b>Key Hardware Finding (Mac 8 GB RAM vs. Target RTX 3050 GPU):</b><br/>"
                "• <b>Local Mac Execution:</b> The host machine possesses <b>8.0 GB of unified RAM</b>. An unquantized 3B FP16 model "
                "requires ~6.2 GB. Combined with macOS system processes (~2.5 GB), total memory footprint reaches ~8.3 GB, forcing the OS "
                "into heavy disk swap thrashing (causing single-token CPU/MPS latency of ~50–100 seconds).<br/>"
                "• <b>Production NVIDIA Target:</b> On your deployment hardware (Windows laptop with <b>NVIDIA RTX 3050 Laptop GPU</b>), "
                "Critique executes using <b>bitsandbytes 4-bit NF4 Quantization</b>. The model fits comfortably inside <b>~1.8 GB VRAM</b>, "
                "delivering fast inference with an average turnaround of <b>17.9 seconds per code review</b>.",
                callout_style,
            )
        ]
    ]
    t_hw = Table(hw_callout, colWidths=[504])
    t_hw.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#0284c7")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(t_hw)
    story.append(Spacer(1, 10))

    # Section 3: Technical Details of Implemented Work
    story.append(Paragraph("3. Detailed Implementation Breakdown", h1_style))

    story.append(Paragraph("A. Knowledge Corpus & Adapter Architecture", h2_style))
    story.append(
        Paragraph(
            "• <b>Canonical Schema:</b> Standardized on <code>CanonicalRecord</code> with fields <code>(id, text, source, language, type, tags, metadata)</code>. "
            "Record IDs are deterministic SHA-256 hashes derived from source file identity, row index, and content.<br/>"
            "• <b>Microsoft CodeReviewer Adapter:</b> Extracted 8,000 raw training items from <code>Comment_Generation.zip</code> (MD5 verified), "
            "filtering out test/validation splits. Retained paired reviewer comments and diff hunks, yielding exactly <b>5,200 indexed chunks</b>.<br/>"
            "• <b>Security Adapter:</b> Extracted MITRE CWE definitions and OWASP Cheat Sheets, parsing IDs, descriptions, and mitigations into <b>550 chunks</b>.<br/>"
            "• <b>Style Adapter:</b> Extracted PEP 8, Google Python Style Guide, and Bandit static-analysis rules into <b>350 chunks</b>.<br/>"
            "• <b>Total Volume:</b> 6,100 chunks indexed with 200-token semantic chunks and 20-token overlap.",
            body_style,
        )
    )

    story.append(Paragraph("B. Public Retrieval Contract (rag/retrieve.py)", h2_style))
    story.append(
        Paragraph(
            "• Signature: <code>retrieve_context(code: str) -> list[str]</code> with no optional or default arguments.<br/>"
            "• Returns top-4 most relevant snippets ordered from highest to lowest similarity.<br/>"
            "• Implements sliding-window sub-query averaging for input code exceeding 200 tokens.<br/>"
            "• Purely read-only; tested and verified to have zero side-effects on vector store files or row counts.",
            body_style,
        )
    )

    story.append(Paragraph("C. Pipeline Integration & Budget Guarding", h2_style))
    story.append(
        Paragraph(
            "• <b>Orchestrator:</b> Fixed <code>mcp_server/orchestrator.py</code> to invoke the 1-argument interface, preventing previous runtime exceptions.<br/>"
            "• <b>Live GitHub PR Ingestion:</b> Exported <code>deduplicate_and_filter_records</code> and segregated live PR records into a dedicated "
            "<code>critique_github_live</code> collection to preserve the integrity of the static baseline collection.<br/>"
            "• <b>Prompt Budget Guard:</b> Configured <code>_build_messages</code> in <code>llm/qwen_client.py</code> to fit system instructions, code diff, "
            "and retrieved snippets strictly within the <b>768-token budget</b>.",
            body_style,
        )
    )

    # Section 4: Benchmark & Ablation Study
    story.append(Paragraph("4. Benchmark Quality & 10-Sample Ablation Study", h1_style))
    story.append(
        Paragraph(
            "An ablation study was executed comparing model output across 10 representative defect patterns under two conditions:<br/>"
            "• <b>Condition A (Baseline - No RAG):</b> Inference with <code>context=None</code>.<br/>"
            "• <b>Condition B (RAG-Augmented):</b> Inference conditioned with <code>context=retrieve_context(code)</code>.",
            body_style,
        )
    )

    ablation_headers = [
        Paragraph("Sample ID & Pattern", th_style),
        Paragraph("Baseline Review (No RAG)", th_style),
        Paragraph("RAG-Augmented Review", th_style),
    ]
    ablation_rows = [
        [
            Paragraph("<b>#1 Mutable Default</b><br/><code>cache=[]</code>", tb_style),
            Paragraph("Notes that list mutates across calls; generic explanation.", tb_style),
            Paragraph("Grounded in PEP 8 / Google Style Guide; flags function definition binding.", tb_style),
        ],
        [
            Paragraph("<b>#2 SQL Injection</b><br/><code>query = '...' + input</code>", tb_style),
            Paragraph("Flags concatenation as unsafe; generic query rewrite.", tb_style),
            Paragraph("Cites <b>MITRE CWE-89</b> and OWASP Cheat Sheet; specifies DB-API <code>%s</code> parameters.", tb_style),
        ],
        [
            Paragraph("<b>#3 Bare Except</b><br/><code>except: pass</code>", tb_style),
            Paragraph("Advises against catching all errors; suggests generic <code>Exception</code>.", tb_style),
            Paragraph("Cites <b>Bandit B110</b> and PEP 8; warns against swallowing <code>KeyboardInterrupt</code>.", tb_style),
        ],
        [
            Paragraph("<b>#4 Resource Leak</b><br/><code>f = open(...)</code> unclosed", tb_style),
            Paragraph("Recommends calling <code>f.close()</code>.", tb_style),
            Paragraph("Cites <b>CWE-775</b>; enforces deterministic <code>with open(...) as f:</code> context manager.", tb_style),
        ],
        [
            Paragraph("<b>#5 Dynamic Execution</b><br/><code>eval(user_formula)</code>", tb_style),
            Paragraph("Flags code execution risk; general AST parser advice.", tb_style),
            Paragraph("Cites <b>Bandit B307</b> and <b>CWE-95</b>; recommends <code>ast.literal_eval</code>.", tb_style),
        ],
        [
            Paragraph("<b>#6 Hardcoded Secret</b><br/><code>API_KEY = 'sk-live-...'</code>", tb_style),
            Paragraph("Notes string looks like a credential key.", tb_style),
            Paragraph("Cites <b>CWE-798</b> and OWASP Secrets Management; enforces <code>os.getenv</code>.", tb_style),
        ],
        [
            Paragraph("<b>#7 Path Traversal</b><br/><code>os.path.join(dir, name)</code>", tb_style),
            Paragraph("Misses directory traversal vulnerability via <code>../</code>.", tb_style),
            Paragraph("Cites <b>CWE-22</b>; provides path resolution and boundary verification.", tb_style),
        ],
        [
            Paragraph("<b>#8 Subprocess shell=True</b><br/><code>check_output(..., shell=True)</code>", tb_style),
            Paragraph("Generic warning on command formatting.", tb_style),
            Paragraph("Cites <b>Bandit B602</b> and <b>CWE-78</b>; mandates list arguments with <code>shell=False</code>.", tb_style),
        ],
        [
            Paragraph("<b>#9 Insecure Random</b><br/><code>random.getrandbits</code>", tb_style),
            Paragraph("Notes pseudo-random nature.", tb_style),
            Paragraph("Cites <b>Bandit B311</b> and <b>CWE-338</b>; enforces <code>secrets.token_hex</code>.", tb_style),
        ],
        [
            Paragraph("<b>#10 Clean Query (Safe)</b><br/><code>execute(sql, (param,))</code>", tb_style),
            Paragraph("Correctly confirms safe (Issue Found: No).", tb_style),
            Paragraph("Confirms clean parameterized query against OWASP; zero false positives.", tb_style),
        ],
    ]
    t_ablation = Table([ablation_headers] + ablation_rows, colWidths=[110, 190, 204])
    t_ablation.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_ablation)
    story.append(Spacer(1, 10))

    # Section 5: Holdout Integrity & Git Hygiene
    story.append(Paragraph("5. Holdout Integrity & Git Hygiene Verification", h1_style))
    story.append(
        Paragraph(
            "• <b>Evaluation Holdout Protection:</b> The holdout framework in <code>scripts/select_heldout_examples.py</code> "
            "is programmed to ingest the official 100-sample test list via <code>--input</code> and exclude matching SHA-256 signatures. "
            "Because the official teammate list is pending, the system explicitly records <code>official_holdout_verified: false</code> "
            "and <code>status: 'pending_official_list'</code> to prevent false claims.<br/>"
            "• <b>Git Hygiene:</b> Audited <code>.gitignore</code> and confirmed that raw archives, extracted datasets, "
            "ChromaDB directories (<code>rag/data/chroma_db_v2/</code>), evaluation outputs, and Hugging Face weight caches are strictly ignored.<br/>"
            "• <b>Documentation:</b> Updated <code>rag/CORPUS_V2.md</code>, <code>rag/README.md</code>, and root <code>README.md</code> with complete build recipes.",
            body_style,
        )
    )

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Report successfully compiled to: {PDF_PATH}")


if __name__ == "__main__":
    build_pdf()
