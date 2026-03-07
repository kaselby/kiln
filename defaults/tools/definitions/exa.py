"""
Exa semantic search API.

Requires EXA_API_KEY environment variable.
Pricing: ~$0.005 per search (1-25 results, neural/auto) + $0.001 per page for content.
"""

import json
import os
import urllib.request
import urllib.error

meta = {
    "name": "exa",
    "description": "Semantic search via Exa API. Best for conceptual/related content, domain-filtered search (e.g. reddit.com), and tweet search (--category tweet). Prefer over tavily for research depth.",
    "cost_per_call": 0.01,  # conservative estimate: search + content for ~5 results
    "params": [
        {"name": "query", "required": True, "description": "Search query"},
        {"name": "num_results", "default": "5", "description": "Number of results (1-100)"},
        {"name": "type", "default": "auto", "description": "Search type: auto, neural, keyword"},
        {"name": "contents", "default": "highlights", "description": "Content mode: none, highlights, text"},
        {"name": "category", "description": "Category filter: research paper, news, tweet, company, etc."},
        {"name": "include_domains", "description": "Comma-separated domains to include"},
        {"name": "exclude_domains", "description": "Comma-separated domains to exclude"},
        {"name": "start_date", "description": "Filter results published after this date (YYYY-MM-DD)"},
    ],
}


def execute(params):
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise RuntimeError("EXA_API_KEY environment variable not set")

    body = {
        "query": params["query"],
        "numResults": int(params.get("num_results", 5)),
        "type": params.get("type", "auto"),
    }

    # Content extraction
    contents_mode = params.get("contents", "highlights")
    if contents_mode == "highlights":
        body["contents"] = {"highlights": {"numSentences": 3}}
    elif contents_mode == "text":
        body["contents"] = {"text": {"maxCharacters": 2000}}
    # "none" = no contents key

    if params.get("category"):
        body["category"] = params["category"]
    if params.get("include_domains"):
        body["includeDomains"] = [d.strip() for d in params["include_domains"].split(",")]
    if params.get("exclude_domains"):
        body["excludeDomains"] = [d.strip() for d in params["exclude_domains"].split(",")]
    if params.get("start_date"):
        body["startPublishedDate"] = params["start_date"] + "T00:00:00.000Z"

    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "User-Agent": "kiln-tools/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Exa API error ({e.code}): {error_body}")

    # Extract actual cost if provided
    actual_cost = None
    if "costDollars" in data:
        actual_cost = data["costDollars"].get("total", meta["cost_per_call"])

    # Format output
    lines = []
    results = data.get("results", [])
    lines.append(f"Exa search: {len(results)} results for \"{params['query']}\"")
    if actual_cost is not None:
        lines.append(f"Cost: ${actual_cost:.4f}")
    lines.append("")

    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '(no title)')}")
        lines.append(f"    {r.get('url', '')}")
        if r.get("publishedDate"):
            lines.append(f"    Published: {r['publishedDate'][:10]}")
        if r.get("highlights"):
            for h in r["highlights"]:
                lines.append(f"    > {h.strip()}")
        elif r.get("text"):
            text = r["text"][:500]
            lines.append(f"    {text}")
        lines.append("")

    result = "\n".join(lines)
    if actual_cost is not None:
        return {"output": result, "actual_cost": actual_cost}
    return result
