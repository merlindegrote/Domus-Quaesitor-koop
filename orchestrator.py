#!/usr/bin/env python3
"""Domus-Quaesitor orchestrator — parallelle batch architectuur

Fases:
1. Collect: 4 parallelle scrapers (elk apart proces, max 200s gezamenlijk)
2. Immovlan: 19 parallelle batches van 5 detailpages (max 60s per batch)
3. Process: merge, dedup, score, email (max 120s)

Robuustheid: harde 550s totale pipeline timeout, kill hangende scrapers.
"""
import subprocess, sys, os, json, math, glob, time, signal, threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor, wait, ALL_COMPLETED

BASE = os.path.dirname(os.path.abspath(__file__))

# Globale tracking van Popen objecten — zodat we ze kunnen killen bij timeout
_ACTIVE_POPENS: list[subprocess.Popen] = []

# Scraper health tracking — results dict keys -> platform names
_SCRAPER_PLATFORM_MAP = {
    "Immoweb": "immoweb",
    "Zimmo": "zimmo",
    "Immoscoop": "immoscoop",
    "2dehands": "2dehands",
    "Immovlan": "immovlan",
}

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def run_script_killable(script, args=None, timeout=120):
    """Run 1 Python script using Popen for force-kill support.
    
    Returns (ok, output, killed) — killed=True means we had to SIGKILL.
    Wordt geregistreerd in _ACTIVE_POPENS voor globale cleanup.
    """
    cmd = [sys.executable, os.path.join(BASE, script)]
    if args:
        cmd.extend(args)
    
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=BASE,
        )
        _ACTIVE_POPENS.append(proc)
        
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            killed = False
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGKILL on timeout
            stdout, stderr = proc.communicate(timeout=5)
            killed = True
            log(f"  ⚠️ KILLED {script} after {timeout}s timeout")
        
        out = (stdout or "").strip()
        err = (stderr or "").strip()[:300]
        
        if proc.returncode == -9:
            killed = True
            return False, f"KILLED ({out[:100]})", True
        if proc.returncode != 0:
            return False, err or out[:200], killed
        return True, out, killed
    except Exception as e:
        return False, str(e), False
    finally:
        if proc and proc in _ACTIVE_POPENS:
            _ACTIVE_POPENS.remove(proc)


def kill_all_active():
    """Kill alle Popen processen die nog hangen."""
    for proc in list(_ACTIVE_POPENS):
        try:
            if proc.poll() is None:  # nog actief
                proc.kill()
                log(f"  💀 Force kill PID {proc.pid}")
                proc.wait(timeout=3)
        except Exception:
            pass
    _ACTIVE_POPENS.clear()


def phase1_collect():
    """Fase 1: 4 parallelle scrapers, gezamenlijke timeout 200s.
    
    Start alle 4 scrapers in eigen thread, wacht max 200s totaal.
    Als een scraper hangt: kill Popen direct, ga door met rest.
    """
    log("FASE 1: Collect — 4 parallelle scrapers (max 200s totaal)")
    
    # Maak output dir leeg
    for f in glob.glob("/tmp/domus-batches/*.json"):
        os.remove(f)
    
    scrapers = [
        ("Immoweb", "phases/run_immoweb.py", 200),
        ("Zimmo", "phases/run_zimmo.py", 200),
        ("Immoscoop", "phases/run_immoscoop.py", 200),
        ("2dehands", "phases/run_2dehands.py", 60),
    ]
    
    results: dict[str, tuple[bool, bool]] = {}  # name -> (ok, killed)
    deadlinems = 200.0
    start_t = time.time()
    
    def run_one(name, script, t):
        ok, out, killed = run_script_killable(script, timeout=t)
        last_line = (out or "").split("\n")[-1][:100]
        return name, ok, killed, last_line
    
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_map = {pool.submit(run_one, n, s, t): n for n, s, t in scrapers}
        all_futures = list(future_map.keys())
        
        # Poll in stappen van 1s tot deadline
        done_set, not_done_set = wait(all_futures, timeout=1, return_when=ALL_COMPLETED)
        while not_done_set and time.time() - start_t < deadlinems:
            done_set, not_done_set = wait(all_futures, timeout=1, return_when=ALL_COMPLETED)
        
        # Na deadline: kill wat hangt
        if not_done_set:
            for f in list(not_done_set):
                name = future_map[f]
                log(f"  ⚠️ {name} hangt na {deadlinems:.0f}s — force kill")
            kill_all_active()
            
            # Wacht nog max 5s tot de threads stoppen (Popen is dood, dus snel)
            done2_set, _ = wait(not_done_set, timeout=5, return_when=ALL_COMPLETED)
            done_set = done_set | done2_set
            not_done_set = not_done_set - done2_set
        
        # Haal resultaten op — alle futures die klaar zijn
        for f in done_set:
            name = future_map[f]
            try:
                _, ok, killed, last_line = f.result(timeout=2)
                status = "✅" if ok else ("💀" if killed else "❌")
                results[name] = (ok, killed)
                log(f"  {status} {name}: {last_line}")
            except Exception as e:
                results[name] = (False, True)
                log(f"  💀 {name}: {e}")
        
        # Futures die nog niet klaar zijn na kill + wait
        for f in not_done_set:
            name = future_map[f]
            results[name] = (False, True)
            log(f"  💀 {name}: timeout {deadlinems:.0f}s, overgeslagen")
    
    # Record health for killed scrapers
    for name, (ok, killed) in results.items():
        if killed and not ok:
            platform = _SCRAPER_PLATFORM_MAP.get(name, name.lower())
            try:
                from scrapers.base import record_scraper_failure
                record_scraper_failure(platform, "Killed by orchestrator timeout")
            except ImportError:
                pass

    # Toon resultaten
    ok_count = sum(1 for ok, _ in results.values() if ok)
    killed_count = sum(1 for _, killed in results.values() if killed)
    log(f"  Collect: {ok_count}/4 scrapers OK, {killed_count} killed")
    
    # Fallback: check partial immoweb data
    immoweb_ok = results.get("Immoweb", (False, False))[0]
    if not immoweb_ok:
        if os.path.exists("/tmp/domus-batches/immoweb.json"):
            try:
                with open("/tmp/domus-batches/immoweb.json") as f:
                    data = json.load(f)
                if data.get("count", 0) > 0:
                    log(f"  🔄 Immoweb partial data gevonden: {data['count']} listings")
            except (json.JSONDecodeError, OSError):
                pass


def phase2_immovlan():
    """Fase 2: Immovlan search → batches van 5 detailpages parallel"""
    log("FASE 2: Immovlan — IDs ophalen + batches van 5")
    
    # 2a: IDs ophalen uit search pages
    ok, out, killed = run_script_killable("phases/extract_immovlan_ids.py", timeout=30)
    if not ok:
        log("  ❌ Immovlan search IDs mislukt, skip")
        return
    log(f"  ✅ Search IDs: {out.split(chr(10))[-1]}")
    
    # Lees IDs
    ids_path = "/tmp/domus-immovlan-ids.json"
    if not os.path.exists(ids_path):
        log("  ⬜ Geen immovlan IDs bestand")
        return
    with open(ids_path) as f:
        ids = json.load(f)
    
    if not ids:
        log("  ⬜ Geen Immovlan IDs")
        return
    
    # 2b: Split in batches van 5
    batches = [(i // 5, ids[i:i+5]) for i in range(0, len(ids), 5)]
    log(f"  📦 {len(ids)} IDs → {len(batches)} batches van 5")
    
    def run_batch(batch_num, batch_ids):
        ids_str = ",".join(batch_ids)
        ok, out, killed = run_script_killable("phases/scrape_batch.py",
                           ["--ids", ids_str, "--batch", str(batch_num)],
                           timeout=120)
        lines = [l for l in out.split("\n") if l.strip()]
        summary = lines[-1][:80] if lines else ""
        return batch_num, ok, summary
    
    batch_results = []
    with ThreadPoolExecutor(max_workers=19) as pool:
        futures = {pool.submit(run_batch, bn, bi): bn for bn, bi in batches}
        for f in as_completed(futures):
            bnum, ok, sm = f.result()
            batch_results.append((bnum, ok))
    
    ok_count = sum(1 for _, ok in batch_results)
    log(f"  Immovlan: {ok_count}/{len(batches)} batches OK")
    
    total = 0
    for fp in glob.glob("/tmp/domus-batches/immovlan_batch_*.json"):
        with open(fp) as f:
            data = json.load(f)
        total += data.get("count", 0)
    log(f"  Immovlan totaal: {total} detail listings")


def phase3_process():
    """Fase 3: merge, dedup, score, email"""
    log("FASE 3: Process — merge + dedup + score + email")
    ok, out, killed = run_script_killable("phases/process_all.py", timeout=1200)
    
    if ok:
        lines = [l for l in out.split("\n") if l.strip()]
        for line in lines[-5:]:
            log(f"  {line}")
    else:
        log(f"  ❌ Process failed: {out[:200]}")


def run_pipeline():
    """Run the full pipeline (used inside ProcessPoolExecutor for hard timeout)."""
    log("🏠 Pipeline gestart")
    phase1_collect()
    phase2_immovlan()
    phase3_process()
    log("🏁 Pipeline klaar")


def main():
    print("\n" + "=" * 55)
    print("🏠 DOMUS-QUAESITOR — Parallelle batch run")
    print("=" * 55)
    
    t0 = time.time()
    
    # Harde pipeline timeout: 550s (onder de 600s cron limiet)
    hard_timeout = 550
    
    pipeline_completed = False
    with ProcessPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_pipeline)
        try:
            future.result(timeout=hard_timeout)
            pipeline_completed = True
        except concurrent.futures.TimeoutError:
            log(f"❌ PIPELINE TIMEOUT na {hard_timeout}s — force kill")
            kill_all_active()
            log("  Fallback: process_all.py met bestaande data")
        except Exception as e:
            log(f"❌ Pipeline error: {e}")
        finally:
            if not pipeline_completed:
                try:
                    executor.shutdown(wait=False)
                except Exception:
                    pass
                kill_all_active()
    
    # Fallback: draai process_all.py met wat er al is in /tmp/domus-batches/
    if not pipeline_completed:
        log("🔁 Fallback: process_all.py met bestaande batch data")
        kill_all_active()
        try:
            ok, out, killed = run_script_killable("phases/process_all.py", timeout=300)
            if ok:
                lines = [l for l in out.split("\n") if l.strip()]
                for line in lines[-5:]:
                    log(f"  {line}")
            else:
                log(f"  ❌ Fallback process_all failed: {out[:200]}")
        except Exception as e:
            log(f"  ❌ Fallback exception: {e}")
    
    elapsed = time.time() - t0
    print(f"\n{'='*55}")
    log(f"🏁 KLAAR in {elapsed/60:.1f} min")
    print("=" * 55)
    
    # Scraper health summary
    try:
        from scrapers.base import show_health_summary
        for line in show_health_summary().split(chr(10)):
            log(line)
    except Exception as e:
        log(f"Health summary unavailable: {e}")

    # Check op achtergebleven processen
    log("🧹 Cleanup check...")
    kill_all_active()
    log("✅ Cleanup OK")

if __name__ == "__main__":
    main()
