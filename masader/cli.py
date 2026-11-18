"""Command line: `python -m masader.cli <command>` (also `masader <command>` once installed).

    migrate                      apply database migrations
    seed [--fixtures]            budgets, source register and, with --fixtures, the SYNTHETIC sample corpus
    key create NAME EMAIL ROLE   create an API key (printed once)
    key revoke KEY_ID
    delete-user EMAIL            delete a user's requests, reports and keys (FR15, privacy)
    reap                         run the reaper once
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config
from .ids import key_hash, new_api_key
from .settings import settings
from .store.db import migrate, pool


def seed_budgets() -> None:
    b = config.budgets()
    with pool().connection() as c:
        for purpose, cap in b["caps_usd"].items():
            c.execute("INSERT INTO budgets (purpose, period_start, cap_usd) VALUES (%s,%s,%s) "
                      "ON CONFLICT (purpose, period_start) DO UPDATE SET cap_usd=EXCLUDED.cap_usd",
                      (purpose, settings.budget_period, cap))


def seed(fixtures: bool) -> dict:
    from .processing.ingest import sync_sources

    seed_budgets()
    out = {"sources": len(sync_sources())}
    if fixtures:
        from .connectors import fixture, worldbank
        from .processing.ingest import ingest

        out["new_items"] = len(ingest(fixture.raw_items()))
        out["statistics"] = worldbank.store_statistics("fx.worldbank.stats", worldbank.load_fixture(fixture.WORLDBANK))
    return out


def create_key(name: str, email: str, role: str, rate: int = 10) -> tuple[str, str]:
    key = new_api_key(role)
    with pool().connection() as c:
        kid = c.execute("INSERT INTO api_keys (holder_name, holder_email, role, key_prefix, key_hash, rate_per_hour) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (name, email, role, key[:8], key_hash(key), rate)).fetchone()["id"]
        c.execute("INSERT INTO audit_log (action, object_id, details) VALUES ('key.create', %s, %s)",
                  (str(kid), json.dumps({"role": role, "via": "cli"})))
    return str(kid), key


def revoke_key(key_id: str) -> bool:
    with pool().connection() as c:
        n = c.execute("UPDATE api_keys SET revoked_at=now() WHERE id::text=%s AND revoked_at IS NULL", (key_id,)).rowcount
        c.execute("INSERT INTO audit_log (action, object_id) VALUES ('key.revoke', %s)", (key_id,))
    return n == 1


def delete_user(email: str) -> dict:
    """Removes the user's requests, unpublished reports (with their claims, through ON DELETE CASCADE)
    and keys. Published reports are public records: they are kept but detached from the person."""
    from .store.objectstore import get_store

    store = get_store()
    with pool().connection() as c, c.transaction():
        keys = [r["id"] for r in c.execute("SELECT id FROM api_keys WHERE holder_email=%s", (email,))]
        if not keys:
            return {"keys": 0}
        reps = c.execute("SELECT r.id, r.publication::text AS publication, r.json_object_key, r.html_object_key FROM reports r "
                         "JOIN requests q ON q.id=r.request_id WHERE q.api_key_id = ANY(%s)", (keys,)).fetchall()
        private = [r for r in reps if r["publication"] != "published"]
        ids = [r["id"] for r in private]
        c.execute("DELETE FROM corrections WHERE report_id = ANY(%s) OR reported_by = ANY(%s)", (ids, keys))
        c.execute("UPDATE reports SET supersedes=NULL WHERE supersedes = ANY(%s)", (ids,))
        c.execute("DELETE FROM reports WHERE id = ANY(%s)", (ids,))
        kept = c.execute("SELECT count(*) AS n FROM reports r JOIN requests q ON q.id=r.request_id "
                         "WHERE q.api_key_id = ANY(%s)", (keys,)).fetchone()["n"]
        if kept:   # published reports stay; their request is kept with the personal fields cleared
            c.execute("UPDATE requests SET payload = payload - 'entities', redactions='[]', clarification=NULL "
                      "WHERE api_key_id = ANY(%s) AND id IN (SELECT request_id FROM reports)", (keys,))
        c.execute("DELETE FROM requests WHERE api_key_id = ANY(%s) AND id NOT IN (SELECT request_id FROM reports)", (keys,))
        c.execute("UPDATE api_keys SET holder_name='deleted user', holder_email='deleted-' || id::text || '@invalid', "
                  "revoked_at=coalesce(revoked_at, now()) WHERE id = ANY(%s)", (keys,))
        c.execute("INSERT INTO audit_log (action, details) VALUES ('user.delete', %s)",
                  (json.dumps({"keys": len(keys), "reports_deleted": len(ids), "published_kept": kept}),))
    for r in private:
        for k in (r["json_object_key"], r["html_object_key"]):
            if k:
                store.delete(k)
    return {"keys": len(keys), "reports_deleted": len(ids), "published_kept": kept}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="masader")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    s = sub.add_parser("seed")
    s.add_argument("--fixtures", action="store_true")
    k = sub.add_parser("key")
    ks = k.add_subparsers(dest="kcmd", required=True)
    kc = ks.add_parser("create")
    kc.add_argument("name")
    kc.add_argument("email")
    kc.add_argument("role", choices=["requester", "reviewer", "admin"])
    kc.add_argument("--rate", type=int, default=10)
    kr = ks.add_parser("revoke")
    kr.add_argument("key_id")
    d = sub.add_parser("delete-user")
    d.add_argument("email")
    sub.add_parser("reap")
    sub.add_parser("openapi")
    sub.add_parser("retention")
    a = p.parse_args(argv)
    if a.cmd == "migrate":
        print(json.dumps({"applied": migrate()}))
    elif a.cmd == "seed":
        print(json.dumps(seed(a.fixtures)))
    elif a.cmd == "key" and a.kcmd == "create":
        kid, key = create_key(a.name, a.email, a.role, a.rate)
        print(json.dumps({"key_id": kid, "api_key": key, "note": "shown once; store it in a password manager"}))
    elif a.cmd == "key" and a.kcmd == "revoke":
        print(json.dumps({"revoked": revoke_key(a.key_id)}))
    elif a.cmd == "delete-user":
        print(json.dumps(delete_user(a.email)))
    elif a.cmd == "reap":
        from .pipeline.tasks import reaper
        print(json.dumps(reaper()))
    elif a.cmd == "retention":
        from .pipeline.retention import run_retention
        print(json.dumps(run_retention()))
    elif a.cmd == "openapi":
        from .api.app import app
        print(json.dumps(app.openapi(), indent=1, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
