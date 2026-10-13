"""Write the demo's index.html as a body fragment, for a host that wraps a page in its own document.

Some static hosts (a claude.ai artifact among them) add the doctype, ``<html>``, ``<head>`` and
``<body>`` themselves, with a charset and viewport meta. This keeps everything else from the
built page in order: the title, the theme bootstrap script, the font preloads, the stylesheet and
module script links and the root element. Usage: ``python fragment.py IN.html OUT.html``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def fragment(html: str) -> str:
    head = re.search(r"<head>(.*?)</head>", html, re.S | re.I)
    body = re.search(r"<body[^>]*>(.*?)</body>", html, re.S | re.I)
    if not head or not body:
        raise SystemExit("index.html has no <head> or <body>")
    inner = head.group(1)
    inner = re.sub(r"\s*<meta\s+(charset|name=\"viewport\")[^>]*>", "", inner, flags=re.I)
    return f"{inner.strip()}\n{body.group(1).strip()}\n"


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(fragment(src.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {dst}")
