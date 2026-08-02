from abc import ABC, abstractmethod
from omegaconf import DictConfig
from ..protocol import Paper, CorpusPaper
import numpy as np
from typing import Type
class BaseReranker(ABC):
    def __init__(self, config:DictConfig):
        self.config = config

    def rerank(self, candidates:list[Paper], corpus:list[CorpusPaper]) -> list[Paper]:
        manual_topics = any(item.weight is not None for item in corpus)
        if not manual_topics:
            corpus = sorted(corpus,key=lambda x: x.added_date,reverse=True)
        sim = self.get_similarity_score([c.abstract for c in candidates], [c.abstract for c in corpus])
        assert sim.shape == (len(candidates), len(corpus))
        if manual_topics:
            positive_indices = [i for i, item in enumerate(corpus) if not item.negative]
            negative_indices = [i for i, item in enumerate(corpus) if item.negative]
            weights = np.array([corpus[i].weight or 1.0 for i in positive_indices], dtype=float)
            weights /= weights.sum()
            scores = (sim[:, positive_indices] * weights).sum(axis=1)
            if negative_indices:
                negative_weights = np.array([corpus[i].weight or 1.0 for i in negative_indices], dtype=float)
                negative_scores = (sim[:, negative_indices] * negative_weights).max(axis=1)
                penalty = float(self.config.interest.get("negative_penalty", 0.5))
                scores -= penalty * negative_scores
            match_count = int(self.config.interest.get("matched_topic_count", 3))
            for row, candidate in enumerate(candidates):
                ranked = sorted(positive_indices, key=lambda i: sim[row, i], reverse=True)
                candidate.matched_topics = [corpus[i].title for i in ranked[:match_count]]
            scores *= 10
        else:
            time_decay_weight = 1 / (1 + np.log10(np.arange(len(corpus)) + 1))
            time_decay_weight = time_decay_weight / time_decay_weight.sum()
            scores = (sim * time_decay_weight).sum(axis=1) * 10
        for s,c in zip(scores,candidates):
            c.score = s
        candidates = sorted(candidates,key=lambda x: x.score,reverse=True)
        return candidates
    
    @abstractmethod
    def get_similarity_score(self, s1:list[str], s2:list[str]) -> np.ndarray:
        raise NotImplementedError

registered_rerankers = {}

def register_reranker(name:str):
    def decorator(cls):
        registered_rerankers[name] = cls
        return cls
    return decorator

def get_reranker_cls(name:str) -> Type[BaseReranker]:
    if name not in registered_rerankers:
        raise ValueError(f"Reranker {name} not found")
    return registered_rerankers[name]
