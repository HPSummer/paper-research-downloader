# Download Strategy

## Source Priority

For a known identifier:

1. arXiv ID or arXiv URL -> arXiv PDF.
2. PMCID -> PMC open PDF, then PMC XML full text if PDF is unavailable.
3. DOI -> Unpaywall with `UNPAYWALL_EMAIL` or `--email`.
4. DOI -> all Unpaywall OA locations, not only best PDF.
5. DOI -> OpenAlex `best_oa_location` or `primary_location`.
6. DOI/title -> Semantic Scholar `openAccessPdf`.
7. Title -> arXiv exact title match and CORE candidate URLs.
8. DOI landing page -> metadata plus access plan unless a direct public PDF is exposed.
9. Local user PDF -> parse directly.

Never use Sci-Hub, credential bypasses, shadow mirrors, or institutional access scraping. For paywalled papers, generate a lawful access plan instead of trying to bypass the wall.

## Evidence Labels

Use these labels in final answers and notes:

| Label | Meaning |
|---|---|
| `[Full text]` | PDF/HTML/XML was downloaded and parsed/read. |
| `[Abstract only]` | Abstract was available, but full text was not obtained. |
| `[Metadata only]` | Only title/authors/year/venue/identifier were obtained. |

## Search Source Routing

| Domain signal | Sources |
|---|---|
| Aerospace, control, optimization, ML, CS | OpenAlex + Semantic Scholar + arXiv + Crossref |
| Biomedical, clinical, biology | PubMed + OpenAlex + Semantic Scholar + Crossref + Unpaywall |
| DOI list | Crossref/OpenAlex metadata + Unpaywall download |
| arXiv-heavy frontier topic | arXiv first, then Semantic Scholar/OpenAlex for citations and published versions |
| Systematic review prep | Keep query strings, dates, sources, and result counts in `run_manifest.json` |

## Obsidian Note Defaults

For this user's vault:

| Note type | Folder |
|---|---|
| Literature paper | `02_literature` |
| Concept extracted from paper | `03_concepts` |
| Topic synthesis | `04_topics` or `06_synthesis` |

Literature-note frontmatter should include:

```yaml
---
title: "Exact paper title"
authors: []
year:
venue:
doi:
arxiv:
url:
pdf_path:
tags:
  - paper
status: "unread"
created: "YYYY-MM-DD"
---
```

Body sections:

```markdown
# Exact paper title

## Metadata

## Abstract

## Core Links
- Related concepts: [[low-thrust trajectory optimization]], [[global optimization]]
- Related topics: [[]]

## Reading Checklist
- [ ] Problem
- [ ] Method
- [ ] Results
- [ ] Limitations
- [ ] Reusable equations / algorithms

## Notes
```

## Environment Variables

| Variable | Purpose |
|---|---|
| `UNPAYWALL_EMAIL` | Required by Unpaywall; improves DOI-to-PDF resolution. |
| `OPENALEX_EMAIL` | Polite pool/contact for OpenAlex. |
| `S2_API_KEY` | Optional Semantic Scholar higher rate limits. |
| `NCBI_API_KEY` | Optional PubMed/PMC higher rate limits. |

## Failure Handling

| Failure | Response |
|---|---|
| No PDF found | Keep metadata and mark `[Abstract only]` or `[Metadata only]`. |
| Publisher paywall | Write `*.access-plan.json` and `*.access-plan.md`; list lawful alternatives and manual next steps. |
| PMCID PDF unavailable | Try PMC XML and write `<paper>.pmc.parsed.md`; mark `[Full text]` when parse succeeds. |
| PDF download returns HTML | Save no PDF; keep landing URL in manifest. |
| Parser unavailable | Keep PDF path; tell the agent to install `pymupdf` or `pypdf` if parsing is needed. |
| Scanned PDF | Mark parse quality low; ask for OCRed PDF or arXiv source if the user needs full reading. |
| Scanned PDF with `--ocr` | Try `ocrmypdf --skip-text`; if unavailable or failed, keep the low-quality parse manifest. |
| Figure/table needs | Use `--extract-figures` to export page-preview PNGs, then hand off to a reader/vision skill for semantic figure/table interpretation. |
| Rate limit | Retry with longer waits for HTTP 429; then skip that source and record each attempt in `run_manifest.json` or the per-paper manifest. |

## Batch Mode

Use `batch` when the user gives a reading list, DOI export, Zotero CSV, or a hand-written identifier list.

Preferred input fields:

| Field | Accepted examples |
|---|---|
| `doi` | `10.2514/1.G006229` |
| `arxiv_id` | `2401.01234`, `arxiv:2401.01234`, `https://arxiv.org/abs/2401.01234` |
| `pmid` | `12345678` |
| `pmcid` | `PMC1234567` |
| `url` | OA PDF URL or publisher landing URL |
| `identifier` / `id` | Any of the above |

Batch mode deduplicates by DOI, arXiv ID, PMID, PMCID, then title-year hash. Keep `run_manifest.json` as the audit trail.

Use `--delay <seconds>` or `batch_delay` in `config.local.json` for polite long batches. Failed identifiers should remain in the manifest while the batch continues.

## Local PDF Mode

Use `ingest-pdf` when the PDF has already been downloaded or supplied by the user.

If an identifier is available, pass it:

```bash
python scripts/paper_research_downloader.py ingest-pdf paper.pdf --identifier "10.xxxx/yyyy" --write-notes
```

If no identifier is available, provide metadata overrides:

```bash
python scripts/paper_research_downloader.py ingest-pdf paper.pdf --title "..." --author "A; B" --year 2024 --write-notes
```

The script copies the PDF to `downloads/`, parses it, writes exports, and creates the Obsidian note.

## Parse Quality

Per-paper manifests include:

| Field | Meaning |
|---|---|
| `pages` | Number of parsed page markers. |
| `word_count` | Extracted word count. |
| `quality` | `good` or `low`; low usually means scanned PDF, image-only PDF, or parser failure. |
| `figures` | Page-preview PNGs created by `--extract-figures`; not semantic figure/table crops. |
| `sentinel` | End-of-paper marker required before full-text synthesis. |

If `quality` is `low`, avoid method/result claims until the PDF is OCRed or a better source is available.

## Reports And Exports

The script writes:

| File | Purpose |
|---|---|
| `papers.csv` | UTF-8 with BOM for Excel compatibility. |
| `papers.bib` | Citation handoff; still validate before manuscript use. |
| `papers.ris` | Zotero/EndNote/Mendeley import handoff. |
| `papers.csl.json` | Pandoc/Citeproc/CSL-compatible citation handoff. |
| `report.md` | Lightweight audit log for Codex/Claude. |
| `report.html` | Static dashboard for browsing records, evidence labels, PDFs, parsed Markdown, and notes. |
| `zotero_matches.json` | Duplicate-candidate audit from local Zotero when `--zotero-check` is used. |

Access-plan files are written under `downloads/` for inaccessible papers:

| File | Purpose |
|---|---|
| `<paper>.access-plan.json` | Structured lawful alternatives and reasons no full text was obtained. |
| `<paper>.access-plan.md` | Human-readable next-step checklist for OA/preprint/manual access. |

Access plans may include Unpaywall locations, DOI landing page, arXiv title matches, CORE candidates, search queries for author repository copies, institutional access, author request, interlibrary loan, and `ingest-pdf` with a user-provided copy.

## Zotero Check

Use `--zotero-check` only as a read-only duplicate audit. It queries the local Zotero API at `zotero_local_api`, defaulting to `http://127.0.0.1:23119/api/users/0`, and writes candidate matches to `zotero_matches.json`.

Do not treat matches as authoritative merges without checking DOI/arXiv/title fields.

## Environment Diagnostics

Run `check-env` before large downloads or first use on a new machine:

```bash
python scripts/paper_research_downloader.py check-env --zotero
```

The command reports Python parser modules (`fitz`, `pypdf`), executables (`pdftotext`, `ocrmypdf`), API-key presence, and optional local Zotero API reachability.

On Windows, command-line JSON output is console-encoding safe; exported files remain UTF-8.

## Version Merge Policy

When multiple sources describe the same paper, merge in this order:

1. Exact DOI, arXiv ID, PMID, or PMCID.
2. Title-year alias only if DOI/arXiv/PMID/PMCID do not conflict.
3. Preserve all source names and the highest citation count.

This catches common arXiv-preprint to DOI-published-version pairs while avoiding obvious identifier conflicts.
