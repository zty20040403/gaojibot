from __future__ import annotations

import argparse
import os
import html
import re
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

REGULAR_FONT = os.environ.get("GAOJI_PDF_FONT", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
FONT_NAME = "gaojiCJK"
BOLD_NAME = FONT_NAME


def inline_markup(value: str) -> str:
    escaped = html.escape(value.strip())
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<font name='gaojiCJK'>\1</font>", escaped)
    return escaped


def markdown_story(text: str, styles: dict[str, ParagraphStyle]):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    story = []
    paragraph: list[str] = []
    bullets: list[str] = []
    code: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(item.strip() for item in paragraph if item.strip())
            if joined:
                story.append(Paragraph(inline_markup(joined), styles["BodyCJK"]))
                story.append(Spacer(1, 2.4 * mm))
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            items = [
                Paragraph("• " + inline_markup(item), styles["BulletCJK"])
                for item in bullets
            ]
            story.append(KeepTogether(items))
            story.append(Spacer(1, 2.4 * mm))
            bullets.clear()

    def flush_code() -> None:
        if code:
            rendered = "<br/>".join(html.escape(item).replace(" ", "&nbsp;") for item in code)
            story.append(Paragraph(rendered or " ", styles["CodeCJK"]))
            story.append(Spacer(1, 2.8 * mm))
            code.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_bullets()
            if in_code:
                flush_code()
            in_code = not in_code
            index += 1
            continue
        if in_code:
            code.append(line)
            index += 1
            continue
        if not stripped:
            flush_paragraph()
            flush_bullets()
            index += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_bullets()
            level = len(heading.group(1))
            story.append(Paragraph(inline_markup(heading.group(2)), styles[f"H{level}CJK"]))
            story.append(Spacer(1, (4 - level) * mm))
            index += 1
            continue
        if re.match(r"^[-*+]\s+", stripped):
            flush_paragraph()
            bullets.append(re.sub(r"^[-*+]\s+", "", stripped))
            index += 1
            continue
        if "|" in stripped and index + 1 < len(lines) and re.match(
            r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$",
            lines[index + 1],
        ):
            flush_paragraph()
            flush_bullets()
            rows: list[list[str]] = []
            header = [part.strip() for part in stripped.strip("|").split("|")]
            rows.append(header)
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append([part.strip() for part in lines[index].strip().strip("|").split("|")])
                index += 1
            width = max(len(row) for row in rows)
            normalized = [row + [""] * (width - len(row)) for row in rows]
            cells = [
                [Paragraph(inline_markup(cell), styles["TableCJK"]) for cell in row]
                for row in normalized
            ]
            table = Table(cells, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), BOLD_NAME),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF2")),
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#AAB7C0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(table)
            story.append(Spacer(1, 3 * mm))
            continue
        paragraph.append(line)
        index += 1

    flush_paragraph()
    flush_bullets()
    flush_code()
    return story


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a CJK-safe PDF from UTF-8 Markdown.")
    parser.add_argument("input", help="UTF-8 Markdown input")
    parser.add_argument("output", help="PDF output path")
    parser.add_argument("--title", default="", help="PDF metadata title")
    args = parser.parse_args()

    source = Path(args.input)
    output = Path(args.output)
    text = source.read_text(encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)

    pdfmetrics.registerFont(TTFont(FONT_NAME, REGULAR_FONT))
    pdfmetrics.registerFontFamily(
        FONT_NAME,
        normal=FONT_NAME,
        bold=BOLD_NAME,
        italic=FONT_NAME,
        boldItalic=BOLD_NAME,
    )

    base = getSampleStyleSheet()
    styles = {
        "BodyCJK": ParagraphStyle(
            "BodyCJK", parent=base["BodyText"], fontName=FONT_NAME,
            fontSize=10.5, leading=17, wordWrap="CJK", textColor=colors.HexColor("#182026"),
        ),
        "BulletCJK": ParagraphStyle(
            "BulletCJK", parent=base["BodyText"], fontName=FONT_NAME,
            fontSize=10.5, leading=17, leftIndent=5 * mm, firstLineIndent=-4 * mm, wordWrap="CJK",
        ),
        "H1CJK": ParagraphStyle(
            "H1CJK", parent=base["Heading1"], fontName=BOLD_NAME,
            fontSize=20, leading=27, wordWrap="CJK", textColor=colors.HexColor("#111827"),
        ),
        "H2CJK": ParagraphStyle(
            "H2CJK", parent=base["Heading2"], fontName=BOLD_NAME,
            fontSize=15, leading=22, wordWrap="CJK", textColor=colors.HexColor("#1F2937"),
        ),
        "H3CJK": ParagraphStyle(
            "H3CJK", parent=base["Heading3"], fontName=BOLD_NAME,
            fontSize=12, leading=19, wordWrap="CJK", textColor=colors.HexColor("#374151"),
        ),
        "CodeCJK": ParagraphStyle(
            "CodeCJK", parent=base["Code"], fontName=FONT_NAME,
            fontSize=8.5, leading=13, borderColor=colors.HexColor("#CBD5E1"),
            borderWidth=0.5, borderPadding=8, backColor=colors.HexColor("#F8FAFC"), wordWrap="CJK",
        ),
        "TableCJK": ParagraphStyle(
            "TableCJK", parent=base["BodyText"], fontName=FONT_NAME,
            fontSize=8.5, leading=13, wordWrap="CJK",
        ),
    }
    title = args.title.strip() or source.stem
    document = SimpleDocTemplate(
        str(output), pagesize=A4, title=title,
        initialFontName=FONT_NAME,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawCentredString(A4[0] / 2, 8 * mm, str(doc.page))
        canvas.restoreState()

    story = markdown_story(text, styles)
    if not story:
        story = [Paragraph("（空文档）", styles["BodyCJK"])]
    document.build(story, onFirstPage=footer, onLaterPages=footer)

    reader = PdfReader(str(output))
    extracted = "".join(page.extract_text() or "" for page in reader.pages).strip()
    if not reader.pages or (text.strip() and not extracted):
        output.unlink(missing_ok=True)
        raise SystemExit("PDF verification failed: no extractable text")
    print(f"created {output} ({len(reader.pages)} page(s), embedded CJK font)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
