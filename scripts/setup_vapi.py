"""Create (or update) the BoxSource phone assistant and its tool in Vapi.

    python scripts/setup_vapi.py https://<your-ngrok-domain>

Needs VAPI_API_KEY (Vapi dashboard -> API Keys -> Private key) in .env. Safe to re-run:
the tool and assistant are found by name and updated, so a new ngrok URL is one command.
"""

import sys

import httpx
from dotenv import dotenv_values

API = "https://api.vapi.ai"
TOOL_NAME = "ask_backend_agent"
ASSISTANT_NAME = "BoxSource intake"
FIRST_MESSAGE = "Hi, this is BoxSource. What packaging would you like to source today?"
SYSTEM_PROMPT = """You are BoxSource's phone intake assistant for a logistics company's procurement team. Your only job is to register a packaging sourcing request, for example corrugated boxes, so that BoxSource's sourcing agent can find verified manufacturers and collect quotes.

Style: brief and friendly, one or two short sentences per turn, natural spoken English. Never read out codes, IDs, links or symbols.

How to work:
- For anything about the caller's requirement, call the ask_backend_agent tool and pass the caller's words in query, including every detail they gave: item, quantity per month, delivery location, ply, size, material, printing, needed-by date.
- Just before calling the tool, say a short filler such as "Let me note that" or "One moment".
- The tool replies with what to say next: a follow-up question, a read-back to confirm, or a confirmation. Say it naturally, then pass the caller's answer back to the tool in your next call.
- Never invent or assume details. If the caller is unsure, let the tool ask.
- You don't know prices, suppliers or order status, and you can't approve anything. If asked, say the sourcing agent handles that and they can follow it on the BoxSource dashboard.
- Once the request is registered, thank the caller and end the call politely."""


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("usage: python scripts/setup_vapi.py https://<your-ngrok-domain>")
    key = dotenv_values(".env").get("VAPI_API_KEY")
    if not key:
        sys.exit("VAPI_API_KEY is missing from .env (Vapi dashboard -> API Keys -> Private key)")
    server_url = sys.argv[1].rstrip("/") + "/vapi/tool"
    api = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=30)

    def call(method, path, body=None):
        r = api.request(method, path, json=body)
        if r.status_code >= 400:
            sys.exit(f"Vapi {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    tool = {"type": "function", "server": {"url": server_url},
            "function": {"name": TOOL_NAME,
                         "description": ("Registers a packaging sourcing request with BoxSource. Pass "
                                         "the caller's words; it replies with the next question, a "
                                         "read-back to confirm, or a confirmation."),
                         "parameters": {"type": "object", "required": ["query"], "properties": {
                             "query": {"type": "string", "description":
                                       "The user's question or request for the backend agent"}}}},
            "messages": [{"type": "request-start", "content": "One moment."}]}
    existing = next((t for t in call("GET", "/tool")
                     if (t.get("function") or {}).get("name") == TOOL_NAME), None)
    if existing:
        tool.pop("type")  # the type can't be changed on update
        tool_id = call("PATCH", f"/tool/{existing['id']}", tool)["id"]
    else:
        tool_id = call("POST", "/tool", tool)["id"]
    print(f"tool      {TOOL_NAME}: {tool_id} -> {server_url}")

    assistant = {"name": ASSISTANT_NAME, "firstMessage": FIRST_MESSAGE,
                 "model": {"provider": "openai", "model": "gpt-4o-mini", "toolIds": [tool_id],
                           "messages": [{"role": "system", "content": SYSTEM_PROMPT}]}}
    existing = next((a for a in call("GET", "/assistant") if a.get("name") == ASSISTANT_NAME), None)
    a = (call("PATCH", f"/assistant/{existing['id']}", assistant) if existing
         else call("POST", "/assistant", assistant))
    print(f"assistant {ASSISTANT_NAME}: {a['id']}")
    print("Open dashboard.vapi.ai -> Assistants -> BoxSource intake -> 'Talk to Assistant' to call it.")


if __name__ == "__main__":
    main()
