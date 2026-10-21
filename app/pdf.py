"""PDF version of a finished audit report, for forwarding to a client or a manager.

Built with fpdf2 (pure Python, no browser or system libraries). DejaVu Sans is
used when the system has it, so page titles in Latin, Greek and Cyrillic print correctly
(scripts DejaVu lacks, such as Devanagari, show as blanks);
otherwise it falls back to the built-in Helvetica, which only covers Latin-1.
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu",)
LEVEL_RGB = {"Critical": (185, 28, 28), "Warning": (180, 83, 9), "Notice": (71, 85, 105)}
MAX_URLS_PER_ISSUE = 10


class _Doc(FPDF):
    def __init__(self, site: str):
        super().__init__(format="A4")
        self.site = site
        self.unicode = False
        for d in FONT_DIRS:
            regular, bold = Path(d, "DejaVuSans.ttf"), Path(d, "DejaVuSans-Bold.ttf")
            if regular.exists() and bold.exists():
                self.add_font("Body", "", str(regular))
                self.add_font("Body", "B", str(bold))
                self.unicode = True
                break
        self.body_font = "Body" if self.unicode else "Helvetica"
        self.set_auto_page_break(True, margin=15)
        self.set_margins(15, 15, 15)

    def safe(self, text) -> str:
        text = "" if text is None else str(text)
        if self.unicode:
            return text
        for a, b in (("·", "-"), ("•", "-"), ("…", "..."), ("—", "-"), ("–", "-")):
            text = text.replace(a, b)
        return text.encode("latin-1", "replace").decode("latin-1")

    def footer(self):
        self.set_y(-12)
        self.set_font(self.body_font, "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, self.safe(f"SiteScope audit of {self.site} · page {self.page_no()}"), align="C")

    def heading(self, text: str, size: int = 13):
        self.ln(3)
        self.set_font(self.body_font, "B", size)
        self.set_text_color(15, 23, 42)
        self.multi_cell(0, 7, self.safe(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def para(self, text: str, size: int = 10, rgb=(30, 41, 59)):
        self.set_font(self.body_font, "", size)
        self.set_text_color(*rgb)
        self.multi_cell(0, 5, self.safe(text), new_x="LMARGIN", new_y="NEXT")


def build_pdf(row: dict, r: dict, comparison: dict | None = None) -> bytes:
    doc = _Doc(row["url"])
    doc.add_page()

    doc.set_font(doc.body_font, "B", 18)
    doc.set_text_color(15, 23, 42)
    doc.multi_cell(0, 9, doc.safe(f"SEO audit: {row['url']}"), new_x="LMARGIN", new_y="NEXT")
    doc.para(f"Finished {str(r.get('finished_at', ''))[:16].replace('T', ' ')} UTC · "
          f"{r.get('pages_crawled', 0)} pages crawled · {r.get('sitemap_urls', 0)} URLs in sitemap · "
          f"{len(r.get('robots_blocked', []))} blocked by robots.txt", size=9, rgb=(100, 116, 139))

    c = r.get("counts", {})
    doc.heading("Summary")
    doc.para(f"Health score: {r.get('health_score')} / 100")
    doc.para(f"Critical: {c.get('Critical', 0)}   Warnings: {c.get('Warning', 0)}   Notices: {c.get('Notice', 0)}")

    if comparison:
        doc.heading("Compared with the previous audit")
        doc.para(f"Health score {comparison['health_before']} -> {comparison['health_after']} "
              f"({comparison['health_delta']:+d}). Pages crawled {comparison['pages_before']} -> "
              f"{comparison['pages_after']}.")
        if comparison["fixed"]:
            doc.para("Fixed: " + "; ".join(f"{i['title']} ({i['pages']} pages)" for i in comparison["fixed"]))
        if comparison["new"]:
            doc.para("New: " + "; ".join(f"{i['title']} ({i['pages']} pages)" for i in comparison["new"]))
        for ch in comparison["changed"]:
            doc.para(f"{ch['title']}: {ch['before']} -> {ch['after']} pages")

    doc.heading("Issues by priority")
    issues = r.get("issues", [])
    if not issues:
        doc.para("No issues found.")
    for i in issues:
        doc.set_font(doc.body_font, "B", 10)
        doc.set_text_color(*LEVEL_RGB.get(i["level"], (30, 41, 59)))
        doc.multi_cell(0, 5, doc.safe(f"[{i['level']}] {i['title']}"), new_x="LMARGIN", new_y="NEXT")
        doc.para(f"{i['pages_affected']} pages · {i['pct_pages']}% of the site · priority {i['priority']}",
              size=9, rgb=(100, 116, 139))
        doc.para(f"How to fix: {i['fix']}", size=9)
        for pg in i["pages"][:MAX_URLS_PER_ISSUE]:
            line = pg["url"] + (f" — {pg['detail']}" if pg.get("detail") else "")
            doc.para("  • " + line, size=8, rgb=(51, 65, 85))
        if i["pages_affected"] > MAX_URLS_PER_ISSUE:
            doc.para(f"  … and {i['pages_affected'] - MAX_URLS_PER_ISSUE} more (see the HTML report)",
                  size=8, rgb=(100, 116, 139))
        doc.ln(2)

    cwv = r.get("core_web_vitals", {})
    doc.heading("Core Web Vitals (homepage)")
    if cwv.get("available"):
        doc.para(f"Source: {cwv.get('source')} · performance score {cwv.get('performance_score')} · "
              f"LCP {cwv.get('lcp_ms')} ms · INP {cwv.get('inp_ms')} ms · CLS {cwv.get('cls')}")
    else:
        doc.para(f"Not available ({cwv.get('reason', 'unknown')}).")

    g = r.get("link_graph", {})
    doc.heading("Internal links")
    doc.para(f"{g.get('nodes', 0)} pages, {g.get('edges', 0)} internal links, deepest page "
          f"{g.get('max_depth', 0)} clicks from the start URL, {len(g.get('orphans', []))} orphan pages.")

    return bytes(doc.output())
