#!/usr/bin/env python3
"""Render the build-log diagrams to 2x PNGs for screen capture.

    python3 render_diagrams.py                  # every *_diagram.html here
    python3 render_diagrams.py foo_diagram.html

Each diagram's `.sheet` element is captured directly rather than the whole
page, so the PNG is exactly the 16:9 card with no browser padding around it —
drop it on a slide or a video track and it fills the frame.

Light only. The dark renders were dropped in #12: only the light ones were ever
used for capture, and they were 2.5 MB of binary in the repo for nothing.
"""

import pathlib
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed. Try: pip3 install playwright && playwright install chromium")

VIEWPORT = {"width": 1600, "height": 1000}


def main():
    args = [pathlib.Path(a) for a in sys.argv[1:]]
    targets = args or sorted(pathlib.Path(".").glob("*_diagram.html"))
    if not targets:
        sys.exit("no *_diagram.html found")

    missing = [t for t in targets if not t.exists()]
    if missing:
        sys.exit("missing: " + ", ".join(str(m) for m in missing))

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception:
            browser = pw.chromium.launch()
        ctx = browser.new_context(device_scale_factor=2, viewport=VIEWPORT)
        for html in targets:
            page = ctx.new_page()
            page.goto(html.resolve().as_uri())
            page.wait_for_timeout(350)  # let webfonts settle
            out = html.with_suffix(".png")
            page.locator(".sheet").screenshot(path=str(out))
            box = page.locator(".sheet").bounding_box()
            print(f"  {out.name}  {int(box['width'])}x{int(box['height'])} css")
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
