"""pgvector through the ORM: write lists, read them back, and order by cosine distance."""

from __future__ import annotations

from sqlalchemy import select

from app.ai.embeddings import embed
from app.models import Exercise


async def test_orm_vector_roundtrip_and_similarity(db):
    rows = [
        ("knee-friendly-squat", "Box squat", "squat to a box, knee friendly, controlled depth"),
        ("push-up", "Push-up", "chest and triceps bodyweight press"),
        ("plank", "Plank", "core isometric hold on forearms"),
    ]
    for slug, name, desc in rows:
        db.add(Exercise(slug=slug, name=name, category="strength", description=desc, goals=[], muscles=[], cues=[],
                        contraindications=[], embedding=embed(f"{name} {desc}")))
    await db.commit()
    stored = (await db.execute(select(Exercise).where(Exercise.slug == "push-up"))).scalar_one()
    assert len(list(stored.embedding)) == 384
    query = embed("squats that are easy on the knees")
    nearest = (await db.execute(
        select(Exercise.slug).order_by(Exercise.embedding.cosine_distance(query)).limit(1)
    )).scalar_one()
    assert nearest == "knee-friendly-squat"
