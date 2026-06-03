"""Subprocess helper for DeepSeek scoring with reliable timeout kill.

Reads .env from the project root for DEEPSEEK_API_KEY.
"""
import json
import os
import sys

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

SYSTEM_PROMPT = """Je bent een vastgoedanalist die Belgische te-koop-woningen beoordeelt.
Je evalueert hoe MODERN en AFGEWERKT een woning is op basis van de beschrijving.

Beoordelingscriteria (1-10):
- 9-10: Duidelijk gerenoveerd/nieuwbouw, moderne afwerking, hedendaags design
- 7-8: Recent vernieuwd, grotendeels modern, goed onderhouden
- 5-6: Gemiddeld, enkele moderne elementen maar ook verouderde aspecten
- 3-4: Oudere stijl, nood aan vernieuwing, basisafwerking
- 1-2: Zeer verouderd, slechte staat, ouderwets

Antwoord ALTIJD in het Nederlands — ook de reasoning. Enkel geldige JSON, geen extra tekst.
Voorbeeld: {"modern_score": 7, "reasoning": "Recent gerenoveerde keuken en badkamer, goede EPC B, moderne vloeren."}"""

if __name__ == "__main__":
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        out = {"ok": False, "err": "DEEPSEEK_API_KEY not set in .env"}
        print(json.dumps(out), flush=True)
        sys.exit(0)

    listings_input = json.loads(sys.argv[1])
    results = []

    try:
        client = OpenAI(base_url=BASE_URL, api_key=api_key, timeout=15.0, max_retries=0)
    except Exception as e:
        out = {"ok": False, "err": f"Client init failed: {e}"}
        print(json.dumps(out), flush=True)
        sys.exit(0)

    for item in listings_input:
        lid = item["id"]
        title = item.get("title", "")
        price = item.get("price", 0)
        address = item.get("address", "")
        surface = item.get("surface_m2", 0) or "unknown"
        epc = item.get("epc_label", "") or "unknown"
        bedrooms = item.get("bedrooms", 0)
        description = (item.get("description", "") or "").strip()
        if len(description) < 20:
            description = "Limited description available."

        prompt = f"""Beoordeel deze woning op moderniteit en afwerkingsgraad.

Titel: {title}
Prijs: €{price}
Adres: {address}
Oppervlakte: {surface}m²
EPC: {epc}
Slaapkamers: {bedrooms}

Beschrijving:
{description[:2000]}

Antwoord in dit exacte JSON formaat:
{{"modern_score": <cijfer 1-10>, "reasoning": "<korte 1-2 zinnen uitleg in correct Nederlands>"}}"""

        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            content = resp.choices[0].message.content
            result = json.loads(content)
            score = float(result.get("modern_score", 5.0))
            score = max(1.0, min(10.0, score))
            reasoning = result.get("reasoning", "Geen uitleg")
        except Exception as e:
            score = 5.0
            reasoning = f"Error: {str(e)[:100]}"

        results.append({"id": lid, "score": score, "reasoning": reasoning})

    out = {"ok": True, "results": results}
    print(json.dumps(out), flush=True)
