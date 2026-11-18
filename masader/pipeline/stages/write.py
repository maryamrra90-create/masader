"""Stage 6, write: the Writer drafts the brief from labelled claims only; insufficient claims are not
sent (architecture D.2 step 10, FR9)."""
from __future__ import annotations

from ..kit import Ctx, StageResult, run_calls
from .brief import labelled_claims

WRITER_FIELDS = ("id", "text", "type", "speaker", "numbers", "label", "low_reliability", "reason",
                 "attribution_required", "outlet", "versions")


def run(ctx: Ctx) -> StageResult:
    claims = labelled_claims(ctx.report_id, ctx.output("label"))
    usable = [{k: c[k] for k in WRITER_FIELDS} for c in claims if c["label"] != "insufficient"]
    req = {k: ctx.request[k] for k in ("topic", "questions", "from", "to") if k in ctx.request}
    if not usable:
        ctx.notes.append("write: no claim had enough evidence; the brief has no findings")
        return StageResult({"sections": {}, "writer_model": None})
    res = run_calls(ctx, "writer", [{"request": req, "claims": usable}])[0]
    return StageResult({"sections": res.output["sections"], "writer_model": res.model})
