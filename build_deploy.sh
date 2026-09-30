set -eo pipefail

# Usage:
#   ./build_deploy.sh                  build fresh images and deploy them
#   ./build_deploy.sh --no-build       redeploy the newest images already built
#   ./build_deploy.sh --tag 20260917184017   redeploy one specific build
#   ./build_deploy.sh --no-oneagent    deploy without the DynaKube (uninstrumented)

BUILD=1
ONEAGENT=1
TAG=""
SVCS="orchestrator knowledge-worker action-worker mcp-crm approvals loadgen"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --no-oneagent) ONEAGENT=0 ;;
    --tag)      shift; TAG="$1"; BUILD=0 ;;
    -h|--help)  sed -n '3,8p' "$0"; exit 0 ;;
    *)          echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

echo " * * * Creating namespace * * * "
kubectl apply -f k8s/o11yag_ns.yaml

# OneAgent goes in BEFORE any workload, because the operator's webhook injects
# at pod creation: a pod created before the DynaKube exists stays uninstrumented
# until it is restarted, and nothing in the deploy output would say so.
if [ "$ONEAGENT" -eq 1 ]; then
  echo " * * * Configuring Dynatrace OneAgent * * * "
  if ! kubectl get crd dynakubes.dynatrace.com >/dev/null 2>&1; then
    echo "The Dynatrace Operator is not installed (no DynaKube CRD)." >&2
    echo "Install it first, or pass --no-oneagent to deploy uninstrumented." >&2
    exit 1
  fi

  # Same pattern the Collector secret used upstream: keep an existing Secret on
  # a redeploy, and rewrite it only when tokens are passed in the environment.
  if [ -z "$DT_API_TOKEN" ] && kubectl get secret o11yag-oa -n dynatrace >/dev/null 2>&1; then
    echo "Secret o11yag-oa already exists in dynatrace, keeping it"
  else
    [ -n "$DT_API_TOKEN" ] || { read -s -p "Enter DT_API_TOKEN: " DT_API_TOKEN; echo; }
    [ -n "$DT_DATA_INGEST_TOKEN" ] || { read -s -p "Enter DT_DATA_INGEST_TOKEN: " DT_DATA_INGEST_TOKEN; echo; }
    kubectl create secret generic o11yag-oa \
      --from-literal=apiToken="$DT_API_TOKEN" \
      --from-literal=dataIngestToken="$DT_DATA_INGEST_TOKEN" \
      -n dynatrace --dry-run=client -o yaml | kubectl apply -f -
  fi

  # The tenant lives in a Secret of its own rather than in the repo. Same rule as
  # the tokens: kept on a redeploy, rewritten when DT_TENANT is passed in.
  if [ -z "$DT_TENANT" ] && kubectl get secret o11yag-oa-tenant -n dynatrace >/dev/null 2>&1; then
    DT_TENANT="$(kubectl get secret o11yag-oa-tenant -n dynatrace -o jsonpath='{.data.tenant}' | base64 -d)"
  else
    [ -n "$DT_TENANT" ] || read -p "Enter DT_TENANT (the id before .live.dynatrace.com): " DT_TENANT
    kubectl create secret generic o11yag-oa-tenant \
      --from-literal=tenant="$DT_TENANT" \
      -n dynatrace --dry-run=client -o yaml | kubectl apply -f -
  fi
  if ! [[ "$DT_TENANT" =~ ^[a-z0-9]+$ ]]; then
    echo "DT_TENANT must be the bare tenant id, e.g. abc12345" >&2
    exit 1
  fi

  # apiUrl cannot come from a Secret, so the placeholder is filled in here, on
  # the way to kubectl, and the tenant never touches a tracked file.
  sed "/^  apiUrl:/ s/DT_TENANT/${DT_TENANT}/" dynatrace/k8s/dynakube.yaml | kubectl apply -f -
  # The webhook is what injects; the ActiveGate can finish coming up later.
  kubectl rollout status deployment/dynatrace-webhook -n dynatrace --timeout=180s
else
  echo " * * * --no-oneagent: deploying WITHOUT OneAgent, nothing will be instrumented * * * "
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

# Roll a Deployment whose ConfigMap changed but whose own spec did not.
#
# `kubectl apply` on a manifest holding both a ConfigMap and a Deployment updates
# the ConfigMap and leaves the Deployment alone, because the Deployment spec is
# byte-identical. The pod keeps running with the config it booted on. For the
# services this never shows, since every deploy gives them a fresh image tag —
# but LiteLLM is not rebuilt, and it reads its config file exactly once, at
# startup.
#
# It bit us for real: a new model alias was added to LiteLLM's config, applied,
# and every call for it came back `400 Invalid model name` — the alias present in
# the ConfigMap and absent from the running proxy. Nothing in the deploy output
# suggested the config had not taken.
#
# The annotation is a checksum of the manifest, so an unchanged file patches the
# same value and Kubernetes does nothing. Only a real change rolls the pod.
roll_on_config_change() {
  local name="$1" file="$2"
  local sum
  sum="$(sha256sum "$file" | cut -c1-12)"
  kubectl patch deployment "$name" -n o11yag-oa --type=strategic \
    -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"o11yag.config/checksum\":\"${sum}\"}}}}}" \
    >/dev/null
}
roll_on_config_change o11yag-litellm litellm/k8s/o11yag-litellm.yaml

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
  # The checksum rides in the same overlay as the image tag, so one apply carries
  # both and the pod rolls at most once. Patching it afterwards would work too and
  # would roll a second time on every build.
  #
  # Without it, a ConfigMap-only change is a silent no-op on --no-build: the tag is
  # unchanged, so the Deployment spec is unchanged, so no pod restarts and the new
  # setting never reaches the process. That is how you switch KB_POISON_DOC on,
  # redeploy, and find the corpus still clean.
  sum="$(sha256sum "${svc}/k8s/o11yag-${svc}.yaml" | cut -c1-12)"
  cat > "${tmp}/kustomization.yaml" <<EOF
resources:
  - $(pwd)/${svc}/k8s/o11yag-${svc}.yaml
images:
  - name: o11yag-${svc}
    newTag: "${TAG}"
patches:
  - target:
      kind: Deployment
      name: o11yag-${svc}
    patch: |
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: o11yag-${svc}
      spec:
        template:
          metadata:
            annotations:
              o11yag.config/checksum: "${sum}"
EOF
  kubectl kustomize --load-restrictor LoadRestrictionsNone "${tmp}" | kubectl apply -f -
  rm -rf "${tmp}"
done

echo
echo "Done. Watch it come up with:   kubectl get pods -n o11yag-oa -w"
