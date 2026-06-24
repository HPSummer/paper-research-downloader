---
name: paper-research-downloader
description: Search, resolve, download, parse, batch-process, audit, and ingest research papers for Codex or Claude. Use when the user wants paper discovery, literature search, DOI/arXiv/PMID/PMCID resolution, open-access PDF download, paywall-aware lawful access plans, local PDF ingestion, mixed identifier batch download, full-text parsing, PMC XML fallback, BibTeX/CSV/RIS/CSL-JSON export, Zotero duplicate checks, dependency diagnostics, resumable cached runs, duplicate-aware Obsidian literature notes with YAML frontmatter, tags, and wiki links. Trigger on requests such as find papers, download papers, paywalled paper, publisher paywall, arXiv PDF, DOI to PDF, PMID/PMC full text, literature set, paper notes, Zotero check, Obsidian paper workflow, wenxian jiansuo, xiazai lunwen, lunwen ruku, lunwen biji, or when combining paper-lookup, PaperScribe, paper-garden, research-papers, and paper-search-pro style workflows.
---

# Paper Research Downloader

## Purpose

Use this skill as a portable paper acquisition pipeline for Codex and Claude:

1. Search multiple scholarly sources.
2. Resolve DOI/arXiv/PMID/PMCID/URL identifiers.
3. Download lawful open-access PDFs or user-provided PDFs.
4. Parse downloaded PDFs into Markdown when local parsers are available.
5. Export `results.json`, Excel-friendly `papers.csv`, `papers.bib`, `papers.ris`, `papers.csl.json`, `report.md`, `report.html`, download manifests, and Obsidian-ready notes.

Do not use piracy sources or bypass paywalls. Use open-access locations, arXiv, PMC, publisher OA files, author pages, repository copies, institutional/manual user access, or user-provided PDFs.

## First Step

If the task asks to improve or compare paper-related skills, read `references/skill-landscape.md`.

If the task involves download or full-text access, read `references/download-strategy.md`.

Then run the local helper from the directory containing this `SKILL.md`:

```bash
python scripts/paper_research_downloader.py --help
```

On Windows PowerShell:

```powershell
python .\scripts\paper_research_downloader.py --help
```

For first use, create a local config:

```powershell
python .\scripts\paper_research_downloader.py config-init
```

Edit `config.local.json` if the user wants default `vault`, `email`, `cache_dir`, `default_out_dir`, tags, source list, resume behavior, or duplicate policy. Do not include `config.local.json` in public uploads.

For long runs, also set `request_delay` and `batch_delay` in `config.local.json`, or pass `--delay` to `batch`.

Check local optional dependencies before a major run:

```powershell
python .\scripts\paper_research_downloader.py check-env --zotero
```

## Workflow

### 1. Classify The Request

| User intent | Action |
|---|---|
| Broad topic search | Run `search`; export ranked metadata; optionally download top papers. |
| Known DOI/arXiv/PMID/PMCID/URL | Run `resolve`; download and parse that one paper. |
| Mixed identifier list | Run `batch` on a `.txt`, `.csv`, or `.json` file. |
| Local PDF already available | Run `ingest-pdf`; enrich with DOI/arXiv metadata if supplied. |
| Obsidian paper library | Use `--vault` and keep notes under `02_literature` unless the user overrides. |
| Zotero duplicate audit | Add `--zotero-check`; keep `zotero_matches.json`. |
| Paywalled publisher paper | Let the run generate `*.access-plan.json` and `*.access-plan.md`; then use legal alternatives or `ingest-pdf` with a user-provided copy. |
| Repeated or long batch | Use cache/resume defaults, `--delay`, and `run_manifest.json`. |
| Deep reading/translation | Use this skill to acquire files, then hand off to `nature-reader` or `paper-scribe` style block reading. |
| Systematic/scoping review | Use this skill for acquisition, then use `literature-review`/`paper-search-pro` style screening and PRISMA logging. |

### 2. Search

Default search uses OpenAlex, Semantic Scholar, arXiv, Crossref, and PubMed public APIs.

```bash
python scripts/paper_research_downloader.py search ^
  --query "low-thrust trajectory optimization multi-objective global optimization" ^
  --limit 30 ^
  --year-min 2018 ^
  --download-top 5 ^
  --email "you@example.com" ^
  --out ".\paper-research-results\low-thrust"
```

Use `--sources openalex,arxiv,crossref,semantic,pubmed` to override source routing. Use `--sort citations`, `--sort year`, or `--sort relevance`.

The command writes `report.md` and a static `report.html` dashboard for quick success/failure review.

### 3. Resolve And Download One Paper

```bash
python scripts/paper_research_downloader.py resolve "arxiv:2301.08243" --out ".\papers\one" --email "you@example.com"
python scripts/paper_research_downloader.py resolve "10.1038/s41586-021-03819-2" --out ".\papers\one" --email "you@example.com"
python scripts/paper_research_downloader.py resolve "PMC1234567" --out ".\papers\one"
```

If Unpaywall is needed for DOI download, pass `--email` or set `UNPAYWALL_EMAIL`.

For PMCID records, if PDF download fails but PMC XML is available, the helper writes `<paper>.pmc.parsed.md` and marks evidence as full text.

For paywalled publisher records, the helper writes an access plan with lawful alternatives: all Unpaywall OA locations, arXiv title matches, CORE candidates, DOI landing page, Google/Scholar all-version queries, institutional access, author copy, interlibrary loan, and manual `ingest-pdf`.

### 4. Batch Download A Literature Set

Use this when the user gives a DOI/arXiv/PMID/PMCID list or an exported CSV.

```bash
python scripts/paper_research_downloader.py batch ^
  --input ".\ids.txt" ^
  --out ".\paper-research-results\batch" ^
  --write-notes ^
  --tag "paper"
```

For large bibliographies, stage metadata first:

```bash
python scripts/paper_research_downloader.py batch --input ".\ids.csv" --metadata-only --write-notes
```

Use `--delay 1.5` for polite throttling. Use `--no-parse` when PDFs should be downloaded but parsed later.

Use `--zotero-check` to query local Zotero for duplicate candidates. Zotero must be running with its local API enabled.

Supported input:

- `.txt`: one identifier per line.
- `.csv`: columns such as `doi`, `arxiv_id`, `pmid`, `pmcid`, `url`, `identifier`, or `id`.
- `.json`: list of strings or objects with the same fields.

Batch runs are resumable by default. Re-run the same command with the same `--out` directory to reuse completed per-paper manifests. Use `--no-resume` to force a fresh attempt.

If a single identifier fails in a batch, the run records the failure and continues.

### 5. Ingest A Local PDF

```bash
python scripts/paper_research_downloader.py ingest-pdf ".\paper.pdf" ^
  --identifier "10.xxxx/yyyy" ^
  --write-notes
```

Use `--title`, `--author`, and `--year` only when no identifier is available or metadata needs correction.

Use `--ocr` when scanned PDFs are likely and `ocrmypdf` is installed. Use `--extract-figures` to export page-preview PNGs for downstream reader workflows. This is reader preparation, not semantic figure/table segmentation.

### 6. Obsidian Ingest

For this user's vault style, prefer:

```bash
python scripts/paper_research_downloader.py search ^
  --query "<topic>" ^
  --download-top 5 ^
  --vault "C:\path\to\trajectory_optimization_kb" ^
  --note-folder "02_literature" ^
  --tag "trajectory-optimization" ^
  --tag "paper"
```

Each note must include:

- YAML frontmatter.
- `tags`.
- `[[...]]` placeholders for concepts/topics.
- absolute `pdf_path` when a file was downloaded.
- metadata table, abstract, and reading checklist.

Notes are duplicate-aware by DOI, arXiv ID, PMID, PMCID, then title hash. Default duplicate policy is `skip`.

### 7. Full-Text Reading Contract

When a PDF is downloaded and parsed successfully, the script emits a parsed Markdown file and a `read_plan`.

Before making full-text claims:

1. Read every `read_plan` chunk in order.
2. Confirm the final `<!-- END-OF-PAPER:<sha> -->` sentinel.
3. Label evidence as `[Full text]`, `[Abstract only]`, or `[Metadata only]`.

Never synthesize methods, results, or limitations from abstract-only records as if the paper had been read.

## Output Files

The helper writes:

```text
<out>/
  results.json
  papers.csv
  papers.bib
  papers.ris
  papers.csl.json
  report.md
  report.html
  run_manifest.json
  zotero_matches.json
  downloads/
    <paper>.pdf
    <paper>.parsed.md
    <paper>.pmc.parsed.md
    <paper>.access-plan.json
    <paper>.access-plan.md
    <paper>.manifest.json
    <paper>_assets/
      page-001.png
  notes/
    <paper>.md
```

Each per-paper manifest includes source candidates tried, retry attempts, download status, SHA256, parser, page count, word count, parse quality, optional OCR/page-preview outputs, optional access plan, `read_plan`, and sentinel.

The helper also maintains a PDF cache under `cache_dir` when configured. Cached PDFs are reused before network download.

When `--vault` is supplied, notes are written to `<vault>/<note-folder>/` and assets stay under the output directory unless the user asks for a different layout.

## Integration Guidance

Use installed local skills as complements:

- `paper-lookup`: broad API reference and source-specific details.
- `nature-reader`: full-paper bilingual reader after PDF acquisition.
- `citation-management`: citation cleanup and BibTeX validation.
- `literature-review`: systematic review methodology after acquisition.

Use external GitHub skills as design inspiration only unless the user approves installing them. Do not execute unreviewed remote scripts.

## Packaging

Create an uploadable package with:

```bash
python scripts/paper_research_downloader.py package
```

The zip excludes `config.local.json`, caches, and `dist/`. The package command also writes release metadata and release notes beside the zip.

For GitHub publishing, keep repository-level docs outside the skill folder. The uploadable skill itself should remain only `SKILL.md`, `agents/`, `references/`, and `scripts/`.

## Validation

After editing this skill, run:

```bash
python scripts/paper_research_downloader.py test
python scripts/paper_research_downloader.py self-test
python C:\Users\Admin\.codex\skills\.system\skill-creator\scripts\quick_validate.py <path-to-this-skill>
```

`test` is offline and covers DOI/arXiv merge behavior, CSV/RIS/CSL export, HTML report generation, Obsidian duplicate skipping, `.txt`/`.csv`/`.json` identifier parsing, PMC XML parsing, Zotero report safety, paywall access plans, environment diagnostics, and package privacy exclusions.
