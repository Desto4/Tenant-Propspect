"""
Agent loops for MMG lead-gen assistant.

Classes
-------
BaseAgent           -- shared tool-execution generator + perf tracking
AnthropicAgent      -- Anthropic SDK (claude-*)
OpenAICompatAgent   -- any OpenAI-compatible endpoint (Gemini, etc.)
PerplexityAgent     -- Perplexity (no tool calling, plain chat)

Module-level entry point
------------------------
run_agent()         -- route to the right provider based on settings
"""

import os
import json
import time as _time

import anthropic

import tools as _tools
from tools import TOOLS, run_tool

# ---------------------------------------------------------------------------
# Shared state (perf tracking)
# ---------------------------------------------------------------------------

_perf_store: list = []

_MODEL_PRICING = {
    "claude-opus-4-6":              (5.00,  25.00),
    "claude-sonnet-4-6":            (3.00,  15.00),
    "claude-haiku-4-5":             (1.00,   5.00),
    "claude-opus-4-5":              (5.00,  25.00),
    "claude-sonnet-4-5":            (3.00,  15.00),
    "claude-opus-4-1":              (15.00, 75.00),
    "gemini-2.5-flash-preview-04-17": (0.075, 0.30),
    "gemini-2.5-pro-preview-05-06":   (1.25,  5.00),
    "gemini-2.0-flash":               (0.075, 0.30),
    "gemini-1.5-flash":               (0.075, 0.30),
    "gemini-1.5-pro":                 (1.25,  5.00),
    "gemini-3-flash-preview":         (0.075, 0.30),
    "gemini-3.1-pro-preview":         (1.25,  5.00),
    "gemini-3.1-flash-lite-preview":  (0.25,  1.50),
    "sonar-pro":           (3.00, 15.00),
    "sonar":               (1.00,  1.00),
    "sonar-reasoning-pro": (2.00,  8.00),
    "sonar-reasoning":     (1.00,  5.00),
}


def _estimate_cost(model, input_tokens, output_tokens):
    price_in, price_out = _MODEL_PRICING.get(model, (1.00, 5.00))
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


def _record_perf(provider, model, duration_ms, input_tokens, output_tokens,
                 tool_calls, leads_found, success):
    global _perf_store
    _perf_store.append({
        "ts": _time.time(), "provider": provider, "model": model,
        "duration_ms": duration_ms, "input_tokens": input_tokens,
        "output_tokens": output_tokens, "tool_calls": tool_calls,
        "leads_found": leads_found, "success": success,
        "cost_usd": _estimate_cost(model, input_tokens, output_tokens),
    })
    if len(_perf_store) > 200:
        _perf_store = _perf_store[-200:]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are MMG Agent, a lead generation assistant for MMG — a commercial real estate brokerage that helps businesses find and lease commercial spaces.

## Your purpose
Find business prospects (tenants) who may be looking to open a new location, expand, or relocate — and draft outreach emails inviting them to consider MMG's available commercial vacancies.

## Required fields — pull these for EVERY lead, every time, no exceptions

1.  Business trade name
2.  Business entity / corporate name (from Sunbiz)
3.  Company formation date + years in business
4.  Business general email (e.g. info@salon.com — from website)
5.  Owner name (from Sunbiz officers section)
6.  Owner email
7.  Owner cell phone (for HubSpot texting)
8.  Registered Agent name (from Sunbiz)
9.  Registered Agent email
10. Registered Agent cell phone (for HubSpot texting)
11. Business address
12. Business phone
13. Website URL
14. Instagram URL + Facebook URL
15. Google rating + Google review count

## Workflow

**Finding new leads — 2-step process:**
Step 1 — Call search_businesses_maps once with the keyword and location the user specified. Do not call it multiple times for the same request.
Step 2 — Once you receive the search results, call enrich_leads_batch in your next tool call, passing result["leads"] as the leads parameter.
Step 3 — After enrichment completes, reply with ONE sentence: "Found and enriched N [type] in [location] — results are in the table below."

Do not call search_businesses_maps and enrich_leads_batch in the same response — they must be separate sequential calls because enrich_leads_batch needs the output of search_businesses_maps.
Never call sunbiz_lookup, scrape_website_contact, or get_google_reviews individually.
Only use apollo_search_people if the user explicitly asks for it.

**Writing outreach emails:**
When asked to write outreach or draft emails, call save_outreach_csv with personalized emails for each lead.
Each email should:
- Be addressed to the owner by first name (or "Business Owner" if unknown)
- Reference the business by name and show you know something about them (years in business, rating, location)
- Position MMG as a commercial real estate partner helping businesses find their next space
- Mention that MMG has available commercial vacancies in their area that could be a great fit
- Keep it short (3-4 sentences), warm, and professional — not salesy
- Subject line: personalized, mention their business or area
- Sign off as: MMG Real Estate Team

**Uploading to HubSpot:**
NEVER search for new leads. NEVER enrich leads. NEVER call hubspot_create_contact manually.
Call upload_leads_to_hubspot() — it handles all field mapping automatically.
Reply with ONE sentence summarising how many contacts were uploaded.

## Rules
- Keep ALL post-tool responses to 1 sentence.
- Do not use web_search unless the user explicitly asks.
- No markdown tables, no field lists — the UI handles display.
"""


# ---------------------------------------------------------------------------
# OpenAI-format tool schema converter
# ---------------------------------------------------------------------------

def _tools_openai_fmt():
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }}
        for t in TOOLS
    ]


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

class BaseAgent:
    """
    Shared tool-execution generator used by all provider subclasses.

    Subclasses must implement:
        run(user_message, history, **kwargs) -> generator of SSE strings
        _format_tool_result(call_id, result) -> dict for the API messages list
    """

    def _exec_tools(self, calls, apollo_key, hubspot_token, out):
        """
        Execute tools and yield SSE event strings.

        Parameters
        ----------
        calls       : list of (name, inputs, call_id) tuples
        out         : dict — populated with out['results'] and out['leads']
                      so the caller can append them to the messages list
        """
        out['results'] = []
        out['leads']   = 0
        for name, inputs, call_id in calls:
            start_evt = {'type': 'tool_start', 'name': name, 'input': inputs}
            if name == 'upload_leads_to_hubspot':
                start_evt['leads'] = [
                    {'company': (l.get('trade_name') or l.get('entity_name') or '').strip(),
                     'email':   (l.get('owner_email') or l.get('general_email') or '').strip()}
                    for l in _tools._leads_store
                    if l.get('owner_email') or l.get('general_email')
                ]
            yield f"data: {json.dumps(start_evt)}\n\n"

            print(f"[TOOL] calling {name} with {list(inputs.keys())}", flush=True)
            result = run_tool(name, inputs, apollo_key=apollo_key, hubspot_token=hubspot_token)
            leads_n = len(result.get("leads", [])) if isinstance(result, dict) else 0
            print(f"[TOOL] {name} done → leads={leads_n} error={result.get('error') if isinstance(result, dict) else None}", flush=True)

            yield f"data: {json.dumps({'type': 'tool_end', 'name': name, 'result': result})}\n\n"
            out['results'].append((call_id, result))
            out['leads'] += leads_n

    def _format_tool_result(self, call_id, result):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Anthropic agent
# ---------------------------------------------------------------------------

class AnthropicAgent(BaseAgent):

    def _format_tool_result(self, call_id, result):
        return {"type": "tool_result", "tool_use_id": call_id, "content": json.dumps(result)}

    def run(self, user_message, history, anthropic_key, model="claude-sonnet-4-6",
            apollo_key="", hubspot_token=""):
        client = anthropic.Anthropic(api_key=anthropic_key)
        model  = model or "claude-sonnet-4-6"

        messages = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if msg.get("role") in ("user", "assistant") and msg.get("content")
        ]
        messages.append({"role": "user", "content": user_message})

        t0 = _time.time()
        success = False
        total_in = total_out = total_tools = total_leads = 0
        try:
            while True:
                response = client.messages.create(
                    model=model, max_tokens=4096,
                    system=SYSTEM_PROMPT, tools=TOOLS,
                    messages=messages,
                )
                total_in  += getattr(response.usage, "input_tokens",  0) or 0
                total_out += getattr(response.usage, "output_tokens", 0) or 0

                full_text  = ""
                tool_calls = []
                for block in response.content:
                    if block.type == "text":
                        full_text += block.text
                    elif block.type == "tool_use":
                        tool_calls.append(block)

                if full_text:
                    yield f"data: {json.dumps({'type': 'text', 'content': full_text})}\n\n"

                if response.stop_reason == "end_turn" or not tool_calls:
                    break

                messages.append({"role": "assistant", "content": response.content})
                total_tools += len(tool_calls)

                calls = [(tc.name, tc.input, tc.id) for tc in tool_calls]
                out   = {}
                yield from self._exec_tools(calls, apollo_key, hubspot_token, out)
                total_leads += out['leads']
                messages.append({
                    "role":    "user",
                    "content": [self._format_tool_result(cid, res) for cid, res in out['results']],
                })
            success = True

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            _record_perf("anthropic", model, int((_time.time() - t0) * 1000),
                         total_in, total_out, total_tools, total_leads, success)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# OpenAI-compatible agent (Gemini, etc.)
# ---------------------------------------------------------------------------

class OpenAICompatAgent(BaseAgent):

    def __init__(self, api_key, base_url, provider):
        self.api_key  = api_key
        self.base_url = base_url
        self.provider = provider

    def _format_tool_result(self, call_id, result):
        return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}

    def run(self, user_message, history, model, apollo_key="", hubspot_token=""):
        from openai import OpenAI as _OAI
        client    = _OAI(api_key=self.api_key, base_url=self.base_url)
        oai_tools = _tools_openai_fmt()

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if msg.get("role") in ("user", "assistant") and msg.get("content")
        ]
        messages.append({"role": "user", "content": user_message})

        t0 = _time.time()
        success = False
        total_in = total_out = total_tools = total_leads = 0
        try:
            while True:
                response = client.chat.completions.create(
                    model=model, messages=messages,
                    tools=oai_tools, tool_choice="auto",
                )
                usage = getattr(response, "usage", None)
                if usage:
                    total_in  += getattr(usage, "prompt_tokens",     0) or 0
                    total_out += getattr(usage, "completion_tokens", 0) or 0

                choice  = response.choices[0]
                msg_obj = choice.message

                if msg_obj.content:
                    yield f"data: {json.dumps({'type': 'text', 'content': msg_obj.content})}\n\n"

                if choice.finish_reason == "stop" or not msg_obj.tool_calls:
                    break

                messages.append(msg_obj)
                total_tools += len(msg_obj.tool_calls)

                calls = []
                for tc in msg_obj.tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except Exception:
                        inputs = {}
                    calls.append((tc.function.name, inputs, tc.id))

                out = {}
                yield from self._exec_tools(calls, apollo_key, hubspot_token, out)
                total_leads += out['leads']
                messages.extend([self._format_tool_result(cid, res) for cid, res in out['results']])
            success = True

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            _record_perf(self.provider, model, int((_time.time() - t0) * 1000),
                         total_in, total_out, total_tools, total_leads, success)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Perplexity agent (no tool calling)
# ---------------------------------------------------------------------------

class PerplexityAgent(BaseAgent):

    def _format_tool_result(self, call_id, result):
        return {}  # Not used — Perplexity has no tool calling

    def run(self, user_message, history, perplexity_key, model="sonar-pro",
            apollo_key="", hubspot_token=""):
        from openai import OpenAI as _OAI
        client = _OAI(api_key=perplexity_key, base_url="https://api.perplexity.ai/")

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if msg.get("role") in ("user", "assistant") and msg.get("content")
        ]
        messages.append({"role": "user", "content": user_message})

        t0 = _time.time()
        success = False
        input_tokens = output_tokens = 0
        try:
            response = client.chat.completions.create(model=model, messages=messages)
            usage = getattr(response, "usage", None)
            if usage:
                input_tokens  = getattr(usage, "prompt_tokens",     0) or 0
                output_tokens = getattr(usage, "completion_tokens", 0) or 0
            text = response.choices[0].message.content or ""
            if text:
                yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
            success = True
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        finally:
            _record_perf("perplexity", model, int((_time.time() - t0) * 1000),
                         input_tokens, output_tokens, 0, 0, success)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def run_agent(user_message, history,
              anthropic_key="", apollo_key="", hubspot_token="",
              claude_model="claude-sonnet-4-6",
              gemini_key="", model_provider="anthropic",
              gemini_model="gemini-3-flash-preview",
              perplexity_key="", perplexity_model="sonar-pro"):
    """Route to the correct provider agent."""

    if model_provider == "gemini":
        gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
        if not gemini_key:
            yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Gemini API key in Settings.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        agent = OpenAICompatAgent(
            gemini_key,
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "gemini",
        )
        yield from agent.run(user_message, history, model=gemini_model,
                             apollo_key=apollo_key, hubspot_token=hubspot_token)
        return

    if model_provider == "perplexity":
        perplexity_key = perplexity_key or os.getenv("PERPLEXITY_API_KEY", "")
        if not perplexity_key:
            yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Perplexity API key in Settings.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        yield from PerplexityAgent().run(
            user_message, history,
            perplexity_key=perplexity_key, model=perplexity_model,
            apollo_key=apollo_key, hubspot_token=hubspot_token,
        )
        return

    # Default: Anthropic
    anthropic_key = anthropic_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Anthropic API key in Settings.'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return
    yield from AnthropicAgent().run(
        user_message, history,
        anthropic_key=anthropic_key, model=claude_model,
        apollo_key=apollo_key, hubspot_token=hubspot_token,
    )
