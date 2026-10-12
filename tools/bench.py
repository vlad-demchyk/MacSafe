"""Repeatable cost check of the sampling code: milliseconds per call, and the agent's own CPU and memory.

    python3 tools/bench.py            costs of the sampling functions
    python3 tools/bench.py watch PID  CPU, memory and thread count of a running process, 6 samples 5 s apart
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def cost(func, runs=20):
    func()
    start = time.perf_counter()
    for _ in range(runs):
        func()
    return (time.perf_counter() - start) / runs * 1000


def functions():
    import history
    import metrics
    import thermal_hid
    return [
        ("take_quick (every cycle)", metrics.take_quick, 50),
        ("take_processes (panel open)", metrics.take_processes, 10),
        ("thermal_hid.read (every 3 s)", thermal_hid.read, 20),
        ("battery_power, ioreg (every 5 s)", history.battery_power, 5),
        ("gpu_percent", metrics.gpu_percent, 50),
    ]


def watch(pid):
    out = subprocess.run(["top", "-pid", str(pid), "-stats", "pid,cpu,mem,idlew,threads", "-l", "6", "-s", "5"],
                         capture_output=True, text=True).stdout
    print("\n".join(line for line in out.splitlines() if line.startswith(str(pid))))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "watch":
        watch(int(sys.argv[2]))
    else:
        for label, func, runs in functions():
            print(f"{label:38s} {cost(func, runs):8.2f} ms")
