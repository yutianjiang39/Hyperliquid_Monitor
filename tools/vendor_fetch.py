#!/usr/bin/env python3
"""Download the report's third-party front-end files once, pinned by version.

The report is opened from file:// and must work offline, so the libraries are
stored in src/vendor/ and copied next to each report. ``manifest.json`` records
the sha256 of every file; ``--verify`` checks the local copies without network.

    python tools/vendor_fetch.py            # download missing files
    python tools/vendor_fetch.py --force    # re-download everything
    python tools/vendor_fetch.py --verify   # offline integrity check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "src" / "vendor"
FILES = {
    "tabler.min.css": ("@tabler/core", "1.4.0", "dist/css/tabler.min.css"),
    "tabler.min.js": ("@tabler/core", "1.4.0", "dist/js/tabler.min.js"),
    "echarts.min.js": ("echarts", "5.6.0", "dist/echarts.min.js"),
    "tabulator.min.js": ("tabulator-tables", "6.3.1", "dist/js/tabulator.min.js"),
    "tabulator_bootstrap5.min.css": ("tabulator-tables", "6.3.1", "dist/css/tabulator_bootstrap5.min.css"),
    # Self-hosted UI / data fonts (SIL OFL 1.1); Latin subset only, CJK falls back to system fonts.
    "inter-latin-wght.woff2": ("@fontsource-variable/inter", "5.2.5", "files/inter-latin-wght-normal.woff2"),
    "inter.LICENSE.txt": ("@fontsource-variable/inter", "5.2.5", "LICENSE"),
    "jetbrains-mono-latin-wght.woff2": ("@fontsource-variable/jetbrains-mono", "5.2.5", "files/jetbrains-mono-latin-wght-normal.woff2"),
    "jetbrains-mono.LICENSE.txt": ("@fontsource-variable/jetbrains-mono", "5.2.5", "LICENSE"),
}
MIRRORS = (
    "https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{path}",
    "https://unpkg.com/{pkg}@{ver}/{path}",
    "https://registry.npmmirror.com/{pkg}/{ver}/files/{path}",
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify():
    manifest = json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))
    bad = [name for name, item in manifest["files"].items()
           if not (VENDOR / name).is_file() or sha256((VENDOR / name).read_bytes()) != item["sha256"]]
    for name in bad:
        print("校验失败：", name)
    return not bad


def fetch(force=False):
    import requests
    VENDOR.mkdir(parents=True, exist_ok=True)
    path = VENDOR / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"files": {}}
    for name, (pkg, ver, sub) in FILES.items():
        target = VENDOR / name
        known = manifest["files"].get(name)
        if target.is_file() and known and known.get("version") == ver and not force:
            continue
        for mirror in MIRRORS:
            url = mirror.format(pkg=pkg, ver=ver, path=sub)
            try:
                response = requests.get(url, timeout=60)
                if response.status_code == 200 and len(response.content) > 500:
                    break
            except requests.RequestException:
                continue
        else:
            raise SystemExit(f"无法下载 {pkg}@{ver}/{sub}")
        target.write_bytes(response.content)
        manifest["files"][name] = {"package": pkg, "version": ver, "path": sub, "url": url,
                                   "sha256": sha256(response.content), "bytes": len(response.content)}
        print(f"{name}: {pkg}@{ver} {len(response.content):,} bytes")
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--force", action="store_true")
    args.add_argument("--verify", action="store_true")
    ns = args.parse_args()
    if ns.verify:
        sys.exit(0 if verify() else 1)
    fetch(ns.force)
    sys.exit(0 if verify() else 1)
