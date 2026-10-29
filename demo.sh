#!/usr/bin/env sh
# One command for the whole demo environment.
#
#   ./demo.sh up        kind cluster (own kubeconfig) + Prometheus/Alertmanager/Grafana + the shop
#                       + alert rules + dashboards + the triage service wired to your model server
#   ./demo.sh open      port-forward Grafana (3000) and the triage board (8400), print the URLs
#   ./demo.sh status    what is running
#   ./demo.sh down      delete the cluster
#
# Environment (all optional)
#   CLUSTER=shop                       kind cluster name
#   KUBECONFIG_FILE=~/.kube/shop-kind.yaml   kept separate from any other cluster you have
#   LAYA_PORT=8000                     where your model server listens on this machine
#   THRESHOLD=0.6                      act alone at or above this confidence
#   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL   optional OpenAI-compatible LLM for Slack-lane notes
set -eu
cd "$(dirname "$0")"

CLUSTER="${CLUSTER:-shop}"
export KUBECONFIG="${KUBECONFIG_FILE:-$HOME/.kube/shop-kind.yaml}"
LAYA_PORT="${LAYA_PORT:-8000}"
THRESHOLD="${THRESHOLD:-0.6}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }

host_ip() {
  # The IP of this machine as seen from inside the cluster's node container.
  node="${CLUSTER}-control-plane"
  ip=$(docker exec "$node" sh -c 'getent ahostsv4 host.docker.internal 2>/dev/null | awk "NR==1{print \$1}"' || true)
  [ -n "$ip" ] || ip=$(docker exec "$node" sh -c 'ip route | awk "/default/ {print \$3}"')
  echo "$ip"
}

case "${1:-}" in
  up)
    need kind; need kubectl; need helm; need docker
    mkdir -p "$(dirname "$KUBECONFIG")"
    if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
      echo "== creating kind cluster '$CLUSTER' (kubeconfig: $KUBECONFIG)"
      kind create cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG" --wait 120s
    else
      echo "== kind cluster '$CLUSTER' already exists"
      kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG" >/dev/null
    fi

    echo "== installing Prometheus, Alertmanager and Grafana (first time pulls ~1 GB of images)"
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
    helm repo update >/dev/null
    helm upgrade --install mon prometheus-community/kube-prometheus-stack \
      -n monitoring --create-namespace -f monitoring/values.yaml --wait --timeout 12m >/dev/null
    echo "   monitoring stack ready"

    echo "== deploying the shop"
    ./deploy.sh >/dev/null
    echo "   shop ready"

    echo "== wiring the model server on this machine into the cluster"
    ip=$(host_ip)
    [ -n "$ip" ] || { echo "could not determine the host IP from inside the cluster"; exit 1; }
    cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Service
metadata: { name: laya, namespace: shop }
spec:
  ports: [{ name: http, port: $LAYA_PORT, targetPort: $LAYA_PORT }]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: laya-host
  namespace: shop
  labels: { kubernetes.io/service-name: laya }
addressType: IPv4
ports: [{ name: http, port: $LAYA_PORT, protocol: TCP }]
endpoints: [{ addresses: ["$ip"] }]
EOF
    echo "   laya.shop.svc -> $ip:$LAYA_PORT"

    echo "== triage service, alert rules, dashboards"
    files="--from-file=triage.py --from-file=triage_service.py --from-file=taxonomy.json"
    [ -f calibration.json ] && files="$files --from-file=calibration.json"
    # shellcheck disable=SC2086
    kubectl -n shop create configmap triage-code $files --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    if [ -n "${LLM_BASE_URL:-}" ]; then
      kubectl -n shop create secret generic llm --from-literal=LLM_BASE_URL="$LLM_BASE_URL" \
        --from-literal=LLM_API_KEY="${LLM_API_KEY:-}" --from-literal=LLM_MODEL="${LLM_MODEL:-}" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      echo "   llm notes: $LLM_MODEL via $LLM_BASE_URL"
    fi
    sed "s/__LAYA_PORT__/$LAYA_PORT/; s/__THRESHOLD__/$THRESHOLD/" k8s/triage.yaml | kubectl apply -f - >/dev/null
    kubectl apply -f k8s/monitoring/rules.yaml -f k8s/monitoring/dashboards.yaml >/dev/null
    kubectl -n shop rollout restart deployment/triage >/dev/null 2>&1 || true
    kubectl -n shop rollout status deployment/triage --timeout=120s >/dev/null
    sleep 3
    kubectl -n shop logs deployment/triage --tail=3 2>/dev/null | sed 's/^/   triage: /'
    echo
    echo "Ready. Next:  ./demo.sh open      (in another terminal)   then   ./chaos.sh storm"
    ;;
  open)
    need kubectl
    echo "Grafana   http://localhost:3000/d/alerts-today     (01 · Alerts · today)"
    echo "          http://localhost:3000/d/alerts-triaged   (02 · Alerts · triaged)"
    echo "Board     http://localhost:8400/board               (fallback view, same data)"
    echo "Alertmgr  http://localhost:9093"
    echo "Ctrl-C stops the port-forwards."
    kubectl -n monitoring port-forward svc/mon-grafana 3000:80 >/dev/null 2>&1 &
    kubectl -n shop port-forward svc/triage 8400:8400 >/dev/null 2>&1 &
    kubectl -n monitoring port-forward svc/mon-kube-prometheus-stack-alertmanager 9093:9093 >/dev/null 2>&1 &
    wait
    ;;
  status)
    kubectl get nodes
    echo; kubectl -n shop get deployments
    echo; kubectl -n monitoring get pods --no-headers | awk '{print $1, $3}'
    echo; kubectl -n shop logs deployment/triage --tail=5
    ;;
  down)
    need kind
    kind delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
    ;;
  *)
    sed -n '2,16p' "$0"; exit 1 ;;
esac
