"""
Tavily search API — optimized for AI agent use.

Requires TAVILY_API_KEY environment variable.
Pricing: 1 credit per basic search ($0.008/credit PAYG), 2 credits for advanced.
"""

import json
import os
import urllib.request
import urllib.error

meta = {
    "name": "tavily",
    "description": "Web search via Tavily API. Has --include_answer for quick AI-synthesized answers and --time_range for recency. Better than exa for quick factual questions; weaker for deep research.",
    "cost_per_call": 0.008,  # 1 credit at PAYG rate
    "params": [
        {"name": "query", "required": True, "description": "Search query"},
        {"name": "search_depth", "default": "basic", "description": "basic (1 credit) or advanced (2 credits)"},
        {"name": "max_results", "default": "5", "description": "Max results (1-20)"},
        {"name": "topic", "default": "general", "description": "general, news, or finance"},
        {"name": "include_answer", "default": "false", "description": "Include AI-generated answer (true/false)"},
        {"name": "time_range", "description": "Filter by recency: day, week, month, year"},
        {"name": "include_domains", "description": "Comma-separated domains to include"},
        {"name": "exclude_domains", "description": "Comma-separated domains to exclude"},
        {"name": "include_raw_content", "default": "false", "description": "Include full page content (true/false)"},
    ],
}


def execute(params):
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY environment variable not set")

    depth = params.get("search_depth", "basic")

    body = {
        "query": params["query"],
        "search_depth": depth,
        "max_results": int(params.get("max_results", 5)),
        "topic": params.get("topic", "general"),
        "include_answer": params.get("include_answer", "false").lower() == "true",
        "include_raw_content": params.get("include_raw_content", "false").lower() == "true",
    }

    if params.get("time_range"):
        body["time_range"] = params["time_range"]
    if params.get("include_domains"):
        body["include_domains"] = [d.strip() for d in params["include_domains"].split(",")]
    if params.get("exclude_domains"):
        body["exclude_domains"] = [d.strip() for d in params["exclude_domains"].split(",")]

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "kiln-tools/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Tavily API error ({e.code}): {error_body}")

    # Cost: 1 credit for basic, 2 for advanced. $0.008/credit PAYG.
    actual_cost = 0.008 if depth == "basic" else 0.016

    # Format output
    lines = []
    results = data.get("results", [])
    lines.append(f"Tavily search: {len(results)} results for \"{params['query']}\"")
    lines.append(f"Cost: ${actual_cost:.4f}")
    lines.append("")

    if data.get("answer"):
        lines.append("Answer:")
        lines.append(data["answer"])
        lines.append("")

    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '(no title)')}")
        lines.append(f"    {r.get('url', '')}")
        if r.get("score"):
            lines.append(f"    Score: {r['score']:.3f}")
        if r.get("content"):
            content = r["content"][:500]
            lines.append(f"    {content}")
        lines.append("")

    return {"output": "\n".join(lines), "actual_cost": actual_cost}
