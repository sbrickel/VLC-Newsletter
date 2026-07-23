#!/bin/bash
# Extracts Playwright Chromium's required shared libraries WITHOUT root.
#
# `playwright install --with-deps chromium` (used in the GitHub Actions
# workflow) needs sudo to apt-install these system libraries, which isn't
# available in every local dev environment. This downloads the same .deb
# packages and extracts them into .playwright-libs/ instead of installing
# them system-wide — scraper.py auto-detects this folder and points
# LD_LIBRARY_PATH at it before launching the browser. Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."
LIBDIR=".playwright-libs"
mkdir -p "$LIBDIR/debs"
cd "$LIBDIR/debs"

PACKAGES="libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2"

for pkg in $PACKAGES; do
  apt-get download "$pkg"
done

cd ..
mkdir -p extracted
for f in debs/*.deb; do
  dpkg -x "$f" extracted/
done
rm -rf debs

echo "Done. Libraries extracted to $LIBDIR/extracted/"
