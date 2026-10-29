#!/usr/bin/env python3
"""Alertmanager webhook -> System One model -> three lanes -> Prometheus metrics (+ a board).

Runs inside the cluster (reads pods, logs and events with its service account) or on a laptop
with kubectl access. Every firing alert from Alertmanager is rewritten in plain language,
enriched with the pod's recent logs, sent to the model with five typed questions, and sorted:

  page   confident, actionable, serious            -> page the owning team, cause attached
  slack  not confident enough to act alone         -> post to a channel with the model's best guess
  noise  confident that nothing needs doing        -> close it, keep it for audit, page nobody

Alerts that are symptoms of an already-open alert on the same service (pod not ready, pod
restarting often) are folded into it instead of being triaged twice.

Endpoints
  POST /alerts       Alertmanager webhook, firing and resolved
  GET  /metrics      Prometheus text format: one triage_alert series per open alert, lane counters
  GET  /state.json   everything the board shows
  GET  /board        fallback dashboard: two tabs, the raw alert feed and the triaged lanes
  GET  /healthz

Environment
  SYSTEMONE_BASE_URL  model server, e.g. http://laya:8600/v1        SYSTEMONE_MODEL   e.g. typed-decisions
  TRIAGE_THRESHOLD    act alone at or above this confidence (0.6)   TRIAGE_NAMESPACE  shop
  PORT                8400
  LLM_BASE_URL, LLM_API_KEY, LLM_MODEL  optional OpenAI-compatible endpoint; writes a two-sentence
                      note for alerts in the slack lane (the slow System 2 step, only where needed)
"""

import json
import os
import queue
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage  # noqa: E402

NAMESPACE = os.environ.get("TRIAGE_NAMESPACE", "shop")
THRESHOLD = float(os.environ.get("TRIAGE_THRESHOLD", "0.6"))
MODEL = os.environ.get("SYSTEMONE_MODEL") or None
PORT = int(os.environ.get("PORT", "8400"))
triage.API = os.environ.get("SYSTEMONE_BASE_URL", triage.API).rstrip("/")
triage.PRICE_PER_MTOK = 0.0
LLM = {k: os.environ.get(f"LLM_{k}") for k in ("BASE_URL", "API_KEY", "MODEL")}

POD_RULES = {"KubePodOOMKilled", "KubePodCrashLooping", "KubeContainerImagePullFailed", "KubePodNotReady",
             "KubePodRestartingOften", "CPUThrottlingHigh"}      # rules whose `pod` label is the pod in trouble
CHILD_ALERTS = {"KubePodNotReady", "KubePodRestartingOften"}      # symptoms folded into the parent alert
ALWAYS_FOLD = {"KubePodRestartingOften"}                          # never worth a decision of its own
HOLD_SECONDS = 25                                                 # wait this long for a parent before triaging a child

LANE_TITLE = {"page": "Page on-call", "slack": "Slack #ops", "noise": "Closed as noise"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ── Kubernetes API: in-cluster token, or kubectl on a laptop ─────────────────

class KubeAPI:
    def __init__(self):
        tok = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.in_cluster = tok.exists()
        if self.in_cluster:
            self.token = tok.read_text().strip()
            self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
            self.ctx = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

    def raw(self, path, text=False):
        try:
            if self.in_cluster:
                req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
                with urllib.request.urlopen(req, timeout=10, context=self.ctx) as r:
                    body = r.read().decode(errors="replace")
            else:
                out = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, timeout=15)
                body = out.stdout if out.returncode == 0 else ""
        except urllib.error.HTTPError as e:  # 400/404 = pod gone or not started yet; triage continues without logs
            if e.code not in (400, 404):
                log(f"  k8s api {path.split('?')[0]}: {e}")
            return "" if text else {}
        except Exception as e:  # the alert still gets triaged, just without enrichment
            log(f"  k8s api {path.split('?')[0]}: {e}")
            return "" if text else {}
        if text:
            return body
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError:
            return {}

    def pod(self, name):
        return self.raw(f"/api/v1/namespaces/{NAMESPACE}/pods/{name}")

    def pods(self, selector):
        return self.raw(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={selector}").get("items", [])

    def deployment(self, name):
        return self.raw(f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}")

    def logs(self, pod, container=None, previous=False, tail=12):
        q = f"tailLines={tail}" + (f"&container={container}" if container else "") + ("&previous=true" if previous else "")
        body = self.raw(f"/api/v1/namespaces/{NAMESPACE}/pods/{pod}/log?{q}", text=True)
        if not body and previous:
            body = self.raw(f"/api/v1/namespaces/{NAMESPACE}/pods/{pod}/log?{q.replace('&previous=true', '')}", text=True)
        return [l.strip()[:160] for l in body.splitlines() if l.strip()][-tail:]

    def unhealthy_message(self, prefix):
        ev = self.raw(f"/api/v1/namespaces/{NAMESPACE}/events?fieldSelector=reason%3DUnhealthy").get("items", [])
        ev = [e for e in ev if e.get("involvedObject", {}).get("name", "").startswith(prefix + "-")]
        ev.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        return ev[-1]["message"].strip()[:160] if ev else ""


kube = KubeAPI()


def owner_of_pod(pod):
    for ref in pod.get("metadata", {}).get("ownerReferences", []):
        if ref["kind"] == "ReplicaSet":
            return "deployment", ref["name"].rsplit("-", 1)[0]
        if ref["kind"] == "Job":
            return "job", ref["name"]
    return "pod", pod.get("metadata", {}).get("name", "?")


def guess_owner(pod_name):
    """Owner from the pod name when the pod no longer exists: deployment pods end in two hashes, job pods in one."""
    parts = pod_name.split("-")
    if len(parts) >= 3 and len(parts[-2]) >= 8:
        return "deployment", "-".join(parts[:-2])
    if len(parts) >= 2:
        return "job", "-".join(parts[:-1])
    return "pod", pod_name


def quick_object(lab):
    """Object of an alert without touching the API (for alerts we fold or hold)."""
    if lab.get("job_name"):
        return f"job/{lab['job_name']}"
    if lab.get("deployment"):
        return f"deployment/{lab['deployment']}"
    if lab.get("pod"):
        k, o = guess_owner(lab["pod"])
        return f"{k}/{o}"
    return "?"


def container_status(pod, container):
    for cs in pod.get("status", {}).get("containerStatuses", []):
        if cs["name"] == container or not container:
            return cs
    return {}


def memory_limit(pod, container):
    for c in pod.get("spec", {}).get("containers", []):
        if c["name"] == container or not container:
            return c.get("resources", {}).get("limits", {}).get("memory", "unset")
    return "unset"


# ── from a Prometheus alert to something an engineer would write ────────────

def describe(am_alert):
    """Return (object_kind, object_name, summary, description, logs) for an Alertmanager alert."""
    lab = am_alert["labels"]
    name = lab["alertname"]
    pod_name = lab.get("pod") if name in POD_RULES else None
    container = lab.get("container") if pod_name else None
    pod = kube.pod(pod_name) if pod_name else {}
    if pod:
        kind, owner = owner_of_pod(pod)
    elif pod_name:
        kind, owner = guess_owner(pod_name)
    elif lab.get("deployment"):
        kind, owner = "deployment", lab["deployment"]
    elif lab.get("job_name"):
        kind, owner = "job", lab["job_name"]
    else:
        kind, owner = "pod", "?"
    cs = container_status(pod, container) if pod else {}
    restarts = cs.get("restartCount", 0)
    logs = []

    if name == "KubePodOOMKilled":
        summary = f"{owner}: container killed for exceeding its memory limit (OOMKilled, {restarts} restarts)"
        desc = (f"The {owner} service's process grew past its memory limit of {memory_limit(pod, container)} and was "
                f"killed by the kernel. Kubernetes restarts it and it happens again. Its last log lines are below.")
        logs = kube.logs(pod_name, container, previous=True)
    elif name == "KubePodCrashLooping":
        code = ((cs.get("lastState") or {}).get("terminated") or {}).get("exitCode", "?")
        summary = f"{owner}: the application exits right after it starts (CrashLoopBackOff, {restarts} restarts)"
        desc = (f"The {owner} service's own process exits with code {code} a moment after starting, every time. This "
                f"began right after a new {owner} configuration or version was rolled out. Its last log lines are below.")
        logs = kube.logs(pod_name, container, previous=True)
    elif name == "KubeContainerImagePullFailed":
        msg = ((cs.get("state") or {}).get("waiting") or {}).get("message", "")
        image = next((c.get("image") for c in pod.get("spec", {}).get("containers", []) if c["name"] == container), "?")
        summary = f"{owner}: the new release references a container image that does not exist"
        desc = (f"The release that was just rolled out for the {owner} service points at the image '{image}', which the "
                f"registry does not have. The new instances never start; the previous version keeps running where it "
                f"still exists. Registry message: {msg[:140]}")
    elif name == "KubeDeploymentReplicasUnavailable":
        dep = kube.deployment(owner)
        want = dep.get("spec", {}).get("replicas", "?")
        have = dep.get("status", {}).get("availableReplicas", 0) or 0
        sel = ",".join(f"{k}={v}" for k, v in dep.get("spec", {}).get("selector", {}).get("matchLabels", {}).items())
        pods = kube.pods(sel) if sel else []
        bad = next((p for p in pods if any(not c.get("ready") and (c.get("state") or {}).get("running")
                                            for c in p.get("status", {}).get("containerStatuses", []))), None)
        probe = kube.unhealthy_message(owner)
        summary = f"{owner}: {have} of {want} instances are healthy"
        desc = (f"The {owner} service's instances are running but failing their health check"
                f"{(': ' + probe) if probe else ''}. Its last log lines are below.")
        if bad:
            logs = kube.logs(bad["metadata"]["name"])
    elif name == "KubeJobFailed":
        pods = kube.pods(f"job-name={owner}")
        failed = sum(1 for p in pods if p.get("status", {}).get("phase") == "Failed")
        ok = any(p.get("status", {}).get("phase") == "Succeeded" for p in pods)
        summary = f"{owner}: batch job failed {failed} time(s)" + (", then succeeded on retry" if ok else "")
        desc = ("The retry succeeded and the report was produced. No data is missing." if ok
                else "A retry is pending (backoff). Its log lines are below.")
        for p in sorted(pods, key=lambda p: p["metadata"].get("creationTimestamp", "")):
            logs += [f"[{p['metadata']['name']}] {l}" for l in kube.logs(p["metadata"]["name"], tail=4)]
    elif name == "CPUThrottlingHigh":
        summary = f"{owner}: CPU is being throttled, requests are slowing down"
        desc = (f"The {owner} service keeps hitting its CPU limit, so the kernel pauses it for part of every second. "
                f"Latency rises; nothing is crashing. Its last log lines are below.")
        logs = kube.logs(pod_name, container)
    else:  # child alerts or anything we did not anticipate: keep Prometheus's own words
        ann = am_alert.get("annotations", {})
        summary = ann.get("summary") or f"{owner}: {name}"
        desc = ann.get("description", "")
        if pod_name:
            logs = kube.logs(pod_name, container)
    return kind, owner, summary, desc, logs


def lane_for(d):
    if 1 - d["p_actionable"] >= THRESHOLD:
        return "noise"
    if d["confidence"] >= THRESHOLD and d["actionable"] and d["severity"] >= 1.5:
        return "page"
    return "slack"


def llm_note(state, d):
    """Optional: a two-sentence note from an LLM for alerts a human will look at."""
    if not (LLM["BASE_URL"] and LLM["MODEL"]):
        return ""
    prompt = (f"You are helping an on-call engineer. In two short sentences, say what most likely happened and the first "
              f"thing to check. A fast triage model suggests component '{d['component']}' ({d['p_component']:.0%}) and cause "
              f"'{d['cause']}' ({d['p_cause']:.0%}); use that as a hint, not as truth.\n\nALERT:\n{state}")
    body = {"model": LLM["MODEL"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 120, "temperature": 0.2}
    headers = {"Content-Type": "application/json"}
    if LLM["API_KEY"]:
        headers["Authorization"] = f"Bearer {LLM['API_KEY']}"
    try:
        req = urllib.request.Request(LLM["BASE_URL"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"  llm note failed: {e}")
        return ""


# ── state ───────────────────────────────────────────────────────────────────

LOCK = threading.Lock()
STATE = {
    "received": 0, "folded": 0, "decisions": {"page": 0, "slack": 0, "noise": 0},
    "open": {},      # fingerprint -> triaged alert (lane, decision, ...)
    "raw": {},       # fingerprint -> every firing alert as received (the "today" feed)
    "history": [],   # last 50 events
    "latencies": [],
    "held": {},      # fingerprint -> (alert, since, object): symptoms waiting for their incident
}
WORK = queue.Queue()


def remember(event):
    STATE["history"].insert(0, event)
    del STATE["history"][50:]


def process(am_alert):
    fp = am_alert.get("fingerprint") or json.dumps(am_alert["labels"], sort_keys=True)
    lab = am_alert["labels"]
    name = lab["alertname"]

    if am_alert.get("status") == "resolved":
        with LOCK:
            STATE["raw"].pop(fp, None)
            STATE["held"].pop(fp, None)
            gone = STATE["open"].pop(fp, None)
        if gone:
            remember({"ts": now(), "kind": "resolved", "alertname": name, "object": gone["object"]})
            log(f"resolved  {name} {gone['object']}")
        return

    obj = quick_object(lab)
    with LOCK:
        STATE["received"] += 1
        STATE["raw"][fp] = {"ts": now(), "alertname": name, "object": obj, "severity": lab.get("severity", "?"),
                            "summary": am_alert.get("annotations", {}).get("summary", name)}
        if name in ALWAYS_FOLD:
            STATE["folded"] += 1
            STATE["raw"][fp]["folded"] = True
            log(f"folded    {name} {obj} (symptom only)")
            return
        if name in CHILD_ALERTS:
            STATE["held"][fp] = (am_alert, time.time(), obj)
            log(f"holding   {name} {obj} for {HOLD_SECONDS}s in case its incident arrives")
            return
    triage_alert(am_alert, fp)


def triage_alert(am_alert, fp):
    lab = am_alert["labels"]
    name = lab["alertname"]
    t0 = time.time()
    kind, owner, summary, desc, logs = describe(am_alert)
    obj = f"{kind}/{owner}"
    with LOCK:
        if fp in STATE["raw"]:
            STATE["raw"][fp]["summary"] = summary
            STATE["raw"][fp]["object"] = obj
        # one open incident per service: a second rule firing on the same object (OOM then crash loop,
        # image pull then replicas unavailable) is the same incident, not a new decision
        same = [a for a in STATE["open"].values() if a["object"] == obj and a["alertname"] not in CHILD_ALERTS]
        if name not in CHILD_ALERTS and same and same[0]["alertname"] != name:
            STATE["folded"] += 1
            STATE["raw"][fp]["folded"] = True
            log(f"folded    {name} {obj} into the open {same[0]['alertname']} incident")
            return
        if name not in CHILD_ALERTS:  # a real incident folds any held or open symptom on the same service
            for k, (_, _, hobj) in list(STATE["held"].items()):
                if hobj == obj:
                    STATE["held"].pop(k)
                    STATE["folded"] += 1
                    STATE["raw"][k]["folded"] = True
                    log(f"folded    held symptom on {obj} into {name}")
            for k, a in list(STATE["open"].items()):
                if a["object"] == obj and a["alertname"] in CHILD_ALERTS:
                    STATE["open"].pop(k)
                    STATE["folded"] += 1
                    STATE["decisions"][a["lane"]] -= 1

    alert = {"labels": {**lab, "namespace": NAMESPACE, kind: owner}, "annotations": {"summary": summary, "description": desc},
             "logs": logs}
    state = triage.render(alert)
    try:
        answers, tokens, latency, model = triage.ask_model(state, MODEL)
    except Exception as e:
        log(f"model error on {name} {obj}: {e}")
        return
    d = triage.decide(triage.calibrated(answers))
    lane = lane_for(d)
    note = llm_note(state, d) if lane == "slack" else ""
    team = triage.TEAMS.get(d["component"], d["component"])
    sev_label = triage.SEV_SHORT[min(3, round(d["severity"]))]
    rec = {"fp": fp, "ts": now(), "alertname": name, "object": obj, "severity": lab.get("severity", "?"),
           "summary": summary, "logs": logs[-3:], "lane": lane, "team": team, "sev_label": sev_label,
           "component": d["component"], "p_component": round(d["p_component"], 2), "cause": d["cause"],
           "p_cause": round(d["p_cause"], 2), "actionable": d["actionable"], "p_actionable": round(d["p_actionable"], 2),
           "impact": round(d["impact"], 2), "severity_score": round(d["severity"], 2), "confidence": round(d["confidence"], 2),
           "model": model, "latency_s": round(time.time() - t0, 2), "note": note}
    with LOCK:
        STATE["open"][fp] = rec
        STATE["decisions"][lane] += 1
        STATE["latencies"].append(rec["latency_s"])
    remember({"ts": rec["ts"], "kind": lane, **{k: rec[k] for k in ("alertname", "object", "team", "component", "cause", "confidence")}})
    log(f"{lane:<6}    {name} {obj} -> {d['component']} ({d['p_component']:.0%}) · {d['cause']} ({d['p_cause']:.0%})"
        f" · confidence {d['confidence']:.0%} · {rec['latency_s']}s" + (" · llm note" if note else ""))


def sweeper():
    """Held symptom alerts: fold them if their incident is open, triage them if nothing came."""
    while True:
        time.sleep(3)
        due = []
        with LOCK:
            for fp, (a, t, obj) in list(STATE["held"].items()):
                if any(o["object"] == obj and o["alertname"] not in CHILD_ALERTS for o in STATE["open"].values()):
                    STATE["held"].pop(fp)
                    STATE["folded"] += 1
                    STATE["raw"][fp]["folded"] = True
                    log(f"folded    {a['labels']['alertname']} {obj} into the open incident")
                elif time.time() - t >= HOLD_SECONDS:
                    STATE["held"].pop(fp)
                    due.append((a, fp))
        for a, fp in due:
            try:
                triage_alert(a, fp)
            except Exception as e:
                log(f"error triaging held alert: {e}")


def worker():
    while True:
        item = WORK.get()
        try:
            process(item)
        except Exception as e:
            log(f"error processing alert: {e}")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── HTTP ────────────────────────────────────────────────────────────────────

def metrics_text():
    with LOCK:
        lines = ["# HELP triage_alert One series per open, triaged alert; the value is the model's confidence.",
                 "# TYPE triage_alert gauge"]
        for a in STATE["open"].values():
            labels = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in (
                ("fingerprint", a["fp"]), ("alertname", a["alertname"]), ("object", a["object"]), ("lane", a["lane"]),
                ("team", a["team"]), ("component", a["component"]), ("cause", a["cause"]), ("severity", a["sev_label"]),
                ("summary", a["summary"][:120])))
            lines.append(f"triage_alert{{{labels}}} {a['confidence']}")
        lines += ["# TYPE triage_open_alerts gauge"]
        for lane in ("page", "slack", "noise"):
            lines.append(f'triage_open_alerts{{lane="{lane}"}} {sum(1 for a in STATE["open"].values() if a["lane"] == lane)}')
        lines += ["# TYPE triage_decisions_total counter"]
        for lane, n in STATE["decisions"].items():
            lines.append(f'triage_decisions_total{{lane="{lane}"}} {n}')
        lines += ["# TYPE triage_alerts_received_total counter", f"triage_alerts_received_total {STATE['received']}",
                  "# TYPE triage_alerts_folded_total counter", f"triage_alerts_folded_total {STATE['folded']}",
                  "# TYPE triage_decision_latency_seconds gauge",
                  f"triage_decision_latency_seconds {statistics.median(STATE['latencies']) if STATE['latencies'] else 0}"]
    return "\n".join(lines) + "\n"


def state_json():
    with LOCK:
        return json.dumps({"threshold": THRESHOLD, "model": MODEL or triage.API, "received": STATE["received"],
                           "folded": STATE["folded"], "decisions": STATE["decisions"],
                           "median_latency_s": round(statistics.median(STATE["latencies"]), 2) if STATE["latencies"] else None,
                           "raw": sorted(STATE["raw"].values(), key=lambda a: a["ts"], reverse=True),
                           "open": sorted(STATE["open"].values(), key=lambda a: a["ts"], reverse=True),
                           "history": STATE["history"]})


BOARD = """<!doctype html><html><head><meta charset="utf-8"><title>Alert triage</title>
<style>
body{margin:0;background:#111217;color:#d8d9da;font:15px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
header{display:flex;gap:18px;align-items:center;padding:14px 24px;border-bottom:1px solid #2c2f36}
header h1{font-size:18px;margin:0;font-weight:600}
.tabs button{background:#1f2229;color:#d8d9da;border:1px solid #363a44;padding:8px 16px;border-radius:6px;font-size:15px;cursor:pointer}
.tabs button.on{background:#3d71d9;border-color:#3d71d9;color:#fff}
.stats{display:flex;gap:12px;padding:16px 24px}
.stat{flex:1;background:#181b1f;border:1px solid #2c2f36;border-radius:8px;padding:12px 16px}
.stat .v{font-size:34px;font-weight:600;line-height:1.1}.stat .l{color:#9aa0a6;font-size:13px}
.red .v{color:#f2495c}.amber .v{color:#ff9830}.grey .v{color:#8e8e8e}.blue .v{color:#5794f2}
main{padding:0 24px 24px}
.lanes{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.lane{background:#181b1f;border:1px solid #2c2f36;border-radius:8px;padding:12px;min-height:300px}
.lane h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin:0 0 10px;color:#9aa0a6}
.lane.page h2{color:#f2495c}.lane.slack h2{color:#ff9830}.lane.noise h2{color:#8e8e8e}
.card{background:#111217;border:1px solid #2c2f36;border-left:4px solid #555;border-radius:6px;padding:10px 12px;margin-bottom:10px}
.page .card{border-left-color:#f2495c}.slack .card{border-left-color:#ff9830}.noise .card{border-left-color:#555;opacity:.75}
.card .t{font-weight:600}.card .s{color:#9aa0a6;font-size:13px;margin:2px 0 6px}
.card .d{font-size:13px}.card .d b{color:#fff}.card .n{font-size:13px;color:#c8c8c8;border-top:1px dashed #2c2f36;margin-top:6px;padding-top:6px}
.bar{display:inline-block;height:8px;width:90px;background:#2c2f36;border-radius:4px;vertical-align:middle;margin:0 6px}
.bar i{display:block;height:100%;background:#5794f2;border-radius:4px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 10px;border-bottom:1px solid #2c2f36;font-size:14px}
th{color:#9aa0a6;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.sev{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;background:#f2495c;color:#fff}
.sev.warning{background:#ff9830}.sev.info{background:#5794f2}
.folded{color:#777}
</style></head><body>
<header><h1>Alert triage</h1><div class="tabs"><button id="b1" class="on" onclick="tab(1)">Alert feed · today</button>
<button id="b2" onclick="tab(2)">Triaged · System One model</button></div><div id="meta" style="color:#9aa0a6;font-size:13px;margin-left:auto"></div></header>
<div class="stats" id="stats"></div><main id="main"></main>
<script>
let T=location.hash=='#triaged'?2:1;function tab(n){T=n;document.getElementById('b1').className=n==1?'on':'';document.getElementById('b2').className=n==2?'on':'';draw()}
let S=null;async function load(){try{S=await (await fetch('/state.json')).json();draw()}catch(e){}}
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function pct(p){return Math.round(p*100)+'%'}
function draw(){if(!S)return;const t=s=>s.slice(11,19);
document.getElementById('meta').textContent='model '+S.model+' · threshold '+pct(S.threshold)+(S.median_latency_s?' · median '+S.median_latency_s+' s to decide':'');
const open=S.open,by=l=>open.filter(a=>a.lane==l);
if(T==1){document.getElementById('stats').innerHTML=`<div class="stat red"><div class="v">${S.raw.filter(a=>!a.folded).length+S.raw.filter(a=>a.folded).length}</div><div class="l">alerts firing right now</div></div>
<div class="stat red"><div class="v">${S.raw.filter(a=>a.severity=='critical').length}</div><div class="l">critical</div></div>
<div class="stat"><div class="v">${S.received}</div><div class="l">received since start</div></div>`;
document.getElementById('main').innerHTML=`<table><tr><th>time</th><th>severity</th><th>alert</th><th>object</th><th>summary</th></tr>`+
S.raw.map(a=>`<tr><td>${t(a.ts)}</td><td><span class="sev ${a.severity}">${esc(a.severity)}</span></td><td>${esc(a.alertname)}</td><td>${esc(a.object)}</td><td>${esc(a.summary)}</td></tr>`).join('')+`</table>`}
else{document.getElementById('stats').innerHTML=`<div class="stat"><div class="v">${S.received}</div><div class="l">alerts received</div></div>
<div class="stat blue"><div class="v">${S.folded}</div><div class="l">folded into an existing incident</div></div>
<div class="stat red"><div class="v">${by('page').length}</div><div class="l">paged on-call</div></div>
<div class="stat amber"><div class="v">${by('slack').length}</div><div class="l">posted to Slack</div></div>
<div class="stat grey"><div class="v">${by('noise').length}</div><div class="l">closed as noise</div></div>`;
const card=a=>`<div class="card"><div class="t">${esc(a.summary)}</div><div class="s">${esc(a.alertname)} · ${esc(a.object)} · ${t(a.ts)}</div>
<div class="d">→ <b>${esc(a.team)}</b> · ${esc(a.component)} ${pct(a.p_component)} · ${esc(a.cause)} ${pct(a.p_cause)} · ${esc(a.sev_label)}<br>confidence <span class="bar"><i style="width:${pct(a.confidence)}"></i></span>${pct(a.confidence)}</div>${a.note?`<div class="n">${esc(a.note)}</div>`:''}</div>`;
document.getElementById('main').innerHTML=`<div class="lanes">
<div class="lane page"><h2>Page on-call · act now</h2>${by('page').map(card).join('')||'<div class="folded">nothing to page</div>'}</div>
<div class="lane slack"><h2>Slack #ops · look when you can</h2>${by('slack').map(card).join('')||'<div class="folded">quiet</div>'}</div>
<div class="lane noise"><h2>Closed as noise · nobody paged</h2>${by('noise').map(card).join('')||'<div class="folded">no noise yet</div>'}</div></div>`}}
tab(T);setInterval(load,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/metrics"):
            self._send(200, metrics_text(), "text/plain; version=0.0.4")
        elif self.path.startswith("/state.json"):
            self._send(200, state_json())
        elif self.path.startswith("/board") or self.path == "/":
            self._send(200, BOARD, "text/html; charset=utf-8")
        elif self.path.startswith("/healthz"):
            self._send(200, '{"ok": true}')
        else:
            self._send(404, '{"error": "not found"}')

    def do_POST(self):
        if not self.path.startswith("/alerts"):
            self._send(404, '{"error": "not found"}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, '{"error": "bad json"}')
            return
        alerts = payload.get("alerts", [])
        for a in alerts:
            WORK.put(a)  # answer Alertmanager right away; the model runs in the worker
        self._send(200, json.dumps({"queued": len(alerts)}))

    def log_message(self, fmt, *args):
        pass


def main():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=sweeper, daemon=True).start()
    try:
        root = triage.API.rsplit("/v1", 1)[0]
        with urllib.request.urlopen(root + "/health", timeout=10) as r:
            log(f"model server {triage.API} reachable: {r.read().decode()[:80]}")
    except Exception as e:
        log(f"WARNING model server {triage.API} not reachable yet: {e}")
    log(f"triage service on :{PORT} · namespace {NAMESPACE} · threshold {THRESHOLD:.0%} · "
        f"calibration {'on' if triage.CALIBRATION else 'off'} · kube api {'in-cluster' if kube.in_cluster else 'kubectl'}"
        f" · llm notes {'on' if LLM['BASE_URL'] and LLM['MODEL'] else 'off'}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
