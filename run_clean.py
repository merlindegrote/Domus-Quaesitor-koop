#!/usr/bin/env python3
"""Run process_all.py with resource limits and reliable monitoring."""
import subprocess, sys, os, time, signal

os.chdir("/config/.openclaw/workspace/domus-quaesitor")
log = open("/tmp/domus_run_final.log", "w", buffering=1)

# Use pipe for real-time output
proc = subprocess.Popen(
    [sys.executable, "-u", "phases/process_all.py"],
    stdout=log, stderr=subprocess.STDOUT,
)

def check(proc, start):
    elapsed = time.time() - start
    if elapsed > 1800:  # 30 min hard limit
        print(f"\n=== HARD TIMEOUT {elapsed:.0f}s ===")
        proc.kill()
        return True
    return False

start = time.time()
while proc.poll() is None:
    time.sleep(30)
    if proc.returncode is not None:
        break

log.close()
elapsed = time.time() - start
print(f"\n=== EXIT CODE: {proc.returncode} (elapsed: {elapsed:.0f}s) ===")
print("=== LAST 10 LINES ===")
with open("/tmp/domus_run_final.log") as f:
    lines = f.readlines()
    for l in lines[-10:]:
        print(l.rstrip())
print("=== DONE ===")
