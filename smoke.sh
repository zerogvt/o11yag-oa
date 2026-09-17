set -eo pipefail

# Boot every built image and check it actually comes up, WITHOUT a cluster.
#
# This exists because a service can pass every static check — manifests parse,
# Python compiles, imports look right — and still die on boot because a
# dependency is missing from requirements.txt. That is not hypothetical: the
# orchestrator did exactly that, because traceloop-sdk imports httpx without
# declaring it and nothing else in the stack pulls httpx in any more.
#
# Telemetry is OFF here on purpose: this checks that the process starts and
# answers, not that it can reach a Collector. Run it after build_deploy.sh
# builds images, before deploying.
#
# Usage:
#   ./smoke.sh              use the newest build tag present on all services
#   ./smoke.sh --tag <tag>  check one specific build

SVCS="orchestrator knowledge-worker action-worker mcp-crm approvals"
TAG=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)     shift; TAG="$1" ;;
    -h|--help) sed -n '3,20p' "$0"; exit 0 ;;
    *)         echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ -z "$TAG" ]; then
  want="$(echo $SVCS | wc -w)"
  TAG="$(for svc in $SVCS; do
           docker images "o11yag-${svc}" --format '{{.Tag}}' | grep -E '^[0-9]{14}$'
         done | sort | uniq -c | awk -v n="$want" '$1 == n {print $2}' | sort -r | head -1)"
  if [ -z "$TAG" ]; then
    echo "No build found covering all of: $SVCS" >&2
    echo "Run build_deploy.sh first, or pass --tag <tag>." >&2
    exit 1
  fi
fi
echo "Smoke-testing build ${TAG}"

port_of() {
  case "$1" in
    orchestrator)     echo 8000 ;;
    knowledge-worker) echo 8001 ;;
    action-worker)    echo 8002 ;;
    mcp-crm)          echo 8003 ;;
    approvals)        echo 8004 ;;
  esac
}

fails=0
for svc in $SVCS; do
  port="$(port_of $svc)"
  name="o11yag-smoke-${svc}"
  docker rm -f "$name" >/dev/null 2>&1 || true
  # OTEL off, and every backing service pointed at a dead port: we are testing
  # that the process boots and serves, not that its dependencies are up.
  docker run -d --name "$name" \
    -e OTEL_ENABLED=false \
    -e QDRANT_URL=http://127.0.0.1:1 \
    -e REDIS_URL=redis://127.0.0.1:1/0 \
    -e LITELLM_URL=http://127.0.0.1:1/v1 \
    -p "${port}:${port}" "o11yag-${svc}:${TAG}" >/dev/null

  ok=0
  for _ in $(seq 1 30); do
    if [ "$svc" = "mcp-crm" ]; then
      # Streamable HTTP answers a bare GET with an error, so check the socket.
      (exec 3<>/dev/tcp/127.0.0.1/${port}) 2>/dev/null && { ok=1; break; }
    else
      curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && { ok=1; break; }
    fi
    sleep 1
  done

  if [ "$ok" -eq 1 ]; then
    printf "  %-18s OK\n" "$svc"
  else
    printf "  %-18s FAILED — last 20 log lines:\n" "$svc"
    docker logs "$name" 2>&1 | tail -20 | sed 's/^/      /'
    fails=$((fails + 1))
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
done

echo
if [ "$fails" -eq 0 ]; then
  echo "All services booted."
else
  echo "${fails} service(s) failed to boot." >&2
  exit 1
fi
