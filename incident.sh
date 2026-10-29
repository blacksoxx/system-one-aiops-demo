#!/usr/bin/env sh
# Break the shop in realistic ways, one command at a time.
#
#   ./incident.sh oom [service]        container exceeds its memory limit -> OOMKilled      (default: catalog)
#   ./incident.sh bad-config [service] deploy an invalid config            -> CrashLoopBackOff (default: checkout)
#   ./incident.sh db-down              scale orders-db to 0               -> checkout not ready, 504s
#   ./incident.sh flaky-job            batch job fails once, retry succeeds -> noise
#   ./incident.sh heal                 undo everything
#   ./incident.sh status               pods and deployments at a glance
set -eu
cd "$(dirname "$0")"
K="kubectl -n shop"

case "${1:-}" in
  oom)
    svc="${2:-catalog}"
    echo "-> $svc: memory limit 64Mi, cache warm-up allocates 200 MiB"
    $K set resources deployment/"$svc" --limits=memory=64Mi
    $K set env deployment/"$svc" MEM_HOG_MB=200
    ;;
  bad-config)
    svc="${2:-checkout}"
    echo "-> $svc: rolling out a config with a typo in a field name"
    $K set env deployment/"$svc" CONFIG='{"timeout_ms": 3000, "retry_polciy": "exponential"}'
    ;;
  db-down)
    echo "-> orders-db: scaling to 0 replicas (checkout depends on it)"
    $K scale deployment/orders-db --replicas=0
    ;;
  flaky-job)
    run="run-$(date +%s)"
    echo "-> nightly-report: job that fails once and succeeds on retry ($run)"
    $K delete job nightly-report --ignore-not-found >/dev/null
    sed "s/value: \"run\"/value: \"$run\"/" k8s/jobs/nightly-report.yaml | $K apply -f -
    ;;
  heal)
    echo "-> restoring the shop"
    for svc in storefront catalog cart checkout orders-db payments; do
      $K set env deployment/"$svc" MEM_HOG_MB- CONFIG- >/dev/null 2>&1 || true
      $K set resources deployment/"$svc" --limits=memory=128Mi >/dev/null 2>&1 || true
    done
    $K scale deployment/orders-db --replicas=1 >/dev/null
    $K delete job nightly-report --ignore-not-found >/dev/null
    $K rollout status deployment --timeout=120s
    ;;
  status)
    $K get deployments
    echo
    $K get pods
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
