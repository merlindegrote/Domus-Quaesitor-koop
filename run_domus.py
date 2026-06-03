#!/usr/bin/env python3
"""Standalone runner for process_all.py - saves exit code to file."""
import os, subprocess, sys, time

logfile = "/tmp/domus_run.log"
statusfile = "/tmp/domus_status"

with open(logfile, "w") as f:
    f.write("")

proc = subprocess.Popen(
    [sys.executable, "-u", "phases/process_all.py"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    stdout=open(logfile, "a"),
    stderr=subprocess.STDOUT,
    preexec_fn=os.setpgrp,  # new process group so signals don't propagate
)

with open(statusfile, "w") as f:
    f.write(f"STARTED {proc.pid} {time.time()}\n")

proc.wait()

with open(statusfile, "a") as f:
    f.write(f"EXIT {proc.returncode} {time.time()}\n")
