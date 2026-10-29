# Alert triage with a System One model

A small, reproducible demo of a **System One model** doing operations work: a fake shop runs in a
Kubernetes cluster, you break it in realistic ways, and every alert is answered in one pass by a
model that returns **typed decisions with probabilities instead of text**. Confident decisions are
routed or closed automatically; uncertain ones go to a human.

The model is [Laya](https://huggingface.co/convaiinnovations/laya), an open-weights System One
model (Apache 2.0) that you run yourself. It speaks the same API as TypeSafe's Jev, so the same
code works against either.

```
   kind cluster, namespace "shop"                 python3 triage.py watch
   ┌───────────────────────────────┐              ┌──────────────────────────────────┐
   │ storefront → catalog          │  events,     │ 1. turn the condition into an    │
   │            → cart             │  pod status, │    alert (Alertmanager shape)    │
   │            → checkout → orders-db ──logs────▶│ 2. ask the model 5 typed         │
   │                       → payments│            │    questions in one request      │
   │ nightly-report (Job)          │              │ 3. route on confidence           │
   └───────────────────────────────┘              └──────────────┬───────────────────┘
          ▲  ./incident.sh oom | bad-config | db-down | flaky-job  │  POST /v1/systemone
                                                                   ▼
                                                          laya-serve (typed-decisions)
```

The five questions, asked about every alert:

| Question | Type | Answers |
|---|---|---|
| `component` | choice | storefront · catalog · cart · checkout · orders-db · payments-provider · cluster-platform · network |
| `cause` | choice | out-of-memory · bad-config-or-deploy · dependency-down · network-connectivity · disk-pressure · certificate-expiry · load-spike · hardware-fault · transient-noise |
| `actionable` | choice (yes/no) | does anyone need to act? |
| `customer_impact` | choice (yes/no) | are shoppers affected right now? |
| `severity` | score | none · working hours · today · page now |

An alert is **auto-routed** to the component's team when the model is at least `--threshold`
sure of component, cause and actionability, **auto-closed** when it is that sure the alert is
noise, and **escalated** otherwise. Every component maps to a team in `taxonomy.json`.

## What's in the repo

| Path | What it is |
|---|---|
| `services/app.py` | The one microservice all six shop services run. Answers JSON, checks dependencies on `/healthz`, and has three fault switches (`CONFIG`, `MEM_HOG_MB`, `DEPENDS_ON`). |
| `k8s/` | Namespace, the six Deployments and Services, and the flaky batch Job. |
| `deploy.sh` | Creates the namespace, mounts `app.py` as a ConfigMap, applies everything. |
| `incident.sh` | `oom`, `bad-config`, `db-down`, `flaky-job`, `heal`, `status`. |
| `triage.py` | `check`, `replay`, `watch`, `ask`, `log`. Standard library only. |
| `alerts.json` | 30 labelled alerts for the shop (21 actionable, 9 noise) for offline scoring. |
| `taxonomy.json` | Components with their descriptions and owning teams, causes, severity scale. |
| `calibrate.py` | Fits temperature scaling on the labelled alerts; `triage.py` applies the result. |
| `scripts/serve-laya.sh` | Runs Laya's `typed-decisions` checkpoint behind a Jev-compatible API. |

## Prerequisites

- Python 3.10+ (Laya's requirement; `triage.py` itself needs 3.9+)
- Docker or Podman, `kubectl`, and [kind](https://kind.sigs.k8s.io/) (a single binary)
- About 2 GB of disk for the model, and a GPU if you want sub-100 ms answers (CPU works, slower)

## 1. Run the model

```sh
git clone https://github.com/blacksoxx/system-one-aiops-demo && cd system-one-aiops-demo
./scripts/serve-laya.sh                      # CPU
LAYA_DEVICE=cuda ./scripts/serve-laya.sh     # NVIDIA GPU
```

The first run creates `.venv`, installs `laya[serve]`, and downloads the `typed-decisions`
checkpoint (about 1.6 GB) into `.hf/`. The server listens on `http://0.0.0.0:8000`; set
`LAYA_PORT` if 8000 is taken. Check it from another shell:

```sh
python3 triage.py check --base-url http://localhost:8000/v1
```

`triage.py` defaults to `http://localhost:8000/v1`; pass `--base-url` or set `SYSTEMONE_BASE_URL`
to point anywhere else, including a server on another machine.

## 2. Score the model offline

```sh
python3 triage.py replay --model typed-decisions
```

This sends the 30 labelled alerts, prints each decision next to the right answer, and ends with
three tables: accuracy, what happens as you move the threshold, and whether the probabilities are
honest (a reliability table with ECE). Measured on a laptop CPU with Laya `typed-decisions`
0.4.17, before calibration:

```
accuracy: component 87% · cause 57% · actionable 77% · all three 43%   (random: component 12%, cause 11%)
threshold 80%: 0 auto-routed, 0 auto-closed as noise, 30 escalated

said 0.3–0.5: 11 alerts · mean probability 0.39 · actually right 91%
expected calibration error (ECE): 0.386
```

Read that carefully: the model ranks the right component first 87% of the time, but it is
**under-confident**. When it says 39% it is right 91% of the time, so nothing crosses a sensible
threshold and everything escalates. Accurate but badly calibrated is not something you can
automate on, which is the point of the next step.

## 3. Calibrate

```sh
python3 calibrate.py --model typed-decisions
python3 triage.py replay --model typed-decisions
```

`calibrate.py` fits one temperature per question on the labelled alerts (temperature scaling
changes how sure the probabilities claim to be, never which answer ranks first) and writes
`calibration.json`. It prints the in-sample improvement and a two-fold cross-check so you can
see whether the correction holds on alerts it was not fitted on. After calibration:

```
component   T = 0.339  ECE 0.386 → 0.139 (in-sample) · 0.198 (two-fold)  mean top-probability 0.48 → 0.80

threshold  auto-handled  accuracy when auto  accuracy when escalated
  60%        10/30        70%                 30%
  70%         5/30       100%                 32%
  80%         2/30       100%                 39%
```

Accuracy is unchanged. What changed is that the threshold now means something: raise it and the
automatic decisions get more accurate while fewer of them are made. That is the dial you want in
production. These numbers are in-sample (fitted and measured on the same 30 alerts); the live
incidents below are the out-of-sample check. Delete `calibration.json` or pass
`--no-calibration` to compare.

## 4. Create a cluster and deploy the shop

Give the kind cluster **its own kubeconfig file** so it never touches the clusters you already
have, and point `KUBECONFIG` at it in every shell you use for the demo:

```sh
kind create cluster --name shop --kubeconfig ~/.kube/shop-kind.yaml
export KUBECONFIG=~/.kube/shop-kind.yaml
./deploy.sh
```

The image is plain `python:3.12-slim`; nothing is built or pushed. `deploy.sh` waits for the six
services to be ready.

On a machine without kind (for example an NVIDIA DGX with Docker already installed):

```sh
mkdir -p ~/bin && export PATH="$HOME/bin:$PATH"
curl -Lo ~/bin/kind "https://kind.sigs.k8s.io/dl/latest/kind-linux-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
chmod +x ~/bin/kind
```

If that machine also runs another Kubernetes distribution, the separate kubeconfig above is what
keeps the two apart: never merge the kind context into your main kubeconfig for this demo.

## 5. Watch and break things

Two terminals. In the first:

```sh
python3 triage.py watch --model typed-decisions --threshold 0.7
```

The watcher scans the `shop` namespace every two seconds for four conditions (OOMKilled,
CrashLoopBackOff, deployment replicas unavailable, job failed), holds brief `for:` windows like
Alertmanager does, builds an alert with the pod's recent logs, and sends it to the model. It
prints every decision and appends it to `decisions.jsonl`.

In the second:

```sh
./incident.sh oom catalog        # catalog gets a 64Mi limit and allocates 200 MiB → OOMKilled
./incident.sh bad-config checkout # checkout rolls out a config with a typo → CrashLoopBackOff
./incident.sh db-down             # orders-db scaled to 0 → checkout not ready, logs show the timeouts
./incident.sh flaky-job           # nightly-report fails once, succeeds on retry → noise
./incident.sh heal
python3 triage.py log
```

Each incident produces an alert within seconds to a minute (the `for:` windows are 15 s for
unavailable deployments and 25 s for jobs, so the job can show its retry). The third incident is
the interesting one: the alert is about `checkout`, but the fix is `orders-db`. Watch whether the
model routes to the Data team or escalates.

The watcher stands in for Prometheus and Alertmanager so the demo moves fast. It emits alerts in
Alertmanager's webhook shape, so pointing a real Alertmanager at the same code is a small change.

## 6. Try your own alert

```sh
python3 triage.py ask --model typed-decisions
```

Paste anything, finish with an empty line. Useful on stage: take an alert from the audience.

## Using TypeSafe Jev instead

```sh
export TYPESAFE_API_KEY=...
python3 triage.py replay --base-url https://api.typesafe.ai/v1 --model jev-1.13
```

Same request, same response shape, so every command works unchanged. Cost is printed per run at
TypeSafe's list price for input tokens. Run `calibrate.py` again with the same `--base-url`,
because the temperatures are per model.

## Rehearsing without a model

Every command accepts `--mock`. Mock answers are simulated from the labelled truth and the
output says so in a banner; never present them as model output.

## Honest notes

- The services are fake. Their business logic is `{"service": "cart", "ok": true}`. The OOM
  kills, crash loops, failed readiness probes and job failures are real Kubernetes behaviour.
- `actionable` and `customer_impact` are asked as two-option **choices** rather than Laya's
  dedicated `noul` type: on this checkpoint the noul was far weaker on the same question (13%
  vs 77%). Both shapes are accepted when reading answers, so noul works with other models.
- The accuracy and calibration numbers above are from 30 hand-written alerts and one checkpoint
  version. They are enough to show the method, not to claim a benchmark. Measure on your own
  alerts before trusting any threshold; that is what `replay` and `calibrate.py` are for.
- Treat the model's output as a signal, never as authorization. Text inside an alert (logs
  included) can steer the answer.

## Credits and sources

- Laya, Convai Innovations: https://huggingface.co/convaiinnovations/laya
- TypeSafe Jev and the System One API: https://docs.typesafe.ai
- Temperature scaling: Guo et al., 2017, "On Calibration of Modern Neural Networks"

MIT licensed.
