from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper, InterestTopic
import random
from datetime import datetime
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

        # Repeat weighted topics so the existing weighted-average reranker can
        # approximately respect user weights until the dedicated topic reranker
        # is introduced in a later step.
        corpus: list[CorpusPaper] = []
        now = datetime.now()
        for topic in topics:
            repeat_count = max(1, round(topic.weight * 10))
            corpus.extend(
                CorpusPaper(
                    title=topic.name,
                    abstract=f"{topic.name}. {topic.description}",
                    added_date=now,
                    paths=["manual-interest"],
                )
                for _ in range(repeat_count)
            )
        logger.info(f"Loaded {len(topics)} manual research interests")
        return corpus

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
            papers = retriever.retrieve_papers()
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
        reranked_papers = []
        if len(all_papers) > 0:
            logger.info("Reranking papers...")
            reranked_papers = self.reranker.rerank(all_papers, corpus)
            reranked_papers = reranked_papers[:self.config.executor.max_paper_num]
            logger.info("Generating TLDR and affiliations...")
            for p in tqdm(reranked_papers):
                p.generate_tldr(self.openai_client, self.config.llm)
                p.generate_affiliations(self.openai_client, self.config.llm)
        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return
        logger.info("Sending email...")
        email_content = render_email(reranked_papers)
        send_email(self.config, email_content)
        logger.info("Email sent successfully")
