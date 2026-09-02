#!/usr/bin/env bash
# Build an installable QGIS plugin ZIP.
#
# The archive contains a single top-level invest_qgis/ directory, which is what
# QGIS's "Install from ZIP" expects.  Install with:
#   Plugins > Manage and Install Plugins > Install from ZIP
set -euo pipefail

cd "$(dirname "$0")"

version=$(sed -n 's/^version=//p' invest_qgis/metadata.txt | tr -d '[:space:]')
output="invest_qgis-${version}.zip"

rm -f "$output"
# Exclude caches and the harvested spec fixtures, which are test data.
zip -r -q "$output" invest_qgis \
    -x '*__pycache__*' -x '*.pyc' -x '*.DS_Store'

echo "$output"
unzip -l "$output" | tail -3
