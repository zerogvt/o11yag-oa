set -eo pipefail

# Usage:
#   ./build_deploy.sh                  build fresh images and deploy them
#   ./build_deploy.sh --no-build       redeploy the newest images already built
#   ./build_deploy.sh --tag 20260917184017   redeploy one specific build

BUILD=1
TAG=""
SVCS="orchestrator knowledge-worker action-worker mcp-crm approvals loadgen"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --tag)      shift; TAG="$1"; BUILD=0 ;;
    -h|--help)  sed -n '3,7p' "$0"; exit 0 ;;
    *)          echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

echo " * * * Creating namespace * * * "
kubectl apply -f k8s/o11yag_ns.yaml

echo " * * * Creating Dynatrace secret * * * "
# On a redeploy the secret usually survived (stop.sh removes Deployments, not
# the namespace or this imperatively-created secret), so don't prompt for a
# token we already have. Setting DT_API_TOKEN/DT_TENANT still forces a rewrite.
if [ -z "$DT_API_TOKEN" ] && kubectl get secret o11yag-collector -n o11yag >/dev/null 2>&1; then
  echo "Secret o11yag-collector already exists, keeping it"
else
  if [ -n "$DT_API_TOKEN" ]; then
    echo "Using DT_API_TOKEN from environment"
  else
    read -s -p "Enter DT_API_TOKEN: " DT_API_TOKEN
    echo
  fi

  if [ -n "$DT_TENANT" ]; then
    echo "Using DT_TENANT from environment ($DT_TENANT)"
  else
    read -p "Enter DT_TENANT: " DT_TENANT
  fi
  if [ -z "$DT_TENANT" ]; then
    echo "DT_TENANT is required" >&2
    exit 1
  fi

  kubectl create secret generic o11yag-collector \
    --from-literal=DT_API_TOKEN="$DT_API_TOKEN" \
    --from-literal=DT_OTLP_ENDPOINT=https://$DT_TENANT.live.dynatrace.com/api/v2/otlp \
    -n o11yag --dry-run=client -o yaml | kubectl apply -f -
fi

# Stateful/pulled-image infrastructure first: the services below all fail their
# readiness probes until these are up, and Ollama in particular is slow on a
# cold start (two model pulls). Nothing here is built locally, so none of it
# takes a build tag.
echo " * * * Deploying infrastructure (redis, qdrant, ollama, litellm) * * * "
kubectl apply -f redis/k8s/o11yag-redis.yaml
kubectl apply -f qdrant/k8s/o11yag-qdrant.yaml
kubectl apply -f ollama/k8s/o11yag-ollama.yaml
kubectl apply -f litellm/k8s/o11yag-litellm.yaml

if [ "$BUILD" -eq 1 ]; then
  echo "* * * Building * * *"
  TAG="$(date +%Y%m%d%H%M%S)"
  for svc in $SVCS; do
    docker build -t o11yag-${svc}:${TAG} ${svc}/
  done
elif [ -z "$TAG" ]; then
  # Reuse the newest build. Only timestamp tags are considered, and only a tag
  # present on ALL deployed services counts — a half-built tag would otherwise
  # deploy a mix of generations. Timestamps sort lexically, so newest is last.
  want="$(echo $SVCS | wc -w)"
  TAG="$(for svc in $SVCS; do
           docker images "o11yag-${svc}" --format '{{.Tag}}' | grep -E '^[0-9]{14}$'
         done | sort | uniq -c | awk -v n="$want" '$1 == n {print $2}' | sort -r | head -1)"
  if [ -z "$TAG" ]; then
    echo "No build found covering all of: $SVCS" >&2
    echo "Run without --no-build to build them, or pass --tag <tag>." >&2
    exit 1
  fi
  echo "* * * Reusing newest build ${TAG} * * *"
else
  for svc in $SVCS; do
    if ! docker image inspect "o11yag-${svc}:${TAG}" >/dev/null 2>&1; then
      echo "Image o11yag-${svc}:${TAG} not found locally" >&2
      exit 1
    fi
  done
  echo "* * * Reusing build ${TAG} * * *"
fi

# Deploy the built services via a Kustomize overlay generated fresh in a temp
# dir each run — so none of the checked-in Deployment yamls (or any other
# tracked file) ever change; only this throwaway overlay carries the tag.
# --load-restrictor is needed because the overlay's resource path points back
# into the repo, outside the temp dir Kustomize otherwise treats as its root.
echo "* * * Deploying ${TAG} * * *"
for svc in $SVCS; do
  tmp="$(mktemp -d)"
  cat > "${tmp}/kustomization.yaml" <<EOF
resources:
  - $(pwd)/${svc}/k8s/o11yag-${svc}.yaml
images:
  - name: o11yag-${svc}
    newTag: "${TAG}"
EOF
  kubectl kustomize --load-restrictor LoadRestrictionsNone "${tmp}" | kubectl apply -f -
  rm -rf "${tmp}"
done

echo "* * * Deploying OTEL collector * * *"
kubectl apply -f collector/k8s/o11yag-collector.yaml

echo
echo "Done. Watch it come up with:   kubectl get pods -n o11yag -w"
