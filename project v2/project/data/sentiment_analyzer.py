from transformers import pipeline, AutoModelForSequenceClassification, AutoTokenizer
from typing import Dict
import torch


class RussianSentimentAnalyzer:
    def __init__(self, device="cpu"):
        self.device = device
        # Основная модель для настроений
        self.sentiment_model = pipeline(
            "text-classification",
            model="blanchefort/rubert-base-cased-sentiment",
            device=0 if device == "cuda" else -1
        )
        # Детектор ругательств
        self.swear_model = pipeline(
            "text-classification",
            model="cointegrated/rubert-tiny-toxicity",
            device=0 if device == "cuda" else -1
        )

    def analyze(self, text: str) -> Dict[str, str | float]:
        try:
            # Анализ настроения
            sentiment_result = self.sentiment_model(text)[0]
            # Проверка на брань
            swear_result = self.swear_model(text)[0]

            # Если обнаружена брань с высокой уверенностью
            if swear_result["label"] == "toxic" and swear_result["score"] > 0.85:
                return {
                    "sentiment": "negative",
                    "confidence": max(swear_result["score"], 0.95),
                    "is_toxic": True
                }

            # Стандартный анализ
            return {
                "sentiment": sentiment_result["label"].lower(),
                "confidence": round(sentiment_result["score"], 4),
                "is_toxic": False
            }

        except Exception as e:
            print(f"[Analyzer Error] {e}")
            return {
                "sentiment": "neutral",
                "confidence": 0.0,
                "is_toxic": False
            }
