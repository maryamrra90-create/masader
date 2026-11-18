"""NFR1 load test: report and status API calls answer in under 2 s (p95) with 50 concurrent users.

Each simulated user reads delivered reports (JSON with ETag revalidation, the HTML export) and polls report
status, with 1-3 s think time. Report ids and the key come from the environment (perf/run_load.py sets them).
Run: locust -f perf/locustfile.py --headless -u 50 -r 10 -t 3m --host http://127.0.0.1:8000
"""
import os
import random

from locust import HttpUser, between, task

KEY = os.environ["LOAD_API_KEY"]
REPORTS = os.environ["LOAD_REPORT_IDS"].split(",")


class Reader(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.client.headers.update({"X-API-Key": KEY})
        self.etags: dict[str, str] = {}

    @task(5)
    def report_json(self):
        rid = random.choice(REPORTS)
        h = {"If-None-Match": self.etags[rid]} if rid in self.etags and random.random() < 0.5 else {}
        with self.client.get(f"/v1/reports/{rid}", headers=h, name="/v1/reports/{id}", catch_response=True) as r:
            if r.status_code in (200, 304):
                if "ETag" in r.headers:
                    self.etags[rid] = r.headers["ETag"]
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")

    @task(2)
    def report_html(self):
        self.client.get(f"/v1/reports/{random.choice(REPORTS)}.html", name="/v1/reports/{id}.html")

    @task(3)
    def status(self):
        self.client.get(f"/v1/reports/{random.choice(REPORTS)}", name="/v1/reports/{id} (status poll)")

    @task(1)
    def me(self):
        self.client.get("/v1/me")
