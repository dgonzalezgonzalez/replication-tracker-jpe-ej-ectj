"""Build a double-clickable local dashboard HTML from docs assets/data."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"
INDEX_HTML = DOCS_DIR / "index.html"
OUT_HTML = DOCS_DIR / "index-local.html"
DATA_FILES = [
    "journals.json",
    "papers.json",
    "stats-by-journal.json",
    "stats-by-year.json",
    "stats-overview.json",
]


def _extract_asset_paths(index_html: str) -> tuple[str, str]:
    js_match = re.search(r'src="([^"]+\.js)"', index_html)
    css_match = re.search(r'href="([^"]+\.css)"', index_html)
    if not js_match or not css_match:
        raise RuntimeError("Could not find JS/CSS assets in docs/index.html")
    return js_match.group(1), css_match.group(1)


def _to_local_asset_path(asset_path: str) -> str:
    # If it was built for GitHub Pages absolute path, keep only filename under ./assets.
    filename = Path(asset_path).name
    return f"./assets/{filename}"


def _escape_script_ending(text: str) -> str:
    return text.replace("</script", "<\\/script")


def main() -> int:
    if not INDEX_HTML.exists():
        raise RuntimeError("docs/index.html does not exist. Build frontend first.")

    index_html = INDEX_HTML.read_text(encoding="utf-8")
    js_path, css_path = _extract_asset_paths(index_html)
    js_local = _to_local_asset_path(js_path)
    css_local = _to_local_asset_path(css_path)

    embedded_blocks: list[str] = []
    for filename in DATA_FILES:
        path = DOCS_DIR / "data" / filename
        if not path.exists():
            raise RuntimeError(f"Missing data file: {path}")
        content = _escape_script_ending(path.read_text(encoding="utf-8"))
        block = (
            f'<script type="application/json" id="embedded-{filename}">\n'
            f"{content}\n"
            "</script>"
        )
        embedded_blocks.append(block)

    local_html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Economics Replication Tracker (Local)</title>
    <link rel="stylesheet" href="{css_local}">
    <script>
      (function () {{
        var originalFetch = window.fetch ? window.fetch.bind(window) : null;
        if (!originalFetch) return;

        function embeddedJsonText(name) {{
          var el = document.getElementById("embedded-" + name);
          return el ? el.textContent : null;
        }}

        function responseFromEmbedded(text) {{
          return Promise.resolve(new Response(text, {{
            status: 200,
            headers: {{ "Content-Type": "application/json" }}
          }}));
        }}

        window.fetch = function (input, init) {{
          var rawUrl = typeof input === "string" ? input : (input && input.url);
          if (typeof rawUrl === "string") {{
            try {{
              var u = new URL(rawUrl, window.location.href);
              var match = u.pathname.match(/\\/data\\/([^/?#]+\\.json)$/);
              if (match && match[1]) {{
                var text = embeddedJsonText(match[1]);
                if (text !== null) return responseFromEmbedded(text);
              }}
            }} catch (_err) {{
              // Fall through to native fetch.
            }}
          }}
          return originalFetch(input, init);
        }};
      }})();
    </script>
    {"\n    ".join(embedded_blocks)}
  </head>
  <body>
    <div id="root"></div>
    <script src="{js_local}"></script>
  </body>
</html>
"""

    OUT_HTML.write_text(local_html, encoding="utf-8")
    print(f"Wrote {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
