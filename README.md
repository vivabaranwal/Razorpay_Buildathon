# SettleTrace

**Reconciliation copilot for Razorpay merchants.**
Razorpay AI Buildathon 2026 — **AI Finance Controller** track.

SettleTrace answers two questions merchants cannot currently answer for
themselves:

1. **Which transactions and fee deductions make up the net amount that landed in
   my bank account?** Razorpay's own 2026 settlement-transparency post notes
   that most merchants receive net-only payouts with no transaction linkage.
2. **Does every order in my store reflect the true payment status on Razorpay's
   side?** Razorpay's webhook documentation recommends polling the Payments API
   as a fallback for missed or delayed webhooks; almost no small merchant builds
   it.

**Module 1 — Settlement Linkage Auditor** reconstructs a payout transaction by
transaction, independently recomputing the expected MDR fee, GST and reserve
from the merchant's rate card, and flags anything that disagrees with a reason
code and the exact delta.

**Module 2 — Payment State Reconciler** finds orders stuck in a non-terminal
state past their payment method's window, polls Razorpay for ground truth on a
capped backoff, corrects the local record, and writes an audit row for every
change. It runs automatically in the background.

---

## Running it

Requires Python 3.11+ and Node 18+.

The dashboard is at `/`; `/the-itch` is a long-form page on why the project
exists. Both are client-side routes, so a static host needs an SPA rewrite
(serve `index.html` for unknown paths) or a deep link to `/the-itch` will 404.

```bash
# 1. Backend
pip install -r requirements.txt
python reset_demo.py                          # seed the demo data
uvicorn settletrace.api.app:app --reload      # API on :8000

# 2. Frontend, in a second terminal
cd frontend
npm install
npm run dev                                   # dashboard on http://localhost:5173
```

**No credentials are needed.** The app runs on generated sample data with
template explanations, and says so on screen. Everything in the demo works in
this mode.

### Environment variables

All optional. Copy `.env.example` to `.env` in the project root and fill in what
you have. The path is anchored to the project root, so it is picked up wherever
you start the server from. **Restart the backend after editing.**

| Variable | What it is | If missing or invalid |
|---|---|---|
| `GEMINI_API_KEY` | **Free, no card** — [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) | Explanations use templates, badge reads "AI: fallback mode" |
| `GROQ_API_KEY` | **Free, no card** — [console.groq.com/keys](https://console.groq.com/keys) | Same |
| `ANTHROPIC_API_KEY` | Paid — [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) | Same |
| `LLM_PROVIDER` | `auto`, `gemini`, `groq` or `anthropic` | `auto` picks whichever key is present |
| `LLM_MODEL` | Model override | Blank uses the provider's default |
| `RAZORPAY_KEY_ID` | Razorpay Dashboard → Test Mode → Settings → API Keys (needs a business account) | Falls back to generated data, shows a banner |
| `RAZORPAY_KEY_SECRET` | Shown once when you generate the key | Same |
| `SETTLETRACE_USE_SANDBOX` | `true` to reconcile live sandbox data | Defaults to `false` (generated data) |
| `DATABASE_URL` | SQLite path | Defaults to `sqlite:///./settletrace.db` |

Any **one** LLM key enables live explanations. `auto` prefers Groq, then
Gemini, then Anthropic — ordered by how easily a key can be obtained, since the
job is three sentences of plain-language prose. Per-provider default models:
`gemini-3.6-flash`, `openai/gpt-oss-120b`, `claude-sonnet-5`.

On boot the backend prints a **CREDENTIAL CHECK** block naming each variable as
found or missing, so you never have to click through the UI to find out whether
your `.env` was read. Secrets are masked.

### Resetting between demo takes

```bash
python reset_demo.py
```

Wipes and reseeds to a byte-identical state every time: settlement
`setl_DEMO001`, 250 transactions, 9 planted defects, 10 exceptions, 3 stuck
orders, empty audit trail. Verified identical across consecutive runs.

---

## How AI is used, and how it is disclosed

The AI never makes a reconciliation decision. Matching, fee verification and
status correction are entirely deterministic and reproducible from the inputs
and `config/rules.yaml`. The LLM does exactly one thing: it writes a
plain-language explanation of an exception that has **already been computed and
persisted**. Impact ranking is arithmetic, not model output.

The provider is swappable (Gemini, Groq or Anthropic) precisely because the
explanation layer is advisory — which model writes the prose is a deployment
detail the reconciliation logic cannot depend on. `settletrace/engine/` imports
no LLM code at all.

The boundary is structural rather than a convention: `Explainer.explain()`
receives a frozen `ExceptionSummary` dataclass and returns a string, so it has
no object it could write a reason code or an amount onto even by mistake. A test
(`test_explanation_does_not_alter_exception_data`) reconciles the same batch
with explanations on and off and asserts every numeric field is byte-identical.

Disclosure is enforced by data, not by copy. Every exception stores
`explanation_source` (`llm` or `fallback`), and the API derives
`is_ai_explained` from that single field. The UI badge — "AI-generated
explanation" or "Fallback explanation (AI unavailable)" — reads from it
directly, so template text can never appear under an AI label. The header pill
reports *observed* liveness rather than mere key presence: the Anthropic SDK
accepts an invalid key at construction and only fails when called, so the badge
downgrades to "AI: fallback mode" after an authentication failure instead of
advertising a model that is not answering.

---

## Acceptance checklist — honest status

| # | Item | Status |
|---|---|---|
| 1 | AI explanation is a real LLM call when keyed; UI discloses fallback | **Pass** — verified live end to end with a real Groq key: explanations arrive from the model, `explanation_source=llm`, and the UI badge reads "AI-generated explanation". Every failure path (no key, rejected key, rate limit, timeout, truncated reply) degrades to template text with the badge flipped |
| 2 | At least one real call to Razorpay's sandbox API | **Partial — see caveat below** |
| 3 | Backoff re-checks run automatically in the background | **Pass** — scheduler corrected 3/3 stuck orders on its own tick, no click |
| 4 | Every correction writes an AuditLog row, visible in its own tab | **Pass** — 3 automatic rows, dedicated Audit log tab |
| 5 | Duplicate webhook event IDs are deduplicated | **Pass** — same ID twice returns `processed`, then `duplicate` |
| 6 | Batch report exportable as a file | **Pass** — CSV and JSON, with provenance in the file |
| 7 | Accuracy reported with precision/recall, not one percentage | **Pass** — 96.4% matched, precision 100%, recall 100% (9 tp, 0 fp, 0 fn) |
| 8 | Designed dark theme, tabs, working detail panel | **Pass** — verified by screenshot on all four tabs |
| 9 | Nothing silently dropped; rows in = rows out | **Pass** — 241 verified + 9 exceptions = 250 of 250 |

### The Razorpay caveat, stated plainly

**What is verified:** one real, read-only HTTPS call to Razorpay's live API
(`GET /v1/payments`), exercised through the "Test connection" button on the
Payment state tab. Against deliberately invalid test credentials it returned a
genuine `401 Authentication failed` in 714 ms — a real network round trip, not a
stub.

**What is not verified:** the sandbox *data-fetching* logic —
`settlement.recon_entity` and paginated payment fetching — is implemented
against Razorpay's documented API shapes but has **never been run against a live
sandbox account**, because no test-mode credentials were available during the
build. The parsed output has not been confirmed to match what the reconciliation
engine expects.

**Therefore the demo runs on generated sample data**, with one verified real
connectivity call. This is disclosed in the UI at all times: the header shows a
"Sample data" pill, and if live data is requested but unavailable a banner reads
"Sandbox unavailable — showing generated data" with the specific reason.

This is a deliberate, disclosed limitation, not an untested claim. If you supply
working test-mode keys, set `SETTLETRACE_USE_SANDBOX=true` and expect to debug
the fetch path — do not first try it live on stage.

---

## Deploying it

The backend is a single web process - the background re-check scheduler runs as
an asyncio task inside the API process, not as a separate worker - so it fits a
free tier that allows only one service.

**Backend.** Start command, reading the port the host injects rather than a
hardcoded one:

```bash
uvicorn settletrace.api.app:app --host 0.0.0.0 --port $PORT
```

Set `SEED_ON_BOOT=true` and `CORS_ALLOW_ORIGINS=https://your-frontend.example`
in the host's dashboard, along with whichever LLM key you have. Every variable
is optional in the sense that the app boots without it; the table above says
what each one costs you.

**Frontend.** A static Vite build. Set `VITE_API_BASE_URL` to the backend's
public origin - it is read at *build* time, so changing it needs a redeploy,
not just a restart. `frontend/vercel.json` supplies the SPA rewrite that keeps
a direct link to `/the-itch` from 404ing.

**Order matters:** deploy the backend first, because the frontend needs its URL
baked in at build time; then deploy the frontend; then put the frontend's URL
into `CORS_ALLOW_ORIGINS` and restart the backend.

**On ephemeral disks** - most free tiers - the SQLite file is lost on every
redeploy. That is fine here: the schema is recreated on boot and `SEED_ON_BOOT`
reseeds the demo dataset, so a redeploy returns the live site to the same known
state `reset_demo.py` produces locally. It only seeds an empty database, so a
restart never discards work done on the live site. Do not put real merchant
data behind this configuration - it is a demo deployment, not durable storage.

---

## Design decisions worth knowing

**Money is integer paise, never float.** Comparing settlement totals is the
whole point of the system, and binary floating point cannot represent decimal
currency exactly — a float model would manufacture penny-sized exceptions that
are artefacts of the representation rather than real discrepancies.

**Nothing is silently dropped.** Every input row leaves the engine either
verified or attached to an exception; `ReconciliationResult` asserts this before
returning. An empty batch reports 0% accuracy, never a vacuous 100%.

**Nothing changes state silently.** Every automatic correction and every human
resolution writes to an append-only audit table with old value, new value, actor
and reason. There is deliberately no endpoint that updates or deletes an audit
row.

**Precision and recall, not one number.** A single accuracy percentage cannot
distinguish "found every defect" from "flagged everything indiscriminately" — a
system raising an exception on every row would score perfect recall and be
useless. Detection is scored per (transaction, reason code), so a transaction
flagged for the *wrong* reason counts as a miss.

**Degrade, never crash.** Missing or rejected credentials, an unreachable LLM,
or a failing upstream fetch all degrade to a working state with a visible,
specific explanation. The app is designed to survive a bad network on stage.

---

## Layout

```
settletrace/
  engine/            deterministic reconciliation - no LLM calls anywhere
    fees.py          expected fee/GST/reserve, recomputed independently
    reconciler.py    matching, exception classification, batch accuracy
    metrics.py       precision/recall against known planted defects
  providers/         Razorpay client, sample generator, connectivity probe
  explainer.py       AI layer, structurally unable to alter outcomes
  llm_clients.py     swappable Gemini / Groq / Anthropic clients
  batch_service.py   ingest, reconcile, persist, then explain - in that order
  reconciler_service.py  stuck detection, backoff, webhook dedup
  scheduler.py       background loop driving the automatic re-check cycle
  audit.py           append-only trail; no update or delete path exists
  export.py          CSV/JSON reports that stand alone once downloaded
  startup.py         boot-time credential report
  api/               FastAPI service
frontend/            React + TypeScript + Tailwind dashboard
reset_demo.py        one-command reset to the exact demo state
config/rules.yaml    fee rates, tolerances, windows, backoff schedule
tests/               146 tests
```

## API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/batches/settlement` | Run a reconciliation batch |
| GET | `/batches/{id}/summary` | Throughput, accuracy, precision/recall |
| GET | `/batches/{id}/export` | Download the report as CSV or JSON |
| GET | `/exceptions` | List, filter by reason code, search by ID |
| POST | `/exceptions/{id}/resolve` | Mark resolved; requires a reviewer name |
| GET | `/orders/stuck` | Current stuck-candidate orders |
| POST | `/orders/{id}/recheck` | Poll Razorpay for ground truth now |
| GET | `/audit-log` | The append-only trail, newest first |
| GET | `/scheduler/status` | Heartbeat of the automatic re-check loop |
| GET | `/connectivity/razorpay` | One real read-only call to Razorpay |
| POST | `/webhooks/razorpay` | Receive webhooks, discarding duplicates |
| GET | `/health` | Status, data source, explanation mode, degraded state |

Interactive docs at `http://127.0.0.1:8000/docs`.

## Tests

```bash
python -m pytest tests/ -q     # 146 tests
```

## Status

Hackathon MVP: single-tenant, single-machine, SQLite. The production path in the
PRD roadmap — Go, PostgreSQL with an append-only ledger, Redis, OpenTelemetry —
is out of scope, though the audit table already follows the append-only pattern
that path depends on.
