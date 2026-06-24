#!/usr/bin/env python3
"""Portable scholarly search, OA download, PDF parse, and Obsidian note helper."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


VERSION = "1.4.0"
USER_AGENT = "paper-research-downloader/1.4 (+https://github.com/openai/codex)"
ATOM = "{http://www.w3.org/2005/Atom}"
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = {
    "email": "",
    "vault": "",
    "note_folder": "02_literature",
    "default_out_dir": "",
    "cache_dir": "",
    "default_tags": ["paper"],
    "sources": "openalex,semantic,arxiv,crossref,pubmed",
    "s2_key": "",
    "ncbi_key": "",
    "resume": True,
    "duplicate_policy": "skip",
    "request_delay": 0.0,
    "batch_delay": 0.0,
    "zotero_local_api": "http://127.0.0.1:23119/api/users/0",
    "institutional_proxy_prefix": "",
    "download_dir": str(Path.home() / "Downloads"),
}
PRIVATE_ARTIFACT_NAMES = {
    ".env",
    ".netrc",
    "config.local.json",
    "cookies.txt",
    "credentials.json",
    "institutional-credentials.json",
    "institutional_credentials.json",
}
PRIVATE_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|credential|cookie|session|secret|access[_-]?token|refresh[_-]?token|bearer|institutional[_-]?username|institutional[_-]?password)\b"
    r"\s*[:=]\s*['\"]?([^'\"\s,;}]+)"
)
PRIVATE_URL_RE = re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@")
PLACEHOLDER_VALUES = {"", "none", "null", "false", "true", "changeme", "example", "placeholder", "your_password", "your-token"}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def today_iso() -> str:
    return dt.date.today().isoformat()


def iso_now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def print_json(data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        print(json.dumps(data, ensure_ascii=True, indent=2))


def skill_config_path() -> Path:
    return SKILL_DIR / "config.local.json"


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config_path = Path(path).expanduser() if path else skill_config_path()
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            config.update({k: v for k, v in loaded.items() if v is not None})
    return config


def write_default_config(path: Path, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        return path
    sample = dict(DEFAULT_CONFIG)
    sample.update(
        {
            "email": os.getenv("UNPAYWALL_EMAIL") or os.getenv("OPENALEX_EMAIL") or "",
            "cache_dir": str((Path.home() / ".cache" / "paper-research-downloader").resolve()),
            "default_out_dir": str((Path.cwd() / "paper-research-results").resolve()),
            "default_tags": ["paper", "literature"],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def config_value(args: argparse.Namespace, config: Dict[str, Any], name: str, default: Any = "") -> Any:
    value = getattr(args, name, None)
    if value not in (None, "", [], False):
        return value
    return config.get(name, default)


def default_output_dir(config: Dict[str, Any], kind: str, slug: str) -> Path:
    base = config.get("default_out_dir") or ""
    root = Path(base).expanduser() if base else Path.cwd() / "paper-research-results"
    return root / f"{kind}_{safe_slug(slug, 40)}_{now_stamp()}"


def default_cache_dir(config: Dict[str, Any]) -> Path:
    cache = config.get("cache_dir") or ""
    return Path(cache).expanduser() if cache else Path.home() / ".cache" / "paper-research-downloader"


def default_download_dir(config: Dict[str, Any]) -> Path:
    download_dir = config.get("download_dir") or ""
    return Path(download_dir).expanduser() if download_dir else Path.home() / "Downloads"


def safe_slug(text: str, max_len: int = 96) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"[^\w\-\. ]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text.strip().lower())
    text = text.strip("-._")
    if not text:
        text = "paper"
    return text[:max_len].strip("-._") or "paper"


def normalize_space(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def looks_like_doi(value: str) -> bool:
    return bool(re.match(r"(?i)^10\.\d{4,9}/\S+$", (value or "").strip()))


def is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def ensure_safe_open_url(url: str) -> str:
    if not is_http_url(url):
        raise ValueError(f"Refusing to open non-HTTP URL: {url}")
    if PRIVATE_URL_RE.search(url):
        raise ValueError("Refusing to open URL containing embedded credentials.")
    return url


def build_proxy_url(proxy_prefix: str, target_url: str) -> str:
    target = ensure_safe_open_url(target_url)
    prefix = (proxy_prefix or "").strip()
    if not prefix:
        return ""
    ensure_safe_open_url(prefix)
    parsed = urllib.parse.urlparse(prefix)
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    if "=" in (parsed.query or "") and "url" not in query_keys and not prefix.endswith(("=", "%3D")):
        separator = "&" if parsed.query else "?"
        return prefix + separator + "url=" + urllib.parse.quote(target, safe="")
    if prefix.endswith(("=", "%3D")):
        return prefix + urllib.parse.quote(target, safe="")
    if parsed.query:
        separator = "&" if not prefix.endswith(("&", "?")) else ""
        return prefix + separator + "url=" + urllib.parse.quote(target, safe="")
    if prefix.endswith(("/", "?")):
        return prefix + urllib.parse.quote(target, safe="")
    return prefix + urllib.parse.quote(target, safe="")


def open_url_in_browser(url: str) -> bool:
    return webbrowser.open(ensure_safe_open_url(url), new=2)


def compact_title_key(title: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_space(title).lower())


def strip_tags(text: Optional[str]) -> str:
    return normalize_space(re.sub(r"<[^>]+>", " ", text or ""))


def normalize_doi(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    value = value.strip().rstrip(".,;")
    match = re.search(r"10\.\d{4,9}/\S+", value, flags=re.I)
    return match.group(0).lower() if match else ""


def parse_arxiv_id(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    patterns = [
        r"arxiv\.org/(?:abs|pdf|html)/([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
        r"\b([a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if match:
            return match.group(1)
    return ""


def parse_pmcid(value: Optional[str]) -> str:
    if not value:
        return ""
    match = re.search(r"\bPMC\d+\b", value.strip(), flags=re.I)
    return match.group(0).upper() if match else ""


def http_request(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> bytes:
    delay = float(os.getenv("PRD_REQUEST_DELAY", "0") or "0")
    if delay > 0:
        time.sleep(delay)
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query}"
    req_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    retries: int = 1,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            data = http_request(url, params=params, headers=headers, timeout=timeout)
            return json.loads(data.decode("utf-8", errors="replace"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                wait = 1.5 * (attempt + 1)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    wait = max(wait, 8.0)
                time.sleep(wait)
    raise RuntimeError(f"GET JSON failed for {url}: {last_error}")


def get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    retries: int = 1,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            data = http_request(url, params=params, timeout=timeout)
            return data.decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                wait = 1.5 * (attempt + 1)
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    wait = max(wait, 8.0)
                time.sleep(wait)
    raise RuntimeError(f"GET text failed for {url}: {last_error}")


def abstract_from_inverted_index(index: Optional[Dict[str, List[int]]]) -> str:
    if not index:
        return ""
    positions: List[Tuple[int, str]] = []
    for word, locs in index.items():
        for loc in locs:
            positions.append((loc, word))
    return " ".join(word for _, word in sorted(positions))


def record_key(record: Dict[str, Any]) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv_id = parse_arxiv_id(record.get("arxiv_id") or record.get("url"))
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower()}"
    pmid = str(record.get("pmid") or "").strip()
    if pmid:
        return f"pmid:{pmid}"
    pmcid = parse_pmcid(record.get("pmcid") or record.get("url"))
    if pmcid:
        return f"pmcid:{pmcid.lower()}"
    title = normalize_space(record.get("title")).lower()
    year = record.get("year") or ""
    digest = hashlib.sha1(f"{title}|{year}".encode("utf-8")).hexdigest()[:12]
    return f"title:{digest}"


def make_record(**kwargs: Any) -> Dict[str, Any]:
    record = {
        "id": "",
        "title": "",
        "authors": [],
        "year": None,
        "venue": "",
        "doi": "",
        "arxiv_id": "",
        "pmid": "",
        "pmcid": "",
        "url": "",
        "pdf_url": "",
        "abstract": "",
        "citation_count": None,
        "source_rank": None,
        "relevance_score": 0.0,
        "sources": [],
        "evidence": "Metadata only",
    }
    record.update(kwargs)
    record["title"] = normalize_space(record.get("title"))
    record["venue"] = normalize_space(record.get("venue"))
    record["abstract"] = strip_tags(record.get("abstract"))
    record["doi"] = normalize_doi(record.get("doi"))
    record["arxiv_id"] = parse_arxiv_id(record.get("arxiv_id") or record.get("url"))
    record["pmcid"] = parse_pmcid(record.get("pmcid") or record.get("url"))
    record["id"] = record_key(record)
    return record


def query_terms(query: str) -> List[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "about",
        "paper",
        "papers",
        "study",
        "studies",
        "review",
        "literature",
        "research",
    }
    terms = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", query.lower()):
        if token not in stop:
            terms.append(token)
    return list(dict.fromkeys(terms))


def score_relevance(record: Dict[str, Any], query: str) -> float:
    terms = query_terms(query)
    if not terms:
        return 0.0
    title = (record.get("title") or "").lower()
    abstract = (record.get("abstract") or "").lower()
    venue = (record.get("venue") or "").lower()
    haystack = f"{title} {abstract} {venue}"
    score = 0.0
    for term in terms:
        if term in title:
            score += 3.0
        elif term in abstract:
            score += 1.2
        elif term in venue:
            score += 0.4
    phrases = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+(?:\s+[A-Za-z0-9][A-Za-z0-9\-]+)+", query.lower())
    for phrase in phrases:
        words = [w for w in query_terms(phrase)]
        if len(words) >= 2:
            phrase_text = " ".join(words)
            if phrase_text in title:
                score += 4.0
            elif phrase_text in haystack:
                score += 2.0
    if record.get("sources"):
        score += min(len(record["sources"]), 3) * 0.2
    return round(score, 3)


def merge_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}

    def title_year_alias(record: Dict[str, Any]) -> str:
        title_key = compact_title_key(record.get("title"))
        year = record.get("year") or ""
        return f"titleyear:{title_key}:{year}" if title_key and year else ""

    def has_identifier_conflict(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        for field in ("doi", "arxiv_id", "pmid", "pmcid"):
            a = str(left.get(field) or "").strip().lower()
            b = str(right.get(field) or "").strip().lower()
            if a and b and a != b:
                return True
        return False

    def absorb(current: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        for field in ("title", "venue", "doi", "arxiv_id", "pmid", "pmcid", "url", "pdf_url", "abstract"):
            if not current.get(field) and record.get(field):
                current[field] = record[field]
        if not current.get("authors") and record.get("authors"):
            current["authors"] = record["authors"]
        if not current.get("year") and record.get("year"):
            current["year"] = record["year"]
        if record.get("citation_count") is not None:
            current_count = current.get("citation_count")
            if current_count is None or record["citation_count"] > current_count:
                current["citation_count"] = record["citation_count"]
        current_sources = set(current.get("sources") or [])
        current_sources.update(record.get("sources") or [])
        current["sources"] = sorted(current_sources)
        current["id"] = record_key(current)
        return current

    def reassign_aliases(old_key: str, new_key: str) -> None:
        for alias, target in list(aliases.items()):
            if target == old_key:
                aliases[alias] = new_key

    for record in records:
        record = make_record(**record)
        exact_keys = [record["id"]]
        if record.get("doi"):
            exact_keys.append(f"doi:{record['doi']}")
        if record.get("arxiv_id"):
            exact_keys.append(f"arxiv:{record['arxiv_id'].lower()}")
        if record.get("pmid"):
            exact_keys.append(f"pmid:{record['pmid']}")
        if record.get("pmcid"):
            exact_keys.append(f"pmcid:{record['pmcid'].lower()}")
        exact_keys = list(dict.fromkeys(k for k in exact_keys if k))
        fuzzy_key = title_year_alias(record)

        known = [aliases[k] for k in exact_keys if k in aliases]
        fuzzy_conflict = False
        if not known and fuzzy_key and fuzzy_key in aliases:
            fuzzy_target = aliases[fuzzy_key]
            if fuzzy_target in merged and not has_identifier_conflict(merged[fuzzy_target], record):
                known.append(fuzzy_target)
            else:
                fuzzy_conflict = True

        key = known[0] if known else record["id"]
        if record.get("doi"):
            key = f"doi:{record['doi']}"
        for old in known:
            if old != key and old in merged:
                old_record = merged.pop(old)
                if has_identifier_conflict(old_record, record):
                    merged[old] = old_record
                    continue
                reassign_aliases(old, key)
                record = absorb(old_record, record)
                break
        if key not in merged:
            merged[key] = record
            for alias in exact_keys:
                aliases[alias] = key
            if fuzzy_key and not fuzzy_conflict:
                aliases[fuzzy_key] = key
            continue
        current = merged[key]
        if has_identifier_conflict(current, record) and not any(k in aliases and aliases[k] == key for k in exact_keys):
            fallback = record["id"]
            merged[fallback] = record
            for alias in exact_keys:
                aliases[alias] = fallback
            continue
        absorb(current, record)
        for alias in exact_keys:
            aliases[alias] = key
        if fuzzy_key and not fuzzy_conflict:
            aliases[fuzzy_key] = key
    return list(merged.values())


def search_openalex(query: str, limit: int, year_min: Optional[int], email: str = "") -> List[Dict[str, Any]]:
    filters = []
    if year_min:
        filters.append(f"from_publication_date:{year_min}-01-01")
    params = {
        "search": query,
        "per-page": min(limit, 200),
        "sort": "cited_by_count:desc",
    }
    if filters:
        params["filter"] = ",".join(filters)
    if email:
        params["mailto"] = email
    data = get_json("https://api.openalex.org/works", params=params, retries=1)
    records = []
    for rank, item in enumerate(data.get("results", []), start=1):
        authors = [
            a.get("author", {}).get("display_name", "")
            for a in item.get("authorships", [])
            if a.get("author", {}).get("display_name")
        ]
        best = item.get("best_oa_location") or {}
        primary = item.get("primary_location") or {}
        source = (primary.get("source") or {}).get("display_name") or (best.get("source") or {}).get("display_name") or ""
        pdf_url = best.get("pdf_url") or primary.get("pdf_url") or ""
        url = item.get("doi") or item.get("id") or best.get("landing_page_url") or primary.get("landing_page_url") or ""
        records.append(
            make_record(
                title=item.get("title") or "",
                authors=authors,
                year=item.get("publication_year"),
                venue=source,
                doi=item.get("doi") or (item.get("ids") or {}).get("doi") or "",
                url=url,
                pdf_url=pdf_url,
                abstract=abstract_from_inverted_index(item.get("abstract_inverted_index")),
                citation_count=item.get("cited_by_count"),
                source_rank=rank,
                sources=["openalex"],
            )
        )
    return records


def search_semantic(query: str, limit: int, year_min: Optional[int], api_key: str = "") -> List[Dict[str, Any]]:
    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": "title,authors,year,venue,abstract,citationCount,externalIds,openAccessPdf,url,isOpenAccess",
    }
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    data = get_json("https://api.semanticscholar.org/graph/v1/paper/search", params=params, headers=headers, retries=1)
    records = []
    for rank, item in enumerate(data.get("data", []), start=1):
        year = item.get("year")
        if year_min and year and int(year) < year_min:
            continue
        ext = item.get("externalIds") or {}
        pdf = (item.get("openAccessPdf") or {}).get("url") or ""
        authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
        records.append(
            make_record(
                title=item.get("title") or "",
                authors=authors,
                year=year,
                venue=item.get("venue") or "",
                doi=ext.get("DOI") or "",
                arxiv_id=ext.get("ArXiv") or "",
                pmid=ext.get("PubMed") or "",
                url=item.get("url") or "",
                pdf_url=pdf,
                abstract=item.get("abstract") or "",
                citation_count=item.get("citationCount"),
                source_rank=rank,
                sources=["semantic"],
            )
        )
    return records


def search_crossref(query: str, limit: int, year_min: Optional[int], email: str = "") -> List[Dict[str, Any]]:
    filters = []
    if year_min:
        filters.append(f"from-pub-date:{year_min}-01-01")
    params = {
        "query.bibliographic": query,
        "rows": min(limit, 100),
        "sort": "is-referenced-by-count",
        "order": "desc",
    }
    if filters:
        params["filter"] = ",".join(filters)
    if email:
        params["mailto"] = email
    data = get_json("https://api.crossref.org/works", params=params, retries=1)
    records = []
    for rank, item in enumerate((data.get("message") or {}).get("items", []), start=1):
        title = (item.get("title") or [""])[0]
        authors = []
        for author in item.get("author", []) or []:
            name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
            if name:
                authors.append(name)
        year = None
        for date_field in ("published-print", "published-online", "issued"):
            parts = ((item.get(date_field) or {}).get("date-parts") or [[]])[0]
            if parts:
                year = parts[0]
                break
        venue = (item.get("container-title") or [""])[0]
        pdf_url = ""
        for link in item.get("link", []) or []:
            if "pdf" in (link.get("content-type") or "").lower():
                pdf_url = link.get("URL") or ""
                break
        records.append(
            make_record(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=item.get("DOI") or "",
                url=item.get("URL") or "",
                pdf_url=pdf_url,
                abstract=item.get("abstract") or "",
                citation_count=item.get("is-referenced-by-count"),
                source_rank=rank,
                sources=["crossref"],
            )
        )
    return records


def search_arxiv(query: str, limit: int, year_min: Optional[int]) -> List[Dict[str, Any]]:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": min(limit, 100),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    text = get_text("https://export.arxiv.org/api/query", params=params, retries=1)
    root = ET.fromstring(text)
    records = []
    for rank, entry in enumerate(root.findall(f"{ATOM}entry"), start=1):
        title = normalize_space(entry.findtext(f"{ATOM}title"))
        summary = normalize_space(entry.findtext(f"{ATOM}summary"))
        url = entry.findtext(f"{ATOM}id") or ""
        arxiv_id = parse_arxiv_id(url)
        published = entry.findtext(f"{ATOM}published") or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        if year_min and year and year < year_min:
            continue
        authors = [normalize_space(a.findtext(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")]
        pdf_url = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        records.append(
            make_record(
                title=title,
                authors=[a for a in authors if a],
                year=year,
                venue="arXiv",
                arxiv_id=arxiv_id,
                url=url,
                pdf_url=pdf_url,
                abstract=summary,
                citation_count=None,
                source_rank=rank,
                sources=["arxiv"],
            )
        )
    return records


def search_pubmed(query: str, limit: int, year_min: Optional[int], api_key: str = "") -> List[Dict[str, Any]]:
    term = query
    if year_min:
        term = f"({query}) AND {year_min}:3000[dp]"
    params = {"db": "pubmed", "term": term, "retmax": min(limit, 200), "retmode": "json", "sort": "relevance"}
    if api_key:
        params["api_key"] = api_key
    data = get_json(f"{NCBI_BASE}/esearch.fcgi", params=params, retries=1)
    ids = (data.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    summary_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if api_key:
        summary_params["api_key"] = api_key
    summary = get_json(f"{NCBI_BASE}/esummary.fcgi", params=summary_params, retries=1)
    result = summary.get("result") or {}
    records = []
    for rank, pmid in enumerate(ids, start=1):
        item = result.get(pmid) or {}
        article_ids = item.get("articleids") or []
        doi = ""
        for article_id in article_ids:
            if article_id.get("idtype") == "doi":
                doi = article_id.get("value") or ""
                break
        year = None
        pubdate = item.get("pubdate") or item.get("epubdate") or ""
        match = re.search(r"\b(19|20)\d{2}\b", pubdate)
        if match:
            year = int(match.group(0))
        records.append(
            make_record(
                title=item.get("title") or "",
                authors=[a.get("name", "") for a in item.get("authors", []) if a.get("name")],
                year=year,
                venue=item.get("fulljournalname") or item.get("source") or "PubMed",
                doi=doi,
                pmid=pmid,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                abstract="",
                citation_count=None,
                source_rank=rank,
                sources=["pubmed"],
            )
        )
    return records


def pubmed_by_pmid(pmid: str, api_key: str = "") -> Dict[str, Any]:
    pmid = str(pmid or "").strip()
    if not pmid:
        return {}
    params = {"db": "pubmed", "id": pmid, "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    try:
        summary = get_json(f"{NCBI_BASE}/esummary.fcgi", params=params, retries=1)
    except RuntimeError:
        return {}
    item = (summary.get("result") or {}).get(pmid) or {}
    if not item:
        return {}
    doi = ""
    pmcid = ""
    for article_id in item.get("articleids") or []:
        if article_id.get("idtype") == "doi":
            doi = article_id.get("value") or ""
        if article_id.get("idtype") == "pmc":
            pmcid = article_id.get("value") or ""
    year = None
    pubdate = item.get("pubdate") or item.get("epubdate") or ""
    match = re.search(r"\b(19|20)\d{2}\b", pubdate)
    if match:
        year = int(match.group(0))
    return make_record(
        title=item.get("title") or "",
        authors=[a.get("name", "") for a in item.get("authors", []) if a.get("name")],
        year=year,
        venue=item.get("fulljournalname") or item.get("source") or "PubMed",
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        sources=["pubmed"],
    )


def pmc_by_pmcid(pmcid: str, api_key: str = "") -> Dict[str, Any]:
    pmcid = parse_pmcid(pmcid)
    if not pmcid:
        return {}
    params = {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    try:
        xml_text = get_text(f"{NCBI_BASE}/efetch.fcgi", params=params, retries=1)
    except RuntimeError:
        return make_record(pmcid=pmcid, url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", sources=["pmc"])

    title = ""
    doi = ""
    pmid = ""
    year = None
    abstract = ""
    try:
        root = ET.fromstring(xml_text)
        title_node = root.find(".//article-title")
        title = "".join(title_node.itertext()) if title_node is not None else ""
        for aid in root.findall(".//article-id"):
            pub_id_type = (aid.attrib.get("pub-id-type") or "").lower()
            if pub_id_type == "doi":
                doi = normalize_space("".join(aid.itertext()))
            elif pub_id_type == "pmid":
                pmid = normalize_space("".join(aid.itertext()))
        year_node = root.find(".//pub-date/year")
        if year_node is not None and (year_node.text or "").isdigit():
            year = int(year_node.text or "0")
        abs_node = root.find(".//abstract")
        abstract = normalize_space(" ".join(abs_node.itertext())) if abs_node is not None else ""
    except ET.ParseError:
        pass
    return make_record(
        title=title or pmcid,
        year=year,
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        pdf_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/",
        abstract=abstract,
        sources=["pmc"],
    )


def fetch_pmc_xml(pmcid: str, api_key: str = "") -> str:
    pmcid = parse_pmcid(pmcid)
    if not pmcid:
        return ""
    params = {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    try:
        return get_text(f"{NCBI_BASE}/efetch.fcgi", params=params, retries=1)
    except RuntimeError:
        return ""


def parse_pmc_xml_to_markdown(xml_text: str, record: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    if not xml_text.strip():
        return {"status": "unparsed", "parser": "pmc_xml", "parsed_path": "", "read_plan": [], "sentinel": ""}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"status": "unparsed", "parser": "pmc_xml", "parsed_path": "", "error": str(exc), "read_plan": [], "sentinel": ""}

    def node_text(node: Optional[ET.Element]) -> str:
        return normalize_space(" ".join(node.itertext())) if node is not None else ""

    sections: List[str] = []
    title = record.get("title") or node_text(root.find(".//article-title")) or record.get("pmcid") or "PMC article"
    abstract = node_text(root.find(".//abstract"))
    if abstract:
        sections.append("## Abstract\n\n" + abstract)
    for sec in root.findall(".//body//sec"):
        heading = node_text(sec.find("title")) or "Section"
        paras = [node_text(p) for p in sec.findall("p")]
        paras = [p for p in paras if p]
        if paras:
            sections.append(f"## {heading}\n\n" + "\n\n".join(paras))
    if not sections:
        body = node_text(root.find(".//body")) or abstract
        if body:
            sections.append("## Full Text\n\n" + body)
    if not sections:
        return {"status": "unparsed", "parser": "pmc_xml", "parsed_path": "", "read_plan": [], "sentinel": ""}

    downloads_dir = out_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    parsed_path = downloads_dir / f"{safe_slug(title)}.pmc.parsed.md"
    text = "\n\n".join(sections)
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    sentinel = f"END-OF-PAPER:{digest}"
    body = "\n".join(
        [
            f"# {title}",
            "",
            f"- DOI: {record.get('doi') or ''}",
            f"- PMCID: {record.get('pmcid') or ''}",
            "- Parser: pmc_xml",
            "",
            text,
            "",
            f"<!-- {sentinel} -->",
            "",
        ]
    )
    parsed_path.write_text(body, encoding="utf-8")
    lines = body.splitlines()
    words = len(re.findall(r"\b\w+\b", text))
    read_plan = [{"path": str(parsed_path.resolve()), "offset": offset, "limit": 450} for offset in range(0, len(lines), 450)]
    return {
        "status": "ok",
        "parser": "pmc_xml",
        "parsed_path": str(parsed_path.resolve()),
        "parsed_lines": len(lines),
        "parsed_chars": len(body),
        "word_count": words,
        "quality": "good" if words >= 300 else "low",
        "read_plan": read_plan,
        "sentinel": sentinel,
    }


def write_access_plan(record: Dict[str, Any], dl: Dict[str, Any], out_dir: Path, email: str = "") -> Dict[str, Any]:
    plan = build_access_plan(record, dl, email=email)
    downloads_dir = out_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_slug(record.get("title") or record.get("id") or "paper")
    json_path = downloads_dir / f"{slug}.access-plan.json"
    md_path = downloads_dir / f"{slug}.access-plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Access Plan: {record.get('title') or record.get('id') or 'Paper'}",
        "",
        "This plan lists lawful alternatives only. It does not bypass publisher paywalls.",
        "",
        "## Reasons",
        "",
    ]
    lines.extend(f"- {reason}" for reason in plan.get("reasons") or [])
    lines.extend(["", "## Alternatives", ""])
    for item in plan.get("alternatives") or []:
        label = item.get("source") or item.get("action") or "source"
        url = item.get("url") or ""
        action = item.get("action") or ""
        if url:
            lines.append(f"- {label}: {url}" + (f" ({action})" if action else ""))
        else:
            lines.append(f"- {label}: {action}")
    lines.extend(["", "## Next Steps", ""])
    lines.extend(f"- {step}" for step in plan.get("next_steps") or [])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    plan["path"] = str(json_path.resolve())
    plan["markdown_path"] = str(md_path.resolve())
    return plan


def openalex_by_doi(doi: str, email: str = "") -> Dict[str, Any]:
    if not doi:
        return {}
    params = {"mailto": email} if email else None
    url = f"https://api.openalex.org/works/doi:{urllib.parse.quote('https://doi.org/' + doi, safe='')}"
    try:
        item = get_json(url, params=params, retries=1)
    except RuntimeError:
        return {}
    return make_record(
        title=item.get("title") or "",
        authors=[
            a.get("author", {}).get("display_name", "")
            for a in item.get("authorships", [])
            if a.get("author", {}).get("display_name")
        ],
        year=item.get("publication_year"),
        venue=((item.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
        doi=item.get("doi") or doi,
        url=item.get("doi") or item.get("id") or "",
        pdf_url=((item.get("best_oa_location") or {}).get("pdf_url") or (item.get("primary_location") or {}).get("pdf_url") or ""),
        abstract=abstract_from_inverted_index(item.get("abstract_inverted_index")),
        citation_count=item.get("cited_by_count"),
        sources=["openalex"],
    )


def crossref_by_doi(doi: str, email: str = "") -> Dict[str, Any]:
    if not doi:
        return {}
    params = {"mailto": email} if email else None
    try:
        data = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}", params=params, retries=1)
    except RuntimeError:
        return {}
    item = data.get("message") or {}
    title = (item.get("title") or [""])[0]
    authors = []
    for author in item.get("author", []) or []:
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        if name:
            authors.append(name)
    year = None
    for date_field in ("published-print", "published-online", "issued"):
        parts = ((item.get(date_field) or {}).get("date-parts") or [[]])[0]
        if parts:
            year = parts[0]
            break
    pdf_url = ""
    for link in item.get("link", []) or []:
        if "pdf" in (link.get("content-type") or "").lower():
            pdf_url = link.get("URL") or ""
            break
    return make_record(
        title=title,
        authors=authors,
        year=year,
        venue=(item.get("container-title") or [""])[0],
        doi=item.get("DOI") or doi,
        url=item.get("URL") or f"https://doi.org/{doi}",
        pdf_url=pdf_url,
        abstract=item.get("abstract") or "",
        citation_count=item.get("is-referenced-by-count"),
        sources=["crossref"],
    )


def arxiv_by_id(arxiv_id: str) -> Dict[str, Any]:
    if not arxiv_id:
        return {}
    try:
        text = get_text("https://export.arxiv.org/api/query", params={"id_list": arxiv_id}, retries=1)
        root = ET.fromstring(text)
        entry = root.find(f"{ATOM}entry")
        if entry is not None:
            title = normalize_space(entry.findtext(f"{ATOM}title"))
            summary = normalize_space(entry.findtext(f"{ATOM}summary"))
            url = entry.findtext(f"{ATOM}id") or f"https://arxiv.org/abs/{arxiv_id}"
            published = entry.findtext(f"{ATOM}published") or ""
            year = int(published[:4]) if published[:4].isdigit() else None
            authors = [normalize_space(a.findtext(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")]
            pdf_url = ""
            for link in entry.findall(f"{ATOM}link"):
                if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                    pdf_url = link.attrib.get("href", "")
                    break
            return make_record(
                title=title,
                authors=[a for a in authors if a],
                year=year,
                venue="arXiv",
                arxiv_id=arxiv_id,
                url=url,
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{arxiv_id}",
                abstract=summary,
                sources=["arxiv"],
            )
    except Exception:
        pass
    try:
        html_text = get_text(f"https://arxiv.org/abs/{arxiv_id}", timeout=20, retries=0)
        title_match = re.search(r"<h1[^>]*class=\"title[^>]*>\s*<span[^>]*>Title:\s*</span>\s*(.*?)</h1>", html_text, flags=re.I | re.S)
        abstract_match = re.search(r"<blockquote[^>]*class=\"abstract[^>]*>\s*<span[^>]*>Abstract:\s*</span>\s*(.*?)</blockquote>", html_text, flags=re.I | re.S)
        authors_match = re.search(r"<div[^>]*class=\"authors[^>]*>\s*<span[^>]*>Authors:\s*</span>\s*(.*?)</div>", html_text, flags=re.I | re.S)
        title = strip_tags(title_match.group(1)) if title_match else f"arXiv:{arxiv_id}"
        abstract = strip_tags(abstract_match.group(1)) if abstract_match else ""
        authors = []
        if authors_match:
            authors = [strip_tags(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", authors_match.group(1), flags=re.I | re.S)]
        return make_record(
            title=title,
            authors=authors,
            venue="arXiv",
            arxiv_id=arxiv_id,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            abstract=abstract,
            sources=["arxiv"],
        )
    except Exception:
        pass
    return make_record(
        title=f"arXiv:{arxiv_id}",
        venue="arXiv",
        arxiv_id=arxiv_id,
        url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        sources=["arxiv"],
    )


def unpaywall_pdf_url(doi: str, email: str) -> Tuple[str, Dict[str, Any]]:
    if not doi or not email:
        return "", {}
    try:
        data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}", params={"email": email}, retries=1)
    except RuntimeError:
        return "", {}
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or ""
    if not pdf_url:
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                pdf_url = loc["url_for_pdf"]
                break
    return pdf_url, data


def unpaywall_locations(doi: str, email: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not doi or not email:
        return [], {}
    try:
        data = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}", params={"email": email}, retries=1)
    except RuntimeError:
        return [], {}
    locations = []
    best = data.get("best_oa_location") or {}
    for loc in [best] + list(data.get("oa_locations") or []):
        if not loc:
            continue
        url = loc.get("url_for_pdf") or loc.get("url") or loc.get("url_for_landing_page") or ""
        if not url:
            continue
        locations.append(
            {
                "source": "unpaywall",
                "url": url,
                "host_type": loc.get("host_type") or "",
                "license": loc.get("license") or "",
                "version": loc.get("version") or "",
                "is_best": loc == best,
            }
        )
    deduped = []
    seen = set()
    for loc in locations:
        if loc["url"] not in seen:
            deduped.append(loc)
            seen.add(loc["url"])
    return deduped, data


def core_search_locations(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    query = record.get("doi") or record.get("title") or ""
    if not query:
        return []
    try:
        data = get_json("https://api.core.ac.uk/v3/search/works", params={"q": query, "limit": 5}, retries=0, timeout=20)
    except RuntimeError:
        return []
    locations = []
    target_title = compact_title_key(record.get("title"))
    target_title_text = normalize_space(record.get("title")).lower()
    target_doi = normalize_doi(record.get("doi"))
    for item in data.get("results") or []:
        item_title = compact_title_key(item.get("title"))
        item_title_text = normalize_space(item.get("title")).lower()
        item_doi = normalize_doi(item.get("doi") or "")
        if target_doi and item_doi and item_doi != target_doi:
            continue
        if target_title and item_title and target_title != item_title:
            target_tokens = set(re.findall(r"[a-z0-9]{4,}", target_title_text))
            item_tokens = set(re.findall(r"[a-z0-9]{4,}", item_title_text))
            overlap = len(target_tokens & item_tokens) / max(len(target_tokens), 1)
            if overlap < 0.75:
                continue
        for url in [item.get("downloadUrl"), item.get("fullTextLink")]:
            if url:
                locations.append({"source": "core", "url": url, "title": item.get("title") or "", "year": item.get("yearPublished") or ""})
    return locations


def arxiv_title_locations(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    title = normalize_space(record.get("title"))
    if not title or record.get("arxiv_id"):
        return []
    try:
        records = search_arxiv(f'ti:"{title}"', limit=3, year_min=None)
    except Exception:
        return []
    locations = []
    title_key = compact_title_key(title)
    for rec in records:
        if rec.get("pdf_url") and compact_title_key(rec.get("title")) == title_key:
            locations.append({"source": "arxiv_title_match", "url": rec["pdf_url"], "arxiv_id": rec.get("arxiv_id") or "", "title": rec.get("title") or ""})
    return locations


def build_access_plan(record: Dict[str, Any], dl: Dict[str, Any], email: str = "") -> Dict[str, Any]:
    tried = dl.get("tried") or []
    reasons = []
    if not dl.get("candidate_count"):
        reasons.append("no_open_pdf_candidate")
    if any(item.get("status") in {"not_pdf", "failed"} for item in tried):
        reasons.append("candidate_pdf_failed_or_not_pdf")
    if record.get("doi") and not email:
        reasons.append("unpaywall_email_missing")

    alternatives: List[Dict[str, Any]] = []
    if record.get("doi"):
        if email:
            locs, data = unpaywall_locations(record["doi"], email)
            alternatives.extend(locs)
            if data and not locs:
                reasons.append("unpaywall_no_oa_location")
        alternatives.append({"source": "doi_landing_page", "url": f"https://doi.org/{record['doi']}", "action": "open_landing_page"})
    if record.get("arxiv_id"):
        alternatives.append({"source": "arxiv", "url": f"https://arxiv.org/pdf/{record['arxiv_id']}", "action": "download_preprint"})
    if record.get("pmcid"):
        alternatives.append({"source": "pmc", "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{record['pmcid']}/", "action": "open_pmc_fulltext"})
    alternatives.extend(arxiv_title_locations(record))
    alternatives.extend(core_search_locations(record))

    title = normalize_space(record.get("title"))
    if title:
        query = urllib.parse.quote(f'"{title}" filetype:pdf')
        alternatives.append({"source": "web_search_query", "url": f"https://www.google.com/search?q={query}", "action": "search_author_repository_copy"})
        alternatives.append({"source": "scholar_query", "url": f"https://scholar.google.com/scholar?q={urllib.parse.quote(title)}", "action": "check_all_versions"})

    alternatives.append({"source": "manual_user_pdf", "action": "ask_user_to_provide_pdf_or_export_from_library"})
    alternatives.append(
        {
            "source": "institutional_access",
            "action": "run institutional-open to open DOI/publisher/library-proxy pages, then ingest the user-downloaded PDF",
        }
    )
    alternatives.append({"source": "interlibrary_loan", "action": "request_copy_through_library_or_author"})

    deduped = []
    seen = set()
    for item in alternatives:
        key = item.get("url") or f"{item.get('source')}:{item.get('action')}"
        if key in seen:
            continue
        deduped.append(item)
        seen.add(key)

    return {
        "status": "needs_access",
        "legal_only": True,
        "reasons": list(dict.fromkeys(reasons)) or ["no_full_text_obtained"],
        "landing_url": record.get("url") or (f"https://doi.org/{record['doi']}" if record.get("doi") else ""),
        "alternatives": deduped,
        "next_steps": [
            "Try listed OA/preprint/repository URLs first.",
            "If you have institutional access, run institutional-open, log in manually in your browser, download the PDF, and ingest it.",
            "If no legal copy is available, request author copy or interlibrary loan.",
        ],
    }


def record_access_targets(record: Dict[str, Any]) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []
    if record.get("doi"):
        targets.append({"source": "doi_landing_page", "url": f"https://doi.org/{record['doi']}"})
    for source, key in (("record_url", "url"), ("record_pdf_url", "pdf_url")):
        url = str(record.get(key) or "")
        if url and is_http_url(url):
            targets.append({"source": source, "url": url})
    if record.get("arxiv_id"):
        targets.append({"source": "arxiv", "url": f"https://arxiv.org/pdf/{record['arxiv_id']}"})
    if record.get("pmcid"):
        targets.append({"source": "pmc", "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{record['pmcid']}/"})
    deduped = []
    seen = set()
    for target in targets:
        url = target["url"]
        if url in seen:
            continue
        try:
            ensure_safe_open_url(url)
        except ValueError:
            continue
        deduped.append(target)
        seen.add(url)
    return deduped


def candidate_institutional_urls(record: Dict[str, Any], proxy_prefix: str = "") -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for target in record_access_targets(record):
        candidates.append({"source": target["source"], "url": target["url"], "mode": "direct"})
        if proxy_prefix and target["source"] in {"doi_landing_page", "record_url"}:
            try:
                candidates.append({"source": target["source"], "url": build_proxy_url(proxy_prefix, target["url"]), "mode": "library_proxy"})
            except ValueError:
                pass
    deduped = []
    seen = set()
    for candidate in candidates:
        key = candidate["url"]
        if key in seen:
            continue
        deduped.append(candidate)
        seen.add(key)
    return deduped


def pdf_snapshot(download_dir: Path) -> Dict[str, Tuple[int, int]]:
    if not download_dir.exists():
        return {}
    snapshot = {}
    for path in download_dir.glob("*.pdf"):
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.resolve())] = (int(stat.st_mtime_ns), int(stat.st_size))
    return snapshot


def wait_for_new_pdf(download_dir: Path, before: Dict[str, Tuple[int, int]], timeout: int = 600, settle_seconds: float = 2.0) -> Optional[Path]:
    deadline = time.time() + max(timeout, 1)
    last_seen: Dict[str, Tuple[int, int, float]] = {}
    while time.time() < deadline:
        for path in download_dir.glob("*.pdf"):
            try:
                resolved = str(path.resolve())
                stat = path.stat()
            except OSError:
                continue
            current = (int(stat.st_mtime_ns), int(stat.st_size))
            previous = before.get(resolved)
            if previous == current:
                continue
            seen_mtime, seen_size, first_seen = last_seen.get(resolved, (0, -1, time.time()))
            if seen_mtime == current[0] and seen_size == current[1] and time.time() - first_seen >= settle_seconds and current[1] > 1024:
                return path
            last_seen[resolved] = (current[0], current[1], first_seen if seen_size == current[1] else time.time())
        time.sleep(1)
    return None


def write_institutional_access_plan(
    record: Dict[str, Any],
    out_dir: Path,
    candidates: Sequence[Dict[str, str]],
    download_dir: Path,
    opened: Sequence[Dict[str, Any]],
    imported: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    downloads_dir = out_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_slug(record.get("title") or record.get("id") or "paper")
    json_path = downloads_dir / f"{slug}.institutional-access.json"
    md_path = downloads_dir / f"{slug}.institutional-access.md"
    ingest_command = (
        f'python scripts/paper_research_downloader.py ingest-pdf "<downloaded.pdf>" '
        f'--identifier "{record.get("doi") or record.get("arxiv_id") or record.get("pmid") or record.get("pmcid") or record.get("url") or record.get("id") or ""}"'
    )
    plan = {
        "status": "imported" if imported and imported.get("status") == "ok" else "waiting_for_user_download",
        "legal_only": True,
        "user_mediated": True,
        "credentials_handling": {
            "agent_enters_credentials": False,
            "agent_reads_cookies": False,
            "agent_stores_credentials": False,
            "safe_storage": "Use the user's browser or a local password manager only. Do not place usernames, passwords, cookies, or sessions in SKILL.md, config.local.json, git, or release assets.",
        },
        "record": {
            "id": record.get("id"),
            "title": record.get("title"),
            "doi": record.get("doi"),
            "arxiv_id": record.get("arxiv_id"),
            "pmid": record.get("pmid"),
            "pmcid": record.get("pmcid"),
            "url": record.get("url"),
        },
        "download_dir": str(download_dir.resolve()),
        "candidate_urls": list(candidates),
        "opened": list(opened),
        "manual_ingest_command": ingest_command,
        "imported": imported or {},
        "next_steps": [
            "Open one of the candidate URLs in your own browser.",
            "Log in through your institution only if you are authorized.",
            "Download the PDF manually into the watched download directory.",
            "Run ingest-pdf on the downloaded file if automatic ingest was not used.",
        ],
    }
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Institutional Access: {record.get('title') or record.get('id') or 'Paper'}",
        "",
        "User-mediated institutional access only. The agent must not enter credentials, read cookies, store sessions, or bypass publisher access controls.",
        "",
        "## Candidate URLs",
        "",
    ]
    for item in candidates:
        lines.append(f"- {item.get('mode')}: {item.get('source')} - {item.get('url')}")
    lines.extend(["", "## Manual Download", "", f"- Download directory: `{download_dir.resolve()}`", f"- Ingest command: `{ingest_command}`"])
    if imported:
        lines.extend(["", "## Imported PDF", "", f"- Status: {imported.get('status')}", f"- Path: {imported.get('pdf_path') or imported.get('download', {}).get('pdf_path') or ''}"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    plan["path"] = str(json_path.resolve())
    plan["markdown_path"] = str(md_path.resolve())
    return plan


def semantic_by_identifier(identifier: str, api_key: str = "") -> Dict[str, Any]:
    if not identifier:
        return {}
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    fields = "title,authors,year,venue,abstract,citationCount,externalIds,openAccessPdf,url,isOpenAccess"
    url = f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(identifier, safe=':')}"
    try:
        item = get_json(url, params={"fields": fields}, headers=headers, retries=1)
    except RuntimeError:
        return {}
    ext = item.get("externalIds") or {}
    return make_record(
        title=item.get("title") or "",
        authors=[a.get("name", "") for a in item.get("authors", []) if a.get("name")],
        year=item.get("year"),
        venue=item.get("venue") or "",
        doi=ext.get("DOI") or "",
        arxiv_id=ext.get("ArXiv") or "",
        pmid=ext.get("PubMed") or "",
        url=item.get("url") or "",
        pdf_url=(item.get("openAccessPdf") or {}).get("url") or "",
        abstract=item.get("abstract") or "",
        citation_count=item.get("citationCount"),
        sources=["semantic"],
    )


def resolve_identifier(identifier: str, email: str = "", s2_key: str = "", ncbi_key: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    doi = normalize_doi(identifier)
    arxiv_id = parse_arxiv_id(identifier)
    pmcid = parse_pmcid(identifier)
    records: List[Dict[str, Any]] = []
    tried: List[Dict[str, Any]] = []
    unpaywall: Dict[str, Any] = {}

    if arxiv_id:
        rec = arxiv_by_id(arxiv_id)
        if rec:
            records.append(rec)
            tried.append({"source": "arxiv", "status": "ok"})

    if doi:
        for name, func in (("crossref", crossref_by_doi), ("openalex", openalex_by_doi)):
            rec = func(doi, email)
            if rec:
                records.append(rec)
                tried.append({"source": name, "status": "ok"})
            else:
                tried.append({"source": name, "status": "miss"})
        pdf_url, unpaywall = unpaywall_pdf_url(doi, email)
        if pdf_url:
            records.append(make_record(doi=doi, pdf_url=pdf_url, sources=["unpaywall"]))
            tried.append({"source": "unpaywall", "status": "ok"})
        else:
            tried.append({"source": "unpaywall", "status": "miss_or_no_email"})
        rec = semantic_by_identifier(f"DOI:{doi}", s2_key)
        if rec:
            records.append(rec)
            tried.append({"source": "semantic", "status": "ok"})

    if not doi and arxiv_id:
        rec = semantic_by_identifier(f"ARXIV:{arxiv_id}", s2_key)
        if rec:
            records.append(rec)
            tried.append({"source": "semantic", "status": "ok"})

    if pmcid:
        rec = pmc_by_pmcid(pmcid, ncbi_key)
        if rec:
            records.append(rec)
            tried.append({"source": "pmc", "status": "ok"})

    if not doi and not arxiv_id and not pmcid and re.fullmatch(r"\d{5,9}", identifier.strip()):
        pmid = identifier.strip()
        rec = pubmed_by_pmid(pmid, ncbi_key)
        if rec:
            records.append(rec)
            tried.append({"source": "pubmed", "status": "ok"})
        rec = semantic_by_identifier(f"PMID:{pmid}", s2_key)
        if rec:
            records.append(rec)
            tried.append({"source": "semantic", "status": "ok"})
        if not rec:
            records.append(make_record(pmid=pmid, url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", sources=["pubmed"]))

    if not records and re.match(r"https?://", identifier):
        parsed_path = Path(urllib.parse.urlparse(identifier).path)
        records.append(make_record(title=parsed_path.name or identifier, url=identifier, pdf_url=identifier if parsed_path.suffix.lower() == ".pdf" else "", sources=["url"]))
        tried.append({"source": "url", "status": "direct"})

    if not records and doi:
        records.append(make_record(title=doi, doi=doi, url=f"https://doi.org/{doi}", sources=["doi_fallback"]))
        tried.append({"source": "doi_fallback", "status": "metadata_stub"})

    merged = merge_records(records)
    record = merged[0] if merged else make_record(title=identifier, url=identifier, sources=["manual"])
    manifest = {"identifier": identifier, "tried": tried, "unpaywall": bool(unpaywall)}
    return record, manifest


def is_probably_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]


def cache_key_for_record(record: Dict[str, Any]) -> str:
    key = record.get("id") or record_key(record)
    if not key:
        key = hashlib.sha1(json.dumps(record, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return safe_slug(key.replace(":", "-"), 80)


def cached_pdf_path(record: Dict[str, Any], cache_dir: Optional[Path]) -> Optional[Path]:
    if not cache_dir:
        return None
    item_dir = cache_dir / cache_key_for_record(record)
    manifest = item_dir / "cache.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    pdf_path = Path(data.get("pdf_path") or "")
    if pdf_path.exists():
        return pdf_path
    return None


def store_pdf_cache(record: Dict[str, Any], pdf_path: Path, cache_dir: Optional[Path], source: str, url: str) -> Optional[Path]:
    if not cache_dir or not pdf_path.exists():
        return None
    item_dir = cache_dir / cache_key_for_record(record)
    item_dir.mkdir(parents=True, exist_ok=True)
    cached = item_dir / pdf_path.name
    if pdf_path.resolve() != cached.resolve():
        shutil.copy2(pdf_path, cached)
    data = cached.read_bytes()
    manifest = {
        "id": record.get("id"),
        "title": record.get("title"),
        "source": source,
        "url": url,
        "pdf_path": str(cached.resolve()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (item_dir / "cache.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return cached


def fetch_pdf_candidate(source: str, url: str, retries: int = 2) -> Tuple[Optional[bytes], List[Dict[str, Any]]]:
    attempts = []
    for attempt in range(retries + 1):
        try:
            data = http_request(url, headers={"Accept": "application/pdf,*/*"}, timeout=60)
            if is_probably_pdf(data):
                attempts.append({"source": source, "url": url, "status": "ok", "attempt": attempt + 1, "bytes": len(data)})
                return data, attempts
            attempts.append({"source": source, "url": url, "status": "not_pdf", "attempt": attempt + 1, "bytes": len(data)})
            return None, attempts
        except urllib.error.HTTPError as exc:
            attempts.append({"source": source, "url": url, "status": "failed", "attempt": attempt + 1, "error": f"HTTP {exc.code}"})
            if exc.code == 429 and attempt < retries:
                time.sleep(max(8.0, 2.0 * (attempt + 1)))
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            return None, attempts
        except Exception as exc:  # noqa: BLE001 - record and continue source cascade
            attempts.append({"source": source, "url": url, "status": "failed", "attempt": attempt + 1, "error": str(exc)})
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None, attempts
    return None, attempts


def download_pdf(record: Dict[str, Any], out_dir: Path, email: str = "", s2_key: str = "", cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = cached_pdf_path(record, cache_dir)
    title = record.get("title") or record.get("doi") or record.get("arxiv_id") or "paper"
    pdf_path = out_dir / f"{safe_slug(title)}.pdf"
    if cached:
        if cached.resolve() != pdf_path.resolve():
            shutil.copy2(cached, pdf_path)
        data = pdf_path.read_bytes()
        return {
            "status": "ok",
            "source": "cache",
            "url": str(cached.resolve()),
            "pdf_path": str(pdf_path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "candidate_count": 0,
            "tried": [{"source": "cache", "url": str(cached.resolve()), "status": "ok"}],
        }
    candidates = []
    if record.get("pdf_url"):
        candidates.append(("record_pdf_url", record["pdf_url"]))
    if record.get("arxiv_id"):
        candidates.append(("arxiv", f"https://arxiv.org/pdf/{record['arxiv_id']}"))
    if record.get("pmcid"):
        candidates.append(("pmc", f"https://pmc.ncbi.nlm.nih.gov/articles/{record['pmcid']}/pdf/"))
    if record.get("doi"):
        pdf_url, _ = unpaywall_pdf_url(record["doi"], email)
        if pdf_url:
            candidates.append(("unpaywall", pdf_url))
        for loc in unpaywall_locations(record["doi"], email)[0]:
            if loc.get("url"):
                candidates.append((f"unpaywall_{loc.get('host_type') or loc.get('version') or 'oa'}", loc["url"]))
        s2 = semantic_by_identifier(f"DOI:{record['doi']}", s2_key)
        if s2.get("pdf_url"):
            candidates.append(("semantic", s2["pdf_url"]))
        ox = openalex_by_doi(record["doi"], email)
        if ox.get("pdf_url"):
            candidates.append(("openalex", ox["pdf_url"]))
    for loc in arxiv_title_locations(record):
        if loc.get("url"):
            candidates.append(("arxiv_title_match", loc["url"]))
    for loc in core_search_locations(record):
        if loc.get("url"):
            candidates.append(("core", loc["url"]))

    seen = set()
    unique_candidates = []
    for source, url in candidates:
        if url and url not in seen:
            unique_candidates.append((source, url))
            seen.add(url)

    tried = []
    for source, url in unique_candidates:
        data, attempts = fetch_pdf_candidate(source, url)
        tried.extend(attempts)
        if not data:
            continue
        pdf_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        store_pdf_cache(record, pdf_path, cache_dir, source, url)
        return {
            "status": "ok",
            "source": source,
            "url": url,
            "pdf_path": str(pdf_path.resolve()),
            "sha256": digest,
            "bytes": len(data),
            "candidate_count": len(unique_candidates),
            "tried": tried,
        }
    return {"status": "failed", "pdf_path": "", "candidate_count": len(unique_candidates), "tried": tried}


def parse_pdf_text(pdf_path: Path) -> Tuple[str, str]:
    try:
        import fitz  # type: ignore

        pages = []
        doc = fitz.open(str(pdf_path))
        for idx, page in enumerate(doc, start=1):
            pages.append(f"\n\n## Page {idx}\n\n{page.get_text('text')}")
        return "\n".join(pages), "pymupdf"
    except Exception:
        pass

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            pages.append(f"\n\n## Page {idx}\n\n{page.extract_text() or ''}")
        return "\n".join(pages), "pypdf"
    except Exception:
        pass

    exe = shutil.which("pdftotext")
    if exe:
        txt_path = pdf_path.with_suffix(".txt")
        subprocess.run([exe, "-layout", str(pdf_path), str(txt_path)], check=True, capture_output=True)
        return txt_path.read_text(encoding="utf-8", errors="replace"), "pdftotext"

    return "", "none"


def try_ocr_pdf(pdf_path: Path) -> Tuple[str, str]:
    ocrmypdf = shutil.which("ocrmypdf")
    if not ocrmypdf:
        return "", "none"
    ocr_pdf = pdf_path.with_suffix(".ocr.pdf")
    try:
        subprocess.run([ocrmypdf, "--skip-text", str(pdf_path), str(ocr_pdf)], check=True, capture_output=True, timeout=300)
        if ocr_pdf.exists():
            text, parser = parse_pdf_text(ocr_pdf)
            return text, f"ocrmypdf+{parser}"
    except Exception:
        return "", "none"
    return "", "none"


def extract_page_images(pdf_path: Path, max_pages: int = 3) -> List[Dict[str, Any]]:
    try:
        import fitz  # type: ignore
    except Exception:
        return []
    assets_dir = pdf_path.parent / f"{pdf_path.stem}_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    images: List[Dict[str, Any]] = []
    try:
        doc = fitz.open(str(pdf_path))
        for idx, page in enumerate(doc[:max_pages], start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            out = assets_dir / f"page-{idx:03d}.png"
            pix.save(str(out))
            images.append({"page": idx, "path": str(out.resolve()), "kind": "page-preview"})
    except Exception:
        return images
    return images


def write_parsed_markdown(pdf_path: Path, record: Dict[str, Any], ocr: bool = False, extract_figures: bool = False) -> Dict[str, Any]:
    text, parser = parse_pdf_text(pdf_path)
    clean_text = text.strip()
    ocr_status = "not_requested"
    if (not clean_text or len(re.findall(r"\b\w+\b", clean_text)) < 300) and ocr:
        ocr_status = "ocrmypdf_missing" if not shutil.which("ocrmypdf") else "attempted_failed"
        ocr_text, ocr_parser = try_ocr_pdf(pdf_path)
        if ocr_text.strip():
            text, parser = ocr_text, ocr_parser
            clean_text = text.strip()
            ocr_status = "ok"
    if not clean_text:
        return {"status": "unparsed", "parser": parser, "parsed_path": "", "ocr_requested": bool(ocr), "ocr_status": ocr_status, "read_plan": [], "sentinel": ""}
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    parsed_path = pdf_path.with_suffix(".parsed.md")
    header = [
        f"# {record.get('title') or pdf_path.stem}",
        "",
        f"- DOI: {record.get('doi') or ''}",
        f"- arXiv: {record.get('arxiv_id') or ''}",
        f"- Source PDF: {pdf_path.resolve()}",
        f"- Parser: {parser}",
        "",
    ]
    sentinel = f"END-OF-PAPER:{digest}"
    body = "\n".join(header) + text + f"\n\n<!-- {sentinel} -->\n"
    parsed_path.write_text(body, encoding="utf-8")
    lines = body.splitlines()
    pages = len(re.findall(r"(?m)^## Page \d+", body))
    words = len(re.findall(r"\b\w+\b", clean_text))
    quality = "good"
    if words < 300 or (pages and words / max(pages, 1) < 80):
        quality = "low"
    figures = extract_page_images(pdf_path) if extract_figures else []
    chunk_size = 450
    read_plan = []
    for offset in range(0, len(lines), chunk_size):
        read_plan.append({"path": str(parsed_path.resolve()), "offset": offset, "limit": chunk_size})
    return {
        "status": "ok",
        "parser": parser,
        "parsed_path": str(parsed_path.resolve()),
        "parsed_lines": len(lines),
        "parsed_chars": len(body),
        "pages": pages,
        "word_count": words,
        "quality": quality,
        "ocr_requested": bool(ocr),
        "ocr_status": ocr_status,
        "figures": figures,
        "read_plan": read_plan,
        "sentinel": sentinel,
    }


def process_record(
    record: Dict[str, Any],
    out_dir: Path,
    email: str = "",
    s2_key: str = "",
    ncbi_key: str = "",
    note_dir: Optional[Path] = None,
    tags: Optional[Sequence[str]] = None,
    local_pdf: Optional[Path] = None,
    download: bool = True,
    parse: bool = True,
    cache_dir: Optional[Path] = None,
    resume: bool = True,
    duplicate_policy: str = "skip",
    ocr: bool = False,
    extract_figures: bool = False,
) -> Dict[str, Any]:
    downloads_dir = out_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"id": record.get("id"), "title": record.get("title"), "created": dt.datetime.now().isoformat(timespec="seconds")}
    manifest_path = downloads_dir / f"{safe_slug(record.get('title') or record.get('id') or 'paper')}.manifest.json"
    if resume and manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_dl = existing.get("download") or {}
            existing_pdf = Path(existing_dl.get("pdf_path") or "")
            if existing_dl.get("status") == "ok" and existing_pdf.exists():
                record.update(existing.get("record") or {})
                existing_dl["resumed"] = True
                if note_dir:
                    existing_note = find_existing_note(record, note_dir)
                    note = write_obsidian_note(record, note_dir, pdf_path=record.get("pdf_path", ""), tags=tags, duplicate_policy=duplicate_policy)
                    existing_dl["note_path"] = str(note.resolve())
                    if existing_note:
                        existing_dl["note_duplicate"] = True
                return existing_dl
        except json.JSONDecodeError:
            pass
    if not download and not local_pdf:
        dl = {"status": "skipped", "reason": "metadata_only", "pdf_path": ""}
    elif local_pdf:
        if not local_pdf.exists():
            dl = {"status": "failed", "pdf_path": str(local_pdf), "error": "local_pdf_missing"}
        else:
            dest = downloads_dir / f"{safe_slug(record.get('title') or local_pdf.stem)}.pdf"
            if local_pdf.resolve() != dest.resolve():
                shutil.copy2(local_pdf, dest)
            data = dest.read_bytes()
            dl = {
                "status": "ok",
                "source": "local_pdf",
                "url": str(local_pdf.resolve()),
                "pdf_path": str(dest.resolve()),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "tried": [{"source": "local_pdf", "url": str(local_pdf.resolve()), "status": "ok"}],
            }
    else:
        dl = download_pdf(record, downloads_dir, email=email, s2_key=s2_key, cache_dir=cache_dir)

    record["download"] = dl
    if dl.get("status") == "ok":
        record["pdf_path"] = dl.get("pdf_path")
        record["evidence"] = "Full text"
        if parse:
            parsed = write_parsed_markdown(Path(dl["pdf_path"]), record, ocr=ocr, extract_figures=extract_figures)
            dl["parsed"] = parsed
        else:
            dl["parsed"] = {"status": "skipped", "reason": "no_parse"}
    elif parse and record.get("pmcid"):
        parsed = parse_pmc_xml_to_markdown(fetch_pmc_xml(record["pmcid"], ncbi_key), record, out_dir)
        if parsed.get("status") == "ok":
            dl["status"] = "ok"
            dl["source"] = "pmc_xml"
            dl["reason"] = "pdf_unavailable_xml_fulltext"
            dl["parsed"] = parsed
            record["evidence"] = "Full text"
        elif record.get("abstract"):
            dl["parsed"] = parsed
            record["evidence"] = "Abstract only"
        else:
            dl["parsed"] = parsed
            record["evidence"] = "Metadata only"
    elif record.get("abstract"):
        record["evidence"] = "Abstract only"
    else:
        record["evidence"] = "Metadata only"

    if note_dir:
        existing = find_existing_note(record, note_dir)
        note = write_obsidian_note(record, note_dir, pdf_path=record.get("pdf_path", ""), tags=tags, duplicate_policy=duplicate_policy)
        dl["note_path"] = str(note.resolve())
        if existing:
            dl["note_duplicate"] = True

    if record.get("evidence") != "Full text":
        dl["access_plan"] = write_access_plan(record, dl, out_dir, email=email)
        if dl.get("status") == "failed":
            dl["status"] = "needs_access"
        if note_dir and dl.get("note_path"):
            append_access_plan_to_note(Path(dl["note_path"]), dl["access_plan"])

    manifest.update({"record": record, "download": dl})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    dl["manifest_path"] = str(manifest_path.resolve())
    return dl


def bibtex_escape(value: Any) -> str:
    text = normalize_space(str(value or ""))
    text = text.replace("\\", "\\textbackslash{}")
    for char in "{}&%$#_":
        text = text.replace(char, "\\" + char)
    return text


def ris_escape(value: Any) -> str:
    return normalize_space(str(value or "")).replace("\n", " ")


def citation_key(record: Dict[str, Any]) -> str:
    first = "anon"
    authors = record.get("authors") or []
    if authors:
        first = re.sub(r"[^A-Za-z0-9]", "", authors[0].split()[-1]) or "anon"
    year = record.get("year") or "nd"
    word = "paper"
    title_words = re.findall(r"[A-Za-z][A-Za-z0-9]+", record.get("title") or "")
    if title_words:
        word = title_words[0]
    return f"{first}{year}{word}"


def write_exports(records: Sequence[Dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["id", "title", "authors", "year", "venue", "doi", "arxiv_id", "pmid", "pmcid", "url", "pdf_url", "citation_count", "relevance_score", "sources", "evidence"]
    with (out_dir / "papers.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            row = {field: rec.get(field, "") for field in fields}
            row["authors"] = "; ".join(rec.get("authors") or [])
            row["sources"] = "; ".join(rec.get("sources") or [])
            writer.writerow(row)

    used_keys: Dict[str, int] = {}
    entries = []
    for rec in records:
        key = citation_key(rec)
        count = used_keys.get(key, 0)
        used_keys[key] = count + 1
        if count:
            key = f"{key}{chr(ord('a') + count)}"
        entry_type = "misc" if rec.get("venue") == "arXiv" and not rec.get("doi") else "article"
        fields_bib = [
            f"  title = {{{bibtex_escape(rec.get('title'))}}}",
            f"  author = {{{bibtex_escape(' and '.join(rec.get('authors') or []))}}}",
            f"  year = {{{bibtex_escape(rec.get('year') or '')}}}",
        ]
        if rec.get("venue"):
            fields_bib.append(f"  journal = {{{bibtex_escape(rec.get('venue'))}}}")
        if rec.get("doi"):
            fields_bib.append(f"  doi = {{{bibtex_escape(rec.get('doi'))}}}")
        if rec.get("arxiv_id"):
            fields_bib.append(f"  eprint = {{{bibtex_escape(rec.get('arxiv_id'))}}}")
            fields_bib.append("  archivePrefix = {arXiv}")
        if rec.get("url"):
            fields_bib.append(f"  url = {{{bibtex_escape(rec.get('url'))}}}")
        entries.append(f"@{entry_type}{{{key},\n" + ",\n".join(fields_bib) + "\n}")
    (out_dir / "papers.bib").write_text("\n\n".join(entries) + "\n", encoding="utf-8")

    ris_lines: List[str] = []
    csl_items: List[Dict[str, Any]] = []
    for rec in records:
        ris_lines.append("TY  - JOUR" if rec.get("venue") and rec.get("venue") != "arXiv" else "TY  - GEN")
        for author in rec.get("authors") or []:
            ris_lines.append(f"AU  - {ris_escape(author)}")
        if rec.get("title"):
            ris_lines.append(f"TI  - {ris_escape(rec.get('title'))}")
        if rec.get("year"):
            ris_lines.append(f"PY  - {ris_escape(rec.get('year'))}")
        if rec.get("venue"):
            ris_lines.append(f"JO  - {ris_escape(rec.get('venue'))}")
        if rec.get("doi"):
            ris_lines.append(f"DO  - {ris_escape(rec.get('doi'))}")
        if rec.get("url"):
            ris_lines.append(f"UR  - {ris_escape(rec.get('url'))}")
        if rec.get("abstract"):
            ris_lines.append(f"AB  - {ris_escape(rec.get('abstract'))}")
        ris_lines.append("ER  -")
        ris_lines.append("")

        names = []
        for author in rec.get("authors") or []:
            parts = author.split()
            if len(parts) > 1:
                names.append({"family": parts[-1], "given": " ".join(parts[:-1])})
            elif parts:
                names.append({"literal": parts[0]})
        item: Dict[str, Any] = {
            "id": rec.get("id") or citation_key(rec),
            "type": "article-journal" if rec.get("venue") and rec.get("venue") != "arXiv" else "article",
            "title": rec.get("title") or "",
            "author": names,
        }
        if rec.get("year"):
            item["issued"] = {"date-parts": [[rec.get("year")]]}
        if rec.get("venue"):
            item["container-title"] = rec.get("venue")
        if rec.get("doi"):
            item["DOI"] = rec.get("doi")
        if rec.get("url"):
            item["URL"] = rec.get("url")
        if rec.get("abstract"):
            item["abstract"] = rec.get("abstract")
        csl_items.append(item)
    (out_dir / "papers.ris").write_text("\n".join(ris_lines), encoding="utf-8")
    (out_dir / "papers.csl.json").write_text(json.dumps(csl_items, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report(records: Sequence[Dict[str, Any]], out_dir: Path, title: str, manifest: Optional[Dict[str, Any]] = None) -> Path:
    total = len(records)
    full = sum(1 for r in records if r.get("evidence") == "Full text")
    abstract = sum(1 for r in records if r.get("evidence") == "Abstract only")
    metadata = sum(1 for r in records if r.get("evidence") == "Metadata only")
    needs_access = sum(1 for r in records if (r.get("download") or {}).get("access_plan"))
    failed = 0
    duplicates = 0
    for r in records:
        dl = r.get("download") or {}
        if dl.get("status") == "failed":
            failed += 1
        if dl.get("note_duplicate"):
            duplicates += 1
    if manifest and manifest.get("items"):
        failed += sum(1 for item in manifest.get("items") or [] if item.get("status") == "failed")
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Records | {total} |",
        f"| Full text | {full} |",
        f"| Abstract only | {abstract} |",
        f"| Metadata only | {metadata} |",
        f"| Needs access plan | {needs_access} |",
        f"| Failed downloads | {failed} |",
        f"| Existing Obsidian notes reused | {duplicates} |",
        "",
        "## Papers",
        "",
        "| # | Evidence | Year | Title | DOI/arXiv/PMID | PDF | Access plan | Note |",
        "|---:|---|---:|---|---|---|---|---|",
    ]
    for idx, rec in enumerate(records, start=1):
        ident = rec.get("doi") or rec.get("arxiv_id") or rec.get("pmid") or rec.get("pmcid") or ""
        dl = rec.get("download") or {}
        pdf = dl.get("pdf_path") or rec.get("pdf_path") or ""
        access = (dl.get("access_plan") or {}).get("markdown_path") or ""
        note = dl.get("note_path") or ""
        title_text = str(rec.get("title") or "").replace("|", "\\|")
        lines.append(f"| {idx} | {rec.get('evidence') or ''} | {rec.get('year') or ''} | {title_text} | {ident} | {pdf} | {access} | {note} |")
    if manifest and manifest.get("source_logs"):
        lines.extend(["", "## Source Logs", "", "| Source | Status | Count/Error |", "|---|---|---|"])
        for item in manifest.get("source_logs") or []:
            detail = item.get("count", item.get("error", ""))
            lines.append(f"| {item.get('source')} | {item.get('status')} | {detail} |")
    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_html_report(records: Sequence[Dict[str, Any]], out_dir: Path, title: str, manifest: Optional[Dict[str, Any]] = None) -> Path:
    total = len(records)
    full = sum(1 for r in records if r.get("evidence") == "Full text")
    abstract = sum(1 for r in records if r.get("evidence") == "Abstract only")
    metadata = sum(1 for r in records if r.get("evidence") == "Metadata only")
    needs_access = sum(1 for r in records if (r.get("download") or {}).get("access_plan"))
    failed = sum(1 for r in records if (r.get("download") or {}).get("status") == "failed")
    if manifest and manifest.get("items"):
        failed += sum(1 for item in manifest.get("items") or [] if item.get("status") == "failed")
    rows = []
    for idx, rec in enumerate(records, start=1):
        dl = rec.get("download") or {}
        parsed = dl.get("parsed") or {}
        ident = rec.get("doi") or rec.get("arxiv_id") or rec.get("pmid") or rec.get("pmcid") or ""
        pdf = dl.get("pdf_path") or rec.get("pdf_path") or ""
        note = dl.get("note_path") or ""
        parsed_path = parsed.get("parsed_path") or ""
        access_path = (dl.get("access_plan") or {}).get("markdown_path") or ""
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td><span class=\"badge {html.escape(str(rec.get('evidence') or 'Metadata only')).lower().replace(' ', '-')}\">{html.escape(str(rec.get('evidence') or ''))}</span></td>"
            f"<td>{html.escape(str(rec.get('year') or ''))}</td>"
            f"<td><strong>{html.escape(str(rec.get('title') or 'Untitled'))}</strong><div class=\"muted\">{html.escape(str(rec.get('venue') or ''))}</div></td>"
            f"<td>{html.escape(str(ident))}</td>"
            f"<td>{link_cell(pdf, 'PDF')}</td>"
            f"<td>{link_cell(parsed_path, 'Markdown')}</td>"
            f"<td>{link_cell(access_path, 'Plan')}</td>"
            f"<td>{link_cell(note, 'Note')}</td>"
            "</tr>"
        )
    source_logs = ""
    if manifest and manifest.get("source_logs"):
        items = []
        for item in manifest.get("source_logs") or []:
            detail = item.get("count", item.get("error", ""))
            items.append(
                "<tr>"
                f"<td>{html.escape(str(item.get('source') or ''))}</td>"
                f"<td>{html.escape(str(item.get('status') or ''))}</td>"
                f"<td>{html.escape(str(detail))}</td>"
                "</tr>"
            )
        source_logs = "<h2>Source Logs</h2><table><thead><tr><th>Source</th><th>Status</th><th>Count/Error</th></tr></thead><tbody>" + "".join(items) + "</tbody></table>"
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #182026; background: #f7f8fa; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 48px; }}
h1 {{ font-size: 28px; margin: 0 0 18px; }}
h2 {{ margin-top: 28px; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 22px; }}
.metric {{ background: white; border: 1px solid #d9e0e6; border-radius: 8px; padding: 14px; }}
.metric div:first-child {{ color: #52616b; font-size: 13px; }}
.metric div:last-child {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #d9e0e6; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5e9ee; text-align: left; vertical-align: top; font-size: 14px; }}
th {{ background: #edf2f5; color: #26343d; }}
.muted {{ color: #687782; font-size: 12px; margin-top: 3px; }}
.badge {{ display: inline-block; padding: 3px 7px; border-radius: 6px; background: #e5e9ee; white-space: nowrap; }}
.full-text {{ background: #d9f0df; color: #175c2b; }}
.abstract-only {{ background: #fff1cc; color: #6b4b00; }}
.metadata-only {{ background: #e7edf3; color: #33414b; }}
a {{ color: #0b63a8; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<section class="metrics">
<div class="metric"><div>Records</div><div>{total}</div></div>
<div class="metric"><div>Full text</div><div>{full}</div></div>
<div class="metric"><div>Abstract only</div><div>{abstract}</div></div>
<div class="metric"><div>Metadata only</div><div>{metadata}</div></div>
<div class="metric"><div>Needs access</div><div>{needs_access}</div></div>
<div class="metric"><div>Failed items</div><div>{failed}</div></div>
</section>
<h2>Papers</h2>
<table><thead><tr><th>#</th><th>Evidence</th><th>Year</th><th>Title</th><th>Identifier</th><th>PDF</th><th>Parsed</th><th>Access</th><th>Note</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
{source_logs}
</main>
</body>
</html>
"""
    report_path = out_dir / "report.html"
    report_path.write_text(doc, encoding="utf-8")
    return report_path


def link_cell(path: str, label: str) -> str:
    if not path:
        return ""
    href = Path(path).as_uri() if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("/") else path
    return f"<a href=\"{html.escape(href)}\">{html.escape(label)}</a>"


def zotero_selectors(record: Dict[str, Any]) -> List[Tuple[str, str]]:
    selectors = []
    if record.get("doi"):
        selectors.append(("DOI", str(record["doi"])))
    if record.get("arxiv_id"):
        selectors.append(("arXiv", str(record["arxiv_id"])))
    if record.get("pmid"):
        selectors.append(("PMID", str(record["pmid"])))
    title = normalize_space(record.get("title"))
    if title:
        selectors.append(("title", title))
    return selectors


def query_zotero_item(api_base: str, field: str, value: str) -> List[Dict[str, Any]]:
    if not api_base or not value:
        return []
    q = value if field == "title" else f"{field}:{value}"
    url = api_base.rstrip("/") + "/items"
    params = {"q": q, "limit": 5, "format": "json"}
    try:
        data = get_json(url, params=params, retries=0)
    except RuntimeError:
        return []
    return data if isinstance(data, list) else []


def write_zotero_report(records: Sequence[Dict[str, Any]], out_dir: Path, api_base: str) -> Path:
    rows = []
    for rec in records:
        matches = []
        for field, value in zotero_selectors(rec):
            found = query_zotero_item(api_base, field, value)
            if found:
                for item in found:
                    data = item.get("data") or {}
                    matches.append(
                        {
                            "selector": field,
                            "value": value,
                            "key": item.get("key") or data.get("key") or "",
                            "title": data.get("title") or "",
                            "doi": normalize_doi(data.get("DOI") or data.get("doi") or ""),
                            "url": data.get("url") or item.get("links", {}).get("alternate", {}).get("href", ""),
                        }
                    )
                break
        rows.append({"record_id": rec.get("id"), "title": rec.get("title"), "matches": matches})
    path = out_dir / "zotero_matches.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def yaml_list(items: Sequence[str], indent: int = 2) -> str:
    pad = " " * indent
    if not items:
        return f"{pad}[]"
    return "\n".join(f"{pad}- {json.dumps(item, ensure_ascii=False)}" for item in items)


def parse_simple_frontmatter(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip().splitlines()
    data: Dict[str, Any] = {}
    current_key = ""
    for raw in block:
        line = raw.rstrip()
        if not line:
            continue
        if re.match(r"^\s+-\s+", line) and current_key:
            data.setdefault(current_key, []).append(line.split("-", 1)[1].strip().strip('"'))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            current_key = key.strip()
            value = value.strip()
            if not value:
                data[current_key] = []
            else:
                data[current_key] = value.strip('"')
    return data


def note_identity(record: Dict[str, Any]) -> List[str]:
    keys = []
    for field in ("doi", "arxiv_id", "pmid", "pmcid"):
        value = str(record.get(field) or "").strip().lower()
        if value:
            keys.append(f"{field}:{value}")
    title = normalize_space(record.get("title")).lower()
    if title:
        keys.append(f"title:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:16]}")
    return keys


def build_note_index(note_dir: Path) -> Dict[str, Path]:
    index: Dict[str, Path] = {}
    if not note_dir.exists():
        return index
    for path in note_dir.rglob("*.md"):
        fm = parse_simple_frontmatter(path)
        if not fm:
            continue
        pseudo = make_record(
            title=fm.get("title") or path.stem,
            doi=fm.get("doi") or "",
            arxiv_id=fm.get("arxiv") or fm.get("arxiv_id") or "",
            pmid=fm.get("pmid") or "",
            pmcid=fm.get("pmcid") or "",
        )
        for key in note_identity(pseudo):
            index.setdefault(key, path)
    return index


def find_existing_note(record: Dict[str, Any], note_dir: Optional[Path]) -> Optional[Path]:
    if not note_dir:
        return None
    index = build_note_index(note_dir)
    for key in note_identity(record):
        if key in index:
            return index[key]
    return None


def write_obsidian_note(
    record: Dict[str, Any],
    note_dir: Path,
    pdf_path: str = "",
    tags: Optional[Sequence[str]] = None,
    duplicate_policy: str = "skip",
) -> Path:
    note_dir.mkdir(parents=True, exist_ok=True)
    existing = find_existing_note(record, note_dir)
    if existing and duplicate_policy == "skip":
        return existing
    tags = list(tags or ["paper"])
    title = record.get("title") or record.get("id") or "Untitled paper"
    note_path = note_dir / f"{safe_slug(title)}.md"
    authors = record.get("authors") or []
    abstract = record.get("abstract") or ""
    frontmatter = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "authors:",
        yaml_list(authors, 2),
        f"year: {json.dumps(record.get('year'), ensure_ascii=False)}",
        f"venue: {json.dumps(record.get('venue') or '', ensure_ascii=False)}",
        f"doi: {json.dumps(record.get('doi') or '', ensure_ascii=False)}",
        f"arxiv: {json.dumps(record.get('arxiv_id') or '', ensure_ascii=False)}",
        f"pmid: {json.dumps(record.get('pmid') or '', ensure_ascii=False)}",
        f"pmcid: {json.dumps(record.get('pmcid') or '', ensure_ascii=False)}",
        f"url: {json.dumps(record.get('url') or '', ensure_ascii=False)}",
        f"pdf_path: {json.dumps(pdf_path, ensure_ascii=False)}",
        "tags:",
        yaml_list(tags, 2),
        'status: "unread"',
        f"created: {json.dumps(today_iso())}",
        "---",
        "",
    ]
    body = [
        f"# {title}",
        "",
        "## Metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Authors | {', '.join(authors)} |",
        f"| Year | {record.get('year') or ''} |",
        f"| Venue | {record.get('venue') or ''} |",
        f"| DOI | {record.get('doi') or ''} |",
        f"| arXiv | {record.get('arxiv_id') or ''} |",
        f"| PDF | {pdf_path} |",
        "",
        "## Abstract",
        "",
        abstract or "_No abstract captured._",
        "",
        "## Core Links",
        "",
        "- Related concepts: [[]]",
        "- Related topics: [[]]",
        "- Project links: [[trajectory optimization]], [[optimal control]]",
        "",
        "## Reading Checklist",
        "",
        "- [ ] Problem",
        "- [ ] Method",
        "- [ ] Results",
        "- [ ] Limitations",
        "- [ ] Reusable equations / algorithms",
        "",
        "## Notes",
        "",
    ]
    note_path.write_text("\n".join(frontmatter + body), encoding="utf-8")
    return note_path


def append_access_plan_to_note(note_path: Path, access_plan: Dict[str, Any]) -> None:
    if not note_path.exists():
        return
    text = note_path.read_text(encoding="utf-8", errors="replace")
    marker = "## Access Plan"
    if marker in text:
        return
    lines = [
        "",
        marker,
        "",
        f"- Status: {access_plan.get('status') or 'needs_access'}",
        f"- Plan JSON: {access_plan.get('path') or ''}",
        f"- Plan Markdown: {access_plan.get('markdown_path') or ''}",
        "- Legal boundary: use OA/preprint/repository/institutional/manual user PDF routes only.",
        "",
    ]
    for item in (access_plan.get("alternatives") or [])[:8]:
        source = item.get("source") or item.get("action") or "source"
        url = item.get("url") or ""
        action = item.get("action") or ""
        if url:
            lines.append(f"- {source}: {url}")
        elif action:
            lines.append(f"- {source}: {action}")
    note_path.write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


def read_identifier_file(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        identifiers: List[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    identifiers.append(item)
                elif isinstance(item, dict):
                    for key in ("doi", "arxiv_id", "pmid", "pmcid", "url", "identifier", "id"):
                        if item.get(key):
                            identifiers.append(str(item[key]))
                            break
        elif isinstance(data, dict):
            for item in data.get("papers", data.get("results", [])):
                if isinstance(item, dict):
                    for key in ("doi", "arxiv_id", "pmid", "pmcid", "url", "identifier", "id"):
                        if item.get(key):
                            identifiers.append(str(item[key]))
                            break
        return list(dict.fromkeys(i.strip() for i in identifiers if i.strip()))

    if path.suffix.lower() == ".csv":
        identifiers = []
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                for key in ("doi", "arxiv_id", "pmid", "pmcid", "url", "identifier", "id"):
                    if row.get(key):
                        identifiers.append(str(row[key]))
                        break
        return list(dict.fromkeys(i.strip() for i in identifiers if i.strip()))

    identifiers = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        identifiers.append(line.split(",", 1)[0].strip())
    return list(dict.fromkeys(i for i in identifiers if i))


def note_dir_from_args(args: argparse.Namespace, out_dir: Path) -> Optional[Path]:
    if getattr(args, "vault", ""):
        return Path(args.vault) / args.note_folder
    if getattr(args, "write_notes", False):
        return out_dir / "notes"
    return None


def apply_config_defaults(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config(getattr(args, "config", "") or None)
    if config.get("request_delay") and not os.getenv("PRD_REQUEST_DELAY"):
        os.environ["PRD_REQUEST_DELAY"] = str(config.get("request_delay") or 0)
    if hasattr(args, "email") and not getattr(args, "email", ""):
        args.email = config.get("email") or os.getenv("UNPAYWALL_EMAIL") or os.getenv("OPENALEX_EMAIL") or ""
    if hasattr(args, "vault") and not getattr(args, "vault", ""):
        args.vault = config.get("vault") or ""
    if hasattr(args, "note_folder") and getattr(args, "note_folder", "02_literature") == "02_literature":
        args.note_folder = config.get("note_folder") or "02_literature"
    if hasattr(args, "sources") and not getattr(args, "sources", ""):
        args.sources = config.get("sources") or DEFAULT_CONFIG["sources"]
    if hasattr(args, "s2_key") and not getattr(args, "s2_key", ""):
        args.s2_key = config.get("s2_key") or os.getenv("S2_API_KEY") or ""
    if hasattr(args, "ncbi_key") and not getattr(args, "ncbi_key", ""):
        args.ncbi_key = config.get("ncbi_key") or os.getenv("NCBI_API_KEY") or ""
    if hasattr(args, "tag") and getattr(args, "tag", None) == ["paper"]:
        tags = config.get("default_tags")
        if isinstance(tags, list) and tags:
            args.tag = [str(t) for t in tags]
    if hasattr(args, "resume") and getattr(args, "resume", None) is None:
        args.resume = bool(config.get("resume", True))
    if hasattr(args, "zotero_api") and not getattr(args, "zotero_api", ""):
        args.zotero_api = config.get("zotero_local_api") or DEFAULT_CONFIG["zotero_local_api"]
    if hasattr(args, "proxy_prefix") and not getattr(args, "proxy_prefix", ""):
        args.proxy_prefix = config.get("institutional_proxy_prefix") or ""
    if hasattr(args, "download_dir") and not getattr(args, "download_dir", ""):
        args.download_dir = config.get("download_dir") or str(Path.home() / "Downloads")
    return config


def sort_records(records: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    if mode == "year":
        return sorted(records, key=lambda r: (r.get("relevance_score") or 0, r.get("year") or 0, r.get("citation_count") or 0), reverse=True)
    if mode == "relevance":
        return sorted(
            records,
            key=lambda r: (
                r.get("relevance_score") or 0,
                len(r.get("sources") or []),
                -(r.get("source_rank") or 10_000),
                r.get("citation_count") or 0,
            ),
            reverse=True,
        )
    return sorted(
        records,
        key=lambda r: (
            r.get("relevance_score") or 0,
            r.get("citation_count") if r.get("citation_count") is not None else -1,
            r.get("year") or 0,
        ),
        reverse=True,
    )


def run_search(args: argparse.Namespace) -> int:
    config = apply_config_defaults(args)
    out_dir = Path(args.out) if args.out else default_output_dir(config, "search", args.query)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = default_cache_dir(config)
    email = args.email or ""
    s2_key = args.s2_key or ""
    ncbi_key = args.ncbi_key or ""
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    manifest: Dict[str, Any] = {
        "version": VERSION,
        "query": args.query,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "sources_requested": sources,
        "source_logs": [],
        "download_logs": [],
    }

    all_records: List[Dict[str, Any]] = []
    source_funcs = {
        "openalex": lambda: search_openalex(args.query, args.limit, args.year_min, email),
        "semantic": lambda: search_semantic(args.query, args.limit, args.year_min, s2_key),
        "crossref": lambda: search_crossref(args.query, args.limit, args.year_min, email),
        "arxiv": lambda: search_arxiv(args.query, args.limit, args.year_min),
        "pubmed": lambda: search_pubmed(args.query, args.limit, args.year_min, ncbi_key),
    }
    for source in sources:
        func = source_funcs.get(source)
        if not func:
            manifest["source_logs"].append({"source": source, "status": "unknown"})
            continue
        try:
            records = func()
            all_records.extend(records)
            manifest["source_logs"].append({"source": source, "status": "ok", "count": len(records)})
        except Exception as exc:  # noqa: BLE001 - keep partial source results
            manifest["source_logs"].append({"source": source, "status": "failed", "error": str(exc)})

    records = merge_records(all_records)
    for rec in records:
        rec["relevance_score"] = score_relevance(rec, args.query)
    records = sort_records(records, args.sort)[: args.limit]
    write_exports(records, out_dir)

    note_dir = note_dir_from_args(args, out_dir)

    if args.download_top:
        for idx, rec in enumerate(records[: args.download_top], start=1):
            dl = process_record(
                rec,
                out_dir,
                email=email,
                s2_key=s2_key,
                ncbi_key=ncbi_key,
                note_dir=note_dir,
                tags=args.tag,
                parse=not args.no_parse,
                cache_dir=cache_dir,
                resume=args.resume,
                duplicate_policy=config.get("duplicate_policy", "skip"),
                ocr=args.ocr,
                extract_figures=args.extract_figures,
            )
            manifest["download_logs"].append({"rank": idx, "id": rec.get("id"), "title": rec.get("title"), **dl})

    write_exports(records, out_dir)
    if getattr(args, "zotero_check", False):
        manifest["zotero_matches"] = str(write_zotero_report(records, out_dir, args.zotero_api).resolve())
    report = write_report(records, out_dir, f"Paper Search: {args.query}", manifest)
    html_report = write_html_report(records, out_dir, f"Paper Search: {args.query}", manifest)
    manifest["report"] = str(report.resolve())
    manifest["report_html"] = str(html_report.resolve())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json({"status": "ok", "out_dir": str(out_dir.resolve()), "records": len(records), "manifest": str((out_dir / "run_manifest.json").resolve())})
    return 0


def run_resolve(args: argparse.Namespace) -> int:
    config = apply_config_defaults(args)
    out_dir = Path(args.out) if args.out else default_output_dir(config, "resolve", args.identifier)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = default_cache_dir(config)
    email = args.email or ""
    s2_key = args.s2_key or ""
    ncbi_key = args.ncbi_key or ""
    record, manifest = resolve_identifier(args.identifier, email=email, s2_key=s2_key, ncbi_key=ncbi_key)
    note_dir = note_dir_from_args(args, out_dir)
    dl = process_record(
        record,
        out_dir,
        email=email,
        s2_key=s2_key,
        ncbi_key=ncbi_key,
        note_dir=note_dir,
        tags=args.tag,
        download=not args.metadata_only,
        parse=not args.no_parse,
        cache_dir=cache_dir,
        resume=args.resume,
        duplicate_policy=config.get("duplicate_policy", "skip"),
        ocr=args.ocr,
        extract_figures=args.extract_figures,
    )
    manifest["download"] = dl
    note_path = dl.get("note_path", "")

    write_exports([record], out_dir)
    if getattr(args, "zotero_check", False):
        manifest["zotero_matches"] = str(write_zotero_report([record], out_dir, args.zotero_api).resolve())
    report = write_report([record], out_dir, f"Paper Resolve: {args.identifier}", manifest)
    html_report = write_html_report([record], out_dir, f"Paper Resolve: {args.identifier}", manifest)
    manifest["report"] = str(report.resolve())
    manifest["report_html"] = str(html_report.resolve())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json({"status": "ok", "out_dir": str(out_dir.resolve()), "record": record, "download": dl, "note_path": note_path})
    return 0


def run_batch(args: argparse.Namespace) -> int:
    config = apply_config_defaults(args)
    out_dir = Path(args.out) if args.out else default_output_dir(config, "batch", Path(args.input).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = default_cache_dir(config)
    email = args.email or ""
    s2_key = args.s2_key or ""
    ncbi_key = args.ncbi_key or ""
    identifiers = read_identifier_file(Path(args.input))
    if args.limit:
        identifiers = identifiers[: args.limit]
    delay = args.delay if args.delay is not None else float(config.get("batch_delay") or 0)
    note_dir = note_dir_from_args(args, out_dir)
    records: List[Dict[str, Any]] = []
    manifest: Dict[str, Any] = {
        "version": VERSION,
        "input": str(Path(args.input).resolve()),
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(identifiers),
        "items": [],
    }
    seen = set()
    for idx, identifier in enumerate(identifiers, start=1):
        try:
            record, item_manifest = resolve_identifier(identifier, email=email, s2_key=s2_key, ncbi_key=ncbi_key)
            key = record.get("id") or identifier
            if key in seen:
                manifest["items"].append({"rank": idx, "identifier": identifier, "status": "duplicate", "id": key})
                continue
            seen.add(key)
            dl = process_record(
                record,
                out_dir,
                email=email,
                s2_key=s2_key,
                ncbi_key=ncbi_key,
                note_dir=note_dir,
                tags=args.tag,
                download=not args.metadata_only,
                parse=not args.no_parse,
                cache_dir=cache_dir,
                resume=args.resume,
                duplicate_policy=config.get("duplicate_policy", "skip"),
                ocr=args.ocr,
                extract_figures=args.extract_figures,
            )
            item_manifest.update({"rank": idx, "id": key, "download": dl})
            manifest["items"].append(item_manifest)
            records.append(record)
        except Exception as exc:  # noqa: BLE001 - keep long batches resumable
            manifest["items"].append({"rank": idx, "identifier": identifier, "status": "failed", "error": str(exc)})
        finally:
            if delay > 0 and idx < len(identifiers):
                time.sleep(delay)
    write_exports(records, out_dir)
    if getattr(args, "zotero_check", False):
        manifest["zotero_matches"] = str(write_zotero_report(records, out_dir, args.zotero_api).resolve())
    report = write_report(records, out_dir, f"Paper Batch: {Path(args.input).name}", manifest)
    html_report = write_html_report(records, out_dir, f"Paper Batch: {Path(args.input).name}", manifest)
    manifest["report"] = str(report.resolve())
    manifest["report_html"] = str(html_report.resolve())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json({"status": "ok", "out_dir": str(out_dir.resolve()), "records": len(records), "manifest": str((out_dir / "run_manifest.json").resolve())})
    return 0


def run_ingest_pdf(args: argparse.Namespace) -> int:
    config = apply_config_defaults(args)
    pdf_path = Path(args.pdf)
    out_dir = Path(args.out) if args.out else default_output_dir(config, "ingest_pdf", pdf_path.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    identifier = args.identifier or args.doi or args.arxiv or ""
    cache_dir = default_cache_dir(config)
    email = args.email or ""
    s2_key = args.s2_key or ""
    ncbi_key = args.ncbi_key or ""
    if identifier:
        record, manifest = resolve_identifier(identifier, email=email, s2_key=s2_key, ncbi_key=ncbi_key)
    else:
        record = make_record(
            title=args.title or pdf_path.stem,
            authors=[a.strip() for a in args.author.split(";") if a.strip()] if args.author else [],
            year=args.year,
            doi=args.doi,
            arxiv_id=args.arxiv,
            url=str(pdf_path.resolve()),
            sources=["local_pdf"],
        )
        manifest = {"identifier": identifier or str(pdf_path.resolve()), "tried": [{"source": "local_pdf", "status": "metadata"}]}
    if args.title:
        record["title"] = args.title
    if args.author:
        record["authors"] = [a.strip() for a in args.author.split(";") if a.strip()]
    if args.year:
        record["year"] = args.year
    note_dir = note_dir_from_args(args, out_dir)
    dl = process_record(
        record,
        out_dir,
        email=email,
        s2_key=s2_key,
        ncbi_key=ncbi_key,
        note_dir=note_dir,
        tags=args.tag,
        local_pdf=pdf_path,
        parse=not args.no_parse,
        cache_dir=cache_dir,
        resume=args.resume,
        duplicate_policy=config.get("duplicate_policy", "skip"),
        ocr=args.ocr,
        extract_figures=args.extract_figures,
    )
    manifest["download"] = dl
    write_exports([record], out_dir)
    if getattr(args, "zotero_check", False):
        manifest["zotero_matches"] = str(write_zotero_report([record], out_dir, args.zotero_api).resolve())
    report = write_report([record], out_dir, f"Paper Ingest: {pdf_path.name}", manifest)
    html_report = write_html_report([record], out_dir, f"Paper Ingest: {pdf_path.name}", manifest)
    manifest["report"] = str(report.resolve())
    manifest["report_html"] = str(html_report.resolve())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json({"status": "ok", "out_dir": str(out_dir.resolve()), "record": record, "download": dl})
    return 0


def run_institutional_open(args: argparse.Namespace) -> int:
    config = apply_config_defaults(args)
    out_dir = Path(args.out) if args.out else default_output_dir(config, "institutional", args.identifier)
    out_dir.mkdir(parents=True, exist_ok=True)
    download_dir = Path(args.download_dir).expanduser() if args.download_dir else default_download_dir(config)
    download_dir.mkdir(parents=True, exist_ok=True)
    email = args.email or ""
    s2_key = args.s2_key or ""
    ncbi_key = args.ncbi_key or ""
    record, manifest = resolve_identifier(args.identifier, email=email, s2_key=s2_key, ncbi_key=ncbi_key)
    candidates = candidate_institutional_urls(record, proxy_prefix=args.proxy_prefix)
    if not candidates:
        candidates = [{"source": "manual", "url": args.identifier, "mode": "direct"}] if is_http_url(args.identifier) else []

    opened: List[Dict[str, Any]] = []
    before = pdf_snapshot(download_dir)
    for candidate in candidates[: max(args.open_limit, 0)]:
        url = candidate.get("url") or ""
        item = dict(candidate)
        if args.no_open:
            item["opened"] = False
            item["reason"] = "no_open"
        else:
            try:
                item["opened"] = bool(open_url_in_browser(url))
            except Exception as exc:  # noqa: BLE001 - browser opening is best effort
                item["opened"] = False
                item["error"] = str(exc)
        opened.append(item)

    imported: Optional[Dict[str, Any]] = None
    if args.wait_for_pdf:
        found = wait_for_new_pdf(download_dir, before, timeout=args.timeout)
        if found and args.ingest_downloaded:
            ingest_args = argparse.Namespace(
                config=args.config,
                pdf=str(found),
                identifier=args.identifier,
                doi="",
                arxiv="",
                title=args.title or "",
                author="",
                year=None,
                email=args.email,
                s2_key=args.s2_key,
                ncbi_key=args.ncbi_key,
                out=str(out_dir),
                resume=args.resume,
                no_parse=args.no_parse,
                ocr=args.ocr,
                extract_figures=args.extract_figures,
                vault=args.vault,
                note_folder=args.note_folder,
                write_notes=args.write_notes,
                tag=args.tag,
                zotero_check=args.zotero_check,
                zotero_api=args.zotero_api,
            )
            rc = run_ingest_pdf(ingest_args)
            imported = {"status": "ok" if rc == 0 else "failed", "source_pdf": str(found.resolve()), "return_code": rc}
        elif found:
            imported = {"status": "downloaded_not_ingested", "source_pdf": str(found.resolve())}
        else:
            imported = {"status": "timeout", "timeout": args.timeout}

    access_plan = write_institutional_access_plan(record, out_dir, candidates, download_dir, opened, imported=imported)
    manifest.update(
        {
            "version": VERSION,
            "created": iso_now(),
            "institutional_access": access_plan,
            "credential_policy": "user_browser_only_no_agent_storage_no_cookies_no_passwords",
        }
    )
    write_exports([record], out_dir)
    report = write_report([record], out_dir, f"Institutional Access: {args.identifier}", manifest)
    html_report = write_html_report([record], out_dir, f"Institutional Access: {args.identifier}", manifest)
    manifest["report"] = str(report.resolve())
    manifest["report_html"] = str(html_report.resolve())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json(
        {
            "status": "ok",
            "out_dir": str(out_dir.resolve()),
            "record": record,
            "candidate_urls": candidates,
            "opened": opened,
            "institutional_access": access_plan,
        }
    )
    return 0


def run_config_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else skill_config_path()
    write_default_config(path, overwrite=args.force)
    print_json({"status": "ok", "config_path": str(path.resolve()), "exists": path.exists()})
    return 0


def probe_python_module(name: str) -> Dict[str, Any]:
    try:
        __import__(name)
        return {"name": name, "available": True}
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"name": name, "available": False, "error": str(exc)}


def probe_executable(name: str) -> Dict[str, Any]:
    path = shutil.which(name)
    return {"name": name, "available": bool(path), "path": path or ""}


def probe_zotero(api_base: str) -> Dict[str, Any]:
    try:
        data = get_json(api_base.rstrip("/") + "/items", params={"limit": 1, "format": "json"}, retries=0, timeout=5)
        return {"available": isinstance(data, list), "api_base": api_base}
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"available": False, "api_base": api_base, "error": str(exc)}


def run_check_env(args: argparse.Namespace) -> int:
    config = load_config(getattr(args, "config", "") or None)
    api_base = args.zotero_api or config.get("zotero_local_api") or DEFAULT_CONFIG["zotero_local_api"]
    report = {
        "version": VERSION,
        "created": iso_now(),
        "python": sys.version.split()[0],
        "modules": [probe_python_module(name) for name in ("fitz", "pypdf")],
        "executables": [probe_executable(name) for name in ("pdftotext", "ocrmypdf")],
        "zotero": probe_zotero(api_base) if args.zotero else {"available": None, "api_base": api_base, "skipped": True},
        "environment": {
            "UNPAYWALL_EMAIL": bool(os.getenv("UNPAYWALL_EMAIL") or config.get("email")),
            "OPENALEX_EMAIL": bool(os.getenv("OPENALEX_EMAIL") or config.get("email")),
            "S2_API_KEY": bool(os.getenv("S2_API_KEY") or config.get("s2_key")),
            "NCBI_API_KEY": bool(os.getenv("NCBI_API_KEY") or config.get("ncbi_key")),
        },
    }
    print_json(report)
    return 0


def scan_private_data(root: Path) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    skip_parts = {"__pycache__", ".pytest_cache", "dist"}
    text_suffixes = {".md", ".py", ".yaml", ".yml", ".json", ".txt", ".html", ".css", ".js", ".toml", ".ini", ".cfg"}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if set(rel.parts) & skip_parts:
            continue
        if path.name in PRIVATE_ARTIFACT_NAMES:
            continue
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                continue
        if PRIVATE_URL_RE.search(text):
            findings.append({"path": str(rel), "kind": "url_embedded_credentials"})
        for line_no, line in enumerate(text.splitlines(), start=1):
            for match in PRIVATE_VALUE_RE.finditer(line):
                value = (match.group(2) or "").strip().strip("'\"")
                if value.lower() in PLACEHOLDER_VALUES or value.startswith("<") or value.startswith("$"):
                    continue
                findings.append({"path": str(rel), "line": str(line_no), "kind": "private_value", "key": match.group(1)})
    return findings


def run_package(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir or SKILL_DIR / "dist").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = scan_private_data(SKILL_DIR)
    if findings and not getattr(args, "allow_private_findings", False):
        print_json({"status": "blocked", "reason": "private_data_scan_failed", "findings": findings})
        return 2
    zip_path = out_dir / f"paper-research-downloader-v{VERSION}.zip"
    exclude_names = {"config.local.json", "__pycache__", ".pytest_cache", "dist"}
    exclude_suffixes = {".pyc", ".pyo"}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in SKILL_DIR.rglob("*"):
            rel = path.relative_to(SKILL_DIR)
            parts = set(rel.parts)
            if parts & exclude_names:
                continue
            if path.suffix in exclude_suffixes:
                continue
            if path.is_file():
                zf.write(path, Path("paper-research-downloader") / rel)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path, "r") as zf:
        files = sorted(zf.namelist())
    release = {
        "name": "paper-research-downloader",
        "version": VERSION,
        "zip_path": str(zip_path),
        "sha256": digest,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "file_count": len(files),
        "files": files,
        "excluded": sorted(exclude_names),
        "private_scan": {"status": "ok", "findings": []},
    }
    release_json = out_dir / f"paper-research-downloader-v{VERSION}.release.json"
    release_json.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
    release_notes = out_dir / f"paper-research-downloader-v{VERSION}.release-notes.md"
    release_notes.write_text(
        "\n".join(
            [
                f"# paper-research-downloader v{VERSION}",
                "",
                "- Added institutional-open for user-mediated institutional access through the user's own browser.",
                "- Added library proxy URL construction, download-folder watching, and optional user-downloaded PDF ingest.",
                "- Added institutional access Markdown/JSON plans with explicit no-credential/no-cookie handling.",
                "- Added package-time private data scanning before release artifacts are created.",
                "- Preserved OA-first, paywall-aware legal access plans and Obsidian ingestion.",
                "",
                f"SHA256: `{digest}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print_json({"status": "ok", "zip_path": str(zip_path), "version": VERSION, "sha256": digest, "release_json": str(release_json)})
    return 0


def run_regression_tests(_args: Optional[argparse.Namespace] = None) -> int:
    checks: List[str] = []
    with tempfile.TemporaryDirectory(prefix="prd_tests_") as tmp_name:
        tmp = Path(tmp_name)

        merged = merge_records(
            [
                make_record(title="Shared Trajectory Paper", year=2025, arxiv_id="2501.01234", sources=["arxiv"]),
                make_record(title="Shared Trajectory Paper", year=2025, doi="10.1234/shared", sources=["crossref"]),
            ]
        )
        assert len(merged) == 1 and merged[0]["doi"] == "10.1234/shared" and merged[0]["arxiv_id"] == "2501.01234"
        checks.append("doi_arxiv_title_year_merge")

        sample = make_record(
            title="A Test Paper: Low-Thrust Trajectory Optimization",
            authors=["Ada Lovelace", "Katherine Johnson"],
            year=2026,
            venue="Journal of Tests",
            doi="https://doi.org/10.1234/example",
            abstract="<p>This is a test.</p>",
            sources=["regression-test"],
        )
        write_exports([sample], tmp)
        assert (tmp / "papers.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        assert (tmp / "papers.ris").exists() and (tmp / "papers.csl.json").exists()
        checks.append("csv_utf8_bom")
        checks.append("ris_csl_exports")

        html_report = write_html_report([sample], tmp, "Regression Report", {"source_logs": [{"source": "offline", "status": "ok", "count": 1}]})
        assert html_report.exists() and "Regression Report" in html_report.read_text(encoding="utf-8")
        checks.append("html_report")

        notes = tmp / "notes"
        first_note = write_obsidian_note(sample, notes, tags=["paper", "test"])
        duplicate = dict(sample)
        duplicate["title"] = "Retitled Duplicate"
        duplicate_note = write_obsidian_note(duplicate, notes, tags=["paper", "test"], duplicate_policy="skip")
        assert duplicate_note == first_note and len(list(notes.rglob("*.md"))) == 1
        checks.append("obsidian_duplicate_skip")

        (tmp / "ids.txt").write_text("10.1234/a\n# comment\narxiv:2501.01234\n", encoding="utf-8")
        (tmp / "ids.csv").write_text("\ufeffdoi,title\n10.1234/b,Paper B\n,blank\n", encoding="utf-8")
        (tmp / "ids.json").write_text(json.dumps([{"arxiv_id": "2501.00001"}, "PMC123456"], ensure_ascii=False), encoding="utf-8")
        assert read_identifier_file(tmp / "ids.txt") == ["10.1234/a", "arxiv:2501.01234"]
        assert read_identifier_file(tmp / "ids.csv") == ["10.1234/b"]
        assert read_identifier_file(tmp / "ids.json") == ["2501.00001", "PMC123456"]
        checks.append("identifier_input_formats")

        pmc_xml = """<article><front><article-meta><title-group><article-title>PMC Test Article</article-title></title-group><abstract><p>Abstract text.</p></abstract></article-meta></front><body><sec><title>Methods</title><p>Full text paragraph one.</p><p>Full text paragraph two.</p></sec></body></article>"""
        parsed = parse_pmc_xml_to_markdown(pmc_xml, make_record(title="PMC Test Article", pmcid="PMC123"), tmp)
        assert parsed["status"] == "ok" and Path(parsed["parsed_path"]).exists()
        checks.append("pmc_xml_fulltext_parse")

        zotero_path = write_zotero_report([sample], tmp, "http://127.0.0.1:1/api/users/0")
        assert zotero_path.exists()
        checks.append("zotero_report_offline_safe")

        paywalled = make_record(title="Paywalled Test Paper", doi="10.1234/paywall", url="https://doi.org/10.1234/paywall")
        plan = write_access_plan(paywalled, {"status": "failed", "candidate_count": 0, "tried": []}, tmp, email="")
        assert plan["status"] == "needs_access" and Path(plan["path"]).exists() and Path(plan["markdown_path"]).exists()
        assert any(item.get("source") == "institutional_access" for item in plan["alternatives"])
        checks.append("paywall_access_plan")

        proxied = build_proxy_url("https://library.example.edu/login?url=", "https://doi.org/10.1234/paywall")
        assert proxied == "https://library.example.edu/login?url=https%3A%2F%2Fdoi.org%2F10.1234%2Fpaywall"
        institutional_urls = candidate_institutional_urls(paywalled, proxy_prefix="https://library.example.edu/login?url=")
        assert any(item["mode"] == "library_proxy" for item in institutional_urls)
        inst_plan = write_institutional_access_plan(paywalled, tmp, institutional_urls, tmp, opened=[])
        assert Path(inst_plan["path"]).exists() and not inst_plan["credentials_handling"]["agent_stores_credentials"]
        checks.append("institutional_access_plan")

        before = pdf_snapshot(tmp)
        new_pdf = tmp / "downloaded.pdf"
        new_pdf.write_bytes(b"%PDF-1.4\n% test\n" + b"0" * 2048)
        found = wait_for_new_pdf(tmp, before, timeout=3, settle_seconds=0.1)
        assert found and found.name == "downloaded.pdf"
        checks.append("download_dir_pdf_watcher")

        private_file = tmp / "leak.md"
        private_key = "institutional_" + "".join(["pass", "word"])
        private_file.write_text(private_key + " = secret-value\n", encoding="utf-8")
        findings = scan_private_data(tmp)
        assert any(item.get("kind") == "private_value" for item in findings)
        checks.append("private_data_scan")

        env_args = argparse.Namespace(config="", zotero=False, zotero_api="")
        assert run_check_env(env_args) == 0
        checks.append("check_env")

        package_dir = tmp / "package"
        run_package(argparse.Namespace(out_dir=str(package_dir)))
        zip_path = package_dir / f"paper-research-downloader-v{VERSION}.zip"
        release_json = package_dir / f"paper-research-downloader-v{VERSION}.release.json"
        assert zip_path.exists() and release_json.exists()
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
        assert not any(name.endswith("config.local.json") for name in names)
        assert any(name.endswith("SKILL.md") for name in names)
        checks.append("package_excludes_private_config")

    print_json({"status": "ok", "checks": checks})
    return 0


def run_self_test(_args: Optional[argparse.Namespace] = None) -> int:
    sample = make_record(
        title="A Test Paper: Low-Thrust Trajectory Optimization",
        authors=["Ada Lovelace", "Katherine Johnson"],
        year=2026,
        venue="Journal of Tests",
        doi="https://doi.org/10.1234/example",
        abstract="<p>This is a test.</p>",
        sources=["self-test"],
    )
    assert sample["doi"] == "10.1234/example"
    assert parse_arxiv_id("https://arxiv.org/abs/2401.01234v2") == "2401.01234"
    assert record_key(sample).startswith("doi:")
    tmp = Path(os.getenv("TMP", ".")) / f"prd_self_test_{now_stamp()}"
    tmp.mkdir(parents=True, exist_ok=True)
    write_exports([sample], tmp)
    write_html_report([sample], tmp, "Self Test")
    note = write_obsidian_note(sample, tmp / "notes", tags=["paper", "test"])
    ok = (tmp / "results.json").exists() and (tmp / "papers.bib").exists() and (tmp / "report.html").exists() and note.exists()
    print_json({"status": "ok" if ok else "failed", "tmp": str(tmp.resolve())})
    return 0 if ok else 1


def add_zotero_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--zotero-check", action="store_true", help="Query the local Zotero API for duplicate candidates and write zotero_matches.json.")
    command.add_argument("--zotero-api", default="", help="Local Zotero API base URL; defaults to config.local.json zotero_local_api.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search, resolve, download, parse, and stage research papers.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    config_init = sub.add_parser("config-init", help="Create a config.local.json with reusable defaults.")
    config_init.add_argument("--path", default="", help="Config path; defaults to the skill directory config.local.json.")
    config_init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    config_init.set_defaults(func=run_config_init)

    package = sub.add_parser("package", help="Create an uploadable zip package for this skill.")
    package.add_argument("--out-dir", default="", help="Output directory; defaults to <skill>/dist.")
    package.add_argument("--allow-private-findings", action="store_true", help=argparse.SUPPRESS)
    package.set_defaults(func=run_package)

    check_env = sub.add_parser("check-env", help="Diagnose optional parsers, OCR tools, API keys, and local Zotero access.")
    check_env.add_argument("--config", default="", help="Optional config.local.json path.")
    check_env.add_argument("--zotero", action="store_true", help="Probe local Zotero API.")
    check_env.add_argument("--zotero-api", default="", help="Local Zotero API base URL.")
    check_env.set_defaults(func=run_check_env)

    search = sub.add_parser("search", help="Search scholarly sources and optionally download top OA PDFs.")
    search.add_argument("--config", default="", help="Optional config.local.json path.")
    search.add_argument("--query", required=True, help="Search query.")
    search.add_argument("--limit", type=int, default=30, help="Maximum merged records to keep.")
    search.add_argument("--year-min", type=int, default=None, help="Earliest publication year.")
    search.add_argument("--sources", default="", help="Comma-separated sources.")
    search.add_argument("--sort", choices=["citations", "year", "relevance"], default="citations")
    search.add_argument("--download-top", type=int, default=0, help="Attempt OA PDF download for top N records.")
    search.add_argument("--no-parse", action="store_true", help="Download PDFs but skip text parsing.")
    search.add_argument("--ocr", action="store_true", help="Try OCR fallback with ocrmypdf when parse quality is low.")
    search.add_argument("--extract-figures", action="store_true", help="Export a few page-preview images for reader workflows.")
    search.add_argument("--email", default="", help="Email for Unpaywall/OpenAlex polite pool.")
    search.add_argument("--s2-key", default="", help="Semantic Scholar API key.")
    search.add_argument("--ncbi-key", default="", help="NCBI API key.")
    search.add_argument("--out", default="", help="Output directory.")
    search.add_argument("--resume", dest="resume", action="store_true", default=None, help="Reuse completed per-paper manifests.")
    search.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore completed per-paper manifests.")
    search.add_argument("--vault", default="", help="Obsidian vault root for notes.")
    search.add_argument("--note-folder", default="02_literature", help="Folder inside vault for notes.")
    search.add_argument("--write-notes", action="store_true", help="Write notes into <out>/notes when no vault is supplied.")
    search.add_argument("--tag", action="append", default=["paper"], help="Obsidian tag; repeatable.")
    add_zotero_args(search)
    search.set_defaults(func=run_search)

    resolve = sub.add_parser("resolve", help="Resolve one DOI/arXiv/PMID/URL and download if OA PDF is available.")
    resolve.add_argument("--config", default="", help="Optional config.local.json path.")
    resolve.add_argument("identifier", help="DOI, arXiv ID/URL, PMID, or URL.")
    resolve.add_argument("--email", default="", help="Email for Unpaywall/OpenAlex polite pool.")
    resolve.add_argument("--s2-key", default="", help="Semantic Scholar API key.")
    resolve.add_argument("--ncbi-key", default="", help="NCBI API key.")
    resolve.add_argument("--out", default="", help="Output directory.")
    resolve.add_argument("--resume", dest="resume", action="store_true", default=None, help="Reuse completed per-paper manifests.")
    resolve.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore completed per-paper manifests.")
    resolve.add_argument("--metadata-only", action="store_true", help="Resolve metadata and write notes/exports without downloading PDF.")
    resolve.add_argument("--no-parse", action="store_true", help="Download PDF but skip text parsing.")
    resolve.add_argument("--ocr", action="store_true", help="Try OCR fallback with ocrmypdf when parse quality is low.")
    resolve.add_argument("--extract-figures", action="store_true", help="Export a few page-preview images for reader workflows.")
    resolve.add_argument("--vault", default="", help="Obsidian vault root for notes.")
    resolve.add_argument("--note-folder", default="02_literature", help="Folder inside vault for notes.")
    resolve.add_argument("--write-notes", action="store_true", help="Write note into <out>/notes when no vault is supplied.")
    resolve.add_argument("--tag", action="append", default=["paper"], help="Obsidian tag; repeatable.")
    add_zotero_args(resolve)
    resolve.set_defaults(func=run_resolve)

    batch = sub.add_parser("batch", help="Resolve and download a mixed DOI/arXiv/PMID/PMCID/URL list.")
    batch.add_argument("--config", default="", help="Optional config.local.json path.")
    batch.add_argument("--input", required=True, help="Text, CSV, or JSON file with identifiers.")
    batch.add_argument("--limit", type=int, default=0, help="Optional maximum identifiers to process.")
    batch.add_argument("--email", default="", help="Email for Unpaywall/OpenAlex polite pool.")
    batch.add_argument("--s2-key", default="", help="Semantic Scholar API key.")
    batch.add_argument("--ncbi-key", default="", help="NCBI API key.")
    batch.add_argument("--out", default="", help="Output directory.")
    batch.add_argument("--resume", dest="resume", action="store_true", default=None, help="Reuse completed per-paper manifests.")
    batch.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore completed per-paper manifests.")
    batch.add_argument("--metadata-only", action="store_true", help="Resolve metadata and write notes/exports without downloading PDFs.")
    batch.add_argument("--no-parse", action="store_true", help="Download PDFs but skip text parsing.")
    batch.add_argument("--delay", type=float, default=None, help="Seconds to sleep between batch items.")
    batch.add_argument("--ocr", action="store_true", help="Try OCR fallback with ocrmypdf when parse quality is low.")
    batch.add_argument("--extract-figures", action="store_true", help="Export a few page-preview images for reader workflows.")
    batch.add_argument("--vault", default="", help="Obsidian vault root for notes.")
    batch.add_argument("--note-folder", default="02_literature", help="Folder inside vault for notes.")
    batch.add_argument("--write-notes", action="store_true", help="Write notes into <out>/notes when no vault is supplied.")
    batch.add_argument("--tag", action="append", default=["paper"], help="Obsidian tag; repeatable.")
    add_zotero_args(batch)
    batch.set_defaults(func=run_batch)

    ingest = sub.add_parser("ingest-pdf", help="Stage a local PDF, parse it, and create exports/Obsidian note.")
    ingest.add_argument("--config", default="", help="Optional config.local.json path.")
    ingest.add_argument("pdf", help="Local PDF path.")
    ingest.add_argument("--identifier", default="", help="Optional DOI/arXiv/PMID/PMCID/URL to enrich metadata.")
    ingest.add_argument("--doi", default="", help="Optional DOI metadata override.")
    ingest.add_argument("--arxiv", default="", help="Optional arXiv ID metadata override.")
    ingest.add_argument("--title", default="", help="Optional title override.")
    ingest.add_argument("--author", default="", help="Optional semicolon-separated author list.")
    ingest.add_argument("--year", type=int, default=None, help="Optional publication year.")
    ingest.add_argument("--email", default="", help="Email for Unpaywall/OpenAlex polite pool.")
    ingest.add_argument("--s2-key", default="", help="Semantic Scholar API key.")
    ingest.add_argument("--ncbi-key", default="", help="NCBI API key.")
    ingest.add_argument("--out", default="", help="Output directory.")
    ingest.add_argument("--resume", dest="resume", action="store_true", default=None, help="Reuse completed per-paper manifests.")
    ingest.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore completed per-paper manifests.")
    ingest.add_argument("--no-parse", action="store_true", help="Copy PDF but skip text parsing.")
    ingest.add_argument("--ocr", action="store_true", help="Try OCR fallback with ocrmypdf when parse quality is low.")
    ingest.add_argument("--extract-figures", action="store_true", help="Export a few page-preview images for reader workflows.")
    ingest.add_argument("--vault", default="", help="Obsidian vault root for notes.")
    ingest.add_argument("--note-folder", default="02_literature", help="Folder inside vault for notes.")
    ingest.add_argument("--write-notes", action="store_true", help="Write note into <out>/notes when no vault is supplied.")
    ingest.add_argument("--tag", action="append", default=["paper"], help="Obsidian tag; repeatable.")
    add_zotero_args(ingest)
    ingest.set_defaults(func=run_ingest_pdf)

    institutional = sub.add_parser("institutional-open", help="Open lawful institutional access URLs for user-mediated login/download, then optionally ingest the downloaded PDF.")
    institutional.add_argument("--config", default="", help="Optional config.local.json path.")
    institutional.add_argument("identifier", help="DOI, arXiv ID/URL, PMID, PMCID, or publisher URL.")
    institutional.add_argument("--proxy-prefix", default="", help="Library proxy prefix, for example https://proxy.school.edu/login?url=")
    institutional.add_argument("--download-dir", default="", help="Directory where the user will manually save the PDF; defaults to config or Downloads.")
    institutional.add_argument("--wait-for-pdf", action="store_true", help="Wait for a newly downloaded PDF in --download-dir.")
    institutional.add_argument("--ingest-downloaded", action="store_true", help="After --wait-for-pdf finds a PDF, run ingest-pdf automatically.")
    institutional.add_argument("--timeout", type=int, default=600, help="Seconds to wait for a new PDF when --wait-for-pdf is used.")
    institutional.add_argument("--open-limit", type=int, default=2, help="Maximum candidate URLs to open automatically.")
    institutional.add_argument("--no-open", action="store_true", help="Print/write candidate URLs without opening a browser.")
    institutional.add_argument("--title", default="", help="Optional title override when ingesting a manually downloaded PDF.")
    institutional.add_argument("--email", default="", help="Email for Unpaywall/OpenAlex polite pool.")
    institutional.add_argument("--s2-key", default="", help="Semantic Scholar API key.")
    institutional.add_argument("--ncbi-key", default="", help="NCBI API key.")
    institutional.add_argument("--out", default="", help="Output directory.")
    institutional.add_argument("--resume", dest="resume", action="store_true", default=None, help="Reuse completed per-paper manifests.")
    institutional.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore completed per-paper manifests.")
    institutional.add_argument("--no-parse", action="store_true", help="Ingest PDF but skip text parsing.")
    institutional.add_argument("--ocr", action="store_true", help="Try OCR fallback with ocrmypdf when parse quality is low.")
    institutional.add_argument("--extract-figures", action="store_true", help="Export a few page-preview images for reader workflows.")
    institutional.add_argument("--vault", default="", help="Obsidian vault root for notes.")
    institutional.add_argument("--note-folder", default="02_literature", help="Folder inside vault for notes.")
    institutional.add_argument("--write-notes", action="store_true", help="Write note into <out>/notes when no vault is supplied.")
    institutional.add_argument("--tag", action="append", default=["paper"], help="Obsidian tag; repeatable.")
    add_zotero_args(institutional)
    institutional.set_defaults(func=run_institutional_open)

    regression = sub.add_parser("test", help="Run offline regression tests for merge, exports, reports, notes, inputs, and packaging.")
    regression.set_defaults(func=run_regression_tests)

    test = sub.add_parser("self-test", help="Run quick offline smoke checks for filename, exports, and note generation.")
    test.set_defaults(func=run_self_test)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
