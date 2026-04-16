"""Web search tool (DuckDuckGo instant answers)."""
import requests
from urllib.parse import quote_plus


def web_search(query: str) -> dict:
    try:
        url = (
            f"https://api.duckduckgo.com/?q={quote_plus(query)}"
            "&format=json&no_html=1&skip_disambig=1"
        )
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(f"Summary: {data['AbstractText']}")
            if data.get("AbstractURL"):
                results.append(f"Source: {data['AbstractURL']}")
        for topic in data.get("RelatedTopics", [])[:6]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"- {topic['Text']}")
                if topic.get("FirstURL"):
                    results.append(f"  {topic['FirstURL']}")
        if data.get("Answer"):
            results.append(f"Answer: {data['Answer']}")
        if not results:
            results.append(
                f"No direct results. Try: https://www.google.com/search?q={quote_plus(query)}"
            )
        return {"results": "\n".join(results), "query": query}
    except Exception as e:
        return {"error": str(e)}
