#!/usr/bin/env python3
"""Render a Markdown file to a self-contained, theme-aware HTML page, and
optionally to PDF.

Exists so the artifact-links rule has something concrete to invoke: a link
handed to a human should point at a rendered document, never at raw markup.

    uv run --with markdown gestalt/tools/render-doc.py NOTES.md
    uv run --with markdown gestalt/tools/render-doc.py NOTES.md --pdf
    uv run --with markdown --with matplotlib render-doc.py NOTES.md --pdf   # with math

Outputs NOTES.html (and NOTES.pdf) beside the source. The HTML inlines all
CSS and uses system font stacks, so it renders identically offline and under
a strict CSP. Print styles force a light palette regardless of screen theme.

MATH. `$$...$$` blocks are typeset to inline SVG by matplotlib's mathtext, so
no LaTeX install and no MathJax CDN is needed and the equations survive into
the PDF (WeasyPrint runs no JavaScript, so a JS typesetter renders nothing).
The glyph fill is rewritten to `currentColor`, so one copy serves light theme,
dark theme and print. matplotlib is imported only if the document contains
display math.

Write INLINE symbols as `<var>b</var>` / `<var>p<sub>z</sub></var>` rather than
`$b$`: they stay real text on the real baseline, selectable and searchable,
and are styled into a math serif by the stylesheet. Reserve `$$` for display
equations, where the baseline-alignment problem does not arise.

Note mathtext is a LaTeX subset. `\\le`, `\\ge` and `\\tfrac` are NOT accepted;
use `\\leq`, `\\geq`, `\\frac`. Unparseable equations are reported by name and
left as literal text rather than failing the whole build.

PDF generation shells out to `weasyprint`, which is optional; if it is
missing the HTML is still written and the script says so rather than failing.
"""

from __future__ import annotations

import argparse
import html as html_mod
import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("needs the `markdown` package: uv run --with markdown ...")


def math_svg(latex: str, fontsize: float = 13.0) -> str | None:
    """Typeset one expression to SVG that inherits the surrounding text colour."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(0.01, 0.01))
    fig.text(0, 0, f"${latex}$", fontsize=fontsize)
    buf = io.StringIO()
    try:
        fig.savefig(buf, format="svg", bbox_inches="tight", pad_inches=0.06,
                    transparent=True)
    except ValueError as exc:                    # mathtext parse failure
        print(f"  math skipped ({latex[:40]}...): "
              f"{str(exc).strip().splitlines()[-1][:90]}", file=sys.stderr)
        return None
    finally:
        plt.close(fig)
    s = buf.getvalue()
    s = s[s.index("<svg"):]
    s = re.sub(r"<metadata>.*?</metadata>", "", s, flags=re.S)
    s = re.sub(r'<g id="patch_1">.*?</g>', "", s, flags=re.S)   # fill:none warning
    return s.replace("<svg ", '<svg fill="currentColor" ', 1).strip()

CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --rule: #d1d9e0;
  --accent: #0969da; --code-bg: #f6f8fa; --quote: #6e7781; --tbl-alt: #f6f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --rule: #3d444d;
    --accent: #4493f8; --code-bg: #151b23; --quote: #9198a1; --tbl-alt: #151b23;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 3rem 1.5rem 6rem; max-width: 46rem;
  background: var(--bg); color: var(--fg);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter,
        Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
h1, h2, h3, h4 { line-height: 1.25; font-weight: 650; margin: 2.2em 0 .6em; }
h1 { font-size: 2.05em; margin-top: 0; letter-spacing: -.02em; }
h2 { font-size: 1.45em; padding-bottom: .3em; border-bottom: 1px solid var(--rule); }
h3 { font-size: 1.16em; }
p, ul, ol { margin: 0 0 1.05em; }
li { margin: .3em 0; }
a { color: var(--accent); text-decoration-thickness: .07em; text-underline-offset: .18em; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.6em 0; }
code {
  font: .875em/1.5 ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
        "Liberation Mono", monospace;
  background: var(--code-bg); padding: .16em .38em; border-radius: 5px;
}
pre {
  background: var(--code-bg); padding: 1rem 1.1rem; border-radius: 8px;
  overflow-x: auto; border: 1px solid var(--rule); margin: 0 0 1.3em;
}
pre code { background: none; padding: 0; font-size: .855em; line-height: 1.55; }
blockquote {
  margin: 0 0 1.2em; padding: .1em 0 .1em 1.1em;
  border-left: 3px solid var(--rule); color: var(--quote);
}
/* wide tables scroll inside their own box; the page never scrolls sideways */
.table-wrap { overflow-x: auto; margin: 0 0 1.4em; }
table { border-collapse: collapse; width: 100%; font-size: .93em; }
th, td { text-align: left; padding: .5em .8em; border: 1px solid var(--rule); vertical-align: top; }
th { font-weight: 620; background: var(--tbl-alt); }
tbody tr:nth-child(even) { background: var(--tbl-alt); }
img { max-width: 100%; height: auto; }
/* inline math: real text on the real baseline, not an image */
var, .m {
  font-family: "STIX Two Math", "Latin Modern Math", Cambria, Georgia,
               "Times New Roman", serif;
  font-style: italic; font-size: 1.07em; letter-spacing: .01em;
}
var sub, var sup, .m sub, .m sup { font-size: .72em; font-style: italic; }
/* display math: svg inheriting currentColor, so one copy serves every theme */
.eq { margin: 1.5em 0; text-align: center; overflow-x: auto; }
.eq svg { height: auto; max-width: 100%; }
figure { margin: 2em 0; }
figure img, figure svg {
  display: block; width: 100%; height: auto;
  border: 1px solid var(--rule); border-radius: 10px;
}
figcaption { color: var(--muted); font-size: .9em; margin-top: .7em; text-align: center; }
/* copy buttons on fenced code blocks (added for copy-paste-heavy pages) */
.pre-wrap { position: relative; }
.copy-btn {
  position: absolute; top: .55rem; right: .55rem;
  font: 600 12px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  padding: .35em .7em; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--rule); background: var(--bg); color: var(--muted);
  opacity: .78; transition: opacity .12s;
}
.copy-btn:hover { opacity: 1; color: var(--fg); }
.copy-btn.done { color: #1a7f37; border-color: #1a7f37; opacity: 1; }
@media print {
  .copy-btn { display: none; }
  :root {
    --bg: #fff; --fg: #111; --muted: #555; --rule: #ccc;
    --accent: #0645ad; --code-bg: #f4f4f4; --quote: #555; --tbl-alt: #f4f4f4;
  }
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  h1, h2, h3, h4 { page-break-after: avoid; }
  pre, blockquote, table, .table-wrap, figure, .eq { page-break-inside: avoid; }
  /* print has no scrollbars: wrap code and table cells instead of clipping them */
  pre, .table-wrap { overflow: visible; }
  pre, pre code { white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
  td, th { overflow-wrap: anywhere; }
  a { color: inherit; text-decoration: none; }
}
@page { size: letter; margin: 0.9in 0.85in; }
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
<script>
/* Copy buttons on every fenced code block. Clipboard API needs a trustworthy
   origin; file:// qualifies in Firefox/Chrome, and the execCommand fallback
   covers anything that refuses. */
document.querySelectorAll("pre").forEach(function (pre) {{
  var code = pre.querySelector("code") || pre;
  var wrap = document.createElement("div");
  wrap.className = "pre-wrap";
  pre.parentNode.insertBefore(wrap, pre);
  wrap.appendChild(pre);
  var btn = document.createElement("button");
  btn.type = "button"; btn.className = "copy-btn"; btn.textContent = "Copy";
  btn.addEventListener("click", function () {{
    var text = code.innerText.replace(/\\n$/, "");
    var ok = function () {{
      btn.textContent = "Copied!"; btn.classList.add("done");
      setTimeout(function () {{
        btn.textContent = "Copy"; btn.classList.remove("done");
      }}, 1600);
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(ok, function () {{ fallback(); ok(); }});
    }} else {{ fallback(); ok(); }}
    function fallback() {{
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }}
  }});
  wrap.appendChild(btn);
}});
</script>
</body>
</html>
"""


def first_heading(md_text: str, fallback: str) -> str:
    for line in md_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def out_dir_for(src: Path, out_dir: Path | None) -> Path:
    """Where the render lands. Never /tmp.

    A file:///tmp/... link fails to open in snap- and Flatpak-packaged viewers:
    they run in a mount namespace with their own private /tmp, so the file is
    invisible to them even when it is present and world-readable. Since agents
    are steered to keep working files in a /tmp scratchpad, rendering beside the
    source would routinely mint dead links. Anything outside $HOME is therefore
    redirected to ~/artifacts/<stem>/.
    """
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir
    home = Path.home().resolve()
    parent = src.resolve().parent
    if parent == home or home in parent.parents:
        return parent
    safe = home / "artifacts" / src.stem
    safe.mkdir(parents=True, exist_ok=True)
    print(f"  source is outside $HOME; rendering to {safe} so the link opens",
          file=sys.stderr)
    return safe


def render(src: Path, want_pdf: bool, out_dir: Path | None = None) -> list[Path]:
    md_text = src.read_text(encoding="utf-8")

    # display math -> svg, before markdown sees it
    if "$$" in md_text:
        def one(m):
            svg = math_svg(m.group(1).strip())
            return f'<div class="eq">{svg}</div>' if svg else m.group(0)
        md_text = re.sub(r"\$\$(.+?)\$\$", one, md_text, flags=re.S)

    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "footnotes"],
        output_format="html",
    )
    # let wide tables scroll on their own instead of forcing the page sideways
    body = body.replace("<table>", '<div class="table-wrap"><table>')
    body = body.replace("</table>", "</table></div>")

    out_html = out_dir_for(src, out_dir) / f"{src.stem}.html"
    out_html.write_text(
        PAGE.format(
            title=html_mod.escape(first_heading(md_text, src.stem)),
            css=CSS,
            body=body,
        ),
        encoding="utf-8",
    )
    written = [out_html]

    if want_pdf:
        exe = shutil.which("weasyprint")
        if not exe:
            print("weasyprint not found; wrote HTML only", file=sys.stderr)
        else:
            out_pdf = out_html.with_suffix(".pdf")
            proc = subprocess.run(
                [exe, str(out_html), str(out_pdf)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                print(f"weasyprint failed:\n{proc.stderr}", file=sys.stderr)
            else:
                written.append(out_pdf)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="path to a .md file")
    ap.add_argument("--pdf", action="store_true", help="also emit a PDF")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="where to write (default: beside the source, or "
                         "~/artifacts/<stem>/ if the source is outside $HOME)")
    args = ap.parse_args()

    if not args.source.is_file():
        sys.exit(f"no such file: {args.source}")

    for p in render(args.source, args.pdf, args.out_dir):
        print(f"file://{p.resolve()}")


if __name__ == "__main__":
    main()
