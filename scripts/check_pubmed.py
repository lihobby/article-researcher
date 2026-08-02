"""Safely verify PubMed configuration without ranking, LLM calls, or email."""

from pathlib import Path
import sys

from hydra import compose, initialize_config_dir
from omegaconf.errors import InterpolationResolutionError

from zotero_arxiv_daily.retriever.pubmed_retriever import PubmedRetriever


def main() -> int:
    config_dir = str(Path(__file__).resolve().parents[1] / "config")
    try:
        with initialize_config_dir(config_dir=config_dir, version_base=None):
            config = compose(config_name="default")
        retriever = PubmedRetriever(config)
        records = retriever._retrieve_raw_papers()
    except InterpolationResolutionError as exc:
        if "NCBI_EMAIL" in str(exc):
            print(
                "NCBI_EMAIL is not set. Add it as a Codespaces secret or run "
                "`export NCBI_EMAIL=your-email@example.com` before this check.",
                file=sys.stderr,
            )
            return 2
        raise

    papers = [retriever.convert_to_paper(record) for record in records]
    papers = [paper for paper in papers if paper is not None]

    print(f"\nPubMed check succeeded: {len(papers)} articles with abstracts found.\n")
    if not papers:
        print("No matching articles were indexed during the configured lookback window.")
        print("This is not an error. You can temporarily increase source.pubmed.lookback_days.")
        return 0

    for index, paper in enumerate(papers[:20], start=1):
        print(f"{index}. {paper.title}")
        print(f"   Journal: {paper.journal or 'Unknown'}")
        print(f"   Date: {paper.publication_date or 'Unknown'}")
        print(f"   PMID: {paper.pmid or 'Unknown'}")
        print(f"   DOI: {paper.doi or 'Unknown'}")
        print(f"   URL: {paper.url}")

    if len(papers) > 20:
        print(f"\nOnly the first 20 of {len(papers)} articles are displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
