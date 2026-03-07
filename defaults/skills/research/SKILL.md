---
name: research
description: >
  Skill for web research, synthesis, and report/document writing. Activate when
  the task involves researching a topic, gathering information from multiple
  sources, writing reports or documentation based on research, or any task
  requiring structured information gathering and synthesis.
---

# Research

## Workflow

Research tasks follow a gather → organize → synthesize flow. The key discipline is **writing things down as you go** rather than holding everything in context and synthesizing at the end.

### 1. Set Up a Scratch Workspace

At the start of any non-trivial research task, create a scratch directory:

```
<agent_home>/scratch/research-<topic>/
  notes.md      — running notes, key facts, quotes
  sources.md    — every URL fetched, with a one-line summary of what it contained
```

For quick lookups (single question, single source), skip this — it's for tasks requiring multiple sources and synthesis.

### 2. Gather: Search and Fetch

**Search broadly first, then fetch selectively.** Run 2-3 searches with different angles to map the landscape before committing to deep reads.

**When fetching, write to notes immediately.** Don't rely on holding fetched content in context. After each WebFetch, append the key facts, quotes, and technical details to `notes.md`. Include the source URL inline so you can trace claims back later.

**WebFetch prompts matter.** The tool summarizes via a small model — vague prompts get vague results. Always specify what you need:
- "Include exact quotes where available"
- "Include specific class names, method signatures, and code examples"
- "What are the limitations and gotchas mentioned?"

**Parallel fetching:** Batch fetches that are independent. But never mix a speculative fetch (URL that might 404/403) with fetches you need — a single failure kills the whole batch. Put risky fetches in their own batch.

**Twitter/X:** WebFetch can't read tweets (JS-required pages). Use `fetch-tweet <url-or-id>` instead — it calls FxTwitter's free API and returns the full tweet text, author, stats, and quoted tweets. No auth needed.

**When you need exact text:** WebFetch summarizes via Haiku, which can drop code snippets, field names, and specific quotes. Use `fetch-raw <url>` instead — it uses trafilatura to extract main content as markdown, bypassing the summarizer. Truncates to 4000 lines by default; save to a file for full content. Good for API references, technical docs, and anything where precision matters.

### 3. Organize: Structure Your Notes

Before writing the final output, review your notes and organize them:
- Group related facts
- Identify contradictions between sources (flag these — they're often the most interesting parts)
- Note gaps — what couldn't you find? What questions remain?
- Identify which sources are primary (official docs, direct statements) vs secondary (blog posts, news articles)

### 4. Synthesize: Write the Output

**Be opinionated, not neutral.** Don't just restate what sources say — draw conclusions, flag what matters, call out what's misleading. The value is in synthesis and judgment, not aggregation.

**Separate facts from interpretation.** Use direct quotes and specific citations for factual claims. Make it clear when you're drawing your own conclusions vs reporting what a source says.

**Include sources.** Every document should end with a Sources section linking to the URLs used. For Obsidian docs, use standard markdown links. For direct user responses, use inline links.

**Gotchas and limitations are more valuable than happy-path descriptions.** Anyone can read the official docs for how things are supposed to work. The real value is in documenting what's broken, what's misleading, where the gaps are, and what the docs don't tell you.

## Writing Style for Research Docs

- Lead with the bottom line — what does the reader need to know? Put context and details after.
- Use headers and structure to make docs scannable, but don't fragment into tiny sections. A coherent paragraph is better than five bullet points.
- Tables for comparisons and feature matrices. Prose for analysis and narrative.
- Callouts (`> [!warning]`, `> [!note]`) for things that could bite the reader.
- Interlink Obsidian docs with `[[wikilinks]]` when building a doc set.
- Don't pad. If a section doesn't have much to say, keep it short rather than inflating it.

## Common Pitfalls

- **Holding everything in context.** For anything beyond ~5 fetches, write notes to scratch files. Context will compress or the session will end, and you'll lose intermediate work.
- **Taking sources at face value.** Secondary reporting often simplifies or mischaracterizes. When possible, find the primary source (official docs, actual tweets, original announcements) rather than relying on someone's summary.
- **Over-summarizing.** WebFetch already summarizes. If you then summarize the summary in your notes, you've lost two layers of detail. Copy exact quotes and specific facts to your notes, not vibes.
- **Asking permission to build tools.** If you hit a friction during research (unfetchable source type, missing capability), build a workaround or tool immediately. Don't describe the problem and wait for instructions.
