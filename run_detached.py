#!/usr/bin/env python3
"""Complete detached runner for process_all.py.
Writes exit code to /tmp/domus_exit when done.
"""
import os, subprocess, sys

logfile = "/tmp/domus_run.log"
exitfile = "/tmp/domus_exit"
cwd = os.path.dirname(os.path.abspath(__file__))

# Kill any leftover
for name in ["process_all.py", "curl_runner.py"]:
    subprocess.run(["pkill", "-9", "-f", name], capture_output=True)

# Clean state
for f in [logfile, exitfile]:
    try:
        os.remove(f)
    except OSError:
        pass

with open(logfile, "w"):
    pass

proc = subprocess.Popen(
    [sys.executable, "-u", "phases/process_all.py"],
    cwd=cwd, stdout=open(logfile, "a"), stderr=subprocess.STDOUT,
    preexec_fn=lambda: os.system("setpgrp"),
)

with open(exitfile, "w") as f:
    f.write(str(proc.pid) + "\n")

proc.wait()

with open(exitfile, "w") as f:
    f.write(str(proc.returncode) + "\n")
