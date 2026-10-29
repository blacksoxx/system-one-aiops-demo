#!/usr/bin/env sh
# Deploy the demo shop into the current kubectl context.
#   ./deploy.sh          create namespace, code ConfigMap and the six services
#   ./deploy.sh delete   remove everything
set -eu
cd "$(dirname "$0")"

if [ "${1:-}" = "delete" ]; then
  kubectl delete namespace shop --ignore-not-found
  exit 0
fi

kubectl apply -f k8s/namespace.yaml
kubectl -n shop create configmap app-code --from-file=app.py=services/app.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/shop.yaml
echo
echo "Waiting for the shop to come up..."
kubectl -n shop rollout status deployment --timeout=180s
echo
kubectl -n shop get pods
