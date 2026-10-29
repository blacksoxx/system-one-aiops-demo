#!/usr/bin/env python3
"""Alert triage with a System One model.

Every alert goes to a System One model (self-hosted Laya by default, or TypeSafe Jev) with
five typed questions. The model answers all of them in one pass, with probabilities. Alerts
it is confident about are handled automatically; everything else is escalated to a person.

  python3 triage.py check                        the model server is reachable
  python3 triage.py replay [--threshold 0.8]     score the model on the labelled alerts in alerts.json
  python3 triage.py watch  [--namespace shop]    turn live cluster events into alerts and triage them
  python3 triage.py ask                          paste any alert, triage it
  python3 triage.py log                          summarise the decisions a watch session wrote

Model server: --base-url or SYSTEMONE_BASE_URL (default http://localhost:8000/v1, a local
laya-serve). For TypeSafe Jev: --base-url https://api.typesafe.ai/v1 and TYPESAFE_API_KEY.

Add --mock to any command to rehearse without a model. Mock answers are SIMULATED and are
labelled as such; never present them as model output.

Standard library only. `watch` shells out to kubectl. Python 3.9+.
"""

import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = os.environ.get("SYSTEMONE_BASE_URL", "http://localhost:8000/v1").rstrip("/")
PRICE_PER_MTOK = float(os.environ["PRICE_PER_MTOK"]) if "PRICE_PER_MTOK" in os.environ else None  # set after --base-url

TAXONOMY = json.loads((HERE / "taxonomy.json").read_text())
COMPONENTS = {k: v["description"] for k, v in TAXONOMY["components"].items()}
TEAMS = {k: v["team"] for k, v in TAXONOMY["components"].items()}
CAUSES = TAXONOMY["causes"]
SEVERITY = TAXONOMY["severity"]
SEV_SHORT = ["none", "working hours", "today", "page now"]

# Temperature scaling fitted by calibrate.py (one T per question). Applied to the model's
# probabilities before any decision; ranking is unchanged, only how sure the numbers claim to be.
_cal_path = HERE / "calibration.json"
CALIBRATION = json.loads(_cal_path.read_text()).get("temperatures", {}) if _cal_path.exists() else {}


def temper(probs, T):
    z = {k: max(float(p), 1e-9) ** (1.0 / T) for k, p in probs.items()}
    s = sum(z.values())
    return {k: v / s for k, v in z.items()}


def calibrated(answers):
    """Apply the fitted temperatures to every answer that has a probability distribution."""
    if not CALIBRATION:
        return answers
    out = {}
    for q, a in answers.items():
        T = CALIBRATION.get(q)
        a = dict(a)
        if T and "probabilities" in a:
            a["probabilities"] = temper(a["probabilities"], T)
            if "choice" in a:
                a["choice"] = max(a["probabilities"], key=a["probabilities"].get)
        elif T and "noul" in a:
            p = min(max(float(a["noul"]), 1e-9), 1 - 1e-9)
            logit = math.log(p / (1 - p)) / T
            a["noul"] = 1 / (1 + math.exp(-logit))
        out[q] = a
    return out

QUESTIONS = {
    "component": {
        "type": "choice",
        "instructions": "Which component must be fixed to resolve this alert? "
                        "Pick where the fix happens, not where the symptom shows.",
        "criteria": COMPONENTS,
    },
    "cause": {
        "type": "choice",
        "instructions": "What is the most likely root cause of this alert?",
        "criteria": CAUSES,
    },
    # Yes/no questions are asked as two-option choices: on Laya's typed-decisions checkpoint the
    # dedicated noul type was much weaker than a choice on the same question (13% vs 80%+ here).
    # Both shapes are accepted when reading the answer, so noul still works with other models.
    "actionable": {
        "type": "choice",
        "instructions": "Does a human or automation need to take an action to fix this?",
        "criteria": {"yes": "Someone must act: fix, roll back, scale, replace, or open a vendor ticket",
                     "no": "Nothing to do: it resolved itself, it is expected, or it is informational"},
    },
    "customer_impact": {
        "type": "choice",
        "instructions": "Are shoppers affected right now?",
        "criteria": {"yes": "Users see errors, timeouts or missing features right now",
                     "no": "No user-visible effect at the moment"},
    },
    "severity": {
        "type": "score",
        "instructions": "How urgently does this alert need a response?",
        "criteria": SEVERITY,
    },
}


# ── terminal output ─────────────────────────────────────────────────────────

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def bold(t): return c(t, "1")
def dim(t): return c(t, "2")
def blue(t): return c(t, "34")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")


def bar(p, width=10):
    filled = round(p * width)
    return "█" * filled + dim("·" * (width - filled))


def mock_banner():
    print(yellow(bold("  ⚠  MOCK MODE: answers below are SIMULATED, not model output  ⚠")))
    print()


ROUTE_STYLE = {"AUTO-ROUTE": blue, "AUTO-CLOSE": green, "ESCALATE": yellow}


# ── talking to the model ────────────────────────────────────────────────────

def api_key():
    """TypeSafe needs a key. A self-hosted Laya server usually does not."""
    key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("LAYA_API_KEY")
    if not key and "typesafe.ai" in API:
        sys.exit("TYPESAFE_API_KEY is not set. Export it, or add --mock to rehearse offline.")
    return key


def http(method, path, body=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key():
        headers["Authorization"] = f"Bearer {api_key()}"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529) and attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            detail = e.read().decode(errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code} from {API}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise RuntimeError(f"cannot reach {API}: {e.reason}") from None


def ask_model(state, model):
    t0 = time.perf_counter()
    body = {"state": state, "questions": QUESTIONS}
    if model:
        body["model"] = model
    resp = http("POST", "/systemone", body)
    usage = resp.get("usage", {})
    return resp["answers"], usage.get("input_tokens", 0), time.perf_counter() - t0, resp.get("model", model or "?")


# ── simulated answers for rehearsal ─────────────────────────────────────────

def _mock_choice(options, truth, hard, rng):
    if truth not in options:
        truth = options[0]
    if hard and rng.random() < 0.3:
        top = rng.choice([o for o in options if o != truth])
    else:
        top = truth
    p_top = rng.uniform(0.42, 0.72) if hard else rng.uniform(0.86, 0.98)
    rest = [o for o in options if o != top]
    weights = [rng.random() ** 3 for _ in rest]
    if top != truth:
        weights[rest.index(truth)] += sum(weights) * 2  # the right answer is a close second
    total = sum(weights)
    probs = {o: (1 - p_top) * w / total for o, w in zip(rest, weights)}
    probs[top] = p_top
    return {"choice": top, "probabilities": probs, "confidence": p_top - max(probs[o] for o in rest)}


def mock_model(alert):
    rng = random.Random(alert_id(alert))
    t = alert.get("truth") or {"component": "cluster-platform", "cause": "transient-noise",
                               "actionable": True, "difficulty": "hard"}
    hard = t.get("difficulty") == "hard"
    act = rng.uniform(0.62, 0.8) if hard else rng.uniform(0.93, 0.99)
    if not t["actionable"]:
        act = 1 - act
    sev_base = {"critical": 2.6, "warning": 1.5, "info": 0.4}.get(alert["labels"].get("severity"), 1.5)
    sev = max(0.0, min(3.0, (sev_base if t["actionable"] else 0.3) + rng.uniform(-0.3, 0.3)))
    answers = {
        "component": _mock_choice(list(COMPONENTS), t["component"], hard, rng),
        "cause": _mock_choice(list(CAUSES), t["cause"], hard, rng),
        "actionable": {"noul": act},
        "customer_impact": {"noul": min(0.98, sev / 3 + rng.uniform(-0.1, 0.1)) if t["actionable"] else 0.04},
        "severity": {"score": sev},
    }
    latency = rng.uniform(0.08, 0.35)
    time.sleep(latency)
    return answers, len(render(alert)) // 4 + 900, latency, "mock"


# ── alerts ──────────────────────────────────────────────────────────────────
# One shape everywhere, close to Alertmanager's webhook payload:
#   {"labels": {"alertname", "severity", "namespace", "deployment"|"pod"|"job"|"node"},
#    "annotations": {"summary", "description"}, "startsAt": iso, "logs": [...]}

def alert_id(alert):
    return alert.get("id") or f"{alert['labels'].get('alertname')}/{alert_object(alert)}"


def alert_object(alert):
    lab = alert["labels"]
    for k in ("deployment", "job", "pod", "node", "service", "ingress"):
        if k in lab:
            return f"{k}/{lab[k]}"
    return lab.get("instance", "-")


def render(alert):
    """The state the model sees: the alert as an on-call engineer would read it."""
    lab, ann = alert["labels"], alert.get("annotations", {})
    lines = [
        f"ALERT {lab.get('alertname', 'Alert')} (rule severity: {lab.get('severity', 'unknown')})",
        f"namespace: {lab.get('namespace', '-')}   object: {alert_object(alert)}",
        f"summary: {ann.get('summary', '')}",
        f"description: {ann.get('description', '')}",
    ]
    if alert.get("logs"):
        lines.append("recent logs:")
        lines += [f"  {l}" for l in alert["logs"]]
    return "\n".join(lines)


# ── decisions ───────────────────────────────────────────────────────────────

def yes_prob(answer):
    """Probability of 'yes' from either a noul or a yes/no choice answer."""
    if "noul" in answer:
        return float(answer["noul"])
    return float(answer["probabilities"].get("yes", 0.0))


def decide(answers):
    comp = answers["component"]
    cause = answers["cause"]
    act = yes_prob(answers["actionable"])
    p_comp = comp["probabilities"][comp["choice"]]
    p_cause = cause["probabilities"][cause["choice"]]
    # Act automatically only when the model is sure of all three decisions that drive routing.
    confidence = min(p_comp, p_cause, max(act, 1 - act))
    return {
        "component": comp["choice"], "p_component": p_comp,
        "cause": cause["choice"], "p_cause": p_cause,
        "actionable": act >= 0.5, "p_actionable": act,
        "impact": yes_prob(answers["customer_impact"]),
        "severity": float(answers["severity"]["score"]),
        "confidence": confidence,
    }


def route(d, threshold):
    if d["confidence"] < threshold:
        return "ESCALATE"
    return "AUTO-ROUTE" if d["actionable"] else "AUTO-CLOSE"


def is_correct(d, truth):
    return (d["component"] == truth["component"] and d["cause"] == truth["cause"]
            and d["actionable"] == truth["actionable"])


def triage(alert, args):
    """Send one alert to the model and return (decision, route, meta)."""
    if args.mock:
        answers, tokens, latency, model = mock_model(alert)
    else:
        answers, tokens, latency, model = ask_model(render(alert), args.model)
        answers = calibrated(answers)
    d = decide(answers)
    return d, route(d, args.threshold), {"tokens": tokens, "latency_s": latency, "model": model,
                                         "cost_usd": tokens * PRICE_PER_MTOK / 1e6}


def print_decision(alert, d, r, meta):
    lab, ann = alert["labels"], alert.get("annotations", {})
    print(dim("─" * 78) + " " + dim(time.strftime("%H:%M:%S")))
    print(f"  {bold('ALERT')}  {bold(lab.get('alertname', '?'))} · {alert_object(alert)} · {lab.get('severity', '?')}")
    print(f"         {ann.get('summary', '')}")
    for l in alert.get("logs", [])[-3:]:
        print(dim(f"         log: {l[:110]}"))
    print(f"  {bold('MODEL')}  component   {d['component']:<18} {bar(d['p_component'])} {d['p_component']:.2f}")
    print(f"         cause       {d['cause']:<18} {bar(d['p_cause'])} {d['p_cause']:.2f}")
    print(f"         actionable  {('yes' if d['actionable'] else 'no'):<18} {bar(d['p_actionable'])} {d['p_actionable']:.2f}"
          f"     impact {d['impact']:.2f} · severity {d['severity']:.1f}/3 ({SEV_SHORT[min(3, round(d['severity']))]})")
    cost = f" · ${meta['cost_usd']:.6f}" if meta["cost_usd"] else ""
    print(dim(f"         confidence {d['confidence']:.2f} · {meta['latency_s'] * 1000:.0f} ms · {meta['tokens']} tokens{cost} · {meta['model']}"))
    team = TEAMS.get(d["component"], d["component"])
    if r == "AUTO-ROUTE":
        print(f"  {ROUTE_STYLE[r](bold('→ AUTO-ROUTE'))} to {bold(team)}: \"{d['component']} · {d['cause']} · {SEV_SHORT[min(3, round(d['severity']))]}\"")
    elif r == "AUTO-CLOSE":
        print(f"  {ROUTE_STYLE[r](bold('→ AUTO-CLOSE'))} as noise, kept for audit. Nobody is paged.")
    else:
        print(f"  {ROUTE_STYLE[r](bold('→ ESCALATE'))} to on-call, with a head start: {d['component']} {d['p_component']:.0%}, {d['cause']} {d['p_cause']:.0%}")
    print()


# ── commands ────────────────────────────────────────────────────────────────

def cmd_check(args):
    if args.mock:
        print("Mock mode: no model server needed.")
        return
    try:
        models = http("GET", "/models")
        print(green(f"{API} answers.") + " Models:",
              [m.get("id", m) if isinstance(m, dict) else m for m in models.get("data", models if isinstance(models, list) else [])])
    except RuntimeError:
        root = API.rsplit("/v1", 1)[0]
        req = urllib.request.Request(root + "/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(green(f"{API} answers.") + f" /health: {resp.read().decode()[:200]}")


def threshold_table(alerts, decs):
    labelled = [i for i, a in enumerate(alerts) if a.get("truth")]
    print(bold("Move the threshold") + dim("  (the calibration dial)"))
    print(dim(f"  {'threshold':<11}{'auto-handled':<16}{'accuracy when auto':<22}{'accuracy when escalated'}"))
    acc = lambda idx: (f"{sum(is_correct(decs[i], alerts[i]['truth']) for i in idx) / len(idx):.0%}" if idx else "–")
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        a = [i for i in labelled if decs[i]["confidence"] >= t]
        e = [i for i in labelled if decs[i]["confidence"] < t]
        print(f"  {t:<11.0%}{len(a):>3}/{len(labelled):<12}{acc(a):<22}{acc(e)}")


def reliability_table(alerts, decs):
    pairs = [(d["p_component"], d["component"] == a["truth"]["component"]) for d, a in zip(decs, alerts) if a.get("truth")]
    print(bold("Is the component probability honest?") + dim("  (reliability: what it said vs. how often it was right)"))
    ece, n = 0.0, len(pairs)
    for lo, hi in ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01)):
        b = [(p, ok) for p, ok in pairs if lo <= p < hi]
        if not b:
            continue
        conf = sum(p for p, _ in b) / len(b)
        acc = sum(ok for _, ok in b) / len(b)
        ece += len(b) / n * abs(acc - conf)
        print(f"  said {lo:.1f}–{min(hi, 1):.1f}: {len(b):>3} alerts · mean probability {conf:.2f} · actually right {acc:.0%}")
    print(f"  expected calibration error (ECE, lower is better): {ece:.3f}")


def cmd_replay(args):
    alerts = json.loads((HERE / args.file).read_text())[: args.limit or None]
    if args.mock:
        mock_banner()
    print(bold(f"Triaging {len(alerts)} alerts, 5 typed questions each, against {('mock' if args.mock else API)}…")
          + dim(f"  calibration: {'temperatures from calibration.json' if CALIBRATION and not args.mock else 'none'}\n"))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda a: triage(a, args), alerts))
    wall = time.perf_counter() - t0

    print(dim(f"  {'id':<4} {'alert':<30} {'component':<18} {'cause':<21} {'type':<6} {'severity':<14} {'confidence':<16} route"))
    decs = []
    for alert, (d, r, meta) in zip(alerts, results):
        decs.append(d)
        mark = ""
        if alert.get("truth"):
            mark = green(" ✓") if is_correct(d, alert["truth"]) else red(" ✗")
        print(f"  {alert['id']:<4} {alert['labels']['alertname'][:30]:<30} {d['component']:<18} {d['cause']:<21} "
              f"{'act' if d['actionable'] else 'noise':<6} {SEV_SHORT[min(3, round(d['severity']))]:<14} "
              f"{bar(d['confidence'])} {d['confidence']:4.0%}  {ROUTE_STYLE[r](f'{r:<10}')}{mark}")

    tokens = sum(m["tokens"] for _, _, m in results)
    lat = sorted(m["latency_s"] for _, _, m in results)
    routes = [r for _, r, _ in results]
    labelled = [i for i, a in enumerate(alerts) if a.get("truth")]
    auto = [i for i in labelled if routes[i] != "ESCALATE"]
    n = len(labelled)
    print()
    print(bold("Summary") + (yellow("  (SIMULATED)") if args.mock else ""))
    print(f"  {len(alerts)} alerts · {len(alerts) * len(QUESTIONS)} decisions in {wall:.1f}s wall clock"
          f" · median latency {lat[len(lat) // 2] * 1000:.0f} ms per alert · {tokens:,} input tokens"
          + (f" · ${tokens * PRICE_PER_MTOK / 1e6:.5f}" if PRICE_PER_MTOK else ""))
    if n:
        acc = lambda key: sum(decs[i][key] == alerts[i]["truth"][key] for i in labelled) / n
        print(f"  accuracy: component {acc('component'):.0%} · cause {acc('cause'):.0%} · actionable {acc('actionable'):.0%}"
              f" · all three {sum(is_correct(decs[i], alerts[i]['truth']) for i in labelled) / n:.0%}"
              f"   (random: component {1 / len(COMPONENTS):.0%}, cause {1 / len(CAUSES):.0%})")
    print(f"  threshold {args.threshold:.0%}: {blue(str(routes.count('AUTO-ROUTE')))} auto-routed, "
          f"{green(str(routes.count('AUTO-CLOSE')))} auto-closed as noise, {yellow(str(routes.count('ESCALATE')))} escalated")
    if auto:
        ok = sum(is_correct(decs[i], alerts[i]["truth"]) for i in auto)
        print(f"  auto-handled accuracy: {ok}/{len(auto)} = {ok / len(auto):.0%}")
    print()
    if n:
        threshold_table(alerts, decs)
        print()
        reliability_table(alerts, decs)


def cmd_ask(args):
    if args.mock:
        mock_banner()
    print(bold("Paste or type an alert (any format). Finish with an empty line. Ctrl-D to quit.\n"))
    while True:
        lines = []
        try:
            while True:
                line = input("> " if not lines else "  ")
                if not line.strip():
                    break
                lines.append(line)
        except EOFError:
            print()
            return
        if not lines:
            continue
        text = "\n".join(lines)
        alert = {"id": text, "labels": {"alertname": "adhoc", "severity": "unknown"}, "annotations": {"summary": text}}
        if args.mock:
            d, r, meta = triage(alert, args)
        else:
            answers, tokens, latency, model = ask_model(text, args.model)
            d = decide(calibrated(answers))
            r, meta = route(d, args.threshold), {"tokens": tokens, "latency_s": latency, "model": model,
                                                 "cost_usd": tokens * PRICE_PER_MTOK / 1e6}
        print_decision(alert, d, r, meta)


# ── watch: live cluster events → alerts ─────────────────────────────────────

FOR_SECONDS = {  # like Alertmanager's `for:`, how long a condition must hold before it fires
    "KubePodOOMKilled": 0,
    "KubePodCrashLooping": 0,
    "KubeDeploymentReplicasUnavailable": 15,
    "KubeJobFailed": 25,
}


class Kube:
    def __init__(self, namespace, context=None):
        self.base = ["kubectl", "-n", namespace] + (["--context", context] if context else [])

    def run(self, *args, timeout=20):
        try:
            out = subprocess.run(self.base + list(args), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            sys.exit("kubectl not found on PATH")
        except subprocess.TimeoutExpired:
            return ""
        return out.stdout if out.returncode == 0 else ""

    def get(self, kind):
        out = self.run("get", kind, "-o", "json")
        return json.loads(out) if out else {"items": []}

    def logs(self, *selector, previous=False, tail=15):
        args = ["logs", *selector, f"--tail={tail}"] + (["--previous"] if previous else [])
        out = self.run(*args)
        if not out and previous:
            out = self.run("logs", *selector, f"--tail={tail}")
        return [l.strip()[:160] for l in out.splitlines() if l.strip()][-tail:]

    def unhealthy_message(self, prefix):
        ev = self.get("events")
        msgs = [e for e in ev["items"] if e.get("reason") == "Unhealthy"
                and e.get("involvedObject", {}).get("name", "").startswith(prefix + "-")]
        msgs.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        return msgs[-1]["message"].strip() if msgs else ""


def owner_of(pod):
    for ref in pod["metadata"].get("ownerReferences", []):
        if ref["kind"] == "ReplicaSet":
            return "deployment", ref["name"].rsplit("-", 1)[0]
        if ref["kind"] == "Job":
            return "job", ref["name"]
    return "pod", pod["metadata"]["name"]


def memory_limit(pod, container):
    for ctr in pod["spec"].get("containers", []):
        if ctr["name"] == container:
            return ctr.get("resources", {}).get("limits", {}).get("memory", "unset")
    return "unset"


def scan(kube, namespace):
    """Look at the namespace once and return {key: candidate alert} for every active condition."""
    found = {}
    pods, deps, jobs = kube.get("pods"), kube.get("deployments"), kube.get("jobs")

    for pod in pods["items"]:
        kind, owner = owner_of(pod)
        pname = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []):
            cname, restarts = cs["name"], cs.get("restartCount", 0)
            last = (cs.get("lastState") or {}).get("terminated") or {}
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if last.get("reason") == "OOMKilled":
                found[f"{owner}/oom"] = {
                    "labels": {"alertname": "KubePodOOMKilled", "severity": "critical", "namespace": namespace,
                               kind: owner, "pod": pname},
                    "annotations": {"summary": f"Container {cname} in pod {pname} was OOMKilled",
                                    "description": f"The container exceeded its memory limit of {memory_limit(pod, cname)} "
                                                   f"and was killed by the kernel. Restart count {restarts}."},
                    "logs": lambda p=pname, c=cname: kube.logs(p, "-c", c, previous=True),
                }
            elif waiting.get("reason") == "CrashLoopBackOff":
                found[f"{owner}/crashloop"] = {
                    "labels": {"alertname": "KubePodCrashLooping", "severity": "warning", "namespace": namespace,
                               kind: owner, "pod": pname},
                    "annotations": {"summary": f"Pod {pname} is crash looping",
                                    "description": f"The container exits shortly after starting (last exit code "
                                                   f"{last.get('exitCode', '?')}) and has restarted {restarts} times. "
                                                   f"The deployment was updated just before this started."},
                    "logs": lambda p=pname, c=cname: kube.logs(p, "-c", c, previous=True),
                }

    for d in deps["items"]:
        name = d["metadata"]["name"]
        want = d["spec"].get("replicas", 1)
        have = d.get("status", {}).get("availableReplicas", 0) or 0
        if want > 0 and have < want and not any(k.startswith(name + "/") for k in found):
            msg = kube.unhealthy_message(name)
            found[f"{name}/unavailable"] = {
                "labels": {"alertname": "KubeDeploymentReplicasUnavailable",
                           "severity": "critical" if have == 0 else "warning", "namespace": namespace, "deployment": name},
                "annotations": {"summary": f"Deployment {name} has {have} of {want} replicas available",
                                "description": "Pods are running but failing their readiness probe. "
                                               + (f"Last probe result: {msg}" if msg else "")},
                "logs": lambda n=name: kube.logs(f"deployment/{n}"),
            }

    for j in jobs["items"]:
        name = j["metadata"]["name"]
        st = j.get("status", {})
        failed, succeeded = st.get("failed", 0), st.get("succeeded", 0)
        if failed:
            found[f"{name}/jobfailed"] = {
                "labels": {"alertname": "KubeJobFailed", "severity": "info" if succeeded else "warning",
                           "namespace": namespace, "job": name},
                "annotations": {"summary": f"Job {name} failed {failed} time(s)" + (", then succeeded on retry" if succeeded else ""),
                                "description": "The retry succeeded and the output was produced. No data is missing."
                                               if succeeded else "A retry is pending (backoff)."},
                "logs": lambda n=name: kube.logs("-l", f"job-name={n}", "--prefix", tail=8),
                "_settled": bool(succeeded),
            }
    return found


def cmd_watch(args):
    kube = Kube(args.namespace, args.context)
    if args.mock:
        mock_banner()
    print(bold(f"Watching namespace {args.namespace}") + dim(f" · model {('mock' if args.mock else API)} · threshold {args.threshold:.0%}"
                                                            f" · calibration {'on' if CALIBRATION and not args.mock else 'off'}"
                                                            f" · every {args.interval}s · decisions → {args.log}"))
    print(dim("  Conditions: OOMKilled, CrashLoopBackOff, deployment replicas unavailable, job failed. Ctrl-C to stop.\n"))
    active, pending = {}, {}
    log_path = HERE / args.log
    while True:
        now = time.time()
        found = scan(kube, args.namespace)
        for key, cand in found.items():
            if key in active:
                continue
            first = pending.setdefault(key, now)
            hold = FOR_SECONDS.get(cand["labels"]["alertname"], 0)
            if now - first < hold and not cand.get("_settled"):
                continue
            alert = {"labels": cand["labels"], "annotations": cand["annotations"],
                     "startsAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "logs": cand["logs"]() if callable(cand.get("logs")) else cand.get("logs", [])}
            try:
                d, r, meta = triage(alert, args)
            except RuntimeError as e:
                print(red(f"  model error: {e}"))
                continue
            print_decision(alert, d, r, meta)
            with log_path.open("a") as f:
                f.write(json.dumps({"ts": alert["startsAt"], "key": key, "alert": {k: v for k, v in alert.items() if k != "logs"},
                                    "decision": d, "route": r, "team": TEAMS.get(d["component"]), **meta}) + "\n")
            active[key] = alert
        for key in list(active):
            if key not in found:
                print(dim(f"  resolved: {key}  {time.strftime('%H:%M:%S')}"))
                del active[key]
        for key in list(pending):
            if key not in found:
                del pending[key]
        time.sleep(args.interval)


def cmd_log(args):
    path = HERE / args.log
    if not path.exists():
        sys.exit(f"no decisions yet ({path})")
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    print(bold(f"{len(rows)} decisions in {path.name}\n"))
    for r in rows:
        lab = r["alert"]["labels"]
        d = r["decision"]
        print(f"  {r['ts'][11:19]}  {lab['alertname']:<34} {alert_object(r['alert']):<26} "
              f"{d['component']:<18} {d['cause']:<21} {d['confidence']:4.0%}  {ROUTE_STYLE[r['route']](r['route'])}")
    routes = [r["route"] for r in rows]
    lat = sorted(r["latency_s"] for r in rows)
    print()
    print(f"  {blue(str(routes.count('AUTO-ROUTE')))} auto-routed · {green(str(routes.count('AUTO-CLOSE')))} auto-closed · "
          f"{yellow(str(routes.count('ESCALATE')))} escalated · median {lat[len(lat) // 2] * 1000:.0f} ms from alert to decision"
          + (f" · total ${sum(r['cost_usd'] for r in rows):.5f}" if any(r["cost_usd"] for r in rows) else ""))


def main():
    global API
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "replay", "watch", "ask", "log"])
    p.add_argument("--base-url", default=None, help=f"Jev-compatible server (default {API})")
    p.add_argument("--model", default=os.environ.get("SYSTEMONE_MODEL"), help="model name to request, e.g. typed-decisions or jev-1.13")
    p.add_argument("--threshold", type=float, default=0.8, help="act automatically at or above this confidence (default 0.8)")
    p.add_argument("--mock", action="store_true", help="simulated answers, no model needed")
    p.add_argument("--no-calibration", action="store_true", help="ignore calibration.json")
    p.add_argument("--file", default="alerts.json", help="replay: labelled alerts (default alerts.json)")
    p.add_argument("--limit", type=int, default=0, help="replay: only the first N alerts")
    p.add_argument("--workers", type=int, default=4, help="replay: parallel requests")
    p.add_argument("--namespace", default="shop", help="watch: namespace to observe")
    p.add_argument("--context", default=None, help="watch: kubectl context")
    p.add_argument("--interval", type=float, default=2.0, help="watch: seconds between scans")
    p.add_argument("--log", default="decisions.jsonl", help="watch/log: decision log file")
    args = p.parse_args()
    if args.base_url:
        API = args.base_url.rstrip("/")
    global PRICE_PER_MTOK, CALIBRATION
    if args.no_calibration:
        CALIBRATION = {}
    if PRICE_PER_MTOK is None:  # TypeSafe's list price for Jev; a self-hosted server costs nothing per call
        PRICE_PER_MTOK = 0.042 if "typesafe.ai" in API else 0.0
    {"check": cmd_check, "replay": cmd_replay, "watch": cmd_watch, "ask": cmd_ask, "log": cmd_log}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
