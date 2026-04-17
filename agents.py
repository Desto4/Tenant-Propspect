"""Agent loops for Anthropic, Gemini, and Perplexity providers."""
import json
import os
import re
import time as _time

import anthropic

from core.perf  import record_perf
from core.state import get_leads
from core.tools import TOOLS, run_tool

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
Step 1 — Call find_best_leads once with the keyword and location the user specified. This pulls from Google Maps, Yelp, Reddit, and Perplexity in parallel, merges duplicates, ranks by quality signals, and when the search is in Florida (location text, geocoded city, or lead address/state) automatically runs Florida Sunbiz (Division of Corporations) on each ranked lead. Do not call it multiple times for the same request.
Step 2 — Once you receive the ranked results, call enrich_leads_batch in your next tool call, passing result["leads"] as the leads parameter (this adds website scrape, contact search, and fills any gaps — Sunbiz is skipped for leads that already have sunbiz_url).
Step 3 — After enrichment completes, reply with ONE sentence: "Found and enriched N [type] in [location] — results are in the table below."

Do not call find_best_leads and enrich_leads_batch in the same response — they must be separate sequential calls because enrich_leads_batch needs the output of find_best_leads.
Never call sunbiz_lookup, scrape_website_contact, or get_google_reviews individually.
Only use apollo_search_people if the user explicitly asks for it.
Only call search_businesses_maps, search_businesses_yelp, search_businesses_perplexity, or search_reddit directly if the user explicitly asks to limit the search to that one source. Otherwise always prefer find_best_leads.

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tools_openai_fmt():
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t["description"],
                "parameters":  t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def _hs_start_leads():
    """Build the HubSpot preview payload from current leads store.

    Must match upload_leads_to_hubspot email selection (owner → general → reg agent)
    so the chat UI row count matches what the tool actually uploads.
    """
    out = []
    for l in get_leads():
        email = (
            (l.get("owner_email") or l.get("general_email") or l.get("reg_agent_email") or "")
            .strip()
        )
        if not email:
            continue
        out.append(
            {
                "company": (l.get("trade_name") or l.get("entity_name") or "").strip(),
                "email":   email,
            }
        )
    return out


# ── Response cleanup ──────────────────────────────────────────────────────────

def _strip_think_blocks(text: str) -> str:
    """Remove provider-internal <think>...</think> blocks from visible output."""
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


# ── OpenAI-compatible loop (Gemini / future providers) ────────────────────────

def _run_agent_openai_compat(
    user_message, history, api_key, model, base_url,
    provider="gemini", apollo_key="", hubspot_token="",
):
    from openai import OpenAI as _OAI

    client    = _OAI(api_key=api_key, base_url=base_url)
    oai_tools = _tools_openai_fmt()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history:
        role, content = msg.get("role"), msg.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    t0 = _time.time()
    success = False
    total_in = total_out = total_tools = total_leads = 0

    try:
        while True:
            response = client.chat.completions.create(
                model=model, messages=messages, tools=oai_tools, tool_choice="auto",
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

            tool_results = []
            for tc in msg_obj.tool_calls:
                name = tc.function.name
                try:
                    inputs = json.loads(tc.function.arguments)
                except Exception:
                    inputs = {}
                start_evt = {"type": "tool_start", "name": name, "input": inputs}
                if name == "upload_leads_to_hubspot":
                    start_evt["leads"] = _hs_start_leads()
                yield f"data: {json.dumps(start_evt)}\n\n"
                result = run_tool(name, inputs, apollo_key=apollo_key, hubspot_token=hubspot_token)
                yield f"data: {json.dumps({'type': 'tool_end', 'name': name, 'result': result})}\n\n"
                if isinstance(result, dict) and result.get("leads"):
                    total_leads += len(result["leads"])
                tool_results.append({
                    "role": "tool", "tool_call_id": tc.id, "content": json.dumps(result),
                })
            messages.extend(tool_results)
        success = True
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
    finally:
        record_perf(provider, model, int((_time.time() - t0) * 1000),
                    total_in, total_out, total_tools, total_leads, success)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ── Provider-specific wrappers ─────────────────────────────────────────────────

def run_agent_gemini(user_message, history, gemini_key, model, apollo_key="", hubspot_token=""):
    yield from _run_agent_openai_compat(
        user_message, history,
        api_key=gemini_key, model=model,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        provider="gemini", apollo_key=apollo_key, hubspot_token=hubspot_token,
    )


PERPLEXITY_SYSTEM_PROMPT = """You are MMG Agent, a tenant-prospecting research analyst for MMG — a commercial real estate brokerage in Florida that helps businesses find and lease commercial spaces.

## Your job
When the user asks you to find businesses, leads, or prospects in a given category and market, produce a complete **Tenant Prospecting Report** in GitHub-flavored Markdown using the exact template below.

You have live web search built in. Use it aggressively. Cross-reference every prospect against:
- **Florida Sunbiz** (https://search.sunbiz.org) — corporate entity, document number, status, formation date, FEI/EIN, officers, registered agent
- **Google Maps** — ratings, review counts, phone, address, website, hours
- **Business websites** — email, phone, services
- **Yelp, BBB, Fresha, BirdEye** — additional ratings/reviews, verification
- **Instagram & Facebook** — handles, follower counts, post counts
- **LinkedIn** — founders, owners, officers

Only include information your searches actually return. Never fabricate emails, phone numbers, or ownership details. If a field is not publicly discoverable, write `—`.

## Report template — follow exactly

Produce this structure. Fill every section. Use real markdown tables. Use emoji exactly as shown. Include real hyperlinks.

```
# [Category] — Tenant Prospecting Report
## [Category] | [Market Area] | [Month Year]

**Market:** [Market area, e.g. Miami-Dade County, Florida]
**Category:** [Business category]
**Methodology:** Field research via Florida Division of Corporations (Sunbiz), Google Maps, business websites, social media, booking platforms, and business directories

---

## Executive Summary

[1-2 paragraphs: how many prospects profiled, selection criteria (review volume, 4.5+ stars, years in business, brand signals), why these are ideal tenant candidates.]

| # | Business | Area | Google Rating | Reviews | Years in Business | Sunbiz Status |
|---|---|---|---|---|---|---|
| 1 | [Name] | [Neighborhood] | ⭐ [x.x] | [count] | ~[N] years | ✅ Active |
| 2 | … |

> **Sunbiz Status Key:** ✅ Active = clean corporate standing. ⚠️ = entity dissolved or inactive, but business is physically operating.

---

## Prospect Profiles

### 1. [Business Name]
**[One-sentence positioning statement — what makes them stand out.]**

| Field | Details |
|---|---|
| **Trade Name** | [name] |
| **Corporate Entity** | [LLC/Corp name from Sunbiz] |
| **Sunbiz Doc #** | [[DocNumber](https://search.sunbiz.org/...)] |
| **Sunbiz Status** | ✅ Active / ⚠️ Inactive |
| **Formation Date** | [Month DD, YYYY] (~N years in business) |
| **FEI/EIN** | [if public] |
| **Business Email** | [email] |
| **Business Phone** | [phone] |
| **Business Address** | [address] |
| **Website** | [[domain](url)] |
| **Instagram** | [@handle](url) (followers/posts if known) |
| **Facebook** | [[Page name](url)] (likes if known) |
| **Google Rating** | ⭐ [x.x] / [count] reviews |

**Ownership & Contacts:**

| Role | Name | Email | Phone |
|---|---|---|---|
| [Manager Member / CEO / Owner] | [name] | [email or —] | [phone or —] |
| Registered Agent | [name] | — | — |
| Reg. Agent Address | [address] | | |

> **Note:** [Anything interesting — multi-location operator, ownership change, dissolution history, demographic certifications, press mentions.]

**Prospecting Notes:** [1 paragraph explaining why this is a strong prospect, what their real estate needs might be, and the best way to reach them.]

---

### 2. [Next prospect — same structure]
…

---

## Outreach Priority Matrix

| Priority | Business | Why | Best Contact |
|---|---|---|---|
| 🥇 **Highest** | [name] | [1-line reason] | [email or phone] |
| 🥈 **High** | [name] | [1-line reason] | [email or phone] |
| 🥉 **Medium** | [name] | [1-line reason] | [email or phone] |

---

## Research Notes & Disclaimers

- **Data sources:** Florida Division of Corporations ([Sunbiz](https://search.sunbiz.org)), Google Maps, Yelp, Fresha, BBB, BirdEye, Instagram, Facebook, and business websites. All data was collected [date].
- **Contact information:** Only publicly available contact information has been included. No personal email addresses or phone numbers were fabricated. Fields marked "—" indicate the information was not publicly discoverable.
- **Sunbiz status:** "Active" means the entity is in good standing with the Florida Division of Corporations. "Active Reinstatement" means an entity was previously dissolved but has been formally reinstated and is now active.
- **Google review counts:** Review counts are approximate and may fluctuate.
- **Outreach compliance:** All outreach should comply with applicable telemarketing, CAN-SPAM, and Florida commercial solicitation regulations. This report is for informational purposes and does not constitute legal advice.
```

## Rules
- Output ONLY the markdown report. No preamble, no trailing commentary.
- Default to the top 3 prospects unless the user specifies a different count.
- Never output JSON, code blocks (outside the report), or tool call syntax.
- Every Sunbiz document number must be a real, clickable link to search.sunbiz.org.
- Every social handle, website, and Sunbiz link must be real — discovered by your search, not invented.
- For fields you genuinely cannot verify, write `—` rather than guessing.
- Use GitHub-flavored Markdown tables (pipes + dashes). Use the exact emoji shown (⭐ ✅ ⚠️ 🥇 🥈 🥉).
"""


def run_agent_perplexity(user_message, history, perplexity_key, model, apollo_key="", hubspot_token=""):
    """Perplexity sonar models have built-in web search but do NOT support tool calling."""
    from openai import OpenAI as _OAI

    client   = _OAI(api_key=perplexity_key, base_url="https://api.perplexity.ai/")
    messages = [{"role": "system", "content": PERPLEXITY_SYSTEM_PROMPT}]
    for msg in history:
        role, content = msg.get("role"), msg.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    t0 = _time.time()
    success = input_tokens = output_tokens = False, 0, 0
    try:
        response = client.chat.completions.create(model=model, messages=messages)
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens  = getattr(usage, "prompt_tokens",     0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
        text = _strip_think_blocks(response.choices[0].message.content or "")
        if text:
            yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"
        success = True
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
    finally:
        record_perf("perplexity", model, int((_time.time() - t0) * 1000),
                    input_tokens, output_tokens, 0, 0, success)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def run_agent_anthropic(
    user_message, history, anthropic_key,
    model="claude-sonnet-4-6", apollo_key="", hubspot_token="",
):
    client = anthropic.Anthropic(api_key=anthropic_key)
    MODEL  = model or "claude-sonnet-4-6"

    api_messages = []
    for msg in history:
        role, content = msg.get("role"), msg.get("content")
        if role in ("user", "assistant") and content:
            api_messages.append({"role": role, "content": content})
    api_messages.append({"role": "user", "content": user_message})

    t0 = _time.time()
    success = False
    total_in = total_out = total_tools = total_leads = 0

    try:
        while True:
            response = client.messages.create(
                model=MODEL, max_tokens=4096,
                system=SYSTEM_PROMPT, tools=TOOLS,
                messages=api_messages,
            )
            total_in  += getattr(response.usage, "input_tokens",  0) or 0
            total_out += getattr(response.usage, "output_tokens", 0) or 0

            full_text, tool_calls = "", []
            for block in response.content:
                if block.type == "text":
                    full_text += block.text
                elif block.type == "tool_use":
                    tool_calls.append(block)

            if full_text:
                yield f"data: {json.dumps({'type': 'text', 'content': full_text})}\n\n"

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            api_messages.append({"role": "assistant", "content": response.content})
            total_tools += len(tool_calls)

            tool_results = []
            for tc in tool_calls:
                start_evt = {"type": "tool_start", "name": tc.name, "input": tc.input}
                if tc.name == "upload_leads_to_hubspot":
                    start_evt["leads"] = _hs_start_leads()
                yield f"data: {json.dumps(start_evt)}\n\n"
                print(f"[TOOL] calling {tc.name} with {list(tc.input.keys())}", flush=True)
                result = run_tool(tc.name, tc.input, apollo_key=apollo_key, hubspot_token=hubspot_token)
                leads_count = len(result.get("leads", [])) if isinstance(result, dict) else "?"
                print(
                    f"[TOOL] {tc.name} done → leads={leads_count} "
                    f"error={result.get('error') if isinstance(result, dict) else None}",
                    flush=True,
                )
                yield f"data: {json.dumps({'type': 'tool_end', 'name': tc.name, 'result': result})}\n\n"
                if isinstance(result, dict) and result.get("leads"):
                    total_leads += len(result["leads"])
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tc.id, "content": json.dumps(result),
                })
            api_messages.append({"role": "user", "content": tool_results})
        success = True
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
    finally:
        record_perf("anthropic", MODEL, int((_time.time() - t0) * 1000),
                    total_in, total_out, total_tools, total_leads, success)

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ── Unified entry point ────────────────────────────────────────────────────────

def run_agent(
    user_message, history,
    anthropic_key="", apollo_key="", hubspot_token="",
    claude_model="claude-sonnet-4-6",
    gemini_key="", model_provider="anthropic",
    gemini_model="gemini-3-flash-preview",
    perplexity_key="", perplexity_model="sonar-pro",
):
    if model_provider == "gemini":
        gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
        if not gemini_key:
            yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Gemini API key in Settings.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        yield from run_agent_gemini(
            user_message, history, gemini_key=gemini_key, model=gemini_model,
            apollo_key=apollo_key, hubspot_token=hubspot_token,
        )
        return

    if model_provider == "perplexity":
        perplexity_key = perplexity_key or os.getenv("PERPLEXITY_API_KEY", "")
        if not perplexity_key:
            yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Perplexity API key in Settings.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        yield from run_agent_perplexity(
            user_message, history, perplexity_key=perplexity_key, model=perplexity_model,
            apollo_key=apollo_key, hubspot_token=hubspot_token,
        )
        return

    anthropic_key = anthropic_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        yield f"data: {json.dumps({'type': 'text', 'content': 'Please configure your Anthropic API key in Settings.'})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return
    yield from run_agent_anthropic(
        user_message, history, anthropic_key=anthropic_key, model=claude_model,
        apollo_key=apollo_key, hubspot_token=hubspot_token,
    )
