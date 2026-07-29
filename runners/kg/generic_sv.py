"""Deterministic Swedish generic-noun classifier.

The LLM tags generic group/role/abstraction nouns (Politiker, Invandrare,
Konsumenter, Unga Människor, Justitieministern) as named entities — and worse,
as PERSON. Re-running gemma to fix this is slow and the per-name LLM verdict
never covers the long tail. This module decides "is this a generic common noun,
not a named individual?" purely by morphology + head-noun lists, so it is fast,
byte-stable across runs, and covers every name. Shared by ``adapter.py`` (fold)
and ``refine_person_types.py`` (the LLM there is now only a tie-breaker).
"""

from __future__ import annotations

import re


# Swedish definite/plural suffixes, longest first so 'kommunerna' strips to
# 'kommun' not 'kommuner'. Applied to a single token before matching.
_SUFFIXES = ("erna", "arna", "orna", "ernas", "en", "et", "na", "er", "ar", "or", "n")

# Agent / role noun endings — a word ending here is essentially always an
# occupation or actor class (politiker, invandrare, jurist, konsulent...).
_AGENT_SUFFIXES = (
    "are",
    "iker",
    "ister",
    "örer",
    "anter",
    "enter",
    "sökande",
    "tagare",
    "givare",
    "ägare",
    "förare",
    "arbetare",
    "ören",
    "örerna",
)

# Generic "head" nouns: if any token of a multi-word name reduces to one of
# these, the whole name is a group/role descriptor, not a named entity.
_GENERIC_HEADS = frozenset(
    {
        "människor",
        "människa",
        "personer",
        "person",
        "medborgare",
        "invånare",
        "befolkning",
        "befolkningen",
        "familjer",
        "familj",
        "ungdomar",
        "ungdom",
        "kvinnor",
        "kvinna",
        "män",
        "man",
        "barn",
        "pojkar",
        "flickor",
        "äldre",
        "minister",
        "ministern",
        "ministrar",
        "statsråd",
        "chef",
        "chefen",
        "chefer",
        "ordförande",
        "ordföranden",
        "direktör",
        "direktören",
        "tjänstemän",
        "tjänsteman",
        "anställda",
        "ledamöter",
        "ledamot",
        "väljare",
        "konsumenter",
        "konsument",
        "kunder",
        "kund",
        "föräldrar",
        "förälder",
        "medlemmar",
        "medlem",
        "aktörer",
        "aktör",
        "deltagare",
        "företagare",
        "invandrare",
        "flyktingar",
        "flykting",
        "pensionärer",
        "pensionär",
        "arbetare",
        "arbetstagare",
        "studenter",
        "student",
        "elever",
        "elev",
        "lärare",
        "läkare",
        "forskare",
        "journalister",
        "journalist",
        "politiker",
        "jurister",
        "jurist",
        "revisorer",
        "revisor",
        "domare",
        "ledare",
        "individer",
        "individ",
        "parter",
        "part",
        "grupper",
        "grupp",
        "organisationer",
        "myndigheter",
        "företag",
        "ägare",
        "förtroendevalda",
        "experter",
        "expert",
        "representanter",
        "representant",
        "resenärer",
        "boende",
        "brukare",
        "patienter",
        "patient",
        "offer",
        "förövare",
        "gärningsmän",
        "sökande",
        "sakkunniga",
        "berörda",
    }
)

# Exact-form offenders (covers irregulars the morphology rules miss).
_STOP_EXACT = frozenset(
    {
        "män",
        "barn",
        "äldre",
        "unga",
        "vuxna",
        "alla",
        "andra",
        "många",
        "folk",
        "allmänheten",
        "samhället",
        "marknaden",
        "näringslivet",
        "offentligheten",
        "parterna",
        "aktörerna",
        "berörda",
        "drabbade",
    }
)

# Adjectives used as group nouns (often definite -a/-e): "Arbetslösa",
# "Funktionshindrade", "De Anhöriga". Matched as a whole single token or as the
# last token of a "De X" phrase.
_GROUP_ADJ = frozenset(
    {
        "arbetslösa",
        "funktionshindrade",
        "anhöriga",
        "närstående",
        "sjuka",
        "friska",
        "fattiga",
        "rika",
        "hemlösa",
        "sysselsatta",
        "anställda",
        "förtroendevalda",
        "sakkunniga",
        "ansvariga",
        "yngre",
        "gamla",
        "nyanlända",
        "asylsökande",
        "papperslösa",
        "utsatta",
        "berörda",
        "drabbade",
        "äldre",
        "unga",
        "vuxna",
        "arbetande",
        "studerande",
    }
)
_DEMONSTRATIVES = frozenset({"de", "den", "det", "dom", "dessa", "vissa"})

# Job-title acronyms / role words that read as a named person but are roles:
# VD (CEO), ÖB (supreme commander), GD (director-general), "Särskilda Utredaren".
# Matched as a whole single token or as the LAST token of a phrase
# ("Försvarsmakten ÖB", "Kommunstyrelsens Ordförande").
_TITLE_WORDS = frozenset(
    {
        "vd",
        "öb",
        "gd",
        "vvd",
        "vvd:n",
        "utredaren",
        "utredare",
        "talespersonen",
        "talesperson",
        "talesman",
        "talesmannen",
        "företrädare",
        "företrädaren",
        "samordnare",
        "samordnaren",
        "handläggare",
        "föredragande",
        "rapportören",
        "rapportör",
        "generaldirektören",
        "generaldirektör",
        "landshövdingen",
        "landshövding",
        "statssekreteraren",
        "statssekreterare",
    }
)

# Named-law / treaty suffixes are definite-form but ARE real entities — never
# flag these (Miljöbalken, Grundlagen, Kyotoprotokollet, Tjänstedirektivet).
_LAW_SUFFIXES = ("balken", "lagen", "lagstiftningen", "protokollet", "förordningen", "direktivet", "fördraget")

# Common Swedish given names + a few surname endings, so a single token that
# happens to end in -er/-ar (Peter, Holger, Oскар, Ingvar) stays a person.
_GIVEN_NAMES = frozenset(
    {
        "anders",
        "lars",
        "göran",
        "eva",
        "bengt",
        "mats",
        "björn",
        "peter",
        "lena",
        "hans",
        "per",
        "jan",
        "annika",
        "karin",
        "maria",
        "anna",
        "erik",
        "carl",
        "karl",
        "johan",
        "nils",
        "olof",
        "olle",
        "sven",
        "gunnar",
        "stefan",
        "fredrik",
        "mikael",
        "magnus",
        "thomas",
        "tomas",
        "ulf",
        "leif",
        "bo",
        "rolf",
        "kjell",
        "ingvar",
        "holger",
        "oskar",
        "oscar",
        "gustav",
        "henrik",
        "fredrika",
        "margareta",
        "birgitta",
        "kerstin",
        "marianne",
        "ingrid",
        "elisabeth",
        "kristina",
        "christina",
        "barbro",
        "ulla",
        "gun",
        "siv",
        "inger",
        "berit",
        "monica",
        "marie",
        "helena",
        "katarina",
        "åsa",
        "lotta",
        "cecilia",
        "agneta",
        "gabriella",
        "amelie",
        "alexandra",
        "andreas",
        "mona",
        "laila",
        "ylva",
        "maud",
        "lennart",
        "torsten",
        "arne",
        "åke",
        "bertil",
        "sture",
        "ingemar",
        "claes",
        "klas",
        "wilhelm",
        "axel",
        "gustaf",
        "fritiof",
    }
)


def _strip_suffix(token: str) -> str:
    for suf in _SUFFIXES:
        if len(token) > len(suf) + 2 and token.endswith(suf):
            return token[: -len(suf)]
    return token


def is_generic_person(name: str) -> bool:
    """True when a PERSON-typed name is a generic group/role noun, not a
    specific named individual. Conservative on real names (given-name
    whitelist + Firstname-Lastname guard), aggressive on roles/plurals."""
    raw = name.strip()
    if not raw:
        return False
    low = raw.lower()
    if low.endswith(_LAW_SUFFIXES):
        return False
    tokens = low.split()

    def _is_head(tok: str) -> bool:
        # Swedish definite forms are irregular (medborgare -> medborgarna), so
        # try EVERY suffix strip plus a trailing-e restore, against both lists.
        cands = {tok}
        for suf in _SUFFIXES:
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                base = tok[: -len(suf)]
                cands.add(base)
                cands.add(base + "e")  # medborgar -> medborgare, väljar -> väljare
        return any(c in _GENERIC_HEADS or c in _GROUP_ADJ for c in cands)

    # Multi-word: a named individual is Firstname+Lastname — every token
    # capitalized and NO token is a generic head. Otherwise, if any token is a
    # generic head OR the phrase starts with a demonstrative ("De Anhöriga"),
    # it's a group descriptor.
    if len(tokens) > 1:
        if tokens[0] in _DEMONSTRATIVES or _is_head(tokens[-1]) or tokens[-1] in _TITLE_WORDS:
            return True
        if any(_is_head(t) for t in tokens):
            return True
        if any(t.endswith(("minister", "ministern", "direktör", "ordförande", "chefen")) for t in tokens):
            return True
        words = raw.split()
        looks_like_name = all(w[:1].isupper() for w in words) and len(words) <= 3
        return not looks_like_name

    # Single token
    if low in _GIVEN_NAMES:
        return False
    if low in _STOP_EXACT or low in _GROUP_ADJ or low in _TITLE_WORDS:
        return True
    if _is_head(low):
        return True
    if low.endswith(_AGENT_SUFFIXES):  # politiker, invandrare, jurist...
        return True
    if low.endswith(("minister", "ministern", "direktör", "direktören", "ordförande", "chefen")):
        return True
    # productive noun plurals (konsumenter, revisorer, flyktingar) — but not a
    # whitelisted given name and long enough to be a real common noun.
    return len(low) >= 6 and low.endswith(("er", "ar", "or")) and low not in _GIVEN_NAMES


# Generic common nouns the model tags as GEO / WORK / EVENT / ORG but which are
# categories, not named entities. Explicit blocklist (NOT morphology) because
# Swedish named laws/regions are also definite-form (Miljöbalken, Norrland) and
# must survive — only KNOWN generics are listed. Matched as a whole name after
# suffix-stripping; the named-law guard in is_generic_person also applies.
_GENERIC_COMMON = frozenset(
    {
        # GEO that aren't places
        "arbetsplats",
        "arbetsplatsen",
        "skola",
        "skolan",
        "skolor",
        "bankkontor",
        "periferi",
        "periferin",
        "centrum",
        "glesbygd",
        "glesbygden",
        "glesbygdsområden",
        "län",
        "länet",
        "kommun",
        "region",
        "regionen",
        "landet",
        "orten",
        "platsen",
        "miljön",
        "miljö",
        "hemmet",
        "hemma",
        "stad",
        "staden",
        "städer",
        "tätort",
        # WORK that aren't specific works
        "rapport",
        "rapporten",
        "rapporter",
        "proposition",
        "propositionen",
        "förslag",
        "förslaget",
        "dokument",
        "dokumentet",
        "betänkande",
        "betänkandet",
        "skrivelse",
        "skrivelsen",
        "promemoria",
        "utredningen",
        "utredning",
        "utredningar",
        "lagförslag",
        "remiss",
        "remissen",
        "avtal",
        "avtalet",
        "beslut",
        "beslutet",
        "brev",
        "brevet",
        "artikel",
        "artikeln",
        "boken",
        "bok",
        # EVENT that aren't specific events
        "möte",
        "mötet",
        "möten",
        "debatt",
        "debatten",
        "krig",
        "kriget",
        "presskonferens",
        "presskonferensen",
        "lunch",
        "tvist",
        "tvister",
        "konferens",
        "konferensen",
        "sammanträde",
        "sammanträdet",
        "seminarium",
        "seminariet",
        "förhandling",
        "förhandlingar",
        "val",
        "valet",
        "kris",
        "krisen",
        "process",
        "processen",
        "projekt",
        "projektet",
        "reform",
        "reformen",
        # ORG that aren't specific orgs
        "företag",
        "företaget",
        "myndighet",
        "myndigheten",
        "organisation",
        "organisationen",
        "samhället",
        "marknaden",
        "staten",
        "regering",
        "näringslivet",
        "kommittén",
        "kommitté",
        "nämnden",
        "nämnd",
        "styrelsen",
        "styrelse",
        "förvaltningen",
        "förvaltning",
        "branschen",
        "bransch",
    }
)


def is_generic_common(name: str) -> bool:
    """True when a non-PERSON name is a generic category noun, not a named
    entity (Arbetsplats, Rapporten, Möte, Företag). Named laws/regions are
    protected by the law-suffix guard so e.g. Miljöbalken survives."""
    low = name.strip().lower()
    if low.endswith(_LAW_SUFFIXES):
        return False
    if low in _GENERIC_COMMON:
        return True
    return _strip_suffix(low) in _GENERIC_COMMON


_URLISH = re.compile(r"(https?://|www\.|\.(se|com|nu|org|html|htm|net|gov)\b)", re.IGNORECASE)


def is_url(name: str) -> bool:
    return bool(_URLISH.search(name))


_MARKUP = re.compile(r"</?(relation|entity|description|keywords)[^>]*>", re.IGNORECASE)


def clean_desc(text: str) -> str:
    """Strip LLM output-format markup that leaked into edge descriptions
    (`</relation>`, `</entity>`, stray trailing `>`)."""
    return _MARKUP.sub("", text).strip().strip("<>").strip()
