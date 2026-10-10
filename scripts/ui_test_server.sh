#!/usr/bin/env bash
# UI TEST SERVER (RULEBOOK §7: any UI change is validated by the user here BEFORE a tag / CI).
#
#   scripts/ui_test_server.sh start   snapshot prod -> migrate -> fill Jellyfin ids -> serve on :8891
#   scripts/ui_test_server.sh stop    stop the server and the Jellyfin forward
#
# It serves ui/src against a COPY of the production database: real data, real reads; every external
# WRITE is captured (nothing reaches Sonarr, Radarr, AniList, MAL or Jellyfin) and the list tokens
# are blanked. The user logs in with their normal account. A temporary forward makes :8096 on this
# machine reach tiny's Jellyfin, because the UI rewrites link hosts to the page's own host.
# Open: http://<this machine's LAN IP>:8891/ui/   (e.g. http://192.168.1.33:8891/ui/)
set -euo pipefail
cd "$(dirname "$0")/.."
T=/tmp/ui-test; TINY=tiny@192.168.1.77; DB=$T/lcars-test.db
stop() {
  for f in server forward; do
    [ -f $T/$f.pid ] && kill "$(cat $T/$f.pid)" 2>/dev/null || true
    rm -f $T/$f.pid
  done
}
case "${1:-start}" in
  stop) stop; echo "stopped"; exit 0;;
  start) ;;
  *) echo "usage: $0 start|stop"; exit 1;;
esac
stop; mkdir -p $T
echo "1/4 snapshot of the production database (read-only copy)"
ssh $TINY 'docker exec lcars python -c "
import sqlite3
s=sqlite3.connect(\"file:/db/lcars.db?mode=ro\",uri=True); d=sqlite3.connect(\"/tmp/uitest.db\"); s.backup(d); d.close()
" && docker cp lcars:/tmp/uitest.db /tmp/uitest.db && docker exec lcars rm -f /tmp/uitest.db'
scp -q $TINY:/tmp/uitest.db $DB && ssh $TINY 'rm -f /tmp/uitest.db'
echo "2/4 migrate the copy to this branch's schema"
LCARS_DATABASE_URL=sqlite:///$DB .venv/bin/python -m alembic upgrade head 2>&1 | tail -1
SK=$(ssh $TINY 'sqlite3 -readonly /opt/appdata/maintainerr/maintainerr.sqlite "select apiKey from sonarr_settings"')
RK=$(ssh $TINY 'sqlite3 -readonly /opt/appdata/maintainerr/maintainerr.sqlite "select apiKey from radarr_settings"')
JK=$(ssh $TINY 'cat /opt/appdata/lcars/secrets/jellyfin_api_key' | tr -d '[:space:]')
export LCARS_DB_PATH=$DB LCARS_WEB_ROOT="$(pwd)/ui/src" LCARS_EXTERNAL_WRITES=capture \
  LCARS_ANILIST_ACCESS_TOKEN= LCARS_MAL_ACCESS_TOKEN= \
  LCARS_SONARR_URL=http://192.168.1.77:8989 LCARS_SONARR_API_KEY="$SK" \
  LCARS_RADARR_URL=http://192.168.1.77:7878 LCARS_RADARR_API_KEY="$RK" \
  LCARS_JELLYFIN_URL=http://192.168.1.77:8096 LCARS_JELLYFIN_USER=media LCARS_JELLYFIN_API_KEY="$JK"
unset LCARS_BEARER_TOKEN
echo "3/4 fill in the Jellyfin ids (reads only)"
.venv/bin/python - <<PY
from lcars import config, db, external_writes, jellyfin_sync
config.set_current(config.load_config())
assert external_writes.capturing(), "refusing: writes are not captured"
conn = db.connect("$DB")
try:
    r = jellyfin_sync.run(conn, dry_run=False, limit=0)
    print({k: r[k] for k in ("failed", "shows_checked", "episodes_matched")})
except Exception as e:  # an older branch without the Jellyfin tables: the UI still runs
    print("no Jellyfin ids filled:", e)
PY
echo "4/4 start the Jellyfin forward and the server"
nohup .venv/bin/python scripts/ui_test_forward.py > $T/forward.log 2>&1 & echo $! > $T/forward.pid
LCARS_BEARER_TOKEN="uitest-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
  nohup .venv/bin/lcars serve --host 0.0.0.0 --port 8891 > $T/server.log 2>&1 & echo $! > $T/server.pid
sleep 8
IP=$(ip -4 addr show | grep -oP '192\.168\.\d+\.\d+' | head -1)
curl -s -o /dev/null -w "ui page: HTTP %{http_code}\n" http://localhost:8891/ui/show.html
echo "READY: http://${IP:-localhost}:8891/ui/   (writes captured; stop with: $0 stop)"
