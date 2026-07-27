"""Post-build pass: demote generic "person" entities to OTHER — deterministically.

LLM extraction tags person-CATEGORIES (Barn, Forskare, Kvinnor, Politiker) and
pronouns as PERSON alongside real named individuals. Rather than re-extracting
the corpus, this classifies only the distinct PERSON *names* and writes an
overrides file the adapter applies on a re-fold (``adapter.py --type-overrides``).

Determinism contract (re-running produces the same overrides):
- a persistent **verdict cache** (``--cache``, name -> person|generic) is the
  source of truth; only never-seen names reach the LLM, so prior decisions can
  never flip on a re-run;
- unknown names are classified in **sorted order** with ``temperature=0`` and a
  fixed ``seed``;
- a deterministic **stoplist** decides the obvious generic shapes (pronouns,
  pure-lowercase-vocabulary plurals are still the LLM's job — the stoplist only
  covers exact known offenders) before any model call.

Runs in the PROJECT venv (lance + httpx only):

    uv run python scripts/kg/refine_person_types.py \
        --db transcripts_v2.lance --out kg_work/person_overrides.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx
import lance


# Exact-name generic verdicts that never need a model call. Keep SMALL and
# unambiguous — the LLM handles the long tail.
STOPLIST_GENERIC = {
    "barn",
    "barnen",
    "kvinnor",
    "kvinnorna",
    "män",
    "männen",
    "föräldrar",
    "föräldrarna",
    "ungdomar",
    "ungdomarna",
    "människor",
    "medborgare",
    "politiker",
    "forskare",
    "forskarna",
    "konsumenter",
    "konsumenterna",
    "anställda",
    "läkare",
    "lärare",
    "elever",
    "studenter",
    "pensionärer",
    "arbetare",
    "tjänstemän",
    "väljare",
    "svenskar",
    "invandrare",
    "flickor",
    "pojkar",
    "boende",
    "talaren",
    "moderatorn",
    "jag",
    "vi",
    "man",
}

PROMPT = """Du klassificerar entitetsnamn från svenska presskonferens-transkript.

REGLER:
- "generic" = INTE en specifik namngiven individ: yrken/roller (Forskare,
  Statsministern), grupper (Barn, Kvinnor, Svenskar), pronomen (Jag, Vi),
  kategorier (Högutbildade, Närstående).
- "person" = en specifik namngiven individ: fullständiga namn (Anna Lindberg),
  enstaka förnamn/efternamn (Ingegerd, Pettersson), historiska/fiktiva personer.

EXEMPEL:
- "Göran Persson" -> person
- "Statsministern" -> generic
- "Barn i Misshandelsfamiljer" -> generic
- "Ingegerd" -> person

SVAR: ENDAST en JSON-lista med namnen som är "generic". Ingen annan text.

Namn:
{names}"""


def classify_batch(client: httpx.Client, url: str, model: str, names: list[str]) -> set[str]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(names="\n".join(f"- {n}" for n in names))}],
        "max_tokens": 2000,
        "temperature": 0.0,
        "seed": 0,
    }
    resp = client.post(f"{url}/chat/completions", json=body, timeout=120)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return set()
    generic = json.loads(match.group(0))
    # only accept names that were actually in the batch (no hallucinated extras)
    allowed = {n.strip().lower() for n in names}
    return {n.strip().lower() for n in generic if isinstance(n, str) and n.strip().lower() in allowed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Demote generic PERSON entities to OTHER.")
    parser.add_argument("--db", default="transcripts_v2.lance")
    parser.add_argument("--out", default="kg_work/person_overrides.json")
    parser.add_argument(
        "--cache",
        default="kg_work/person_verdicts.json",
        help="persistent name->verdict map; cached names never hit the LLM again, which makes re-runs deterministic",
    )
    parser.add_argument("--gemma-url", default="http://localhost:8003/v1")
    parser.add_argument("--gemma-model", default="google/gemma-4-31B-it")
    parser.add_argument("--batch", type=int, default=60)
    args = parser.parse_args()

    tbl = lance.dataset(str(Path(args.db) / "kg_entities.lance")).to_table().to_pylist()
    persons = sorted(
        ((r["entity_id"], r["name"]) for r in tbl if r["entity_type"] == "PERSON"),
        key=lambda p: p[1].lower(),
    )
    print(f"{len(persons)} PERSON entities to classify")

    cache_path = Path(args.cache)
    verdicts: dict[str, str] = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    for _, name in persons:
        low = name.strip().lower()
        if low in STOPLIST_GENERIC:
            verdicts[low] = "generic"

    unknown = sorted({n.strip().lower() for _, n in persons} - set(verdicts))
    print(f"cache: {len(verdicts)} known, {len(unknown)} new names for the LLM")

    with httpx.Client() as client:
        for start in range(0, len(unknown), args.batch):
            batch = unknown[start : start + args.batch]
            try:
                generic = classify_batch(client, args.gemma_url, args.gemma_model, batch)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
                print(f"batch {start}: retrying after {exc}")
                try:
                    generic = classify_batch(client, args.gemma_url, args.gemma_model, batch)
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc2:
                    print(f"batch {start}: SKIPPED ({exc2}) — names stay unclassified")
                    continue
            for low in batch:
                verdicts[low] = "generic" if low in generic else "person"
            print(f"batch {start + len(batch)}/{len(unknown)} classified", flush=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(verdicts, indent=0, ensure_ascii=False, sort_keys=True))

    overrides = {eid: "OTHER" for eid, name in persons if verdicts.get(name.strip().lower()) == "generic"}
    Path(args.out).write_text(json.dumps(overrides, indent=0, sort_keys=True))
    demoted = [n for eid, n in persons if eid in overrides]
    print(f"wrote {len(overrides)} overrides -> {args.out} (verdict cache: {cache_path})")
    print("examples:", demoted[:12])


if __name__ == "__main__":
    main()
