"""Central tool registry — schemas (TOOLS), dispatch map (TOOL_MAP), run_tool()."""
from tools.browser  import (
    search_businesses_maps, sunbiz_lookup, scrape_website_contact, get_google_reviews,
    search_businesses_yelp, search_reddit,
)
from tools.multi_source import find_best_leads
from tools.perplexity_search import search_businesses_perplexity
from tools.search   import web_search
from tools.apollo   import apollo_search_people
from tools.leads    import enrich_leads_batch, research_company, get_collected_leads, save_leads_csv, save_outreach_csv
from tools.hubspot  import hubspot_create_contact, upload_leads_to_hubspot
from tools.gmail    import send_gmail_email, create_gmail_drafts

TOOLS = [
    {
        "name": "find_best_leads",
        "description": (
            "PRIMARY multi-source lead discovery. Pulls from Google Maps, Yelp, Perplexity "
            "(live web search across all public sources), and Reddit in parallel, merges "
            "businesses that appear on multiple sources, scores each one by quality signals "
            "(cross-source presence, Google rating × review count, Yelp rating × review count, "
            "Reddit mentions, data completeness), and returns the top N ranked leads. "
            "For Florida searches (explicit FL, keyword hints, geocoded US city→state, or lead address/state), "
            "it also queries the Florida Division of Corporations (Sunbiz) for each ranked lead so entity name, "
            "status, and registered agent are populated in the same step. "
            "Use this as the DEFAULT when the user asks to find leads, find businesses, "
            "or find the best prospects — it produces better results than any single source alone. "
            "After calling this, call enrich_leads_batch in your NEXT tool call to fill in "
            "website contact info, Google review gaps, and owner/agent contact search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":     {"type": "string", "description": "Business type, e.g. 'nail salon'"},
                "location":    {"type": "string", "description": "City and state, e.g. 'Miami, FL'"},
                "num_results": {"type": "integer", "description": "Number of top-ranked leads to return (default 10, max 20)", "default": 10},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of sources to query. Default: ['maps', 'yelp', 'reddit', 'perplexity']",
                },
                "enrich_sunbiz": {
                    "type": "boolean",
                    "description": (
                        "If true (default), run Florida Sunbiz registry lookup on ranked leads when "
                        "the query or the lead rows (state FL / FL in address) indicate Florida. "
                        "Set false to skip Sunbiz."
                    ),
                    "default": True,
                },
            },
            "required": ["keyword", "location"],
        },
    },
    {
        "name": "search_businesses_maps",
        "description": (
            "PRIMARY lead discovery tool. Call this EXACTLY ONCE per lead generation request. "
            "Searches Google Maps for businesses by keyword and location using a real browser. "
            "Returns {leads: [...], total: N}. "
            "After receiving the result, call enrich_leads_batch in your NEXT tool call, "
            "passing result['leads'] as the leads parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":     {"type": "string", "description": "Business type, e.g. 'nail salon'"},
                "location":    {"type": "string", "description": "City and state, e.g. 'Miami, FL'"},
                "num_results": {"type": "integer", "description": "Number of businesses to return (default 10, max 20)", "default": 10},
            },
            "required": ["keyword", "location"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web for any information — news, business details, market research, "
            "contact info, pricing, reviews, or anything else. Use this whenever you need "
            "current information from the internet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "apollo_search_people",
        "description": (
            "Search Apollo for companies by keyword/location and, when available, "
            "attach the best matching contact email for each company. "
            "Only use this if the user explicitly asks for Apollo results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords":    {"type": "string", "description": "Industry keywords, e.g. 'nail salon'"},
                "locations":   {"type": "array", "items": {"type": "string"}, "description": "Locations, e.g. ['Miami, FL']"},
                "num_results": {"type": "integer", "description": "Number of results (max 50)", "default": 20},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "enrich_leads_batch",
        "description": (
            "Step 2 of lead generation — call this after search_businesses_maps returns results. "
            "Enriches every lead in parallel with all 15 required fields: "
            "(1) Sunbiz: entity/corporate name, formation date, years in business, status, owner name, registered agent; "
            "(2) Website scrape: general email, Instagram URL, Facebook URL; "
            "(3) Google Maps: rating + review count; "
            "(4) Web search: owner email + cell phone, registered agent email + cell phone. "
            "NEVER call sunbiz_lookup / scrape_website_contact / get_google_reviews individually."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "description": "Full lead objects from search_businesses_maps — pass the entire leads array unchanged",
                    "items": {"type": "object"},
                },
            },
            "required": ["leads"],
        },
    },
    {
        "name": "hubspot_create_contact",
        "description": "Create or update a contact in HubSpot CRM with full enriched lead data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email":      {"type": "string"},
                "first_name": {"type": "string"},
                "last_name":  {"type": "string"},
                "company":    {"type": "string"},
                "phone":      {"type": "string"},
                "website":    {"type": "string"},
                "job_title":  {"type": "string"},
                "linkedin":   {"type": "string"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "save_leads_csv",
        "description": "Save the fully enriched lead list to leads.csv.",
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {"type": "array", "description": "List of enriched lead objects", "items": {"type": "object"}},
            },
            "required": ["leads"],
        },
    },
    {
        "name": "get_collected_leads",
        "description": "Return the leads that were already collected and enriched in this session.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "upload_leads_to_hubspot",
        "description": (
            "Upload all collected leads to HubSpot CRM in one call. "
            "Handles all field mapping automatically — do NOT use hubspot_create_contact manually."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_outreach_csv",
        "description": "Save email drafts to outreach_drafts.csv.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drafts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":         {"type": "string"},
                            "email":        {"type": "string"},
                            "subject_line": {"type": "string"},
                            "email_body":   {"type": "string"},
                        },
                    },
                    "description": "List of email drafts",
                }
            },
            "required": ["drafts"],
        },
    },
    {
        "name": "send_gmail_email",
        "description": (
            "Send an email via the user's connected Gmail account. "
            "Use this when the user explicitly asks to SEND emails (not draft them)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to":               {"type": "string"},
                "subject":          {"type": "string"},
                "body":             {"type": "string"},
                "send_all_drafts":  {"type": "boolean", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "research_company",
        "description": (
            "Deep-research a single company. Use when the user asks to 'research', 'look up', "
            "'find everything about', or 'deep dive' on a specific business."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string"},
                "city":         {"type": "string"},
                "website":      {"type": "string"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "search_businesses_perplexity",
        "description": (
            "Use Perplexity's live web search to find businesses across Yelp, Reddit, Google, "
            "local directories, and any other public source — all in one call. "
            "Returns leads with name, address, phone, website, ratings from multiple platforms. "
            "Use when the user asks to search via Perplexity, or when other sources are unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":     {"type": "string", "description": "Business type, e.g. 'nail salon'"},
                "location":    {"type": "string", "description": "City and state, e.g. 'Miami, FL'"},
                "num_results": {"type": "integer", "description": "Number of results (default 10)", "default": 10},
            },
            "required": ["keyword", "location"],
        },
    },
    {
        "name": "search_businesses_yelp",
        "description": (
            "Search Yelp for businesses by keyword and location using a headless browser. "
            "Returns leads with name, address, phone, Yelp rating, review count, and website — "
            "same lead shape as search_businesses_maps. "
            "Use when the user asks for Yelp results or as a supplementary source alongside Google Maps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword":     {"type": "string", "description": "Business type, e.g. 'nail salon'"},
                "location":    {"type": "string", "description": "City and state, e.g. 'Miami, FL'"},
                "num_results": {"type": "integer", "description": "Number of results (default 10, max 20)", "default": 10},
            },
            "required": ["keyword", "location"],
        },
    },
    {
        "name": "search_reddit",
        "description": (
            "Search Reddit for posts and discussions matching a query. "
            "Useful for finding businesses mentioned in local subreddits, spotting companies "
            "discussing expansion or new locations, or gathering tenant prospect intelligence "
            "from community recommendations. "
            "Optionally scope to specific subreddits (e.g. ['miami', 'entrepreneurs']). "
            "Returns post titles, URLs, subreddit, score, comment count, and a text snippet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Search query, e.g. 'nail salon Miami opening'"},
                "subreddits":  {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of subreddits to search within, e.g. ['miami', 'smallbusiness']",
                },
                "num_results": {"type": "integer", "description": "Number of posts to return (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_gmail_drafts",
        "description": (
            "Create Gmail drafts from the outreach emails so the user can review and send them manually from Gmail. "
            "Use this by default when the user asks to 'save to Gmail', 'create drafts', or 'push to Gmail'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_all": {"type": "boolean", "default": True},
                "to":        {"type": "string"},
                "subject":   {"type": "string"},
                "body":      {"type": "string"},
            },
            "required": [],
        },
    },
]

TOOL_MAP = {
    "find_best_leads":        find_best_leads,
    "search_businesses_maps": search_businesses_maps,
    "web_search":             web_search,
    "apollo_search_people":   apollo_search_people,
    "enrich_leads_batch":     enrich_leads_batch,
    "get_collected_leads":    get_collected_leads,
    "upload_leads_to_hubspot": upload_leads_to_hubspot,
    "sunbiz_lookup":           sunbiz_lookup,
    "scrape_website_contact":  scrape_website_contact,
    "get_google_reviews":      get_google_reviews,
    "search_businesses_perplexity": search_businesses_perplexity,
    "search_businesses_yelp":       search_businesses_yelp,
    "search_reddit":           search_reddit,
    "hubspot_create_contact": hubspot_create_contact,
    "save_leads_csv":         save_leads_csv,
    "save_outreach_csv":      save_outreach_csv,
    "send_gmail_email":       send_gmail_email,
    "create_gmail_drafts":    create_gmail_drafts,
    "research_company":       research_company,
}


def run_tool(name: str, inputs: dict, apollo_key: str = "", hubspot_token: str = "") -> dict:
    fn = TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    kwargs = dict(inputs)
    if name == "apollo_search_people":
        kwargs["_apollo_key"] = apollo_key
    elif name in ("hubspot_create_contact", "upload_leads_to_hubspot"):
        kwargs["_hubspot_token"] = hubspot_token
    return fn(**kwargs)
