"""Tests for the PubMed E-utilities retriever."""

from types import SimpleNamespace

from omegaconf import open_dict

from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever, parse_pubmed_xml


PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal><JournalIssue><PubDate><Year>2026</Year><Month>Aug</Month><Day>01</Day></PubDate></JournalIssue><Title>Nature Chemical Biology</Title></Journal>
        <ArticleTitle>A chemical biology discovery</ArticleTitle>
        <Abstract><AbstractText Label="BACKGROUND">Probe development.</AbstractText><AbstractText Label="RESULTS">A new target was found.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Smith</LastName><Initials>JA</Initials><AffiliationInfo><Affiliation>Department of Chemistry, Example University</Affiliation></AffiliationInfo></Author></AuthorList>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="pubmed">12345678</ArticleId><ArticleId IdType="doi">10.1000/example</ArticleId></ArticleIdList></PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


def _configure(config):
    with open_dict(config.source):
        config.source.pubmed = {
            "journals": ["Nature Chemical Biology", "Journal of the American Chemical Society"],
            "lookback_days": 3,
            "email": "researcher@example.com",
            "api_key": None,
            "max_results": 300,
        }


def test_parse_pubmed_xml():
    records = parse_pubmed_xml(PUBMED_XML)
    assert len(records) == 1
    assert records[0]["pmid"] == "12345678"
    assert records[0]["doi"] == "10.1000/example"
    assert records[0]["authors"] == ["Smith JA"]
    assert records[0]["affiliations"] == ["Department of Chemistry, Example University"]
    assert "RESULTS: A new target was found." in records[0]["abstract"]


def test_pubmed_retrieve(config, monkeypatch):
    _configure(config)
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        if "esearch" in url:
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"esearchresult": {"idlist": ["12345678"]}},
            )
        return SimpleNamespace(raise_for_status=lambda: None, text=PUBMED_XML)

    monkeypatch.setattr("zotero_arxiv_daily.retriever.pubmed_retriever.requests.get", fake_get)
    monkeypatch.setattr("zotero_arxiv_daily.retriever.base.sleep", lambda _: None)
    papers = PubmedRetriever(config).retrieve_papers()

    assert len(papers) == 1
    assert papers[0].journal == "Nature Chemical Biology"
    assert papers[0].pmid == "12345678"
    assert papers[0].doi == "10.1000/example"
    assert '"Nature Chemical Biology"[jour]' in calls[0][1]["term"]
    assert calls[0][1]["reldate"] == "3"


def test_pubmed_empty_search(config, monkeypatch):
    _configure(config)
    monkeypatch.setattr(
        "zotero_arxiv_daily.retriever.pubmed_retriever.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"esearchresult": {"idlist": []}},
        ),
    )
    assert PubmedRetriever(config)._retrieve_raw_papers() == []
