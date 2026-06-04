"""Subprocess helper for DeepSeek scoring with reliable timeout kill.

Batch processing: stuurt 20 listings per API call naar DeepSeek.
Leest .env uit de project root voor DEEPSEEK_API_KEY.
"""
import json
import os
import sys
import time

# Load .env from project root
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key, _val)

from openai import OpenAI

MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com/v1"
BATCH_SIZE = 20

SYSTEM_PROMPT = """Je bent een vastgoedanalist die Belgische te-koop-woningen beoordeelt.
Je evalueert hoe MODERN en AFGEWERKT een woning is op basis van de beschrijving.

Beoordelingscriteria (1-10):
- 9-10: Duidelijk gerenoveerd/nieuwbouw, moderne afwerking, hedendaags design
- 7-8: Recent vernieuwd, grotendeels modern, goed onderhouden
- 5-6: Gemiddeld, enkele moderne elementen maar ook verouderde aspecten
- 3-4: Oudere stijl, nood aan vernieuwing, basisafwerking
- 1-2: Zeer verouderd, slechte staat, ouderwets

Antwoord ALTIJD in het Nederlands — ook de reasoning. Enkel geldige JSON, geen extra tekst.

Voorbeeld output (JSON array):
[
  {"id": "abc123", "modern_score": 7, "reasoning": "Recent gerenoveerde keuken en badkamer, goede EPC B, moderne vloeren."},
  {"id": "def456", "modern_score": 5, "reasoning": "Gemiddelde staat, deels gemoderniseerd, maar verouderde badkamer."}
]

BELANGRIJK: 
1. Antwoord met een JSON array van objecten — één per woning, in dezelfde volgorde
2. Elke entry heeft: id, modern_score (1-10), reasoning (1-2 zinnen Nederlands)
3. Geen extra tekst voor of na de JSON
4. Het aantal objecten in de array moet exact overeenkomen met het aantal aangeboden woningen"""


def format_listing_for_prompt(item):
    """Format 1 listing voor de batch prompt."""
    lid = item["id"]
    title = item.get("title", "")
    price = item.get("price", 0)
    address = item.get("address", "")
    surface = item.get("surface_m2", 0) or "onbekend"
    epc = item.get("epc_label", "") or "onbekend"
    bedrooms = item.get("bedrooms", 0)
    description = (item.get("description", "") or "").strip()
    if len(description) < 20:
        description = "Beperkte beschrijving beschikbaar."

    return f"""--- Woning {lid} ---
Titel: {title}
Prijs: €{price}
Adres: {address}
Oppervlakte: {surface}m²
EPC: {epc}
Slaapkamers: {bedrooms}
Beschrijving: {description[:1500]}"""


def score_batch(client, batch_items):
    """Score 1 batch van listings in 1 API call. Returns list of result dicts."""
    listings_text = "\n\n".join(format_listing_for_prompt(item) for item in batch_items)

    prompt = f"""Beoordeel onderstaande {len(batch_items)} woningen op moderniteit en afwerkingsgraad.

{listings_text}

Antwoord met een JSON array van {len(batch_items)} objecten — exact 1 per woning, in dezelfde volgorde.
Elk object: {{"id": "<id>", "modern_score": <1-10>, "reasoning": "<korte uitleg in Nederlands>"}}"""

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content

        # Try to parse JSON array from response
        # DeepSeek sometimes wraps in markdown code blocks
        content = content.strip()
        if content.startswith("```"):
            # Strip markdown code block
            content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.rsplit("```", 1)[0] if "```" in content else content
            content = content.strip()

        results = json.loads(content)

        # Validate: should be a list
        if not isinstance(results, list):
            raise ValueError(f"Response is not a list: {type(results)}")

        # Map results by ID
        scored = []
        for r in results:
            score = float(r.get("modern_score", 5.0))
            score = max(1.0, min(10.0, score))
            scored.append({
                "id": r.get("id", ""),
                "score": score,
                "reasoning": r.get("reasoning", "Geen uitleg"),
            })

        return scored

    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}, content: {content[:200]}...")
    except Exception as e:
        raise ValueError(f"API error: {str(e)[:200]}")


if __name__ == "__main__":
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        out = {"ok": False, "err": "DEEPSEEK_API_KEY not set in .env"}
        print(json.dumps(out), flush=True)
        sys.exit(0)

    listings_input = json.loads(sys.argv[1])

    if not listings_input:
        out = {"ok": True, "results": []}
        print(json.dumps(out), flush=True)
        sys.exit(0)

    try:
        client = OpenAI(
            base_url=BASE_URL,
            api_key=api_key,
            timeout=30.0,  # increased for batch responses
            max_retries=1,
        )
    except Exception as e:
        out = {"ok": False, "err": f"Client init failed: {e}"}
        print(json.dumps(out), flush=True)
        sys.exit(0)

    # Split into batches
    batches = [listings_input[i:i + BATCH_SIZE] for i in range(0, len(listings_input), BATCH_SIZE)]
    all_results = []
    errors = []

    total_batches = len(batches)
    print(f"  [deepseek] {len(listings_input)} listings in {total_batches} batches van max {BATCH_SIZE}", file=sys.stderr)

    for batch_idx, batch in enumerate(batches):
        print(f"  [deepseek] Batch {batch_idx + 1}/{total_batches} ({len(batch)} listings)...", file=sys.stderr)
        try:
            batch_results = score_batch(client, batch)
            all_results.extend(batch_results)
            print(f"  [deepseek] ✅ Batch {batch_idx + 1} gescoord ({len(batch_results)} ok)", file=sys.stderr)
        except Exception as e:
            print(f"  [deepseek] ❌ Batch {batch_idx + 1} failed: {e}", file=sys.stderr)
            errors.append(batch_idx)
            # Fallback: score 5.0 for all listings in failed batch
            for item in batch:
                all_results.append({
                    "id": item["id"],
                    "score": 5.0,
                    "reasoning": f"Batch {batch_idx + 1} failed: {str(e)[:80]}",
                })

        # Small delay between batches to avoid rate limiting
        if batch_idx < total_batches - 1:
            time.sleep(0.5)

    # Verify all IDs are present
    input_ids = {item["id"] for item in listings_input}
    result_ids = {r["id"] for r in all_results}

    if input_ids != result_ids:
        missing = input_ids - result_ids
        for mid in missing:
            all_results.append({
                "id": mid,
                "score": 5.0,
                "reasoning": "Missing from batch results (fallback)",
            })
        print(f"  [deepseek] ⚠ {len(missing)} missing IDs added as fallback", file=sys.stderr)

    out = {"ok": True, "results": all_results}
    if errors:
        out["partial_errors"] = [f"batch_{i}" for i in errors]

    print(json.dumps(out), flush=True)
