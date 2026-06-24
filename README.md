# Paper Research Downloader

Portable Codex/Claude skill for lawful scholarly paper search, open-access acquisition, parsing, citation export, and Obsidian literature-note ingestion.

## What It Does

- Search OpenAlex, Semantic Scholar, arXiv, Crossref, PubMed, PMC, and Unpaywall-backed OA locations.
- Resolve DOI, arXiv, PMID, PMCID, URL, and mixed batch identifier files.
- Download lawful open-access PDFs and cache/resume long runs.
- Generate lawful access plans for paywalled publisher records.
- Stage user-mediated institutional access by opening DOI/publisher/library-proxy URLs, then ingest PDFs the user manually downloads.
- Fall back to PMC XML full text when PMCID PDF download is unavailable.
- Parse PDFs to Markdown with read plans and end-of-paper sentinels.
- Export `results.json`, `papers.csv`, `papers.bib`, `papers.ris`, `papers.csl.json`, `report.md`, and `report.html`.
- Write duplicate-aware Obsidian notes with YAML frontmatter, tags, and wiki-link placeholders.
- Run read-only local Zotero duplicate-candidate checks.

## Install

For Codex:

```powershell
Copy-Item -Path .\paper-research-downloader -Destination $env:USERPROFILE\.codex\skills\paper-research-downloader -Recurse -Force
```

For Claude:

```powershell
Copy-Item -Path .\paper-research-downloader -Destination $env:USERPROFILE\.claude\skills\paper-research-downloader -Recurse -Force
```

Or download `dist/paper-research-downloader-v1.4.0.zip` and extract the `paper-research-downloader` folder into your skills directory.

## Quick Start

```powershell
cd .\paper-research-downloader
python .\scripts\paper_research_downloader.py check-env --zotero
python .\scripts\paper_research_downloader.py config-init
python .\scripts\paper_research_downloader.py resolve arxiv:2103.15348 --write-notes
python .\scripts\paper_research_downloader.py batch --input .\ids.txt --metadata-only --write-notes --delay 1
python .\scripts\paper_research_downloader.py institutional-open "10.xxxx/yyyy" --proxy-prefix "https://library.example.edu/login?url=" --download-dir "$env:USERPROFILE\Downloads" --wait-for-pdf --ingest-downloaded
```

## Notes

This skill only uses lawful open-access sources, author/repository copies, PMC/arXiv, publisher OA files, user-mediated institutional access, or user-provided PDFs. It does not use shadow libraries or credential bypasses.

Institutional login is user-mediated only. Do not store usernames, passwords, cookies, sessions, proxy credentials, or tokens in `config.local.json`, skill files, Git commits, release zips, or issue/PR text. The package command runs a private-data scan and blocks likely credential leaks.

`ocrmypdf` is optional. If it is not installed, OCR fallback is skipped and recorded in the manifest.

## Validation

```powershell
python .\paper-research-downloader\scripts\paper_research_downloader.py test
python .\paper-research-downloader\scripts\paper_research_downloader.py self-test
```
