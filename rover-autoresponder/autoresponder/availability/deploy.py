"""Addendum D / D3 — upload the static availability page to the public R2 bucket.

    python -m autoresponder.availability.deploy [path/to/index.html]

Defaults to availability-front-end-design/index.html. Idempotent: overwrites the page at its
stable key (AVAIL_R2_PUBLIC_KEY_HTML), served at the domain root. The FEED (availability.json)
is published continuously by the publisher thread inside rover-sms.service — this command is
only for the page itself, which changes rarely, so it's a manual deploy, not a service.
"""
import os
import sys

from .. import config
from . import hosting

DEFAULT_PAGE = os.path.join(os.path.dirname(config._HERE),
                            "availability-front-end-design", "index.html")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = os.path.abspath(argv[0]) if argv else DEFAULT_PAGE
    if not os.path.exists(path):
        raise SystemExit(f"page not found: {path}")
    if not (config.AVAIL_R2_BUCKET and config.AVAIL_PUBLIC_BASE_URL):
        raise SystemExit("R2 public hosting not configured — set AVAIL_R2_BUCKET and "
                         "AVAIL_PUBLIC_BASE_URL in .env first")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    url = hosting.publish_page(html)
    print(f"published {path}\n     -> {url}")
    print(f"     -> {config.AVAIL_PUBLIC_BASE_URL.rstrip('/')}/   (root)")


if __name__ == "__main__":
    main()
