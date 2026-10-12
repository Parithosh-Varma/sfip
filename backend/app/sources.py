"""Paper discovery over OpenAlex, Crossref, and Europe PMC.

Seeds: DOI, URL (incl. doi.org / arXiv links), researcher name, or free query.
Expansion: citing works + OpenAlex related works. Only openly accessible
papers get PDF fetch jobs; everything stays within source rate limits.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlparse

from . import httpc

OPENALEX = "https://api.openalex.org/works"
OPENALEX_AUTHORS = "https://api.openalex.org/authors"
CROSSREF = "https://api.crossref.org/works"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_PDF = "https://europepmc.org/backend/ptpmcrender.fcgi"

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")


def classify_seed(seed: str) -> str:
    s = seed.strip()
    if DOI_RE.search(s):
        return "doi"
    if s.lower().startswith(("http://", "https://")):
        host = urlparse(s).netloc.lower()
        if "arxiv.org" in host:
            return "arxiv"
        return "url"
    # person-name heuristic: 2-4 alphabetic tokens, mostly capitalized
    tokens = s.split()
    if 2 <= len(tokens) <= 4 and all(re.fullmatch(r"[A-Za-z.\-']+", t) for t in tokens):
        return "author"
    return "query"


def _norm_work(w: dict, source: str, relevance: float = 0.0) -> dict:
    loc = w.get("best_oa_location") or {}
    pdf_url = loc.get("pdf_url")
    oa_url = loc.get("landing_page_url")
    if not pdf_url:
        for l in w.get("locations") or []:
            if l.get("pdf_url"):
                pdf_url = l["pdf_url"]
                oa_url = oa_url or l.get("landing_page_url")
                break
    # Europe PMC fallback by pmcid
    ids = w.get("ids") or {}
    pmcid = (ids.get("pmcid") or "").replace("PMC:", "")
    if not pdf_url and pmcid:
        pdf_url = f"{EPMC_PDF}?accid=PMC{pmcid}&blobtype=pdf"
    authors = ", ".join(a["author"]["display_name"] for a in (w.get("authorships") or []) if a.get("author"))
    primary = (w.get("primary_location") or {}).get("source") or {}
    meta = {
        "cited_by_count": w.get("cited_by_count", 0),
        "is_oa": w.get("open_access", {}).get("is_oa", False),
        "oa_status": w.get("open_access", {}).get("oa_status"),
        "type": w.get("type"),
        "pmcid": pmcid or None,
        "publication_date": w.get("publication_date"),
    }
    return {
        "external_id": w.get("id"),
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
        "title": w.get("display_name") or w.get("title") or "(untitled)",
        "authors": authors[:1000],
        "year": (w.get("publication_year") or None),
        "venue": primary.get("display_name"),
        "source": source,
        "oa_url": oa_url,
        "pdf_url": pdf_url,
        "landing_url": w.get("doi") or oa_url,
        "relevance": relevance,
        "metadata": meta,
    }


def lookup_doi(doi: str) -> Optional[dict]:
    doi = DOI_RE.search(doi).group(1) if DOI_RE.search(doi) else doi
    try:
        w = httpc.get_json(f"{OPENALEX}/doi:{quote(doi, safe='')}")
        return _norm_work(w, "openalex")
    except httpc.FetchError:
        pass
    try:  # Crossref fallback
        d = httpc.get_json(f"{CROSSREF}/{quote(doi, safe='/')}")
        m = d.get("message", {})
        title = (m.get("title") or ["(untitled)"])[0]
        authors = ", ".join(filter(None, (f"{a.get('given', '')} {a.get('family', '')}".strip()
                                          for a in m.get("author", []))))
        return {
            "external_id": f"crossref:{doi}", "doi": doi, "title": title,
            "authors": authors[:1000],
            "year": (m.get("issued", {}).get("date-parts", [[None]])[0] or [None])[0],
            "venue": (m.get("container-title") or [None])[0],
            "source": "crossref",
            "oa_url": m.get("URL"), "pdf_url": None, "landing_url": m.get("URL"),
            "relevance": 1.0, "metadata": {"from_crossref": True},
        }
    except httpc.FetchError:
        return None


def _search_works(query: str, per_page: int = 10, sort: str = "relevance_score:desc") -> list[dict]:
    try:
        d = httpc.get_json(OPENALEX, {"search": query, "per-page": per_page, "sort": sort})
        return d.get("results", [])
    except httpc.FetchError:
        return []


def search_author(name: str) -> list[dict]:
    """Resolve a researcher name to an author and return their works (most cited first)."""
    try:
        d = httpc.get_json(OPENALEX_AUTHORS, {"search": name, "per-page": 3})
        authors = d.get("results", [])
    except httpc.FetchError:
        authors = []
    works: list[dict] = []
    if authors:
        a = authors[0]
        try:
            w = httpc.get_json(OPENALEX, {"filter": f"author.id:{a['id']}",
                                          "sort": "cited_by_count:desc", "per-page": 20})
            works = [_norm_work(x, "openalex", relevance=min(1.0, x.get("cited_by_count", 0) / 500))
                     for x in w.get("results", [])]
        except httpc.FetchError:
            pass
    if not works:
        works = search_query(name)
    return works


def search_query(q: str, per_page: int = 15) -> list[dict]:
    return [_norm_work(w, "openalex", relevance=0.5) for w in _search_works(q, per_page)]


def expand_paper(ext_id: str, max_items: int = 20) -> list[dict]:
    """Citing + related works from OpenAlex, relevance-ranked."""
    out: dict[str, dict] = {}
    try:
        citing = httpc.get_json(OPENALEX, {"filter": f"cites:{ext_id}", "per-page": max_items,
                                           "sort": "publication_date:desc"})
        for w in citing.get("results", []):
            n = _norm_work(w, "openalex", relevance=0.4)
            out[n["external_id"]] = n
    except httpc.FetchError:
        pass
    try:
        work = httpc.get_json(f"{OPENALEX}/{ext_id}")
        related_ids = (work.get("related_works") or [])[:max_items]
        for rid in related_ids:
            if rid in out:
                continue
            try:
                w = httpc.get_json(f"{OPENALEX}/{rid}")
                n = _norm_work(w, "openalex", relevance=0.6)
                out[n["external_id"]] = n
            except httpc.FetchError:
                continue
    except httpc.FetchError:
        pass
    return sorted(out.values(), key=lambda p: -p["relevance"])
