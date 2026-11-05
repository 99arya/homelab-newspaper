#!/bin/sh
# Makes today's paper in Docker and prints it. Pass --no-print to only make the PDF
# (saved as editions/preview.pdf). Settings live in config.json (run ./setup.sh to change them).
cd "$(dirname "$0")"
IMAGE=homelab-newspaper
[ -f config.json ] || { echo "No config.json yet. Run ./setup.sh first."; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -q -t "$IMAGE" . >/dev/null || exit 1
eval "$(python3 -c '
import json, urllib.parse
c = json.load(open("config.json"))
print("HOST=" + (urllib.parse.urlparse(c.get("printer", "")).hostname or ""))
print("FALLBACK=" + c.get("printer_fallback_ip", ""))
print("TZONE=" + c.get("timezone", "UTC"))')"
IP=""
case "$HOST" in
  *.local)  # Docker can't resolve mDNS names, so look it up here and pass the address in
    for i in 1 2 3 4 5; do
        IP=$(getent hosts "$HOST" | awk '{print $1; exit}')
        [ -n "$IP" ] && break
        sleep 3
    done
    if [ -z "$IP" ] && [ -n "$FALLBACK" ]; then echo "Could not look up $HOST; using last known address $FALLBACK"; IP=$FALLBACK; fi ;;
esac
exec docker run --rm --user "$(id -u):$(id -g)" -e TZ="$TZONE" \
    ${IP:+--add-host "$HOST:$IP"} -v "$PWD":/app "$IMAGE" "$@"
