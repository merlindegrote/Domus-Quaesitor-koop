#!/usr/bin/env python3
"""Domus-Quaesitor orchestrator — parallelle batch architectuur

Fases:
1. Collect: 5 parallelle scrapers (elk apart proces, max 30s)
2. Immovlan: 19 parallelle batches van 5 detailpages (max 60s per batch)
3. Process: merge, dedup, score, email (max 120s)

Error handling: fase faalt → skip, 1 batch faalt → rest gaat door.
"""
import subprocess, sys, os, json, math, glob, time
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def run_script(script, args=None, timeout=120):
    """Run 1 Python script, return (ok, output)"""
    cmd = [sys.executable, os.path.join(BASE, script)]
    if args:
        cmd.extend(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=BASE)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()[:300]
        if r.returncode != 0:
            return False, err or out
        return True, out
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)

def phase1_collect():
    """Fase 1: 5 parallelle scrapers, elk apart proces"""
    log("FASE 1: Collect — 5 parallelle scrapers")
    
    # Maak output dir leeg
    for f in glob.glob("/tmp/domus-batches/*.json"):
        os.remove(f)
    
    results = []
    scrapers = [
        ("Immoweb", "phases/run_immoweb.py", 60),
        ("Zimmo", "phases/run_zimmo.py", 60),
        ("Immoscoop", "phases/run_immoscoop.py", 180),
        ("2dehands", "phases/run_2dehands.py", 60),
    ]
    
    def run_one(name, script, t):
        ok, out = run_script(script, timeout=t)
        return name, ok, out
    
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_one, n, s, t): n for n, s, t in scrapers}
        for f in as_completed(futures):
            name, ok, out = f.result()
            status = "✅" if ok else "❌"
            results.append((name, ok))
            # Laatste regel van output
            last_line = (out or "").split("\n")[-1][:100]
            log(f"  {status} {name}: {last_line}")
    
    # Toon resultaten
    ok_count = sum(1 for _, ok in results if ok)
    log(f"  Collect: {ok_count}/4 scrapers OK")

def phase2_immovlan():
    """Fase 2: Immovlan search → batches van 5 detailpages parallel"""
    log("FASE 2: Immovlan — IDs ophalen + batches van 5")
    
    # 2a: IDs ophalen uit search pages
    ok, out = run_script("phases/extract_immovlan_ids.py", timeout=30)
    if not ok:
        log("  ❌ Immovlan search IDs mislukt, skip")
        return
    log(f"  ✅ Search IDs: {out.split(chr(10))[-1]}")
    
    # Lees IDs
    with open("/tmp/domus-immovlan-ids.json") as f:
        ids = json.load(f)
    
    if not ids:
        log("  ⬜ Geen Immovlan IDs")
        return
    
    # 2b: Split in batches van 5
    batches = [(i // 5, ids[i:i+5]) for i in range(0, len(ids), 5)]
    log(f"  📦 {len(ids)} IDs → {len(batches)} batches van 5")
    
    def run_batch(batch_num, batch_ids):
        ids_str = ",".join(batch_ids)
        ok, out = run_script("phases/scrape_batch.py", 
                           ["--ids", ids_str, "--batch", str(batch_num)], 
                           timeout=120)
        # Laatste niet-lege regel uit output
        lines = [l for l in out.split("\n") if l.strip()]
        summary = lines[-1][:80] if lines else ""
        return batch_num, ok, summary
    
    batch_results = []
    with ThreadPoolExecutor(max_workers=19) as pool:
        futures = {pool.submit(run_batch, bn, bi): bn for bn, bi in batches}
        for f in as_completed(futures):
            bnum, ok, sm = f.result()
            batch_results.append((bnum, ok))
    
    # Tel resultaten
    ok_count = sum(1 for _, ok in batch_results)
    log(f"  Immovlan: {ok_count}/{len(batches)} batches OK")
    
    # Tel total listings
    total = 0
    for fp in glob.glob("/tmp/domus-batches/immovlan_batch_*.json"):
        with open(fp) as f:
            data = json.load(f)
        total += data.get("count", 0)
    log(f"  Immovlan totaal: {total} detail listings")

def phase3_process():
    """Fase 3: merge, dedup, score, email"""
    log("FASE 3: Process — merge + dedup + score + email")
    ok, out = run_script("phases/process_all.py", timeout=300)
    
    if ok:
        # Toon laatste paar regels
        lines = [l for l in out.split("\n") if l.strip()]
        for line in lines[-5:]:
            log(f"  {line}")
    else:
        log(f"  ❌ Process failed: {out[:200]}")

def main():
    print("\n" + "=" * 55)
    print("🏠 DOMUS-QUAESITOR — Parallelle batch run")
    print("=" * 55)
    
    t0 = time.time()
    
    phase1_collect()
    phase2_immovlan()
    phase3_process()
    
    elapsed = time.time() - t0
    print(f"\n{'='*55}")
    log(f"🏁 KLAAR in {elapsed/60:.1f} min")
    print("=" * 55)

if __name__ == "__main__":
    main()
