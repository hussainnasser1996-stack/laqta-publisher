#!/usr/bin/env python3
"""Self-healing triage, adapted from GG Economics (scripts/triage.py + FIXER.md).

classify(error_text) → "transient" | "owner" | "code" | "gate"
record(slot, step, error) → updates log/failures.json:
  transient (network, 5xx, 429, budget) → status "retrying"; run.py tries again on its next pass
  owner     (lost Instagram login, expired key, billing)   → status "open", needs Hussain/Mahmood; listed in PLAN.md terms
  gate      (a quality gate said no)                        → status "open"; fix the post, not the code
  code      (anything else)                                 → status "open" for the fixer (a Claude session reading FIXER.md)
Circuit breaker: 3 failures of the same slot in one day → touch queue/<slot>/halted (run.py skips it). GG looped 55x on
22 Nov before it had this.
resolve(slot) closes open entries when the slot later books fine.

CLI: triage.py --report   prints open entries."""
import json, re, sys
from common import LOG, Q, now_utc

F = LOG / "failures.json"
TRANSIENT = (r"Could not resolve host|Connection (reset|refused|timed out)|timed? ?out|HTTP Error 5\d\d|\b50[234]\b|"
             r"Internal Server Error|rate.?limit|429|ECONNRESET|Temporary failure|TLS|RemoteDisconnected|IncompleteRead|"
             r"media not reachable yet|BudgetExceeded|budget reached|non-fast-forward")
OWNER = r"isDisconnected|disconnected|re-?authori[sz]e|invalid_grant|token (has )?expired|Unauthorized|401|no Buffer API key|billing|payment"
GATE = r"gates? (fail|have not passed)|did not pass arabic|humanizer|banned|not approved"


def classify(err):
    if re.search(OWNER, err, re.I): return "owner"
    if re.search(TRANSIENT, err, re.I): return "transient"
    if re.search(GATE, err, re.I): return "gate"
    return "code"


def _db():
    try: return json.loads(F.read_text())
    except Exception: return []


def record(slot, step, err):
    db, now = _db(), now_utc().strftime("%Y-%m-%dT%H:%MZ")
    kind = "gate" if step == "gates" else classify(err)
    e = next((x for x in db if x["status"] != "resolved" and x["slot"] == slot and x["step"] == step), None)
    if not e:
        e = {"slot": slot, "step": step, "opened": now, "status": "open", "kind": kind, "runs": []}; db.append(e)
    e["runs"].append({"at": now, "error": err[-1500:]}); e["kind"] = kind
    e["status"] = "retrying" if kind == "transient" else "open"
    e["runs"] = e["runs"][-20:]
    # a gate failure is "waiting for a human" (media to shoot, caption to fix): never halt it, the next run re-checks
    if kind != "gate" and len([r for r in e["runs"] if r["at"][:10] == now[:10]]) >= 3 and (Q / slot).is_dir():
        (Q / slot / "halted").write_text(f"{now} halted after 3 failures today: {kind}\n"); e["halted"] = now; e["status"] = "open"
    F.write_text(json.dumps(db, ensure_ascii=False, indent=1))
    return kind


def resolve(slot):
    db = _db(); now = now_utc().strftime("%Y-%m-%dT%H:%MZ"); hit = False
    for e in db:
        if e["slot"] == slot and e["status"] != "resolved": e.update(status="resolved", resolved=now); hit = True
    if hit: F.write_text(json.dumps(db, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    for e in _db():
        if e["status"] != "resolved":
            lines = [l.strip() for l in e['runs'][-1]['error'].splitlines() if l.strip().startswith(('x ', 'FAIL', 'Buffer', 'Runtime'))] or e['runs'][-1]['error'].splitlines()
            print(f"{e['status']:9} {e['kind']:9} {e['slot']} {e['step']}: {lines[-1][:140]}")
