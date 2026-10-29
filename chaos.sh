#!/usr/bin/env sh
# Break the shop the way production breaks, one command at a time, or all at once.
#
#   ./chaos.sh oom [service]          memory limit cut, cache allocates 200 MiB   -> OOMKilled           (catalog)
#   ./chaos.sh bad-config [service]   config with a typo rolled out               -> CrashLoopBackOff    (checkout)
#   ./chaos.sh bad-image [service]    release points at an image tag that doesn't exist -> ImagePullBackOff (cart)
#   ./chaos.sh cpu-spike [service]    hot loop under a tiny CPU limit              -> CPU throttling      (payments)
#   ./chaos.sh db-down                orders-db scaled to 0                        -> checkout unhealthy, 504s
#   ./chaos.sh oops                   someone deletes the cart Service by hand     -> storefront unhealthy
#   ./chaos.sh flaky-job              batch job fails once, retry succeeds          -> noise
#   ./chaos.sh storm                  five of the above, a few seconds apart        -> Friday afternoon
#   ./chaos.sh heal                   undo everything
#   ./chaos.sh status                 deployments and pods at a glance
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
  bad-image)
    svc="${2:-cart}"
    echo "-> $svc: release 2.4.0-rc1 references an image tag that does not exist"
    $K set image deployment/"$svc" "$svc=python:3.12-slim-2.4.0-rc1"
    ;;
  cpu-spike)
    svc="${2:-payments}"
    echo "-> $svc: hot loop under a 50m CPU limit"
    $K set resources deployment/"$svc" --limits=cpu=50m,memory=128Mi
    $K set env deployment/"$svc" CPU_BURN=1
    ;;
  db-down)
    echo "-> orders-db: scaling to 0 replicas (checkout depends on it)"
    $K scale deployment/orders-db --replicas=0
    ;;
  oops)
    echo "-> someone runs 'kubectl delete svc cart' in production"
    $K delete service cart --ignore-not-found
    ;;
  flaky-job)
    run="run-$(date +%s)"
    echo "-> nightly-report: job that fails once and succeeds on retry ($run)"
    $K delete job nightly-report --ignore-not-found >/dev/null
    sed "s/value: \"run\"/value: \"$run\"/" k8s/jobs/nightly-report.yaml | $K apply -f -
    ;;
  storm)
    echo "-> Friday, 17:03. Five things go wrong within a minute."
    "$0" oom catalog;        sleep 8
    "$0" bad-image cart;     sleep 8
    "$0" db-down;            sleep 8
    "$0" flaky-job;          sleep 8
    "$0" cpu-spike payments
    echo "-> storm started. Watch the dashboards."
    ;;
  heal)
    echo "-> restoring the shop"
    for svc in storefront catalog cart checkout orders-db payments; do
      $K set env deployment/"$svc" MEM_HOG_MB- CONFIG- CPU_BURN- >/dev/null 2>&1 || true
      $K set resources deployment/"$svc" --limits=cpu=0,memory=128Mi >/dev/null 2>&1 || true
      $K set image deployment/"$svc" "$svc=python:3.12-slim" >/dev/null 2>&1 || true
    done
    $K scale deployment/orders-db --replicas=1 >/dev/null
    kubectl apply -f k8s/shop.yaml >/dev/null   # recreates anything deleted by hand
    $K delete job nightly-report --ignore-not-found >/dev/null
    $K rollout status deployment --timeout=180s
    ;;
  status)
    $K get deployments
    echo
    $K get pods
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 1
    ;;
esac
