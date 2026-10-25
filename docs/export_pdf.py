"""Render docs/final-documentation.md to an A4 PDF with headless Chrome.

    .venv/bin/python docs/export_pdf.py
"""
import subprocess
from pathlib import Path

import markdown

HERE = Path(__file__).parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CSS = """
@page { size: A4; margin: 14mm 15mm; }
body { font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif; font-size: 9.6pt; line-height: 1.38; color: #111; }
h1 { font-size: 15pt; margin: 0 0 4px; } h2 { font-size: 11.5pt; margin: 11px 0 4px; border-bottom: 1px solid #ccc; }
p, ul { margin: 4px 0; } li { margin: 1px 0; }
table { border-collapse: collapse; width: 100%; margin: 5px 0; font-size: 8.8pt; }
th, td { border: 1px solid #bbb; padding: 2px 5px; text-align: left; vertical-align: top; } th { background: #f0f0f0; }
code { font-family: Menlo, monospace; font-size: 8.4pt; background: #f4f4f4; padding: 0 2px; }
em { color: #8a4b00; }
"""

html = markdown.markdown((HERE / "final-documentation.md").read_text(), extensions=["tables"])
src = HERE / "final-documentation.html"
src.write_text(f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html}</body></html>")
pdf = HERE / "final-documentation.pdf"
subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf}", src.as_uri()],
               check=True, capture_output=True)
src.unlink()
print(f"wrote {pdf}")
