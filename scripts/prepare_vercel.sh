#!/bin/sh
# Copy the static site into site/ for Vercel. Does not install Python packages.
set -eu
cd "$(dirname "$0")/.."
rm -rf site
mkdir -p site/data/overlays
cp -R frontend/. site/
cp -R data/geojson site/data/geojson
cp -R data/overlays/clipped site/data/overlays/clipped
echo "Prepared site/ for Vercel"
