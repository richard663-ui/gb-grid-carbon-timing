"""Build the working paper: Markdown -> DOCX (pandoc) -> table styling -> PDF (LibreOffice).

Run from the paper/ folder:  python build.py working_paper.md
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.shared import Pt


def style_tables(path: Path, size: float = 8.5) -> None:
    doc = Document(str(path))
    for table in doc.tables:
        table.autofit = True
        for row in table.rows:
            for cell in row.cells:
                for par in cell.paragraphs:
                    par.paragraph_format.space_after = Pt(0)
                    for run in par.runs:
                        run.font.size = Pt(size)
    doc.save(str(path))


def main(md: str) -> None:
    src = Path(md)
    docx = src.with_suffix(".docx")
    subprocess.run(["pandoc", str(src), "-o", str(docx), "--resource-path=."], check=True)
    style_tables(docx)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", str(docx)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"built {docx} and {docx.with_suffix('.pdf')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "working_paper.md")
