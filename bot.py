"""
Vera Bot — magicpin AI Challenge submission
Full production-ready FastAPI implementation.
"""
import os, time, uuid, json, re
from datetime import datetime, timezone
from typing import Any, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx

# ─── App bootstrap ────────────────────────────────────────────────────────────
app = FastAPI(title="Vera Bot")
START = time.time()

# ─── In-memory stores ─────────────────────────────────────────────────────────
# contexts[(scope, context_id)] = {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}
# conversations[conv_id] = [{"from": role, "body": str, "ts": str}, ...]
conversations: dict[str, list] = {}
# suppression set: suppression_key → bool
fired_suppressions: set[str] = set()
# track which triggers have already produced an action this tick
fired_triggers: set[str] = set()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def get_ctx(scope: str, cid: str) -> Optional[dict]:
    entry = contexts.get((scope, cid))
    return entry["payload"] if entry else None

def count_by_scope() -> dict:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return counts

def detect_auto_reply(message: str) -> bool:
    """Detect WhatsApp Business canned auto-replies."""
    lower = message.lower()
    auto_patterns = [
        "thank you for contacting",
        "automated assistant",
        "we have received your message",
        "aapki madad ke liye shukriya",
        "our team will get back",
        "please note that this is an automated",
        "main ek automated",
        "hamari team",
        "pahuncha deti hoon",
        "aapki jaankari ke liye bahut-bahut shukriya",
        "we will respond shortly",
        "business hours",
    ]
    return any(p in lower for p in auto_patterns)

def detect_intent_transition(message: str) -> bool:
    """Detect when merchant signals a clear yes/intent to proceed."""
    lower = message.lower()
    yes_patterns = [
        "let's do it", "ok let's", "chalte hain", "haan kar lo", "yes do it",
        "go ahead", "please proceed", "kar do", "send it", "yes please",
        "ok go", "yes, send", "yes send", "perfect go", "haan bhejo",
        "mujhe join karna hai", "join karna hai", "judrna hai",
    ]
    return any(p in lower for p in yes_patterns)

def detect_not_interested(message: str) -> bool:
    lower = message.lower()
    no_patterns = [
        "not interested", "no thanks", "stop", "don't contact",
        "nahi chahiye", "mat karo", "band karo", "mujhe nahi chahiye",
        "please stop", "unsubscribe", "opt out",
    ]
    return any(p in lower for p in no_patterns)

# ─── Core LLM composer ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Vera — magicpin's merchant WhatsApp AI assistant.
Your job: compose the perfect next WhatsApp message to a merchant (or on behalf of a merchant to their customer).

SCORING DIMENSIONS (maximize each):
1. Specificity — real numbers, dates, source citations. Never generic ("increase sales").
2. Category fit — match the vertical's voice exactly (dentists=clinical-peer, restaurants=local-warm, salons=aspirational, gyms=motivational, pharmacies=trust-utility).
3. Merchant fit — personalize to their exact metrics, active offers, conversation history, language preference.
4. Trigger relevance — WHY NOW must be crystal clear from the message itself.
5. Engagement compulsion — use 1-2 levers: specificity, loss aversion, social proof, effort externalization, curiosity, reciprocity, single binary CTA.

HARD RULES:
- Single CTA per message (YES/STOP for action triggers; no CTA for pure-info).
- NO promotional tone for dentists/pharmacies — peer/clinical/trust voice only.
- Hindi-English code-mix if merchant languages include "hi". Match what they write.
- NO fabrication — only use data given in contexts.
- NO long preambles ("I hope you're doing well..."). Get to the point immediately.
- NO re-introduction after first message.
- Service+price specificity: "Haircut @ ₹99" not "10% off".
- Keep messages concise — WhatsApp readability (~80-150 words max).
- CTA must be the LAST line.

ANTI-PATTERNS (will lose points):
- Generic offers ("Flat 30% off").
- Multiple CTAs.
- Buried CTA.
- Hallucinated data.
- Same message verbatim as a previous turn.

OUTPUT FORMAT — respond ONLY with valid JSON (no markdown, no backticks):
{
  "body": "<the WhatsApp message>",
  "cta": "open_ended" | "binary_yes_stop" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "rationale": "<1-2 sentences: why this message, what compulsion lever used>"
}
"""

async def call_claude(user_prompt: str) -> dict:
    """Call Claude claude-sonnet-4-20250514 and return parsed JSON output."""
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            ANTHROPIC_API_URL,
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b["text"] for b in data["content"] if b["type"] == "text")
        # Strip markdown code fences if present
        text = re.sub(r"```(?:json)?|```", "", text).strip()
        return json.loads(text)


def build_compose_prompt(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    conv_history: Optional[list] = None,
) -> str:
    """Build the structured prompt for the composer."""
    merchant_name = merchant.get("identity", {}).get("name", "Merchant")
    owner = merchant.get("identity", {}).get("owner_first_name", "")
    langs = merchant.get("identity", {}).get("languages", ["en"])
    lang_note = "Hindi-English code-mix preferred" if "hi" in langs else "English only"

    perf = merchant.get("performance", {})
    ctr = perf.get("ctr", 0)
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr", 0.03)
    ctr_vs_peer = f"{ctr:.3f} vs peer median {peer_ctr:.3f} ({'BELOW' if ctr < peer_ctr else 'above'})"

    active_offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
    expired_offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "expired"]

    # Digest items most relevant to this trigger
    digest_items = category.get("digest", [])
    digest_text = ""
    if digest_items:
        digest_text = "\n".join(
            f"  - [{d.get('source','')}] {d.get('title','')} (n={d.get('trial_n','')}, segment={d.get('patient_segment','')})"
            for d in digest_items[:3]
        )

    signals = merchant.get("signals", [])
    cust_agg = merchant.get("customer_aggregate", {})

    # Trigger payload
    trg_kind = trigger.get("kind", "")
    trg_payload = trigger.get("payload", {})
    trg_urgency = trigger.get("urgency", 2)

    # Conversation history (last 4 turns)
    hist = conv_history or merchant.get("conversation_history", [])
    hist_text = ""
    if hist:
        recent = hist[-4:]
        hist_text = "\n".join(f"  [{t['from']}]: {t['body']}" for t in recent)

    # Customer context
    cust_text = ""
    if customer:
        cid = customer.get("identity", {})
        crel = customer.get("relationship", {})
        cust_text = f"""
CUSTOMER (this is a customer-facing message):
  Name: {cid.get('name')}
  Language pref: {cid.get('language_pref', 'en')}
  State: {customer.get('state')}
  Last visit: {crel.get('last_visit')} | Visits: {crel.get('visits_total')}
  Services received: {crel.get('services_received', [])}
  Preferred slots: {customer.get('preferences', {}).get('preferred_slots', 'any')}
  Consent scope: {customer.get('consent', {}).get('scope', [])}
"""

    return f"""COMPOSE A WHATSAPP MESSAGE FOR THIS EXACT SCENARIO:

TRIGGER (why we're messaging NOW):
  Kind: {trg_kind}
  Urgency: {trg_urgency}/5
  Payload: {json.dumps(trg_payload, ensure_ascii=False)}

MERCHANT:
  Name: {merchant_name} ({owner})
  Category: {merchant.get('category_slug')}
  City/Locality: {merchant.get('identity', {}).get('city')}, {merchant.get('identity', {}).get('locality')}
  Subscription: {merchant.get('subscription', {}).get('status')} | Plan: {merchant.get('subscription', {}).get('plan')} | Days remaining: {merchant.get('subscription', {}).get('days_remaining')}
  Performance (30d): views={perf.get('views')}, calls={perf.get('calls')}, directions={perf.get('directions')}, CTR={ctr_vs_peer}
  7d delta: views {perf.get('delta_7d', {}).get('views_pct', 0):+.0%}, calls {perf.get('delta_7d', {}).get('calls_pct', 0):+.0%}
  Active offers: {active_offers or 'none'}
  Expired offers: {expired_offers or 'none'}
  Customer aggregate: total={cust_agg.get('total_unique_ytd')}, lapsed 180d+={cust_agg.get('lapsed_180d_plus')}, retention 6mo={cust_agg.get('retention_6mo_pct', 0):.0%}
  Signals: {signals}
  Language: {lang_note}

CATEGORY ({category.get('slug')}):
  Voice: {category.get('voice', {}).get('tone')} | Taboos: {category.get('voice', {}).get('vocab_taboo', [])}
  Peer stats: avg_rating={category.get('peer_stats', {}).get('avg_rating')}, avg_ctr={peer_ctr}
  Offer catalog examples: {[o['title'] for o in category.get('offer_catalog', [])[:3]]}
  Recent digest:
{digest_text or '  (none)'}
  Seasonal beats: {category.get('seasonal_beats', [])}
  Trend signals: {category.get('trend_signals', [])}
{cust_text}
CONVERSATION HISTORY (recent):
{hist_text or '  (first message to this merchant)'}

---
Now compose the ideal message. Remember: be specific with real numbers from above, match voice/language, make the CTA the last line.
"""


def build_reply_prompt(
    merchant_message: str,
    conv_history: list,
    merchant: dict,
    category: dict,
    trigger: Optional[dict] = None,
    is_auto_reply: bool = False,
    intent_transition: bool = False,
    not_interested: bool = False,
) -> str:
    owner = merchant.get("identity", {}).get("owner_first_name", "")
    name = merchant.get("identity", {}).get("name", "Merchant")
    langs = merchant.get("identity", {}).get("languages", ["en"])
    lang_note = "Hindi-English code-mix" if "hi" in langs else "English"

    hist_text = "\n".join(f"  [{t['from']}]: {t['body']}" for t in conv_history[-6:])

    situation = ""
    if is_auto_reply:
        situation = "⚠️ DETECTED AUTO-REPLY: This is a WhatsApp Business canned auto-reply, NOT a real human response. Try once more to reach the human, then plan to gracefully exit if they auto-reply again."
    elif intent_transition:
        situation = "✅ INTENT TRANSITION: The merchant has clearly said yes / want to proceed. IMMEDIATELY move to action mode — do NOT ask another qualifying question. Start doing the thing."
    elif not_interested:
        situation = "❌ NOT INTERESTED: The merchant has declined. Return action=end with a warm, graceful exit. Do not push further."
    else:
        situation = "Normal merchant reply — continue the conversation naturally, advance toward value."

    active_offers = [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"]
    peer_ctr = category.get("peer_stats", {}).get("avg_ctr", 0.03)
    merchant_ctr = merchant.get("performance", {}).get("ctr", 0)

    return f"""REPLY COMPOSER — What should Vera say next?

SITUATION: {situation}

MERCHANT: {name} ({owner}) | {merchant.get('category_slug')} | {lang_note}
  CTR: {merchant_ctr:.3f} vs peer {peer_ctr:.3f}
  Active offers: {active_offers}

CONVERSATION SO FAR:
{hist_text}

LATEST MERCHANT MESSAGE:
  "{merchant_message}"

---
Choose action:
- If auto-reply detected and already tried once → action=end
- If not interested → action=end  
- If needs more time → action=wait
- Otherwise → action=send with the next message

Respond ONLY with valid JSON (no markdown):
{{
  "action": "send" | "wait" | "end",
  "body": "<message if action=send, omit if end/wait>",
  "cta": "open_ended" | "binary_yes_stop" | "none",
  "wait_seconds": <int if action=wait>,
  "rationale": "<1 sentence>"
}}
"""

# ─── Endpoint schemas ─────────────────────────────────────────────────────────

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": count_by_scope(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera Pro",
        "team_members": ["AI Challenger"],
        "model": "claude-sonnet-4-20250514",
        "approach": (
            "Trigger-dispatched LLM composer with 4-context fusion. "
            "Auto-reply detection, intent-transition routing, and language-adaptive copy. "
            "Category-voice enforcement via system prompt. Rationale-driven CTA placement."
        ),
        "contact_email": "challenger@example.com",
        "version": "2.0.0",
        "submitted_at": now_iso(),
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"Must be one of {valid_scopes}"},
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]},
        )

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": now_iso(),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trg_id in body.available_triggers:
        # Skip if already fired
        if trg_id in fired_triggers:
            continue

        trg = get_ctx("trigger", trg_id)
        if not trg:
            continue

        sup_key = trg.get("suppression_key", "")
        if sup_key and sup_key in fired_suppressions:
            continue

        merchant_id = trg.get("merchant_id") or trg.get("payload", {}).get("merchant_id")
        customer_id = trg.get("customer_id")

        if not merchant_id:
            continue

        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue

        cat_slug = merchant.get("category_slug")
        category = get_ctx("category", cat_slug)
        if not category:
            continue

        customer = get_ctx("customer", customer_id) if customer_id else None

        # Check expiry
        exp = trg.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                now_dt = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
                if now_dt > exp_dt:
                    continue
            except Exception:
                pass

        try:
            prompt = build_compose_prompt(category, merchant, trg, customer)
            result = await call_claude(prompt)
        except Exception as e:
            # Fail gracefully — skip this trigger
            continue

        conv_id = f"conv_{merchant_id}_{trg_id}_{uuid.uuid4().hex[:6]}"
        send_as = result.get("send_as", "vera")
        body_text = result.get("body", "")
        if not body_text:
            continue

        # Track
        fired_triggers.add(trg_id)
        if sup_key:
            fired_suppressions.add(sup_key)
        conversations[conv_id] = [{"from": "vera", "body": body_text, "ts": body.now}]

        # Build template params (merchant name + first 2 words of body as preview)
        owner = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "Merchant")
        preview = " ".join(body_text.split()[:5]) + "..."
        template_name = f"vera_{trg.get('kind', 'generic')}_v1"

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trg_id,
            "template_name": template_name,
            "template_params": [owner, preview, body.now[:10]],
            "body": body_text,
            "cta": result.get("cta", "open_ended"),
            "suppression_key": sup_key,
            "rationale": result.get("rationale", ""),
        })

        # Cap at 20 actions per tick
        if len(actions) >= 20:
            break

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    merchant_id = body.merchant_id
    customer_id = body.customer_id
    message = body.message
    turn = body.turn_number

    # Add merchant's message to history
    conversations.setdefault(conv_id, []).append({
        "from": body.from_role,
        "body": message,
        "ts": body.received_at,
    })

    conv_history = conversations[conv_id]

    # Get contexts
    merchant = get_ctx("merchant", merchant_id) if merchant_id else {}
    cat_slug = (merchant or {}).get("category_slug", "")
    category = get_ctx("category", cat_slug) or {}

    # Determine conversation flags
    is_auto = detect_auto_reply(message)
    intent_yes = detect_intent_transition(message)
    not_int = detect_not_interested(message)

    # Count consecutive auto-replies
    auto_count = sum(
        1 for t in conv_history
        if t["from"] == body.from_role and detect_auto_reply(t["body"])
    )

    # Graceful exit after 2 consecutive auto-replies
    if auto_count >= 2:
        return {
            "action": "end",
            "rationale": f"Detected {auto_count} consecutive auto-replies. Gracefully exiting to avoid spam.",
        }

    # Hard not-interested → end
    if not_int:
        return {
            "action": "end",
            "rationale": "Merchant signaled not interested. Exiting gracefully without further push.",
        }

    # Max turns safety
    if turn >= 6:
        return {
            "action": "end",
            "rationale": "Reached max conversation depth. Closing gracefully.",
        }

    try:
        prompt = build_reply_prompt(
            message, conv_history, merchant or {}, category or {},
            is_auto_reply=is_auto,
            intent_transition=intent_yes,
            not_interested=not_int,
        )
        result = await call_claude(prompt)
    except Exception as e:
        return {
            "action": "send",
            "body": "Got it! Main kuch aur helpful information le aati hoon aapke liye. Ek minute.",
            "cta": "none",
            "rationale": f"LLM error fallback: {str(e)[:80]}",
        }

    action = result.get("action", "send")

    # Append bot reply to history if sending
    if action == "send" and result.get("body"):
        conversations[conv_id].append({
            "from": "vera",
            "body": result["body"],
            "ts": now_iso(),
        })

    if action == "end":
        return {"action": "end", "rationale": result.get("rationale", "Conversation concluded.")}
    elif action == "wait":
        return {
            "action": "wait",
            "wait_seconds": result.get("wait_seconds", 1800),
            "rationale": result.get("rationale", "Backing off as requested."),
        }
    else:
        return {
            "action": "send",
            "body": result.get("body", ""),
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", ""),
        }


@app.post("/v1/teardown")
async def teardown():
    """Optional: wipe state at end of test."""
    contexts.clear()
    conversations.clear()
    fired_suppressions.clear()
    fired_triggers.clear()
    return {"status": "wiped"}
