#!/usr/bin/env bash
# Push tools/conjure to the ConfigMap and restart only the app process (the container's shell loop
# starts it again), so the pod is not recreated. Run from the repo root on the workshop VM.
set -euo pipefail
NS="${NS:-${USERNAME:?set USERNAME or NS}}"
export KUBECONFIG="${KUBECONFIG:-/config/${NS}-k8s.yaml}"
find tools/conjure -name __pycache__ -prune -exec rm -rf {} +
kubectl -n "$NS" create configmap conjure-code --from-file=tools/conjure --dry-run=client -o yaml | kubectl -n "$NS" replace -f -
echo "waiting for the ConfigMap volume to sync..."
POD=$(kubectl -n "$NS" get pod -l app=conjure -o jsonpath='{.items[0].metadata.name}')
LOCAL=$(cat tools/conjure/*.py tools/conjure/*.js tools/conjure/*.html tools/conjure/*.css | sha256sum | cut -d' ' -f1)
for _ in $(seq 1 40); do
  REMOTE=$(kubectl -n "$NS" exec "$POD" -- sh -c 'cd /code && cat *.py *.js *.html *.css | sha256sum' | cut -d' ' -f1)
  [ "$LOCAL" = "$REMOTE" ] && break
  sleep 5
done
# Non-interactive bash starts background jobs with SIGINT ignored, so use SIGTERM.
OLD_PID=$(kubectl -n "$NS" exec "$POD" -- cat /tmp/conjure.pid)
kubectl -n "$NS" exec "$POD" -- sh -c "kill -TERM $OLD_PID" || true
sleep 3
for _ in $(seq 1 60); do
  NEW_PID=$(kubectl -n "$NS" exec "$POD" -- cat /tmp/conjure.pid 2>/dev/null || true)
  if [ -n "$NEW_PID" ] && [ "$NEW_PID" != "$OLD_PID" ] && \
     kubectl -n "$NS" exec "$POD" -- python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)" 2>/dev/null; then
    echo "conjure is back up (pid $OLD_PID -> $NEW_PID)"; exit 0
  fi
  sleep 3
done
echo "conjure did not come back; check: kubectl -n $NS logs $POD"; exit 1
