"""Runs the NFR1 load test end to end and saves the result to eval/results/load_nfr1.json.

Starts the API (2 Uvicorn workers, as in the architecture) on the dev database, makes 5 delivered reports
in-process, then runs Locust headless with 50 users for 3 minutes on the same machine.
Run: python perf/run_load.py
"""
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from common import HORMUZ, client_and_key, prepare, run_report, save  # noqa: E402

USERS, SPAWN, DURATION, PORT = 50, 10, "3m", 8766


def main():
    prepare()
    client, key = client_and_key()
    ids = [run_report(client, key, dict(HORMUZ, questions=[HORMUZ["questions"][0] + f" (load {i})"], purpose="eval"))
           for i in range(5)]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "masader.api.app:app", "--port", str(PORT), "--workers", "2",
                            "--log-level", "warning"], cwd=ROOT, env=env, start_new_session=True)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/readyz").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        out = ROOT / "eval" / "results" / "load"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "locust", "-f", str(ROOT / "perf" / "locustfile.py"), "--headless",
                        "-u", str(USERS), "-r", str(SPAWN), "-t", DURATION, "--host", f"http://127.0.0.1:{PORT}",
                        "--csv", str(out / "nfr1"), "--only-summary"], env={**env, "LOAD_API_KEY": key,
                        "LOAD_REPORT_IDS": ",".join(ids)}, check=False)
    finally:
        os.killpg(api.pid, signal.SIGTERM)
        api.wait(20)
    rows = list(csv.DictReader(open(out / "nfr1_stats.csv")))
    res = {r["Name"]: {"requests": int(r["Request Count"]), "failures": int(r["Failure Count"]),
                       "p50_ms": float(r["50%"]), "p95_ms": float(r["95%"]), "p99_ms": float(r["99%"]),
                       "rps": round(float(r["Requests/s"]), 1)} for r in rows}
    agg = res.get("Aggregated", {})
    save("load_nfr1", {"what": "NFR1 read load: 50 users, 3 minutes, API with 2 Uvicorn workers on the same host as Locust",
                       "target": "p95 < 2000 ms", "users": USERS, "duration": DURATION, "report_ids": ids,
                       "by_endpoint": res, "met": bool(agg) and agg["p95_ms"] < 2000 and agg["failures"] == 0})
    print(agg)


if __name__ == "__main__":
    main()
