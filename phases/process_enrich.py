#!/usr/bin/env python3
"""Subprocess-based enrich — elke listing in apart subprocess.
OS ruimt ALL curl_cffi C-state op als het proces stopt.
Geen geheugenaccumulatie mogelijk."""

import subprocess
import json
import sys
import os
import time
import resource

WORKER = os.path.join(os.path.dirname(__file__), "..", "scrapers", "enrich_worker.py")
WORKER = os.path.abspath(WORKER)


def enrich_one(ld: dict, timeout: int = 45) -> dict:
    """Verrijk 1 listing in een apart subprocess.
    
    Returns: (ld_dict, success_bool)
    - Bij succes: de verrijkte listing
    - Bij fout: de originele listing + error flag
    """
    try:
        proc = subprocess.run(
            [sys.executable, WORKER],
            input=json.dumps({"listing": ld}),
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:200]
            return ld, False, f"exit {proc.returncode}: {stderr}"
        
        result = json.loads(proc.stdout)
        if result.get("ok"):
            return result["listing"], True, None
        else:
            return ld, False, result.get("error", "onbekend")
    except subprocess.TimeoutExpired:
        return ld, False, "timeout"
    except json.JSONDecodeError as e:
        return ld, False, f"JSON decode: {e}"
    except Exception as e:
        return ld, False, f"{type(e).__name__}: {e}"


def enrich_batch(listings: list, platform: str, batch_label: str = "",
                 workers: int = 4, skip_if_rich: bool = True) -> list:
    """Verrijk een batch listings, max `workers` parallel.
    
    Args:
        listings: lijst met dict listings
        platform: platform naam (voor logging)
        batch_label: label voor progress logging
        workers: max parallelle processen
        skip_if_rich: skip listings die al description + photos hebben
    
    Returns: verrijkte listings (originele data behouden + nieuwe velden)
    """
    total = len(listings)
    if total == 0:
        return listings
    
    # Filter: welke moeten we echt ophalen?
    to_enrich = []
    skip_count = 0
    for ld in listings:
        if skip_if_rich and ld.get("description") and len(ld.get("image_urls", [])) > 1:
            skip_count += 1
            to_enrich.append((ld, True))  # True = skip
        else:
            to_enrich.append((ld, False))
    
    if skip_count == total:
        print(f"  ⏩ {batch_label} Alles {skip_count} al rijk — overslaan")
        return listings
    
    print(f"  {batch_label} {total} listings ({skip_count} skip, {total-skip_count} enrich)...")
    
    enriched = list(listings)  # kopie
    done = 0
    failed = 0
    skipped = 0
    
    # Verwerk in parallelle golven
    for start in range(0, total, workers):
        batch = to_enrich[start:start + workers]
        processes = []
        
        for idx_offset, (ld, should_skip) in enumerate(batch):
            idx = start + idx_offset
            if should_skip:
                skipped += 1
                done += 1
                continue
            
            proc = subprocess.Popen(
                [sys.executable, WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append((idx, proc, ld))
        
        # Wacht op processen (parallel)
        for idx, proc, ld in processes:
            try:
                out, err = proc.communicate(
                    input=json.dumps({"listing": ld}),
                    timeout=45,
                )
                if proc.returncode == 0:
                    result = json.loads(out)
                    if result.get("ok"):
                        enriched[idx] = result["listing"]
                        done += 1
                    else:
                        failed += 1
                else:
                    failed += 1
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                failed += 1
            except Exception:
                failed += 1
            
            # Forceer OS cleanup
            proc.stdin = None
            proc.stdout = None
            proc.stderr = None
        
        # GC hint (alleen Python-level)
        import gc
        gc.collect()
        
        # Memory check
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        done_total = done + skipped
        print(f"    [{time.strftime('%H:%M:%S')}] [{done_total}/{total}] "
              f"{done} ok, {failed} fails, {skipped} skip — geheugen: {mem_mb:.0f} MB")
    
    print(f"  ✅ {batch_label}: {done} enriched, {failed} failed, {skipped} skipped")
    return enriched
