"""Vision AI for the wellness / stress check-in (optional selfie).

The photo is analysed IN MEMORY only: callers pass the (re-encoded) bytes, nothing is written to disk and
nothing identifying is requested from the model. Output is advice that can nudge the questionnaire result by
at most one level (see ``app.services.health.combine_stress``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.ai.health import PROFILE_PROMPT_CHARS, VISION_TIMEOUT_SECONDS, _clip, _complete

DEFAULT_DISCLAIMER = (
    "This is an automated look at visible cues only - not a medical assessment. Lighting, camera and "
    "make-up can change how tired anyone looks."
)

FatigueLevel = Literal["low", "moderate", "high"]


def _short_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value if isinstance(value, (list, tuple)) else []:
        text = _clip(item, 160)
        if text:
            out.append(text)
    return out[:6]


class FaceAnalysis(BaseModel):
    observations: list[str] = Field(default_factory=list)
    fatigue_signs: FatigueLevel = "low"
    stress_indicators: list[str] = Field(default_factory=list)
    disclaimer: str = DEFAULT_DISCLAIMER

    @field_validator("fatigue_signs", mode="before")
    @classmethod
    def _fatigue(cls, value: Any) -> str:
        text = str(value or "low").strip().lower()
        if text in ("low", "moderate", "high"):
            return text
        if text in ("medium", "mid", "some", "mild-moderate"):
            return "moderate"
        if text in ("severe", "very high", "strong"):
            return "high"
        return "low"

    @field_validator("observations", "stress_indicators", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _short_list(value)

    @field_validator("disclaimer", mode="before")
    @classmethod
    def _disclaimer(cls, value: Any) -> str:
        return _clip(value, 300) or DEFAULT_DISCLAIMER


FACE_SYSTEM = (
    "You are a wellness assistant. You look at ONE selfie only to describe visible, general signs of tiredness "
    "or tension (for example eye redness, dark circles, drooping eyelids, a tense jaw or brow). Never identify "
    "the person and never guess identity, age, gender, ethnicity, attractiveness, weight or body shape. Never "
    "diagnose. If the image does not show a face clearly, say so in observations and use fatigue_signs 'low'."
)
FACE_PROMPT = (
    "Describe up to 4 short observations, rate visible fatigue_signs as low, moderate or high, list any visible "
    "stress_indicators (short phrases) and add a one-sentence disclaimer that this is not a medical assessment."
)


async def analyse_face(image: bytes, *, minor: bool = False, profile: str = "") -> tuple[FaceAnalysis, str]:
    """``profile``: the member's saved profile block (app.ai.member_context) - conditions and medications that can
    explain visible tiredness (thyroid, anaemia, a sedating medicine). Data only: the model still never diagnoses."""
    system = FACE_SYSTEM + (" The person is a teenager: keep the wording gentle and encouraging." if minor else "")
    prompt = FACE_PROMPT
    if profile:
        prompt = (f"{profile[:PROFILE_PROMPT_CHARS + 20]}\nUse it only to word your observations kindly - never "
                  "diagnose and never mention a condition the photo cannot show.\n\n" + FACE_PROMPT)
    res = await _complete(
        feature="wellness_face", system=system, prompt=prompt, schema=FaceAnalysis, images=[image],
        max_tokens=400, temperature=0.2, timeout=VISION_TIMEOUT_SECONDS,
    )
    return res.data, res.provider
