"""The official IBO syllabus sections, created at startup if missing.

Questions must have a topic before they can be submitted for review, so a fresh
database (for instance a new Railway Postgres) cannot be empty of topics.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.content import Topic, TopicTranslation

# (slug, IBO syllabus weight, colour, {locale: name})
IBO_TOPICS: list[tuple[str, float, str, dict[str, str]]] = [
    ("biosystematics", 5.0, "#9059ff",
     {"en": "Biosystematics", "ru": "Биосистематика", "ky": "Биосистематика"}),
    ("cell-molecular-biology", 20.0, "#1865f2",
     {"en": "Cell and Molecular Biology", "ru": "Клеточная и молекулярная биология",
      "ky": "Клетка жана молекулалык биология"}),
    ("ecology", 10.0, "#0d923f",
     {"en": "Ecology", "ru": "Экология", "ky": "Экология"}),
    ("ethology", 5.0, "#f5a623",
     {"en": "Ethology", "ru": "Этология", "ky": "Этология"}),
    ("genetics-evolution", 20.0, "#00a5a2",
     {"en": "Genetics and Evolution", "ru": "Генетика и эволюция",
      "ky": "Генетика жана эволюция"}),
    ("plant-anatomy-physiology", 15.0, "#5a8f29",
     {"en": "Plant Anatomy and Physiology", "ru": "Анатомия и физиология растений",
      "ky": "Өсүмдүктөрдүн анатомиясы жана физиологиясы"}),
    ("animal-anatomy-physiology", 25.0, "#d92916",
     {"en": "Animal Anatomy and Physiology", "ru": "Анатомия и физиология животных",
      "ky": "Жаныбарлардын анатомиясы жана физиологиясы"}),
]


def ensure_topics(db: Session) -> dict[str, Topic]:
    """Create any missing topics and translations. Idempotent; edits are kept."""
    topics: dict[str, Topic] = {}
    for order, (slug, weight, color, names) in enumerate(IBO_TOPICS):
        topic = db.query(Topic).filter(Topic.slug == slug).one_or_none()
        if topic is None:
            topic = Topic(slug=slug, ibo_weight=weight, color=color, order=order)
            db.add(topic)
            db.flush()
        existing = {tr.locale for tr in topic.translations}
        for locale, name in names.items():
            if locale not in existing:
                db.add(TopicTranslation(topic_id=topic.id, locale=locale, name=name))
        topics[slug] = topic
    db.flush()
    return topics
