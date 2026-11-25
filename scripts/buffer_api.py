"""Buffer GraphQL client with the free-plan guard rails learned on GG Economics (23 Nov 2026):

- Buffer's free plan allows about 250 API calls per rolling 24 h. Every call is logged in log/buffer_calls.json and the
  client refuses to call once `buffer_daily_call_budget` (config.json, 230) is reached in the last 24 h.
- 429 → back off (Retry-After, else 5, 10, 20 s) up to 4 tries instead of crashing.
- Posts already SENT never change: monitor.py caches them in log/buffer_cache.json so it only asks about posts still in play.

dry=True never touches the network: it records what it would have sent."""
import json, time, urllib.request, urllib.error
from datetime import timedelta
from common import CFG, LOG, buffer_key, now_utc

CALLS = LOG / "buffer_calls.json"


class BudgetExceeded(Exception):
    pass


def _ledger():
    try:
        rows = json.loads(CALLS.read_text())
    except Exception:
        rows = []
    cut = (now_utc() - timedelta(hours=24)).isoformat()
    return [r for r in rows if r["at"] >= cut]


def calls_left():
    return CFG["buffer_daily_call_budget"] - len(_ledger())


def gql(query, variables, what="call", dry=False):
    if dry:
        print(f"  [dry] would call Buffer ({what}): {json.dumps(variables, ensure_ascii=False)[:600]}")
        return None
    rows = _ledger()
    if len(rows) >= CFG["buffer_daily_call_budget"]:
        raise BudgetExceeded(f"Buffer call budget reached ({len(rows)} calls in 24 h); try after {rows[0]['at']} + 24 h")
    key = buffer_key()
    if not key:
        raise RuntimeError("no Buffer API key (keychain service 'laqta-buffer' or env BUFFER_API_KEY)")
    req = urllib.request.Request(CFG["buffer_api_url"], data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    body = None
    for i in range(4):
        rows.append({"at": now_utc().isoformat(), "what": what})
        CALLS.write_text(json.dumps(rows, indent=0))
        try:
            body = json.loads(urllib.request.urlopen(req, timeout=60).read()); break
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 3: raise
            time.sleep(min(60, int(e.headers.get("Retry-After") or 0) or 5 * 2 ** i))
    if body.get("errors"):
        raise RuntimeError("GraphQL error: " + json.dumps(body["errors"])[:500])
    return body["data"]


CREATE_POST = """mutation($i: CreatePostInput!){ createPost(input:$i){ __typename
  ... on PostActionSuccess { post { id dueAt } } ... on MutationError { message } } }"""

SCHEDULED = """query($o: OrganizationId!){ posts(input:{organizationId:$o, filter:{status:[scheduled]}}, first:100){
  edges{ node{ id dueAt channelId text } } } }"""

POST_STATUS = """query($id: PostId!){ post(input:{id:$id}){ id status dueAt sentAt externalLink error { message } } }"""
