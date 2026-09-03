#!/bin/sh
# Deploy web client to the remote server.
# The nginx container on tiny@192.168.0.152 bind-mounts
# /home/tiny/repos/web/src:/ui, so copying the files is enough — no
# container restart needed.
set -e
cd "$(dirname "$0")/src"
tar czf - . | ssh tiny@192.168.0.152 "cd /home/tiny/repos/web/src && tar xzf -"
echo "deployed → http://192.168.0.152:8888/ui/index.html"
