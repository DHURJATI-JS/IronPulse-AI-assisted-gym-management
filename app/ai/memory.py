"""Memory from conversation: "I have asthma", "my left knee hurts on squats", "I only do yoga and swimming", "I can train
3 times a week", "I am allergic to whey" (docs/ai-guard-and-queue.md section 13).

When a member states a durable fact about themselves in the assistant or the live coach, the answer carries a
proposal to remember it - nothing is stored until the member confirms (``confirm``), and every saved item can be undone
(``undo``) or forgotten later ("forget my asthma" in a chat, or the "What the AI knows about you" page) - removed
everywhere at once: the next prompt, the safety rules and the assistant's view of past chats no longer contain it.

* Facts come from deterministic rules (``extract_facts``: the common phrasings, so it works with every provider - the
  simulated one, an offline AI, a full queue) and, with a real model, from the assistant's ``remember`` list
  (``facts_from_ai``): accepted only when grounded in the member's own words, about themselves, from an allow-list of
  fields, and never when it looks like an instruction to the AI.
* Each fact maps to its place: a health condition / injury / medication / allergy (``health_conditions``, safety tag
  from the shared normaliser ``intake.condition_tag``), a fitness-profile list or field (``app.services.fitness_profile``)
  or the experience level (``customer_profiles.fitness_level``).
* Caps hold under concurrency: a health item is added behind the member's advisory lock (``health.add_condition``, at
  most 50 - the 51st is refused with a friendly message), a fitness-profile change behind the row lock.
* Proposals and undo tickets are signed, member-bound and short-lived tokens (``TOKEN_MAX_AGE``); a token's content is
  validated again when it is used, exactly like typed input.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.member_context import MemberContext, clean, load_member_context
from app.config import get_settings
from app.models import CustomerProfile, HealthCondition, User
from app.models.enums import ConditionKind, FitnessLevel
from app.services import fitness_profile as fp
from app.services.conditions import TAG_LABELS

log = logging.getLogger("ironpulse.ai.memory")

MAX_PROPOSALS = 5  # facts proposed for one message
FORGET_MAX = 10  # items one "forget ..." removes at most
TOKEN_MAX_AGE = 24 * 3600  # a proposal / undo ticket stays usable for a day
TOKEN_MAX_CHARS = 8000
HEALTH_FIELDS: dict[str, ConditionKind] = {
    "condition": ConditionKind.CONDITION, "injury": ConditionKind.INJURY,
    "medication": ConditionKind.MEDICATION, "allergy": ConditionKind.ALLERGY,
}
LIST_FIELD_OF = {"likes": "likes", "avoids": "avoids", "goals": "goals", "equipment": "equipment"}
SCALAR_FIELDS = ("sessions_per_week", "session_minutes", "preferred_time")
FIELDS = (*HEALTH_FIELDS, *LIST_FIELD_OF, *SCALAR_FIELDS, "level")
FIELD_TITLES = {
    "condition": "Health condition", "injury": "Injury", "medication": "Medication", "allergy": "Allergy",
    "likes": "Exercise you enjoy", "avoids": "Exercise you avoid", "goals": "Goal", "equipment": "Equipment",
    "sessions_per_week": "Training schedule", "session_minutes": "Session length", "preferred_time": "Preferred time",
    "level": "Experience level",
}
HEALTH_NAME_MAX = 80  # characters of a health item proposed from a chat
INJURY_TAGS = frozenset({"knee_injury", "lower_back", "shoulder_injury", "wrist_injury", "ankle_injury", "neck_injury"})
PANEL = "/me/health/profile/ai"


# ====================================================================================== facts
@dataclass(frozen=True, slots=True)
class Fact:
    field: str
    value: str

    @property
    def key(self) -> tuple[str, str]:
        return self.field, re.sub(r"[^a-z0-9]+", " ", self.value.lower()).strip()

    def tag(self) -> str | None:
        if self.field not in HEALTH_FIELDS:
            return None
        from app.services.intake import condition_tag

        return condition_tag(self.value, HEALTH_FIELDS[self.field])

    @property
    def label(self) -> str:
        if self.field in LIST_FIELD_OF:
            return fp.short_label(self.field, self.value)
        if self.field == "sessions_per_week":
            return f"{self.value} session{'s' if self.value != '1' else ''} a week"
        if self.field == "session_minutes":
            return f"About {self.value} minutes a session"
        if self.field == "preferred_time":
            return f"Trains: {fp.TIME_LABELS.get(self.value, self.value).lower()}"
        if self.field == "level":
            return self.value.capitalize()
        return self.value

    @property
    def detail(self) -> str:
        title = FIELD_TITLES.get(self.field, "Detail")
        tag = self.tag()
        if tag:
            return f"{title} · safety tag: {TAG_LABELS.get(tag, tag)}"
        return title


# ====================================================================================== deterministic extraction
_APOS = re.compile(r"[’‘`´]")
_SENTENCES = re.compile(r"(?<=[.!?;\n])\s+|\s+(?:but|although|though|however|whereas)\s+")
_QUESTION_START = re.compile(r"^(?:do|does|did|can|could|should|would|will|is|are|am|was|were|what|how|why|when|where|"
                             r"which|who|whom|whose|may|might|shall|isn't|aren't|don't|doesn't)\b")
_HYPOTHETICAL = re.compile(r"\b(?:if|what if|suppose|supposing|imagine|pretend|hypothetically|let's say|lets say|"
                           r"in case|unless|whether)\b")
_COMMAND_START = re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:remind|add|create|make|put|message|msg|tell|text|"
                            r"ask|ping|notify|send|open|go to|take me|show|list|mark|complete|finish|tick|check off)\b")
_NEGATION = re.compile(r"\b(?:don't|dont|do not|doesn't|does not|didn't|did not|never|no longer|not|isn't|aren't|"
                       r"wasn't|haven't|have not|hasn't|won't|can't|cannot|no|without|nor|neither|anymore|any more)\b")
_REL_TIME = re.compile(r"\bin\s+(?:\d+|an|a|half an|one|two|ten|twenty|thirty)\s*(?:minutes?|mins?|hours?|hrs?)\b|\bremind\b")

_NUM_WORDS = {"once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "a": 1}

# health ------------------------------------------------------------------------------------
_ALLERGIC_TO = re.compile(r"\b(?:i am|i'm|im|i've (?:always )?been|i have (?:always )?been)\s+(?:(?:very|severely|really|"
                          r"quite|slightly|mildly|a bit|a little|seriously|badly)\s+)?allergic\s+to\s+(?P<x>[a-z][a-z0-9 ,&/'-]{1,80})")
_X_ALLERGY = re.compile(r"\bi(?: have got| have|'ve got| ve got|'ve)\s+(?:a |an |some )?(?:(?:severe|bad|mild|serious|slight)\s+)?"
                        r"(?P<x>[a-z][a-z -]{1,40}?)\s+(?P<w>allergy|allergies|intolerance)\b")
_INTOLERANT = re.compile(r"\bi(?: am|'m|m)\s+(?:(?:very|quite|slightly|a bit|severely)\s+)?"
                         r"(?P<x>lactose|gluten|dairy|fructose|histamine)[ -]intolerant\b")
_MED_TAKE = re.compile(r"\bi(?: take| am taking|'m taking| m taking| use| am using|'m using| m using| am on|'m on|"
                       r"m on| have been taking|'ve been taking| need to take| also take| still take)\s+"
                       r"(?P<x>[a-z][a-z0-9 ,&/'-]{1,80})")
_MED_WORDS = re.compile(r"\b(?:insulin|metformin|inhalers?|salbutamol|asthalin|levothyroxine|thyroxine|thyronorm|"
                        r"amlodipine|telmisartan|losartan|atorvastatin|rosuvastatin|statins?|aspirin|ecosprin|warfarin|"
                        r"blood thinners?|beta[- ]blockers?|antihistamines?|cetirizine|montelukast|budesonide|"
                        r"prednisolone|prednisone|steroids?|epipen|epi-pen|antidepressants?|sertraline|fluoxetine|"
                        r"lithium|anticoagulants?|clopidogrel|ramipril|lisinopril|enalapril|bisoprolol|metoprolol|"
                        r"atenolol|propranolol|glimepiride|gliclazide|sitagliptin|semaglutide|ozempic|methotrexate|"
                        r"medications?|medicines?|meds|pills|tablets|injections|eye drops|blood pressure tablets)\b")
_SUPPLEMENTS = re.compile(r"\b(?:protein|whey|creatine|pre[- ]?workout|supplements?|bcaas?|coffee|caffeine|vitamins?|"
                          r"fish oil|multivitamins?|diet|break|rest day|day off|walk|shower|class|bus|train|metro)\b")
_HAVE = re.compile(r"\bi(?: have got| have been diagnosed with| have| also have| still have|'ve got| ve got|"
                   r"'ve been diagnosed with|'ve| suffer from| am suffering from|'m suffering from| m suffering from|"
                   r" was diagnosed with| got diagnosed with| live with| am living with|'m living with| deal with|"
                   r" struggle with)\s+(?:had\s+)?(?P<x>[a-z][a-z0-9 ,'/-]{1,80})")
_I_AM_ADJ = re.compile(r"\bi(?: am|'m|m)\s+(?:(?:also|now|currently|a|an)\s+)*(?P<x>asthmatic|diabetic|pre-?diabetic|"
                       r"epileptic|pregnant|anaemic|anemic|hypertensive|arthritic|obese|hypothyroid|hyperthyroid)\b")
_ADJ_NAMES = {"asthmatic": "Asthma", "diabetic": "Diabetes", "prediabetic": "Prediabetes", "pre-diabetic": "Prediabetes",
              "epileptic": "Epilepsy", "pregnant": "Pregnancy", "anaemic": "Anaemia", "anemic": "Anaemia",
              "hypertensive": "High blood pressure", "arthritic": "Arthritis", "obese": "Obesity",
              "hypothyroid": "Hypothyroidism", "hyperthyroid": "Hyperthyroidism"}
_HEALTH_NOUN = re.compile(r"\b(?:disease|disorder|syndrome|deficiency|infection|injury|injuries|injured|pain|ache|"
                          r"arthritis|cancer|tumou?r|hernia|fracture|sprain|strain|tear|torn|surgery|operation|allergy|"
                          r"intolerance|blood pressure|blood sugar|cholesterol|thyroid|asthma|migraines?|vertigo|scoliosis|"
                          r"sciatica|tendinitis|tendonitis|bursitis|fasciitis|insomnia|apnoea|apnea|depression|anxiety|"
                          r"adhd|pcos|pcod|endometriosis|gerd|reflux|ibs|crohn's|crohns|colitis|celiac|coeliac|anaemia|"
                          r"anemia|epilepsy|diabetes|hypertension|hypotension|obesity|osteoporosis|gout|herniated|"
                          r"slipped disc|slip disc|bad back|bad knee|weak knee|bad knees|weak knees|heart condition|"
                          r"heart problem|condition)\b|itis\b")
_NOT_HEALTH = re.compile(r"^(?:a |an |the |some |many |few |lots of |a lot of |my |two |three )?(?:question|questions|doubt|"
                         r"doubts|query|queries|appointment|session|sessions|class|classes|meeting|call|plan|plans|idea|"
                         r"feeling|time|day|week|trainer|coach|membership|gym|friend|friends|dog|cat|kid|kids|son|"
                         r"daughter|wife|husband|job|car|bike|house|goal|goals|target|deadline|exam|task|tasks|"
                         r"reminder|workout|workouts|routine|problem with the app|issue with the app|been|to|no|nothing|"
                         r"none|done|finished|completed)\b|^(?:injured|hurt|tore|torn|sprained|strained|broke|broken|"
                         r"fractured|dislocated|pulled|tweaked|twisted|damaged|had surgery on) my\b")
_CLAUSE_CUT = re.compile(r"\s+(?:and i|and my|so|which|that|because|since|but|for|as|and it|and that|and also)\b|,|\s+-\s+")
_INJURY_WORDS = re.compile(r"\b(?:injur\w*|sprain\w*|strain\w*|tear|torn|fractur\w*|broken|dislocat\w*|pulled|pain|"
                           r"aches?|sore|hurts?|bad|weak|dodgy)\b")
_BODY = (r"(?:lower back|upper back|back|knees?|shoulders?|wrists?|ankles?|neck|hips?|elbows?|hamstrings?|groin|"
         r"achilles|acl|mcl|meniscus|rotator cuff|feet|foot|shins?|calf|calves|spine)")
_MY_PART_HURTS = re.compile(
    rf"\bmy (?P<side>(?:left|right|both) )?(?P<part>{_BODY})\s+(?P<verb>hurts?|aches?|"
    r"is (?:sore|painful|weak|bad|injured|stiff|unstable|dodgy|tender|not great)|gets (?:sore|painful|stiff)|"
    r"clicks|pops|gives (?:way|out)|plays up|flares up|swells(?: up)?|locks(?: up)?|has been (?:hurting|sore|painful))"
    r"(?P<when>\s+(?:on|when|during|after|with|while|whenever|every time|doing)\s+[a-z0-9 '-]{2,40}|"
    r"\s+(?:sometimes|often|always|a lot|regularly|constantly|every day|all the time)\b)?")
_CHRONIC_VERBS = re.compile(r"^(?:is (?:weak|bad|injured|unstable|dodgy|not great)|gives (?:way|out)|plays up|flares up|"
                            r"has been (?:hurting|sore|painful)|locks(?: up)?|clicks|pops)$")
_I_HURT_MY = re.compile(
    r"\bi(?: have| had|'ve| ve)? (?P<verb>injured|hurt|tore|torn|sprained|strained|broke|broken|fractured|dislocated|pulled|"
    rf"tweaked|twisted|damaged|had surgery on|had an operation on)\s+my (?P<side>(?:left|right) )?(?P<part>{_BODY})\b"
    r"(?P<when>\s+(?:last|in|a few|two|three|years?|months?|weeks?|before|playing|while)\b[a-z0-9 '-]{0,30})?")
_HURT_NAMES = {"injured": "Injured", "hurt": "Hurt", "tore": "Torn", "torn": "Torn", "sprained": "Sprained",
               "strained": "Strained", "broke": "Broken", "broken": "Broken", "fractured": "Fractured",
               "dislocated": "Dislocated", "pulled": "Pulled", "tweaked": "Tweaked", "twisted": "Twisted",
               "damaged": "Damaged", "had surgery on": "Surgery on", "had an operation on": "Operation on"}

# preferences -------------------------------------------------------------------------------
_AVOID = re.compile(r"\bi (?:really |just |absolutely |kind of |kinda |totally )?(?:hate|dislike|detest|can't stand|"
                    r"cannot stand|can not stand|don't like|dont like|do not like|don't enjoy|do not enjoy|avoid|won't do|"
                    r"will not do|can't do|cant do|cannot do|don't do|do not do|never do|am not into|'m not into|"
                    r"m not into|am not a fan of|'m not a fan of|m not a fan of|refuse to do)\s+(?P<x>[^.;!?]{2,120})")
_NOT_INTO = re.compile(r"\bi(?: am|'m| m) not (?:(?:really|very|a big|big)\s+)*(?:into|a fan of|fan of)\s+"
                       r"(?P<x>[^.;!?]{2,120})")
_NO_X = re.compile(r"^(?:no|not)\s+(?P<x>[a-z -]{2,40}?)(?:\s+(?:please|for me|thanks|thank you))?$")
_LIKE = re.compile(r"\bi (?:only |just |mostly |mainly |usually |really |also |absolutely |do |normally |still )*"
                   r"(?:do|like|love|enjoy|prefer|go)\s+(?P<x>[^.;!?]{2,120})")
_INTO = re.compile(r"\bi(?: am|'m| m) (?:(?:really|very|totally|so|a big|a huge|big|huge|such a)\s+)*"
                   r"(?:into|a fan of|fan of)\s+(?P<x>[^.;!?]{2,120})")
_FAVOURITE = re.compile(r"\bmy fav(?:ou?rite)? (?:workouts?|exercises?|sports?|activit(?:y|ies)|training|thing to do) "
                        r"(?:is|are)\s+(?P<x>[^.;!?]{2,80})")
_NUM = r"(?P<n>\d{1,2}|once|twice|thrice|one|two|three|four|five|six|seven)"
_PER_WEEK = re.compile(_NUM + r"\s*(?:x|times|time|days|day|sessions|session|workouts|days of the week)?\s*"
                       r"(?:a|per|each|every|in a|/)\s*week\b")
_DAILY = re.compile(r"\bi (?:can )?(?:train|work out|workout|exercise|go to the gym|lift|run) (?:every day|daily|"
                    r"every single day)\b")
_TRAIN_WORDS = re.compile(r"\b(?:train|training|work out|workout|workouts|exercise|exercising|gym|lift|lifting|run|"
                          r"running|session|sessions|come in|make it|manage|do|do this|yoga|swim|swimming|cycle|"
                          r"cycling|hiit|pilates|crossfit|cardio|weights|classes|class)\b")
_MINUTES = re.compile(r"(?P<n>\d{2,3})\s*(?:-\s*\d{2,3}\s*)?(?:minutes?|mins?)\b|(?P<h>an hour and a half|"
                      r"one and a half hours|1\.5 hours|half an hour|an hour|one hour|1 hour|two hours|2 hours)")
_MINUTES_CONTEXT = re.compile(r"\b(?:have|got|spare|only|about|around|usually|can|max|maximum|up to|long|last|lasts|"
                              r"takes?|are|is|keep)\b")
_TIME = re.compile(r"\bi (?:prefer to |like to |usually |normally |can only |only |mostly |always |tend to |will |"
                   r"can |love to )?(?:train|work out|workout|exercise|go to the gym|hit the gym|go for (?:a )?runs?|"
                   r"run|do my workouts?|lift)(?:\s+[a-z0-9]+){0,4}?\s+(?:in the |at |during the |during |after |"
                   r"before |early in the |"
                   r"late in the |on )?(?P<t>early mornings?|mornings?|lunch ?time|lunch|midday|noon|afternoons?|"
                   r"evenings?|nights?|late at night|after work|before work|after office|before office)\b")
_PREFER_TIME = re.compile(r"\bi (?:prefer|like|love)\s+(?P<t>early morning|morning|lunchtime|midday|afternoon|evening|"
                          r"night|late night)\s+(?:workouts?|sessions?|training|runs?|gym)\b")
_PERSON = re.compile(r"\bi(?: am|'m|m) (?:a|an) (?P<t>morning|early|evening|night) (?:person|bird|owl)\b")
_TIME_MAP = {"early morning": "early_morning", "early mornings": "early_morning", "before work": "early_morning",
             "before office": "early_morning", "early": "early_morning", "morning": "morning", "mornings": "morning",
             "lunch": "midday", "lunchtime": "midday", "lunch time": "midday", "midday": "midday", "noon": "midday",
             "afternoon": "afternoon", "afternoons": "afternoon", "evening": "evening", "evenings": "evening",
             "after work": "evening", "after office": "evening", "night": "night", "nights": "night",
             "late at night": "night", "late night": "night"}
_LEVEL = re.compile(r"\bi(?: am|'m|m)\s+(?:(?:a|an|still|just|a complete|a total|an absolute|a pretty|fairly|quite|"
                    r"pretty|very|complete|total|absolute)\s+)*(?P<l>beginner|newbie|novice|intermediate|advanced|"
                    r"experienced)\b")
_NEW_TO = re.compile(r"\bi(?: am|'m|m) (?:completely |totally |pretty |quite |fairly |very )?new to (?:the gym|working out|"
                     r"training|fitness|exercise|exercising|lifting|weights|all this|this)\b")
_NEVER_TRAINED = re.compile(r"\bi(?: have|'ve|ve) never (?:really )?(?:worked out|trained|exercised|been to a gym|lifted)\b")
_LEVELS = {"beginner": "beginner", "newbie": "beginner", "novice": "beginner", "intermediate": "intermediate",
           "advanced": "advanced", "experienced": "advanced"}
_GOAL_LEAD = re.compile(r"\b(?P<lead>my (?:main |primary |only |big |biggest |real )?goals? (?:is|are)(?: to)?|i want to|"
                        r"i wanna|i'd like to|i would like to|i'm trying to|i am trying to|im trying to|i need to|"
                        r"i'm aiming to|i am aiming to|i aim to|i hope to|i'm working on|i am working on|"
                        r"i'm training for|i am training for|im training for|i'm preparing for|i am preparing for)"
                        r"\s+(?P<x>[^.;!?]{2,100})")
_EVENT = re.compile(r"\b(?P<v>run|do|complete|finish|train for)\s+(?:a |an |my first |the )?(?P<e>marathon|half marathon|"
                    r"half-marathon|ultramarathon|ultra|5k|10k|21k|42k|triathlon|trek|duathlon|ironman)\b")
_HOME_KIT = re.compile(r"\bi(?: have got| have| own|'ve got| ve got|'ve| bought| use| only have| just have| got)\s+(?P<x>[^.;!?]{2,120}?)"
                       r"\s+(?:at home|in my (?:home|room|garage|flat|apartment|house)|at my (?:place|house|flat))\b")
_KIT_IS = re.compile(r"\bmy (?:home )?(?:equipment|kit|home gym|gear) (?:is|are|includes|has|consists of)\s+"
                     r"(?P<x>[^.;!?]{2,120})")
_TRAIN_WITH = re.compile(r"\bi (?:train|work out|workout|exercise) (?:at home )?with (?:only |just )?(?P<x>[^.;!?]{2,100})")
_NO_KIT = re.compile(r"\bi (?:have no|don't have any|do not have any|have zero|don't own any|do not own any) "
                     r"(?:gym )?equipment\b|\bno equipment at home\b")
_GYM_ACCESS = re.compile(r"\bi (?:have|got) (?:a )?(?:gym membership|access to (?:a |the )?(?:full )?gym)\b")
_FORGET = re.compile(r"^(?:hey |ok |okay |please |pls |can you |could you |would you |i want you to |i'd like you to )*"
                     r"(?:forget|stop remembering|remove|delete|erase|unlearn)\s+(?:about\s+)?(?:the fact\s+)?(?:that\s+)?"
                     r"(?P<x>.{2,120}?)(?:\s+(?:from|in) (?:my|your) (?:profile|memory|records|notes|list))?"
                     r"(?:\s+please)?[.!]*$")
_FORGET_SKIP = re.compile(r"^(?:it|that|this|about it|everything|all|all of it|what i said|my last message|the last "
                          r"message|my tasks?|my reminders?|the task|the reminder|task\b|reminder\b)")
_STOP_WORDS = frozenset({"my", "the", "a", "an", "that", "i", "have", "has", "am", "im", "i'm", "about", "is", "are",
                         "with", "of", "to", "do", "like", "from", "any", "some", "me", "mine", "on", "in", "and", "or",
                         "also", "please", "can", "train", "times", "time", "week", "allergic", "take", "taking"})


def _norm(text: str) -> str:
    t = _APOS.sub("'", str(text or "")).lower()
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> list[str]:
    out = []
    for s in _SENTENCES.split(_norm(text)):
        s = s.strip(" ,")
        if s:
            out.append(s)
    return out[:20]


def _is_question(s: str) -> bool:
    return s.endswith("?") or bool(_QUESTION_START.match(s))


def _negated_before(s: str, end: int) -> bool:
    return bool(_NEGATION.search(s[:end]))


def _cap(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.-'")
    t = re.sub(r"(?<![a-z0-9'])i(?![a-z0-9'])", "I", t)
    t = re.sub(r"\b(acl|mcl|pcos|pcod|adhd|ibs|gerd|copd|ms)\b", lambda m: m.group(1).upper(), t)
    return t[:1].upper() + t[1:] if t else t


def _vocab_hits(field_name: str, text: str) -> list[str]:
    """Vocabulary slugs named in ``text`` (whole words, longest phrase first), in the order they appear."""
    vocab = fp.VOCAB[field_name]
    phrases = sorted(((p, slug) for slug, (_l, ps) in vocab.items() for p in ps), key=lambda x: -len(x[0]))
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for phrase, slug in phrases:
        for m in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((m.start(), slug))
    out: list[str] = []
    for _pos, slug in sorted(found):
        if slug not in out:
            out.append(slug)
    return out


_NEXT_CLAUSE = re.compile(r"\s+(?:and|but|or|so|plus)\s+(?:i|i'm|im|i've|my|it|that|we|they|he|she)\b|"
                          r"\s+(?:but|so|which|because|since|when|while|though|although|whereas|except)\b|"
                          r"[,;]\s*(?:and\s+|but\s+)?(?:i|i'm|im|my|it)\b")


def _clause(x: str) -> str:
    """The words up to the next clause ("yoga and I hate running" -> "yoga"): a list never reaches into another
    statement."""
    m = _NEXT_CLAUSE.search(x)
    return x[: m.start()] if m else x


def _items(x: str, limit: int = 3, trim: Any = None) -> list[str]:
    parts = re.split(r"\s*(?:,|&|/|\band\b|\bor\b|\bplus\b)\s*", _clause(x))
    out = []
    for p in parts:
        p = re.sub(r"^(?:the|some|any|all|most|certain|a|an)\s+", "", p.strip(" .'-"))
        p = re.sub(r"\s+(?:too|as well|also|though|really|very much)$", "", p)
        if trim is not None:
            p = trim(p)
        if 2 <= len(p) <= 40 and len(p.split()) <= 4 and re.search(r"[a-z]", p):
            out.append(p)
    return out[:limit]


# A medication is saved as its name and an optional dose ("Metformin 500 mg") - never how often, when or why it is taken
# ("every morning", "at night", "before breakfast", "for my diabetes", "on an empty stomach") nor a stray token.
_MED_LEAD = re.compile(r"^(?:(?:only|just|also|still|my|daily|regularly|prescribed)\s+)+")
_MED_STOP = re.compile(r"(?:every|each|daily|nightly|weekly|monthly|twice|once|thrice|before|after|when|whenever|while|"
                       r"with|without|for|since|at|in|on|as|per|a|an|the|to|from|until|till|because|if|then|now|"
                       r"currently|also|too|regularly|usually|sometimes|occasionally|mornings?|evenings?|nights?|"
                       r"bedtime|breakfast|lunch|dinner|meals?|days?|weeks?|months?|years?|prescribed|by|via|only|"
                       r"just|times?|x|everyday|anymore|again|still)")
_DOSE_UNIT = re.compile(r"(?:mg|mcg|µg|ug|g|gm|ml|iu|units?|puffs?|drops?|tabs?|tablets?|capsules?)")
_DOSE_GLUED = re.compile(r"\d+(?:\.\d+)?(?:mg|mcg|µg|ug|g|gm|ml|iu)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _med_name(item: str) -> str:
    """'metformin every morning' -> 'metformin', 'metformin 500 mg twice a day' -> 'metformin 500 mg', 'thyroid
    medication daily' -> 'thyroid medication': the words up to the first word about frequency, time or purpose, a
    number that is not a dose, or a token that is not a word (an id, a code)."""
    words = _MED_LEAD.sub("", item.strip()).split()
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if out and _DOSE_GLUED.fullmatch(w):
            out.append(w)
        elif out and _NUMBER.fullmatch(w) and i + 1 < len(words) and _DOSE_UNIT.fullmatch(words[i + 1]):
            out += [w, words[i + 1]]
            i += 1
        elif _MED_STOP.fullmatch(w) or not re.fullmatch(r"[a-z][a-z'-]*", w):
            break
        else:
            out.append(w)
        i += 1
    return " ".join(out)


def _health_key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split())


def _health_name_ok(name: str) -> bool:
    """A name that may be saved from a chat: letters, at most HEALTH_NAME_MAX characters, no markup - and nothing that
    reads like an instruction to the AI (``guard.instruction_marker``, the check every prompt applies to saved text)."""
    from app.ai.guard import instruction_marker

    return (2 <= len(name) <= HEALTH_NAME_MAX and bool(re.search(r"[a-z]", name.lower()))
            and instruction_marker(name) is None and not re.search(r"[<>{}\[\]\\|^`=@#$%*_\"]", name))


def _health_fact(x: str, *, injury_hint: bool = False) -> Fact | None:
    """A condition / injury from the words after "I have ...", or None when they don't name one."""
    from app.services.intake import condition_tag

    cut = _CLAUSE_CUT.search(x)
    head = x[: cut.start()] if cut else x
    when = re.search(r"\s+(?:when|on|during|after|while|whenever|if|every time)\s+[a-z0-9 '-]{2,30}$", head)
    base = head[: when.start()] if when else head
    base = re.sub(r"^(?:a |an |the |some |really |very |quite |pretty )+", "", base.strip()).strip()
    base = re.sub(r"\s+(?:now|currently|too|as well|also|since childhood|for years|for a while)$", "", base)
    if not base or _NOT_HEALTH.match(base) or len(base.split()) > 7 or re.fullmatch(rf"(?:left |right |both )?{_BODY}", base):
        return None
    tag = condition_tag(base, ConditionKind.CONDITION)
    if tag is None and not _HEALTH_NOUN.search(base):
        return None
    injury = injury_hint or tag in INJURY_TAGS or bool(_INJURY_WORDS.search(base) and re.search(_BODY, base))
    name = _cap(f"{base}{when.group(0)}" if injury and when else base)
    if not _health_name_ok(name):
        return None
    return Fact("injury" if injury else "condition", name)


def _sentence_facts(s: str) -> list[Fact]:
    facts: list[Fact] = []
    if _is_question(s) or _HYPOTHETICAL.search(s) or _COMMAND_START.match(s):
        return facts
    # ---------------------------------------------------------------- negative statements first (avoid / no kit)
    m = _AVOID.search(s)
    if m:
        facts += [Fact("avoids", slug) for slug in _vocab_hits("avoids", _clause(m.group("x")))]
    for rx in (_NOT_INTO, _NO_X):
        m = rx.search(s) if rx is _NOT_INTO else rx.match(s)
        if m:
            facts += [Fact("avoids", slug) for slug in _vocab_hits("avoids", _clause(m.group("x")))]
    if _NO_KIT.search(s):
        facts.append(Fact("equipment", "none"))
    if _NEVER_TRAINED.search(s):
        facts.append(Fact("level", "beginner"))
    # ---------------------------------------------------------------- positive statements (never negated)
    for rx, kind in ((_ALLERGIC_TO, "to"), (_X_ALLERGY, "x"), (_INTOLERANT, "intolerant")):
        for m in rx.finditer(s):
            if _negated_before(s, m.start("x")):
                continue
            if kind == "to":
                names = [_cap(i) for i in _items(m.group("x"))]
            elif kind == "x":
                names = [_cap(f"{m.group('x')} {m.group('w').replace('allergies', 'allergy')}")]
            else:
                names = [_cap(f"{m.group('x')} intolerance")]
            facts += [Fact("allergy", n) for n in names if _health_name_ok(n)]
    for m in _MED_TAKE.finditer(s):
        if _negated_before(s, m.start("x")):
            continue
        for item in _items(m.group("x"), trim=_med_name):
            if _MED_WORDS.search(item) and not (_SUPPLEMENTS.search(item) and not _MED_WORDS.search(item)):
                name = _cap(item)
                if _health_name_ok(name):
                    facts.append(Fact("medication", name))
    m = _I_AM_ADJ.search(s)
    if m and not _negated_before(s, m.start("x")):
        facts.append(Fact("condition", _ADJ_NAMES[m.group("x")]))
    for m in _HAVE.finditer(s):
        if _negated_before(s, m.start("x")) or re.match(r"(?:an? |some |a severe |a mild )?[a-z -]*\b(?:allergy|"
                                                         r"allergies|intolerance)\b", m.group("x")):
            continue  # "I have a peanut allergy" is the allergy rule's
        x = m.group("x")
        parts = [p for p in re.split(r"\s+and\s+(?!i\b|my\b|it\b|that\b)|\s*,\s*(?:and\s+)?|\s*&\s*", x) if p.strip()]
        several = [_health_fact(p) for p in parts] if len(parts) > 1 else []
        if several and all(several):
            facts += [f for f in several if f is not None]
            continue
        fact = _health_fact(x)
        if fact is not None:
            facts.append(fact)
    for m in _MY_PART_HURTS.finditer(s):
        if _negated_before(s, m.start("verb")):
            continue
        durable = bool(m.group("when")) or bool(_CHRONIC_VERBS.match(m.group("verb")))
        if durable:
            name = _cap(m.group(0)[3:])  # without "my "
            if _health_name_ok(name):
                facts.append(Fact("injury", name))
    for m in _I_HURT_MY.finditer(s):
        if _negated_before(s, m.start("verb")):
            continue
        name = _cap(f"{_HURT_NAMES[m.group('verb')]} {m.group('side') or ''}{m.group('part')}{m.group('when') or ''}")
        if _health_name_ok(name):
            facts.append(Fact("injury", name))
    # preferences
    for rx in (_LIKE, _INTO, _FAVOURITE):
        for m in rx.finditer(s):
            if _negated_before(s, m.start("x")):
                continue
            facts += [Fact("likes", slug) for slug in _vocab_hits("likes", _clause(m.group("x")))]
    if not _REL_TIME.search(s) and (re.search(r"\bi\b|\bme\b|\bmy\b", s) or _PER_WEEK.match(s)):
        m = _PER_WEEK.search(s)
        if m and _TRAIN_WORDS.search(s) and not _negated_before(s, m.start()):
            raw = m.group("n")
            n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 0)
            if fp.SESSIONS_RANGE[0] <= n <= fp.SESSIONS_RANGE[1]:
                facts.append(Fact("sessions_per_week", str(n)))
        elif _DAILY.search(s) and not _negated_before(s, _DAILY.search(s).start()):
            facts.append(Fact("sessions_per_week", "7"))
        m = _MINUTES.search(s)
        if m and _TRAIN_WORDS.search(s) and _MINUTES_CONTEXT.search(s) and not _negated_before(s, m.start()):
            if m.group("n"):
                n = int(m.group("n"))
            else:
                n = {"an hour and a half": 90, "one and a half hours": 90, "1.5 hours": 90, "half an hour": 30,
                     "an hour": 60, "one hour": 60, "1 hour": 60, "two hours": 120, "2 hours": 120}[m.group("h")]
            if fp.MINUTES_RANGE[0] <= n <= fp.MINUTES_RANGE[1]:
                facts.append(Fact("session_minutes", str(n)))
    for rx in (_TIME, _PREFER_TIME, _PERSON):
        m = rx.search(s)
        if m and not _negated_before(s, m.start("t")):
            slot = _TIME_MAP.get(m.group("t").strip())
            if slot:
                facts.append(Fact("preferred_time", slot))
                break
    m = _LEVEL.search(s) or _NEW_TO.search(s)
    if m and not _negated_before(s, m.start()):
        facts.append(Fact("level", _LEVELS.get(m.groupdict().get("l") or "beginner", "beginner")))
    for m in _GOAL_LEAD.finditer(s):
        if _negated_before(s, m.start("x")):
            continue
        x = _clause(m.group("x"))
        hits = _vocab_hits("goals", x)
        facts += [Fact("goals", slug) for slug in hits]
        if hits:
            continue
        event = _EVENT.search(x)
        lead = m.group("lead")
        if event:
            verb = "Train for" if event.group("v") == "train for" else event.group("v").capitalize()
            facts.append(Fact("goals", f"{verb} a {event.group('e')}"))
        elif lead.startswith(("my", "i'm training", "i am training", "im training", "i'm preparing", "i am preparing")):
            words = x.split()
            if 1 <= len(words) <= 8:
                label = _cap(("Train for " + x) if "training for" in lead or "preparing for" in lead else x)
                if fp.item_error("goals", label) is None and _health_name_ok(label):
                    facts.append(Fact("goals", label))
    for rx in (_HOME_KIT, _KIT_IS, _TRAIN_WITH):
        m = rx.search(s)
        if m and not _negated_before(s, m.start("x")):
            facts += [Fact("equipment", slug) for slug in _vocab_hits("equipment", _clause(m.group("x")))]
    if _GYM_ACCESS.search(s) and not _negated_before(s, _GYM_ACCESS.search(s).start()):
        facts.append(Fact("equipment", "full_gym"))
    return facts


def _dedupe(facts: Iterable[Fact], limit: int = MAX_PROPOSALS) -> list[Fact]:
    out: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for f in facts:
        if f.key in seen:
            continue
        # one health item per wording: "my knee hurts on squats" is not also "knee pain" from another rule
        if f.field in HEALTH_FIELDS and any(o.field in HEALTH_FIELDS and o.key[1] == f.key[1] for o in out):
            continue
        seen.add(f.key)
        out.append(f)
    # scalars: the last statement wins ("3 times a week ... actually 4 times a week")
    final: list[Fact] = []
    for f in out:
        if f.field in (*SCALAR_FIELDS, "level"):
            final = [o for o in final if o.field != f.field]
        final.append(f)
    return final[:limit]


def extract_facts(text: str) -> list[Fact]:
    """Durable facts the member states about THEMSELVES in ``text`` (deterministic; at most MAX_PROPOSALS). Questions,
    hypotheticals ("if I had asthma"), negations ("I don't have asthma"), other people ("my son has asthma") and task
    commands ("remind me to take my insulin") are never facts."""
    facts: list[Fact] = []
    for s in sentences(str(text or "")[:2000]):
        try:
            facts += _sentence_facts(s)
        except Exception:  # noqa: BLE001 - a rule bug must never break the chat
            log.exception("memory: fact extraction failed on a sentence")
    return _dedupe(facts)


# ====================================================================================== the model's suggestions
def _grounded(value: str, text: str) -> bool:
    """Every significant word of ``value`` appears in the member's own message."""
    words = [w for w in re.findall(r"[a-z0-9]+", value.lower()) if len(w) >= 3 and w not in _STOP_WORDS]
    if not words:
        return False
    msg = _norm(text)
    return all(re.search(rf"(?<![a-z0-9]){re.escape(w)}", msg) for w in words)


def facts_from_ai(raw: Any, text: str) -> list[Fact]:
    """The assistant model's ``remember`` list, validated: an allow-listed field, a value grounded in the member's own
    words (never something the model inferred or read in the history), vocabulary values for lists where possible,
    in-range numbers, no instruction-like text. Anything else is ignored."""
    if not isinstance(raw, list):
        return []
    out: list[Fact] = []
    msg = _norm(text)
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field") or "").strip().lower()
        value = clean(item.get("value"), 120)
        if field_name not in FIELDS or not value:
            continue
        if field_name in HEALTH_FIELDS:
            name = _cap(value.lower())
            if _health_name_ok(name) and _grounded(name, msg):
                out.append(Fact(field_name, name))
        elif field_name in LIST_FIELD_OF:
            slug = fp.vocab_slug(field_name, value)
            if slug is not None:
                phrases = (slug.replace("_", " "), *fp.VOCAB[field_name][slug][1], fp.VOCAB[field_name][slug][0].lower())
                if any(re.search(rf"(?<![a-z0-9]){re.escape(p.lower())}(?![a-z0-9])", msg) for p in phrases):
                    out.append(Fact(field_name, slug))
            elif fp.item_error(field_name, value) is None and _grounded(value, msg) and _health_name_ok(value):
                out.append(Fact(field_name, _cap(value)))
        elif field_name in ("sessions_per_week", "session_minutes"):
            digits = re.sub(r"\D", "", value)[:3]
            if digits and re.search(rf"(?<![0-9]){digits}(?![0-9])", msg):
                n = int(digits)
                lo, hi = fp.SESSIONS_RANGE if field_name == "sessions_per_week" else fp.MINUTES_RANGE
                if lo <= n <= hi:
                    out.append(Fact(field_name, str(n)))
        elif field_name == "preferred_time":
            slot = value.lower().replace(" ", "_")
            if slot in fp.TIME_LABELS and any(w in msg for w in slot.split("_")):
                out.append(Fact(field_name, slot))
        elif field_name == "level":
            lvl = _LEVELS.get(value.lower())
            if lvl and (lvl in msg or any(k in msg for k, v in _LEVELS.items() if v == lvl)):
                out.append(Fact(field_name, lvl))
    return out


def merge(*groups: Sequence[Fact]) -> list[Fact]:
    return _dedupe(f for g in groups for f in g)


# ====================================================================================== tokens
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ironpulse-ai-memory")


def make_token(user_id: int, payload: dict[str, Any]) -> str:
    return _serializer().dumps({"u": int(user_id), **payload})


def read_token(token: Any, user_id: int) -> dict[str, Any] | None:
    """A token's payload when it is genuine, unexpired and the member's own - else None."""
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX_CHARS:
        return None
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
    except BadSignature:  # also SignatureExpired
        return None
    if not isinstance(data, dict) or data.get("u") != user_id:
        return None
    return data


# ====================================================================================== proposals
def _known(ctx: MemberContext, fact: Fact) -> bool:
    if fact.field in HEALTH_FIELDS:
        kind = HEALTH_FIELDS[fact.field].value
        key = _health_key(fact.value)
        # a medication saved with its schedule ("Metformin every morning", from before names were trimmed) already
        # covers "Metformin": never a second row for the same medicine
        return any(c.kind == kind and (_health_key(c.name) == key
                                       or (fact.field == "medication" and _health_key(_med_name(c.name.lower())) == key))
                   for c in ctx.conditions)
    if fact.field in LIST_FIELD_OF:
        return fp.contains(getattr(ctx.fitness, fact.field), fact.value)
    if fact.field == "level":
        return ctx.level == fact.value
    return str(getattr(ctx.fitness, fact.field, None) or "") == fact.value


def proposals(ctx: MemberContext, facts: Sequence[Fact], *, source: str) -> list[dict[str, Any]]:
    """The answer's ``memory`` entries for these facts: a signed "remember?" ticket each - or why not ('known': already
    saved; 'full': the 50-item health cap is reached). Nothing is stored here."""
    from app.services.health import MAX_CONDITIONS, conditions_full_message

    out: list[dict[str, Any]] = []
    full = len(ctx.conditions) >= MAX_CONDITIONS
    for fact in facts[:MAX_PROPOSALS]:
        entry: dict[str, Any] = {"op": "remember", "field": fact.field, "label": fact.label, "detail": fact.detail}
        if _known(ctx, fact):
            entry.update(status="known", detail=f"{fact.detail} · already saved")
        elif fact.field in HEALTH_FIELDS and full:
            entry.update(status="full", detail=conditions_full_message())
        else:
            entry.update(status="proposed",
                         token=make_token(ctx.user_id, {"op": "add", "f": fact.field, "v": fact.value, "s": source[:20]}))
        out.append(entry)
    return out


def history_chips(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the chat history keeps of the memory entries: plain chips (no tickets) linking to the memory page."""
    chips = []
    for e in entries:
        if e.get("op") == "forgot":
            detail = e.get("detail") or "Forgotten"
        elif e.get("status") == "proposed":
            detail = f"Offered to remember: {e.get('label')}"
        elif e.get("status") == "known":
            detail = f"Already remembered: {e.get('label')}"
        else:
            detail = f"Couldn't remember {e.get('label')}: the profile is full"
        chips.append({"type": "memory", "ok": e.get("status") != "full", "detail": str(detail)[:300], "link": PANEL})
    return chips


async def propose_for(db: AsyncSession, user: User, text: str, *, source: str, ctx: MemberContext | None = None,
                      extra: Sequence[Fact] = ()) -> list[dict[str, Any]]:
    """Facts in ``text`` (+ ``extra``, e.g. the model's validated suggestions) -> proposals. Members only."""
    from app.models.enums import Role

    if user.role != Role.CUSTOMER:
        return []
    facts = merge(extract_facts(text), extra)
    if not facts:
        return []
    if ctx is None:
        ctx = await load_member_context(db, user)
    return proposals(ctx, facts, source=source)


# ====================================================================================== confirm / undo
class MemoryRefused(Exception):  # noqa: N818 - a friendly, expected refusal (expired ticket, a cap reached)
    def __init__(self, message: str, code: str = "invalid", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


EXPIRED = "This suggestion has expired. Say it again, or add it on your profile."


def _validated(field_name: str, value: Any) -> Fact:
    """A token's (field, value) checked again like typed input."""
    if field_name not in FIELDS or not isinstance(value, str):
        raise MemoryRefused(EXPIRED, "invalid")
    if field_name in HEALTH_FIELDS:
        name = clean(value, HEALTH_NAME_MAX)
        if not _health_name_ok(name):
            raise MemoryRefused("That doesn't look like something I can save.", "invalid")
        return Fact(field_name, name)
    if field_name in LIST_FIELD_OF:
        stored, err = fp.canonical_item(field_name, value)
        if err or not stored:
            raise MemoryRefused(err or "That doesn't look like something I can save.", "invalid")
        return Fact(field_name, stored)
    if field_name in ("sessions_per_week", "session_minutes"):
        lo, hi = fp.SESSIONS_RANGE if field_name == "sessions_per_week" else fp.MINUTES_RANGE
        if not value.isdigit() or not lo <= int(value) <= hi:
            raise MemoryRefused(EXPIRED, "invalid")
        return Fact(field_name, value)
    if field_name == "preferred_time":
        if value not in fp.TIME_LABELS:
            raise MemoryRefused(EXPIRED, "invalid")
        return Fact(field_name, value)
    if value not in {lv.value for lv in FitnessLevel}:
        raise MemoryRefused(EXPIRED, "invalid")
    return Fact(field_name, value)


async def _refresh_after_health_change(db: AsyncSession, user: User, change: str) -> bool:
    from app.services import health as hs

    return await hs.after_conditions_change(db, user, change)


async def confirm(db: AsyncSession, user: User, token: Any) -> dict[str, Any]:
    """Save a proposed fact (the member clicked "Remember"). Idempotent: a second click answers 'known'. Raises
    ``MemoryRefused`` (expired / invalid ticket, 'full' at a cap). The caller commits, then calls ``after_commit``."""
    data = read_token(token, user.id)
    if data is None or data.get("op") != "add":
        raise MemoryRefused(EXPIRED, "expired")
    fact = _validated(str(data.get("f") or ""), data.get("v"))
    result: dict[str, Any] = {"ok": True, "field": fact.field, "label": fact.label, "undo": None, "queue_ac": False}
    if fact.field in HEALTH_FIELDS:
        from app.services import health as hs

        source = "assistant" if data.get("s") == "assistant" else "live_coach" if data.get("s") == "live_coach" else "self"
        added = await hs.add_condition(db, user.id, kind=HEALTH_FIELDS[fact.field], name=fact.value, source=source)
        if added.status == "full":
            raise MemoryRefused(hs.conditions_full_message(), "full", 409)
        if added.status == "known":
            result.update(status="known", detail=f"{fact.label} is already on your health profile.")
            return result
        row = await fp.lock_row(db, user.id)
        fp.unforget(row, fact.value)
        result["queue_ac"] = await _refresh_after_health_change(
            db, user, f"Added {FIELD_TITLES[fact.field].lower()}: {fact.value} (from a chat with the AI)")
        assert added.condition is not None
        result.update(status="saved", detail=f"Saved to your health profile. {_safety_note(added.condition.tag)}",
                      undo=make_token(user.id, {"op": "undo_add", "f": fact.field, "id": added.condition.id,
                                                "v": fact.value}))
        return result
    if fact.field in LIST_FIELD_OF:
        change = await fp.add_item(db, user.id, fact.field, fact.value)
        if change.status == "full":
            raise MemoryRefused(f"Your profile already lists {change.cap} {FIELD_TITLES[fact.field].lower()} items - "
                              "the most it can hold. Remove one on your training preferences page first.", "full", 409)
        if change.status == "known":
            result.update(status="known", detail=f"{fact.label} is already in your profile.")
            return result
        result.update(status="saved", detail=f"Saved: {FIELD_TITLES[fact.field].lower()}.",
                      undo=make_token(user.id, {"op": "undo_add", "f": fact.field, "v": fact.value,
                                                "moved": change.previous}))
        return result
    if fact.field == "level":
        from app.services import health as hs

        profile = await hs.ensure_profile(db, user)
        previous = str(getattr(profile.fitness_level, "value", profile.fitness_level) or "beginner")
        if previous == fact.value:
            result.update(status="known", detail="That's already your experience level.")
            return result
        profile.fitness_level = FitnessLevel(fact.value)
        result.update(status="saved", detail="Experience level updated.",
                      undo=make_token(user.id, {"op": "undo_set", "f": "level", "prev": previous}))
        return result
    value: Any = int(fact.value) if fact.field in ("sessions_per_week", "session_minutes") else fact.value
    change = await fp.set_scalar(db, user.id, fact.field, value)
    if change.status == "same":
        result.update(status="known", detail="That's already in your profile.")
        return result
    result.update(status="saved", detail=f"Saved: {FIELD_TITLES[fact.field].lower()}.",
                  undo=make_token(user.id, {"op": "undo_set", "f": fact.field, "prev": change.previous}))
    return result


def _safety_note(tag: str | None) -> str:
    if tag:
        return f"Your coach and the safety checks now plan around it ({TAG_LABELS.get(tag, tag)})."
    return "Your coach and the AI now take it into account."


async def undo(db: AsyncSession, user: User, token: Any) -> dict[str, Any]:
    """Reverse a confirm (``undo_add`` / ``undo_set``) or a forget (``restore``). Idempotent. Raises MemoryRefused."""
    data = read_token(token, user.id)
    op = data.get("op") if data else None
    if op not in ("undo_add", "undo_set", "restore"):
        raise MemoryRefused("This undo has expired.", "expired")
    result: dict[str, Any] = {"ok": True, "queue_ac": False}
    if op == "restore":
        return await _restore(db, user, data, result)
    field_name = str(data.get("f") or "")
    if op == "undo_add" and field_name in HEALTH_FIELDS:
        from app.services import health as hs

        await hs.lock_conditions(db, user.id)
        gone = (await db.execute(
            delete(HealthCondition).where(HealthCondition.id == int(data.get("id") or 0),
                                          HealthCondition.customer_id == user.id,
                                          func.lower(HealthCondition.name) == str(data.get("v") or "").lower())
            .returning(HealthCondition.id)
        )).first()
        if gone is None:
            result.update(status="gone", detail="That was already removed.")
            return result
        row = await fp.lock_row(db, user.id)
        fp.note_forgotten(row, str(data.get("v") or ""), _tag_for(field_name, str(data.get("v") or "")))
        result["queue_ac"] = await _refresh_after_health_change(db, user, f"Removed: {data.get('v')} (undone)")
        result.update(status="undone", detail=f"Undone - “{data.get('v')}” is no longer remembered.")
        return result
    if op == "undo_add" and field_name in LIST_FIELD_OF:
        value = str(data.get("v") or "")
        change = await fp.remove_item(db, user.id, field_name, value)
        moved = data.get("moved")
        if moved in LIST_FIELD_OF and change.status == "removed":
            await fp.add_item(db, user.id, moved, value)
        result.update(status="undone" if change.status == "removed" else "gone",
                      detail="Undone." if change.status == "removed" else "That was already removed.")
        return result
    if op == "undo_set" and field_name == "level":
        from app.services import health as hs

        prev = str(data.get("prev") or "beginner")
        if prev in {lv.value for lv in FitnessLevel}:
            (await hs.ensure_profile(db, user)).fitness_level = FitnessLevel(prev)
        result.update(status="undone", detail="Undone.")
        return result
    if op == "undo_set" and field_name in SCALAR_FIELDS:
        prev = data.get("prev")
        if not fp.valid_scalar(field_name, prev):
            raise MemoryRefused("This undo has expired.", "expired")
        await fp.set_scalar(db, user.id, field_name, prev)
        result.update(status="undone", detail="Undone.")
        return result
    raise MemoryRefused("This undo has expired.", "expired")


def _tag_for(field_name: str, value: str) -> str | None:
    return Fact(field_name, value).tag() if field_name in HEALTH_FIELDS else None


# ====================================================================================== forget
def forget_target(text: str) -> str | None:
    """The thing a "forget ..." message asks to forget ("forget that I have asthma" -> "i have asthma"), or None."""
    s = _norm(text).rstrip(" .!")
    if len(s) > 160 or "?" in s:
        return None
    m = _FORGET.match(s)
    if not m:
        return None
    target = m.group("x").strip(" .,'\"")
    if not target or _FORGET_SKIP.match(target) or len(target) < 3:
        return None
    return target


@dataclass(slots=True)
class _Hit:
    kind: str  # health | list | scalar | level | health_notes | fitness_notes
    field: str
    value: Any
    label: str
    row: Any = None


def _matches(ctx: MemberContext, target: str) -> list[_Hit]:
    """The saved items ``target`` names: the facts it states ("i like running"), its safety tag ("my asthma") or all of
    its significant words (in a health item's name / tag label, or a list item's label)."""
    from app.services.intake import condition_tag

    hits: list[_Hit] = []
    facts = extract_facts(target if target.startswith(("i ", "i'm", "im ", "my ")) else f"i have {target}")
    words = [w for w in re.findall(r"[a-z0-9]+", target) if len(w) >= 3 and w not in _STOP_WORDS]
    tags = {t for t in (condition_tag(target, ConditionKind.CONDITION), condition_tag(target, ConditionKind.ALLERGY)) if t}
    for c in ctx.conditions:
        hay = f"{c.name} {TAG_LABELS.get(c.tag or '', '')} {(c.tag or '').replace('_', ' ')}".lower()
        by_fact = any(f.field in HEALTH_FIELDS and f.value.lower() == c.name.lower() for f in facts)
        by_tag = c.tag is not None and c.tag in tags
        by_words = bool(words) and all(w in hay for w in words)
        if by_fact or by_tag or by_words:
            hits.append(_Hit("health", c.kind, c.name, c.name, c))
    for name in LIST_FIELD_OF:
        for v in getattr(ctx.fitness, name):
            label = fp.label_of(name, v).lower()
            by_fact = any(f.field == name and fp.contains([v], f.value) for f in facts)
            by_words = bool(words) and all(w in f"{label} {v.lower()}" for w in words)
            if by_fact or by_words or fp.vocab_slug(name, target) == v:
                hits.append(_Hit("list", name, v, fp.short_label(name, v)))
    scalar_words = {"sessions_per_week": ("session", "sessions", "times a week", "a week", "per week", "schedule"),
                    "session_minutes": ("minutes", "session length", "how long", "hour"),
                    "preferred_time": ("morning", "evening", "afternoon", "night", "midday", "lunch", "time of day",
                                       "preferred time")}
    for name in SCALAR_FIELDS:
        current = getattr(ctx.fitness, name)
        if current is None:
            continue
        by_fact = any(f.field == name for f in facts)
        if by_fact or any(k in target for k in scalar_words[name]):
            hits.append(_Hit("scalar", name, current, f"{fp.FIELD_LABELS[name]}: {fp.describe_scalar(name, current)}"))
    if ctx.fitness.notes and "training notes" in target:
        hits.append(_Hit("fitness_notes", "notes", ctx.fitness.notes, "Training notes"))
    if ctx.health_notes and ("coach notes" in target or "notes for my coach" in target or "health notes" in target):
        hits.append(_Hit("health_notes", "health_notes", ctx.health_notes, "Notes for your coach"))
    return hits[:FORGET_MAX]


async def forget(db: AsyncSession, user: User, target: str) -> dict[str, Any]:
    """Remove what ``target`` names - everywhere, at once (no cache to wait for) - and return an undo ticket. Also
    remembered as forgotten, so the assistant hides it from its view of past chats. The caller commits."""
    from app.services import health as hs

    await hs.lock_conditions(db, user.id)  # lock order: the conditions list, then the fitness row (as everywhere)
    ctx = await load_member_context(db, user)
    hits = _matches(ctx, target)
    if not hits:
        return {"ok": False, "status": "nothing", "removed": [], "undo": None, "queue_ac": False,
                "detail": f"I couldn't find “{clean(target, 60)}” in what I know about you. Everything I use is listed on "
                          "the “What the AI knows about you” page."}
    return await _remove(db, user, hits)


async def forget_item(db: AsyncSession, user: User, *, kind: str, field_name: str, value: Any) -> dict[str, Any]:
    """The memory page's "Forget" button: one specific item (validated against what is saved)."""
    from app.services import health as hs

    await hs.lock_conditions(db, user.id)
    ctx = await load_member_context(db, user)
    hit: _Hit | None = None
    if kind == "health":
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            wanted = 0
        found = next((c for c in ctx.conditions if c.id == wanted), None)
        if found is None:
            row = (await db.execute(select(HealthCondition).where(HealthCondition.id == wanted,
                                                                  HealthCondition.customer_id == user.id))).scalars().first()
            found = None if row is None else next(iter(_items_of([row])), None)
        if found is not None:
            hit = _Hit("health", found.kind, found.name, found.name, found)
    elif kind == "list" and field_name in LIST_FIELD_OF:
        v = next((x for x in getattr(ctx.fitness, field_name) if x == value), None)
        if v is not None:
            hit = _Hit("list", field_name, v, fp.short_label(field_name, v))
    elif kind == "scalar" and field_name in SCALAR_FIELDS:
        current = getattr(ctx.fitness, field_name)
        if current is not None:
            hit = _Hit("scalar", field_name, current,
                       f"{fp.FIELD_LABELS[field_name]}: {fp.describe_scalar(field_name, current)}")
    elif kind == "fitness_notes" and ctx.fitness.notes:
        hit = _Hit("fitness_notes", "notes", ctx.fitness.notes, "Training notes")
    elif kind == "health_notes" and ctx.health_notes:
        hit = _Hit("health_notes", "health_notes", ctx.health_notes, "Notes for your coach")
    elif kind == "level" and ctx.level and ctx.level != FitnessLevel.BEGINNER.value:
        hit = _Hit("level", "level", ctx.level, f"Experience level: {ctx.level}")
    if hit is None:
        return {"ok": False, "status": "nothing", "removed": [], "undo": None, "queue_ac": False,
                "detail": "That item isn't saved any more."}
    return await _remove(db, user, [hit])


def _items_of(rows: Sequence[HealthCondition]) -> list[Any]:
    from app.ai.member_context import health_items

    return health_items(rows)


async def _remove(db: AsyncSession, user: User, hits: Sequence[_Hit]) -> dict[str, Any]:
    from app.services import health as hs

    restore: list[dict[str, Any]] = []
    removed: list[str] = []
    health_changed = False
    row = await fp.lock_row(db, user.id)
    for h in hits:
        if h.kind == "health":
            c = h.row
            gone = (await db.execute(
                delete(HealthCondition).where(HealthCondition.id == c.id, HealthCondition.customer_id == user.id)
                .returning(HealthCondition.id)
            )).first()
            if gone is None:
                continue
            health_changed = True
            fp.note_forgotten(row, c.name, c.tag)
            restore.append({"f": "health", "k": c.kind, "n": c.name[:120], "o": (c.notes or "")[:255],
                            "s": c.source[:20]})
        elif h.kind == "list":
            change = await fp.remove_item(db, user.id, h.field, h.value, forget=True)
            if change.status != "removed":
                continue
            restore.append({"f": h.field, "v": h.value})
        elif h.kind == "scalar":
            change = await fp.set_scalar(db, user.id, h.field, None, forget=True)
            if change.status != "set":
                continue
            restore.append({"f": h.field, "prev": change.previous})
        elif h.kind == "fitness_notes":
            change = await fp.set_scalar(db, user.id, "notes", None, forget=True)
            if change.status != "set":
                continue
            restore.append({"f": "notes", "prev": change.previous})
        elif h.kind == "health_notes":
            profile = await hs.ensure_profile(db, user)
            restore.append({"f": "health_notes", "prev": (profile.health_notes or "")[:1000]})
            fp.note_forgotten(row, "Notes for your coach")
            profile.health_notes = None
        elif h.kind == "level":
            profile = await hs.ensure_profile(db, user)
            restore.append({"f": "level", "prev": str(getattr(profile.fitness_level, "value", profile.fitness_level))})
            profile.fitness_level = FitnessLevel.BEGINNER
        removed.append(h.label)
    queue_ac = False
    if health_changed:
        queue_ac = await hs.after_conditions_change(db, user, "Removed at your request: " + ", ".join(removed[:5]))
    if not removed:
        return {"ok": False, "status": "nothing", "removed": [], "undo": None, "queue_ac": False,
                "detail": "That was already removed."}
    names = ", ".join(f"“{clean(r, 60)}”" for r in removed[:5]) + (f" and {len(removed) - 5} more" if len(removed) > 5 else "")
    return {"ok": True, "status": "forgotten", "removed": removed, "queue_ac": queue_ac,
            "detail": f"Forgotten: {names}. Your coach, the safety checks and every AI answer stop using it now.",
            "undo": make_token(user.id, {"op": "restore", "items": restore})}


async def _restore(db: AsyncSession, user: User, data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Undo a forget: put every item back (health items behind the cap lock - 'full' when the profile filled up)."""
    from app.services import health as hs

    items = [i for i in (data.get("items") or []) if isinstance(i, dict)][:FORGET_MAX]
    restored: list[str] = []
    health_changed = False
    # the same lock order as every other writer: the conditions list, then the fitness row, then the profile
    await hs.lock_conditions(db, user.id)
    await fp.lock_row(db, user.id)
    for item in items:
        f = str(item.get("f") or "")
        if f == "health":
            try:
                kind = ConditionKind(str(item.get("k")))
            except ValueError:
                continue
            name = clean(item.get("n"), 120)
            if not name:
                continue
            added = await hs.add_condition(db, user.id, kind=kind, name=name, notes=clean(item.get("o"), 255) or None,
                                           source=str(item.get("s") or "self")[:20])
            if added.status == "full":
                raise MemoryRefused(hs.conditions_full_message(), "full", 409)
            if added.status == "added":
                health_changed = True
                fp.unforget(await fp.lock_row(db, user.id), name)
            restored.append(name)
        elif f in LIST_FIELD_OF:
            value, err = fp.canonical_item(f, item.get("v"))
            if err or not value:
                continue
            change = await fp.add_item(db, user.id, f, value)
            if change.status == "full":
                raise MemoryRefused(f"Your {FIELD_TITLES[f].lower()} list is full - remove an item first.", "full", 409)
            restored.append(fp.short_label(f, value))
        elif f in (*SCALAR_FIELDS, "notes"):
            prev = item.get("prev")
            if fp.valid_scalar(f, prev):
                await fp.set_scalar(db, user.id, f, prev)
                fp.unforget(await fp.lock_row(db, user.id), f"{fp.FIELD_LABELS[f]}: {fp.describe_scalar(f, prev)}")
                restored.append(fp.FIELD_LABELS[f])
        elif f == "health_notes":
            prev = clean(item.get("prev"), 1000)
            profile = await hs.ensure_profile(db, user)
            if prev and not profile.health_notes:
                profile.health_notes = prev
                fp.unforget(await fp.lock_row(db, user.id), "Notes for your coach")
                restored.append("Notes for your coach")
        elif f == "level":
            prev = str(item.get("prev") or "")
            if prev in {lv.value for lv in FitnessLevel}:
                (await hs.ensure_profile(db, user)).fitness_level = FitnessLevel(prev)
                restored.append("Experience level")
    if health_changed:
        result["queue_ac"] = await hs.after_conditions_change(db, user, "Restored: " + ", ".join(restored[:5]))
    result.update(status="restored" if restored else "gone",
                  detail=("Restored: " + ", ".join(restored[:5]) + ".") if restored else "Nothing to restore.")
    return result


async def after_commit(db: AsyncSession, user: User, result: dict[str, Any]) -> None:
    """Queue the AI wording of the AC advice when a health change touched a plan with AI (after the COMMIT)."""
    if result.get("queue_ac"):
        from app.services import health as hs

        try:
            await hs.queue_ac_note(db, user)
        except Exception:  # noqa: BLE001 - the rule-based advice is already current
            log.debug("memory: AC note not queued for user %s", user.id, exc_info=True)


def public(result: dict[str, Any]) -> dict[str, Any]:
    """The JSON a browser gets (internal flags dropped)."""
    return {k: v for k, v in result.items() if k != "queue_ac"}


async def profile_level(db: AsyncSession, user_id: int) -> str | None:
    lvl = await db.scalar(select(CustomerProfile.fitness_level).where(CustomerProfile.user_id == user_id))
    return str(getattr(lvl, "value", lvl)) if lvl is not None else None
