from pathlib import Path
import markdown
from jinja2 import Template

# Paths
MD_INPUT = Path("outputs/by_community/nm-albuquerque/nm-albuquerque_growth_mode2_2026-05-22.md")
HTML_OUTPUT = Path("outputs/by_community/nm-albuquerque/nm-albuquerque_growth_mode2.html")
PDF_OUTPUT = Path("outputs/by_community/nm-albuquerque/nm-albuquerque_growth_mode2.pdf")
TEMPLATE_FILE = Path("templates/strategic_brief.html.j2")

# Read Markdown
md_text = MD_INPUT.read_text()
html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])

# Load template
template_text = TEMPLATE_FILE.read_text()
template = Template(template_text)

# Fill template
html_content = template.render(
    title="Albuquerque, NM — Strategic Brief",
    preset="Growth",
    score="5.3/10",
    confidence="MODERATE",
    classification="WATCHLIST / MONITOR",
    data_date="2024-11-01",
    generated_date="2026-05-22",
    body=html_body
)

# Write HTML
HTML_OUTPUT.write_text(html_content)
print(f"[OK] HTML written to {HTML_OUTPUT}")

# Optional: PDF with Playwright
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{HTML_OUTPUT.resolve()}")
        page.pdf(path=str(PDF_OUTPUT), format="A4")
        browser.close()
    print(f"[OK] PDF written to {PDF_OUTPUT}")
except ImportError:
    print("Playwright not installed. HTML generated but PDF skipped.")
