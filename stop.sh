# Tear down the workloads but keep the namespace, the Dynatrace secret and the
# PVCs (Ollama's models, Qdrant's vectors). build_deploy.sh --no-build then
# brings everything back without a re-download or a re-seed.
#
# Ollama is commented out by default: re-pulling the models is the slowest part
# of a cold start and it rarely needs restarting.
#kubectl delete -f ollama/k8s/o11yag-ollama.yaml
kubectl delete -f loadgen/k8s/o11yag-loadgen.yaml
kubectl delete -f orchestrator/k8s/o11yag-orchestrator.yaml
kubectl delete -f knowledge-worker/k8s/o11yag-knowledge-worker.yaml
kubectl delete -f action-worker/k8s/o11yag-action-worker.yaml
kubectl delete -f mcp-crm/k8s/o11yag-mcp-crm.yaml
kubectl delete -f approvals/k8s/o11yag-approvals.yaml
kubectl delete -f litellm/k8s/o11yag-litellm.yaml
kubectl delete -f qdrant/k8s/o11yag-qdrant.yaml
kubectl delete -f redis/k8s/o11yag-redis.yaml
kubectl delete -f collector/k8s/o11yag-collector.yaml
