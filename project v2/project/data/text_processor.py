from project.data.sentiment_analyzer import RussianSentimentAnalyzer
from project.models.text_data import TextData
from datetime import datetime


class TextProcessor:
    def __init__(self):
        self.analyzer = RussianSentimentAnalyzer()

    def process_text(self, text: str, source: str, chat_id: str = None, author: str = None) -> TextData:
        # Анализ настроения
        analysis = self.analyzer.analyze(text)

        # Создание новой записи
        new_entry = TextData(
            text=text,
            source=source,
            chat_id=chat_id,
            author=author,
            sentiment=analysis['sentiment'],
            sentiment_confidence=str(analysis['confidence']),
            created_at=datetime.now()
        )

        return new_entry
