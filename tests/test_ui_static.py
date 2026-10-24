"""The dashboard shell and assets: routing, CSP-compatibility and XSS guardrails."""
import re
from pathlib import Path

import pytest

from .conftest import build_client

STATIC = Path(__file__).resolve().parent.parent / "app" / "ui" / "static"
SECTIONS = ["overview", "metrics", "traces", "incidents", "scenarios", "infrastructure"]


def static_files(*suffixes):
    return sorted(p for p in STATIC.rglob("*") if p.is_file() and p.suffix in suffixes)


def test_root_redirects_to_the_dashboard():
    r = build_client().get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"


@pytest.mark.parametrize("path", ["/ui", "/ui/"] + [f"/ui/{s}" for s in SECTIONS])
def test_every_section_serves_the_same_shell(path):
    r = build_client().get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "<title>TraceGuard</title>" in r.text


@pytest.mark.parametrize("path", ["/ui/nope", "/ui/static", "/ui/overview/extra", "/ui/..%2Fconfig.py", "/ui/%2e%2e/main.py"])
def test_unknown_sections_are_404(path):
    assert build_client().get(path).status_code == 404


@pytest.mark.parametrize("path", ["/ui/static/../../config.py", "/ui/static/%2e%2e/%2e%2e/config.py", "/ui/static/..%2f..%2fmain.py",
                                  "/ui/static/js/../../../main.py", "/ui/static//etc/passwd", "/ui/static/..\\..\\config.py"])
def test_static_path_traversal_is_blocked(path):
    r = build_client().get(path)
    assert r.status_code in (400, 404) and "create_app" not in r.text and "class Settings" not in r.text


def test_security_headers_on_the_ui_and_api():
    c = build_client()
    ui = c.get("/ui/")
    csp = ui.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp
    assert ui.headers["x-frame-options"] == "DENY" and ui.headers["x-content-type-options"] == "nosniff"
    assert ui.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in c.get("/health").headers   # CSP is for the UI, not the probes


def test_index_has_no_inline_script_style_or_event_handlers():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.DOTALL)
    assert scripts, "expected at least one module script"
    for attrs, body in scripts:
        assert "src=" in attrs and body.strip() == "", "inline script found"
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html) and not re.search(r"\son[a-z]+\s*=", html)


def test_no_external_hosts_anywhere_in_the_assets():
    for path in static_files(".html", ".css", ".js"):
        text = path.read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")
        assert not re.search(r"https?://", text), f"{path.name} references an external host"
        assert "@import" not in text and "url(" not in text, f"{path.name} loads external resources"


def test_javascript_never_writes_html_or_evaluates_strings():
    banned = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout('", 'setTimeout("', "srcdoc"]
    for path in static_files(".js"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} uses {token}"


def test_module_imports_resolve_to_existing_files():
    imports = re.compile(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""")
    for path in static_files(".js"):
        for rel in imports.findall(path.read_text(encoding="utf-8")):
            assert (path.parent / rel).resolve().is_file(), f"{path.name} imports missing {rel}"
    pages = {p.stem for p in (STATIC / "js" / "pages").glob("*.js")}
    assert pages == set(SECTIONS)


def test_each_page_module_exports_render():
    for section in SECTIONS:
        assert re.search(r"export\s+(async\s+)?function\s+render\b", (STATIC / "js" / "pages" / f"{section}.js").read_text(encoding="utf-8"))


def test_every_data_source_has_a_visible_label_helper():
    dom = (STATIC / "js" / "dom.js").read_text(encoding="utf-8")
    for label in ("LIVE", "LIVE · SANDBOX", "RECORDED", "NOT AVAILABLE"):
        assert label in dom


def test_layout_rules_that_prevent_horizontal_overflow_on_phones():
    """Regression: a 1fr grid column let the six-link nav widen the whole page to 624 px on a 390 px screen."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert css.count("minmax(0, 1fr)") >= 2                      # desktop and mobile grid columns
    assert re.search(r"\.side,\s*\.content,\s*nav\s*\{\s*min-width:\s*0", css)


def test_status_badges_keep_a_gap_before_the_text_that_follows_them():
    """Regression: 'Business outcome mismatch:the response ...' (badge text ran into the sentence)."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    rule = re.search(r"\.status\s*\{([^}]*)\}", css).group(1)
    assert "margin-right" in rule
    assert not re.search(r"\.status:(last-child|empty)", css)    # a :last-child reset silently cancelled the gap


def test_index_declares_an_inline_favicon_so_browsers_do_not_request_a_missing_one():
    assert '<link rel="icon" href="data:,">' in (STATIC / "index.html").read_text(encoding="utf-8")


def test_static_assets_have_correct_content_types():
    c = build_client()
    assert c.get("/ui/static/styles.css").headers["content-type"].startswith("text/css")
    assert "javascript" in c.get("/ui/static/js/charts.js").headers["content-type"]
