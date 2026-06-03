"""Subprocess helper for photo scoring with reliable timeout kill."""
import json
import os
import sys

from openai import OpenAI

MODEL = "google/gemini-3.5-flash-latest"
BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """You are a Belgian property photo analyst. Rate how MODERN and well-finished a house looks based on its image filenames (no actual photos — infer from context).

Score 1-10:
- 9-10: Modern, high-quality finishes, stylish interior photos likely
- 7-8: Good condition, updated, clean presentation
- 5-6: Average, some modern elements, some dated
- 3-4: Older style, needs updates
- 1-2: Very outdated, poor condition

Return ONLY valid JSON: {"photo_score": <1-10>, "reasoning": "<brief Dutch reasoning>"}"""

if __name__ == "__main__":
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        out = {"ok": False, "err": "OPENROUTER_API_KEY not set"}
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
        images = item.get("image_urls", [])
        title = item.get("title", "")
        price = item.get("price", 0)
        surface = item.get("surface_m2", 0) or "unknown"
        epc = item.get("epc_label", "") or "unknown"

        prompt = f"""Property: {title} | €{price} | {surface}m² | EPC {epc}
Images: {len(images)} photos available (filenames: {[os.path.basename(u) for u in images[:5]]})
Score how modern/finished based on all available context. Return JSON."""

        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=150,
            )
            content = resp.choices[0].message.content
            result = json.loads(content)
            score = float(result.get("photo_score", 5.0))
            score = max(1.0, min(10.0, score))
            reasoning = result.get("reasoning", "")
        except Exception as e:
            score = 5.0
            reasoning = f"Error: {str(e)[:100]}"

        results.append({"id": lid, "score": score, "reasoning": reasoning})

    out = {"ok": True, "results": results}
    print(json.dumps(out), flush=True)
