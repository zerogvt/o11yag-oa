echo "* * * Cleaning * * *"

for IMAGE in o11yag-orchestrator o11yag-knowledge-worker o11yag-action-worker \
             o11yag-mcp-crm o11yag-approvals o11yag-loadgen; do
  docker images | grep "$IMAGE" | \
  awk '{print $2}' | while read I; do docker rmi $I; done
done
