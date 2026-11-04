#!/usr/bin/env python3
"""Build index.html = the tracker + the state-feed adapter.

tracker.html is the untouched election-night build. This injects the adapter
ahead of the app bundle so the tracker reads the state's mirrored results
instead of civicAPI, without changing a line of the tracker itself.

Re-runnable: it always builds from tracker.html, never from its own output.
"""
import os, sys, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKER = os.path.join(ROOT, "tracker.html")
SHIM = os.path.join(ROOT, "tools", "state_feed_shim.js")
OUT = os.path.join(ROOT, "index.html")

MARK = "<!-- state-feed adapter -->"


def main():
    with open(TRACKER, encoding="utf-8") as f:
        html = f.read()
    with open(SHIM, encoding="utf-8") as f:
        shim = f.read()

    if MARK in html:
        print("tracker.html already carries the adapter — refusing to double-inject",
              file=sys.stderr)
        return 1

    block = "%s\n<script>\n%s\n</script>\n" % (MARK, shim)

    # Ahead of the first script the page runs, so the patched fetch is in place
    # before the app's first request.
    m = re.search(r"<script\b", html)
    if not m:
        print("no <script> tag found in tracker.html", file=sys.stderr)
        return 1
    out = html[:m.start()] + block + html[m.start():]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out)

    print("wrote %s (%d bytes; tracker %d + adapter %d)"
          % (os.path.relpath(OUT, ROOT), len(out), len(html), len(shim)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
