import sys
import os
import random
from typing import List
import numpy as np
from itertools import permutations
from transformers import pipeline
from langdetect import detect
from bittranslate.reward_models import BertScore, VectorSim
from bittranslate.prompt_dataset.german_quad import GermanQuAD
from bittranslate.prompt_dataset.exams import Exams
from bittranslate.prompt_dataset.peer_sum import PeerSum
from bittranslate.prompt_dataset.prompt_dataset import PromptDataset
from bittranslate.prompt_dataset.xquad import XQuAD
from bittranslate.tracker import  Tracker
from bittranslate.constants import TRACKER_HISTORY_COUNT

class Validator:
    def __init__(self, device: str = "cpu", out_dir: str= "bittranslate_out/" ):
        self._reward_models = [BertScore(device=device), VectorSim(device=device)]

        self._reward_weights = [0.5, 0.5]
        self._mgpt_pipeline = pipeline("text-generation", "ai-forever/mGPT", device=device)
        self._langs = ["de", "en", "es", "it", "pl"]
        self._lang_pairs = list(permutations(self._langs, 2))
        self.tracker = Tracker(self._lang_pairs, TRACKER_HISTORY_COUNT)

        self.out_dir= out_dir
        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)

        exams = Exams()
        german_quad = GermanQuAD()
        peer_sum = PeerSum()
        xquad = XQuAD()

        self._datasets = {"de": [german_quad, xquad],
                          "en": [peer_sum, xquad],
                          "es": [xquad],
                          "it": [exams],
                          "pl": [exams]
                          }

    def score(self, sources: List[str], translations: List[List[str]], source_lang: str, target_lang: str):
        len_sources = len(sources)
        miners_count = len(translations[0])
        all_scores = [0]*miners_count
        top_max_score = 0
        top_max_source = ""
        top_max_target = ""
        top_min_score = 1.1
        top_min_source = ""
        top_min_target = ""

        for s, t in zip(sources, translations):
            # s: single source text
            # t: a list of translation where index contains a translation from a given miner.
            # l: target language

            scores = self.single_score(s, t, target_lang)
            all_scores = [a + b for a, b in zip(all_scores, scores)]

            # Tracking:
            try:  # nonessential code:
                max_score = max(scores)
                min_score = min(scores)
                max_score_index = scores.index(max_score)
                min_score_index = scores.index(min_score)
                if max_score > top_max_score:
                    top_max_score = max_score
                    top_max_source = s
                    top_max_target = t[max_score_index]
                if min_score < top_min_score:
                    top_min_score = min_score
                    top_min_source = s
                    top_min_target = t[min_score_index]
            except Exception as e:
                print(f"Error (non-essential code): computing min/max source and target texts", file=sys.stderr)
                print(e, file=sys.stderr)

        final_scores = [score/len_sources for score in all_scores]

        # Track scores
        try: # nonessential code:
            self.tracker.track_scores(source_lang, target_lang, final_scores)
        except Exception as e:
            print(f"Error (non-essential code): tracker.log_scores()", file=sys.stderr)
            print(e, file=sys.stderr)

        # Track texts
        try:  # nonessential code:
            self.tracker.track_texts(source_lang, target_lang,  top_min_source, top_min_target, top_min_score, top_max_source, top_max_target, top_max_score)
        except Exception as e:
            print(f"Error (non-essential code): tracker.track_texts()", file=sys.stderr)
            print(e, file=sys.stderr)

        return final_scores

    def single_score(self, source: str, translations: List[str], target_lang: str) -> List[float]:

        lang_filter = self._filter_lang(translations, target_lang)

        reward_scores = [0.0] * len(translations)
        for i, reward_model in enumerate(self._reward_models):
            # Produce scores with a Reward Model
            scores = reward_model.score(source, translations)

            # Sigmoid normalization
            norm_scores = self._sigmoid_normalize(scores)

            # Get the weight for the Reward Model
            weight = self._reward_weights[i]

            # Multiply each score based on its weight
            weighted_scores = [float(score * weight) for score in norm_scores]

            # Add the resulting weighted scores to the total reward_scores list
            reward_scores = [
                current_score + new_score
                for current_score, new_score in zip(reward_scores, weighted_scores)
            ]

        result = [a * b for a, b in zip(lang_filter, reward_scores)]

        return result

    def _sigmoid_normalize(self, scores: List[float]) -> List[float]:
        np_scores = np.array(scores)
        norm_scores = 1 / (1 + np.exp(-np_scores))

        return norm_scores.tolist()

    def _get_source_dataset(self) -> (PromptDataset, str, str):
        random_lang_pair_index = random.randint(0, len(self._lang_pairs) - 1)
        random_lang_pair = self._lang_pairs[random_lang_pair_index]
        source_lang = random_lang_pair[0]
        target_lang = random_lang_pair[1]

        source_datasets = self._datasets[source_lang]

        random_dataset_index = random.randint(0, len(source_datasets) - 1)
        source_dataset = source_datasets[random_dataset_index]

        return source_dataset, source_lang, target_lang


    def generate_cases(self, count: int=2) -> (str, str, List[str]):
        sources = []

        source_dataset, source_lang, target_lang = self._get_source_dataset()

        for i in range(0, count):
            starting_case = source_dataset.sample_case(source_lang)
            prompt = self._generate_prompt(starting_case)
            sources.append(prompt)
        return source_lang, target_lang, sources

    def _generate_prompt(self, text: str) -> str:
        current_token_length = len(self._mgpt_pipeline.tokenizer.encode(text))
        return self._mgpt_pipeline(
            text,
            return_full_text=False,
            no_repeat_ngram_size=3,
            do_sample=True,
            top_k=10,
            temperature=1,
            min_length=32 + current_token_length,
            max_length=64 + current_token_length,
        )[0]["generated_text"]


    def _filter_lang(self, translations, target_lang):
        # Lang detection filter
        lang_filter = []

        for translation in translations:
            try:
                pred = detect(translation)

            except Exception as e:
                lang_filter.append(0)
                print(f"Language detection exception. Error {str(e)}. Translation: {translation}", file=sys.stderr)
                continue
            if pred == target_lang:
                lang_filter.append(1)
            else:
                lang_filter.append(0)

        return lang_filter

    def save_tracked_results(self):
        out_scores_path = self.out_dir + "scores.json"
        self.tracker.scores_to_json(out_scores_path)
        out_texts_path = self.out_dir + "texts.json"
        self.tracker.texts_to_json(out_texts_path)