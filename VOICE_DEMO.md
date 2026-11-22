# BoxSource voice intake — Vapi phone demo

Call a phone number, say what packaging you need, and BoxSource registers the sourcing
request. The voice agent asks the follow-up questions the product needs (item, quantity
per month, delivery location, ply, size), reads the request back, and on "yes" registers
it. **Then the sourcing agent takes over**: it finds and verifies manufacturers, drafts
quote requests, and so on. The voice agent never answers status questions or approves
anything — progress, quotes and approvals live on the dashboard.

```
Caller ⇄ Vapi (phone, speech-to-text, LLM, text-to-speech)
          └─ tool ask_backend_agent(query) ─▶ POST https://<ngrok>/vapi/tool
                                               voice_server.py → engine/voice.py
                                               └─ registers a row in `requests`
                                                    └─ worker (python cli.py run) sources it
```

## 1. Run it

Prerequisites: Python 3.12+, then `python -m pip install -r engine/requirements.txt`.
`.env` needs `GEMINI_API_KEY`, plus `SUPABASE_URL` and `SUPABASE_SECRET_KEY` when
`storage.backend` in `config.yaml` is `supabase`. **The bridge refuses to start and names
the missing variable if one is absent.**

| Terminal | Command | What it is |
|---|---|---|
| 1 | `python cli.py run` | the sourcing agent (worker) that takes over registered requests |
| 2 | `python cli.py voice` | the Vapi bridge on `http://127.0.0.1:8000` |
| 3 | `ngrok http 8000` | public HTTPS URL for Vapi — copy the `Forwarding` URL |

Check through the tunnel: `curl https://<ngrok-url>/health` → `{"ok":true}`

Test without a phone (sample payload in `scripts/vapi-sample.json`):

```bash
curl -X POST http://127.0.0.1:8000/vapi/tool \
  -H "Content-Type: application/json" -d @scripts/vapi-sample.json
```

Expected (HTTP 200, ~1–3 s):

```json
{"results":[{"toolCallId":"call_abc","result":"Let me confirm: 50,000 5-ply corrugated boxes a month, 18 by 12 by 12 inches, delivered to Gurgaon. Shall I register it?"}]}
```

Then answer for the same call id (`"query": "yes"`) and the reply is
*"Your request is registered. Our sourcing agent takes it from here, and you can follow it
on the BoxSource dashboard."* — `python cli.py status` now lists a `VOICE-democall-…`
request (readable call-id prefix plus a hash of the full call id).

How the endpoint behaves:
- `message.type` other than `tool-calls` (status updates, end-of-call reports) → `200 {}`.
- `arguments` may be an object or a JSON string; a missing or empty `query` gets a spoken
  "Sorry, I didn't catch that".
- Each tool call is capped at 8 s; on a timeout or any error the caller hears *"Sorry, I
  couldn't get that right now, could you try again?"*. Vapi never sees a 500.
- One phone call registers one request (`VOICE-<call id>`), so a retried tool call can't
  register it twice.

## 2. Vapi dashboard setup

1. **Create an Assistant** — dashboard.vapi.ai → Assistants → Create Assistant (blank).
2. **Add the tool** — Tools → Create Tool → **Function**:
   - Name: `ask_backend_agent`
   - Description: *Registers a packaging sourcing request with BoxSource. Pass the caller's
     words; it replies with the next question, a read-back to confirm, or a confirmation.*
   - Parameters: one **required** string `query` — *"The user's question or request for
     the backend agent"*
   - Server URL: `https://<ngrok-url>/vapi/tool`
   - Optional messages: Request Start *"One moment."*; Request Response Delayed *"Still
     working on it."*
   - Attach the tool to the assistant.
3. **First message:** *"Hi, this is BoxSource. What packaging would you like to source today?"*
4. **System prompt** (paste as is):

   > You are BoxSource's phone intake assistant for a logistics company's procurement team.
   > Your only job is to register a packaging sourcing request — for example corrugated
   > boxes — so that BoxSource's sourcing agent can find verified manufacturers and collect
   > quotes.
   >
   > Style: brief and friendly, one or two short sentences per turn, natural spoken English.
   > Never read out codes, IDs, links or symbols.
   >
   > How to work:
   > - For anything about the caller's requirement, call the `ask_backend_agent` tool and
   >   pass the caller's words in `query`, including every detail they gave: item, quantity
   >   per month, delivery location, ply, size, material, printing, needed-by date.
   > - Just before calling the tool, say a short filler such as "Let me note that" or
   >   "One moment".
   > - The tool replies with what to say next: a follow-up question, a read-back to
   >   confirm, or a confirmation. Say it naturally, then pass the caller's answer back to
   >   the tool in your next call.
   > - Never invent or assume details. If the caller is unsure, let the tool ask.
   > - You don't know prices, suppliers or order status, and you can't approve anything. If
   >   asked, say the sourcing agent handles that and they can follow it on the BoxSource
   >   dashboard.
   > - Once the request is registered, thank the caller and end the call politely.

5. **Phone number** — Phone Numbers → create the free Vapi number → assign this assistant.
   Or use **Talk to Assistant** (web call) in the dashboard for the demo.

## 3. ngrok URLs change

A free ngrok URL changes every time ngrok restarts. **After every restart, update the
tool's Server URL in Vapi.** (A reserved ngrok domain avoids this:
`ngrok http --url=<your-domain> 8000`.)

## Demo day checklist

1. Start the worker: `python cli.py run` — without it, registered requests sit at RECEIVED.
2. Start the bridge: `python cli.py voice`.
3. Start ngrok: `ngrok http 8000`, copy the HTTPS URL.
4. Update the tool's Server URL in Vapi to `https://<new-url>/vapi/tool`.
5. Open `https://<new-url>/health` through ngrok → `{"ok":true}`.
6. Make one test call (or a Talk to Assistant web call); check `data/voice.log` and
   `python cli.py status`.
7. Keep a backup screen recording of a working call in case the venue Wi-Fi fails.

## Notes

- **Approvals never happen by voice.** Until the dashboard reads the shared Supabase
  database, approve with `python cli.py approve <REQUEST_ID> --action send_rfq --approver "<name>"`.
- **Where requests land:** `storage.backend: sqlite` (this laptop) today; switch to
  `supabase` once `SUPABASE_URL` and `SUPABASE_SECRET_KEY` are in `.env`, so the dashboard
  sees voice requests too.
- **Live changes:** the follow-up questions and every spoken message are in `config.yaml` →
  `voice`. Restart the bridge after editing.
- **Logs:** one JSON line per tool call in `data/voice.log` (query, answer, duration_ms,
  outcome), plus the console.
- **Latency:** both lite Gemini models are asked at once and the first valid answer wins —
  about 1–3 s per turn in testing.
- **No guessing:** a quantity, size, ply or place the caller never said is dropped and asked
  for again, even if the model filled it in.
- **Privacy:** the bridge stores what the caller said about the requirement; it never reads
  or logs the caller's phone number.
