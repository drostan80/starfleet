#!/bin/sh
# Deploy web client to the remote server.
#
# The UI is now baked into both Docker images (starfleet:<ver> serves it
# via LCARS at /ui/, starfleet:<ver>-web serves it via nginx). This
# script is no longer needed for production — tag a release and deploy
# with the normal starfleet deploy process instead.
#
# Kept for local dev: serves ui/src/ on localhost:3000.
set -e
cd "$(dirname "$0")/src"
echo "Production deploy: tag a release (the UI is baked into the Docker images now)."
echo "For local dev, serving on http://localhost:3000/..."
python3 -m http.server 3000
