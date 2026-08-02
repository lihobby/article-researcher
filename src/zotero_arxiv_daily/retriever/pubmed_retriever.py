"""Retrieve recently indexed journal articles from PubMed via NCBI E-utilities."""

from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

from loguru import logger
import requests

from .base import BaseRetriever, register_retriever
from ..protocol import Paper


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_TIMEOUT = (10, 60)


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _publication_date(article: ET.Element) -> str | None:
    date = article.find(".//JournalIssue/PubDate")
    if date is None:
        date = article.find(".//ArticleDate")
    if date is None:
        return None
    year = _element_text(date.find("Year"))
    month = _element_text(date.find("Month"))
    day = _element_text(date.find("Day"))
    medline_date = _element_text(date.find("MedlineDate"))
    parts = [part for part in (year, month, day) if part]
    return "-".join(parts) if parts else (medline_date or None)


def parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    """Convert PubMed XML into small dictionaries used by ``convert_to_paper``."""
    root = ET.fromstring(xml_text)
    records: list[dict[str, Any]] = []
    for citation in root.findall(".//PubmedArticle"):
        medline = citation.find("MedlineCitation")
        article = medline.find("Article") if medline is not None else None
        if medline is None or article is None:
            continue

        pmid = _element_text(medline.find("PMID"))
        title = _element_text(article.find("ArticleTitle"))
        abstract_parts = []
        for part in article.findall("Abstract/AbstractText"):
            text = _element_text(part)
            label = part.attrib.get("Label")
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = "\n".join(abstract_parts)

        authors = []
        for author in article.findall("AuthorList/Author"):
            collective = _element_text(author.find("CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            last = _element_text(author.find("LastName"))
            initials = _element_text(author.find("Initials"))
            name = " ".join(part for part in (last, initials) if part)
            if name:
                authors.append(name)

        doi = None
        for article_id in citation.findall(".//ArticleIdList/ArticleId"):
            if article_id.attrib.get("IdType") == "doi":
                doi = _element_text(article_id)
                break

        records.append(
            {
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "journal": _element_text(article.find("Journal/Title")),
                "publication_date": _publication_date(article),
                "doi": doi,
                "article_types": [
                    _element_text(item)
                    for item in article.findall("PublicationTypeList/PublicationType")
                    if _element_text(item)
                ],
            }
        )
    return records


@register_retriever("pubmed")
class PubmedRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        journals = self.retriever_config.get("journals")
        if not journals:
            raise ValueError("journals must be specified for pubmed")
        if not self.retriever_config.get("email"):
            raise ValueError("email must be specified for pubmed (NCBI requires a contact email)")

    def _common_params(self) -> dict[str, str]:
        params = {
            "tool": "zotero-arxiv-daily",
            "email": str(self.retriever_config.email),
        }
        api_key = self.retriever_config.get("api_key")
        if api_key:
            params["api_key"] = str(api_key)
        return params

    def _retrieve_raw_papers(self) -> list[dict[str, Any]]:
        journals = [str(journal) for journal in self.retriever_config.journals]
        journal_query = " OR ".join(f'"{journal}"[jour]' for journal in journals)
        search_params = {
            **self._common_params(),
            "db": "pubmed",
            "retmode": "json",
            "retmax": str(self.retriever_config.get("max_results", 300)),
            "sort": "pub_date",
            "datetype": "edat",
            "reldate": str(self.retriever_config.get("lookback_days", 3)),
            "term": f"({journal_query}) AND has abstract[FILT]",
        }
        search_response = requests.get(
            f"{EUTILS_BASE}/esearch.fcgi", params=search_params, timeout=REQUEST_TIMEOUT
        )
        search_response.raise_for_status()
        pmids = search_response.json().get("esearchresult", {}).get("idlist", [])
        if self.config.executor.debug:
            pmids = pmids[:10]
        if not pmids:
            return []

        fetch_params = {
            **self._common_params(),
            "db": "pubmed",
            "retmode": "xml",
            "id": ",".join(pmids),
        }
        fetch_response = requests.get(
            f"{EUTILS_BASE}/efetch.fcgi", params=fetch_params, timeout=REQUEST_TIMEOUT
        )
        fetch_response.raise_for_status()
        records = parse_pubmed_xml(fetch_response.text)
        logger.info(f"PubMed returned {len(records)} articles with metadata")
        return records

    def convert_to_paper(self, raw_paper: dict[str, Any]) -> Paper | None:
        if not raw_paper["title"] or not raw_paper["abstract"]:
            return None
        pmid = raw_paper["pmid"]
        return Paper(
            source=self.name,
            title=raw_paper["title"],
            authors=raw_paper["authors"],
            abstract=raw_paper["abstract"],
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            journal=raw_paper["journal"],
            publication_date=raw_paper["publication_date"],
            doi=raw_paper["doi"],
            pmid=pmid,
            article_types=raw_paper["article_types"],
        )
