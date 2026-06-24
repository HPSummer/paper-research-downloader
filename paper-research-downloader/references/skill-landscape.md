# Paper Skill Landscape

Survey date: 2026-06-23.

## Recommendation

Use a combination skill, not a single downloaded "paper download skill".

Best architecture:

| Layer | Best source to absorb | Role |
|---|---|---|
| Discovery | `paper-search-pro`, local `paper-lookup` | Multi-source search, deduplication, source logs. |
| Full-text acquisition | `marciob/skill-research-papers` | OA cascade, parsed Markdown, read-plan/sentinel discipline. |
| Obsidian ingest | `zy150/PaperScribe`, `genliusrocks/paper_garden` | Notes, tags, assets, knowledge-garden conventions. |
| Deep reading | local `nature-reader`, `PaperScribe` | Block-level reading, figures/tables, translation. |
| Citation hygiene | local `citation-management` | DOI/BibTeX validation and duplicate cleanup. |

## Candidates

| Project / Skill | Relevance | Practicality | Strengths | Weaknesses | Reuse decision |
|---|---:|---:|---|---|---|
| `marciob/skill-research-papers` | 5/5 | 4/5 | Strong full-text default; OA cascade; cache; PDF/HTML/XML parsing; read-plan and end sentinel reduce abstract-only mistakes. | More Claude-oriented; fetch helper may bring heavier dependencies; not Obsidian-first. | Absorb the full-text contract and OA cascade idea. |
| `O0000-code/paper-search-pro` | 4/5 | 4/5 | Strong multi-source search; tiering; PRISMA-S style logging; HTML/BibTeX/RIS/CSV report; good for scoping and systematic-review prep. | Its own SKILL says not to use for PDF download; large workflow; subagent assumptions may not map to every harness. | Absorb tiering, source routing, exports; keep our downloader separate. |
| `genliusrocks/paper_garden` | 4/5 | 3/5 | Clear arXiv/local-PDF to Markdown garden pipeline; duplicate checks; user-confirmed tags and one-line summaries. | arXiv/local-PDF centered; requires repo root and `uv`; less useful for broad DOI/OA search. | Absorb garden/duplicate/tag confirmation ideas. |
| `zy150/PaperScribe` | 4/5 | 4/5 | Chinese paper notes; Obsidian output; arXiv PDF/source; Zotero read-only lookup; block-by-block reading discipline. | Not a broad literature searcher; configuration-heavy; no OCR; focused on one-paper reading. | Use after acquisition for deep Chinese notes; absorb Obsidian note contract. |
| `renocrypt/latex-arxiv-SKILL` | 2/5 | 3/5 | Good LaTeX/BibTeX review-paper workflow; arXiv registry/cache; citation gates. | Review writing, not acquisition; not a knowledge-base pipeline. | Borrow citation verification discipline only. |
| `a-attia/scicomp-research-skills/literature-survey` | 3/5 | 3/5 | Scientific-computing survey workflow; BibTeX/PDF/pdftotext style; project discipline. | Less specialized for OA cascade and Obsidian. | Borrow survey-set organization ideas. |
| Local `paper-lookup` | 5/5 | 5/5 | Covers OpenAlex, Crossref, Semantic Scholar, arXiv, PubMed, PMC, CORE, Unpaywall; already installed. | Metadata/API reference skill, not a deterministic downloader/parser. | Use as API reference and fallback source. |
| Local `nature-reader` | 4/5 | 5/5 | Full-paper bilingual Markdown reader with figures/tables and source anchors. | Assumes paper source already exists or can be fetched separately. | Use after this downloader produces local files. |

## Design Choices For This Skill

| Requirement | Adopted design |
|---|---|
| Works in Codex and Claude | Plain `SKILL.md` plus Python helper; no harness-specific tools required. |
| No key barrier | Public APIs first; optional `UNPAYWALL_EMAIL`, `OPENALEX_EMAIL`, `S2_API_KEY`, `NCBI_API_KEY`. |
| Legal download | OA cascade only; no pirate mirrors. |
| Avoid abstract-only errors | Parsed Markdown plus `read_plan` and sentinel when full text is available. |
| Obsidian compatible | YAML frontmatter, tags, `[[concept]]` placeholders, vault folder routing. |
| User's research workflow | Defaults suit trajectory optimization and aerospace-control literature, but remain domain-general. |

## Current Boundary

This skill does not try to replace a full reader or translator. It acquires, stages, audits, and lightly prepares papers.

Version 1.4 adds a user-mediated institutional access bridge: safe DOI/publisher/library-proxy URL opening, download-folder watching, optional ingest of user-downloaded PDFs, institutional access plan files, and package-time private-data scanning. It explicitly forbids storing or uploading institutional usernames, passwords, cookies, sessions, proxy credentials, or tokens. Version 1.3 adds paywall-aware lawful access plans, all Unpaywall OA-location expansion, arXiv title-match fallback, CORE candidate expansion, and access-plan links in notes/reports. Version 1.2 adds environment diagnostics, PMC XML full-text fallback, RIS/CSL-JSON exports, and read-only Zotero duplicate-candidate reports on top of the v1.1 OCR hooks, page-preview extraction, HTML reports, regression tests, retry/delay controls, Excel-safe CSV export, and safer arXiv/DOI merge behavior.

For paragraph-level bilingual interpretation, semantic figure/table extraction, or long-form synthesis, hand off to `nature-reader` or a PaperScribe-style reader after acquisition.
