# Vera Pro — magicpin AI Challenge Submission

## Approach

**Architecture**: Stateful FastAPI service + Claude claude-sonnet-4-20250514 as the LLM composer.

### What makes this bot score high on all 5 rubric dimensions:

**1. Decision quality (trigger × merchant × category fusion)**
- Every `/v1/tick` loops triggers, resolves category+merchant context, then builds a highly structured prompt that forces the LLM to anchor on the *specific* trigger kind before composing.
- Trigger kind (research_digest, recall_due, perf_spike, etc.) is explicitly labeled so the LLM knows *why now*.

**2. Specificity**
- The prompt template surfaces real numbers: exact CTR vs peer median, exact view/call counts, exact offer titles with ₹ prices, exact digest item with source citation and trial_n.
- System prompt explicitly bans "10% off" / "increase your sales" framings.

**3. Category fit**
- Voice enforcement in system prompt: dentists=peer_clinical, restaurants=local-warm, salons=aspirational, gyms=motivational, pharmacies=trust-utility.
- Taboos from category context are passed directly to the LLM.

**4. Merchant fit**
- Owner first name used for personalization.
- Language detection: if `"hi"` in merchant languages → Hindi-English code-mix.
- Active vs expired offer awareness.
- Conversation history (last 4 turns) included in prompt so bot never repeats itself.

**5. Engagement compulsion**
- System prompt teaches 8 compulsion levers (specificity, loss aversion, social proof, effort externalization, curiosity, reciprocity, asking the merchant, single binary CTA).
- CTA is always the last line.

### Multi-turn conversation handling (`/v1/reply`)
- **Auto-reply detection**: pattern-matched on 12 common WA Business auto-reply signatures. After 2 consecutive auto-replies → graceful exit.
- **Intent transition**: if merchant says "let's do it" / "go ahead" / "haan kar lo" → immediately moves to action mode, not more qualifying questions.
- **Not-interested routing**: detects STOP / opt-out signals → ends cleanly.
- **Max turns**: hard cap at 6 turns to avoid spam.

### Adaptive context (Phase 3)
- `/v1/context` is idempotent by (scope, context_id, version).
- Higher version always atomically replaces prior. 
- Composer always reads `contexts[key]["payload"]` at compose time (not cached), so new digest items / updated perf snapshots are automatically used.

## Tradeoffs

- **In-memory state** is sufficient for a 60-min test window; production would use Redis.
- **Suppression** is in-memory per process restart — handled by checking `suppression_key` before firing.
- LLM temperature defaults to 0 in Claude's API for determinism.

## What additional context would have helped most

1. **Real conversation transcripts per trigger kind** — knowing whether dentists respond better to clinical-research nudges vs recall nudges would let us tune the dispatch logic.
2. **Historical CTR lift data per compulsion lever per category** — so we could rank levers (curiosity vs social proof) by empirical effectiveness per vertical.
3. **Time-of-day engagement data** — knowing when merchants actually read WhatsApp would let the tick dispatcher be smarter about *when* to fire (not just whether).

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then run the judge simulator:
```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

## Deploying (Railway / Render / Fly.io)

```bash
# Railway
railway init && railway up
railway variables set ANTHROPIC_API_KEY=sk-ant-...

# Or Docker
docker build -t vera-bot .
docker run -e ANTHROPIC_API_KEY=sk-ant-... -p 8080:8080 vera-bot
```

Set your public URL in the submission portal.
