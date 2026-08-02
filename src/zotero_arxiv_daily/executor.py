from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper, InterestTopic
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI
from tqdm import tqdm


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config:DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)

    def fetch_manual_interest_corpus(self) -> list[CorpusPaper]:
        """Build a compatibility corpus from user-written research topics.

        The current reranker compares candidate abstracts with a list of corpus
        abstracts. Treating each topic description as a corpus abstract lets us
        remove the Zotero requirement now without rewriting ranking in the same
        step.
        """
        raw_topics = self.config.interest.get("topics", [])
        topics: list[InterestTopic] = []
        for index, item in enumerate(raw_topics):
            name = str(item.get("name", "")).strip()
            description = str(item.get("description", "")).strip()
            weight = float(item.get("weight", 1.0))
            if not name:
                raise ValueError(f"interest.topics[{index}].name cannot be empty")
            if not description:
                raise ValueError(f"interest.topics[{index}].description cannot be empty")
            if weight <= 0:
                raise ValueError(f"interest.topics[{index}].weight must be greater than 0")
            topics.append(InterestTopic(name=name, description=description, weight=weight))

        corpus: list[CorpusPaper] = []
        now = datetime.now()
        for topic in topics:
            corpus.append(
                CorpusPaper(
                    title=topic.name, abstract=f"{topic.name}. {topic.description}",
                    added_date=now, paths=["manual-interest"], weight=topic.weight,
                )
            )
        for index, item in enumerate(self.config.interest.get("negative_topics", [])):
            if isinstance(item, str):
                name, description, weight = item, item, 1.0
            else:
                name = str(item.get("name", "")).strip()
                description = str(item.get("description", name)).strip()
                weight = float(item.get("weight", 1.0))
            if not name or not description or weight <= 0:
                raise ValueError(f"interest.negative_topics[{index}] must have a name, description, and positive weight")
            corpus.append(CorpusPaper(
                title=name, abstract=f"{name}. {description}", added_date=now,
                paths=["manual-interest-negative"], weight=weight, negative=True,
            ))
        logger.info(f"Loaded {len(topics)} manual research interests")
        return corpus

    @staticmethod
    def _paper_keys(paper) -> set[str]:
        keys = set()
        if paper.pmid:
            keys.add(f"pmid:{paper.pmid.strip()}")
        if paper.doi:
            keys.add(f"doi:{paper.doi.strip().lower()}")
        if paper.url:
            keys.add(f"url:{paper.url.strip().rstrip('/').lower()}")
        title = re.sub(r"[^a-z0-9]+", " ", paper.title.lower()).strip()
        if title:
            keys.add(f"title:{title}")
        return keys

    def _history_path(self) -> Path:
        return Path(str(self.config.executor.get("history_path", "data/sent_history.json")))

    def load_sent_history(self) -> dict[str, str]:
        path = self._history_path()
        if not path.exists():
            return {}
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Ignoring unreadable sent history {path}: {exc}")
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(self.config.executor.get("history_retention_days", 30)))
        clean = {}
        for key, value in history.items():
            try:
                if datetime.fromisoformat(value) >= cutoff:
                    clean[key] = value
            except (TypeError, ValueError):
                continue
        return clean

    def deduplicate_papers(self, papers, history: dict[str, str]):
        unique, seen = [], set(history)
        for paper in papers:
            keys = self._paper_keys(paper)
            if keys & seen:
                logger.info(f"Skipping duplicate or previously sent paper: {paper.title}")
                continue
            unique.append(paper)
            seen.update(keys)
        return unique

    def save_sent_history(self, history: dict[str, str], papers) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        for paper in papers:
            for key in self._paper_keys(paper):
                history[key] = timestamp
        path = self._history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def fetch_interest_corpus(self) -> list[CorpusPaper]:
        provider = self.config.interest.get("provider", "manual")
        if provider == "manual":
            return self.fetch_manual_interest_corpus()
        if provider == "zotero":
            return self.filter_corpus(self.fetch_zotero_corpus())
        raise ValueError('interest.provider must be either "manual" or "zotero"')

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = zot.everything(zot.collections())
        collections = {c['key']:c for c in collections}
        corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']
        def get_collection_path(col_key:str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']
        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]
    
    def filter_corpus(self, corpus:list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    
    def run(self):
        corpus = self.fetch_interest_corpus()
        if len(corpus) == 0:
            logger.error("No research interests found. Please add at least one item to interest.topics.")
            return
        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            try:
                papers = retriever.retrieve_papers()
            except Exception as exc:
                logger.warning(f"Failed to retrieve {source}; continuing with other sources: {exc}")
                continue
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
        history = self.load_sent_history()
        all_papers = self.deduplicate_papers(all_papers, history)
        reranked_papers = []
        if len(all_papers) > 0:
            logger.info("Reranking papers...")
            reranked_papers = self.reranker.rerank(all_papers, corpus)
            minimum_score = float(self.config.executor.get("min_score", 0))
            reranked_papers = [paper for paper in reranked_papers if paper.score is not None and paper.score >= minimum_score]
            reranked_papers = reranked_papers[:self.config.executor.max_paper_num]
            logger.info("Generating TLDR and affiliations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                if p.affiliations is None:
                    p.generate_affiliations(self.openai_client, self.config.llm)
            if not reranked_papers and not self.config.executor.send_empty:
                logger.info("No papers met the relevance threshold. No email will be sent.")
                return
        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return
        logger.info("Sending email...")
        email_content = render_email(reranked_papers)
        send_email(self.config, email_content)
        self.save_sent_history(history, reranked_papers)
        logger.info("Email sent successfully")
