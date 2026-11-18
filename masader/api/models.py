"""Public API contract v1 (Pydantic models, architecture C.3). The OpenAPI document generated from these
models is committed as docs/openapi.v1.json and checked by a test, so a removed or renamed field fails CI.

One change from C.3 (deviation D-07): sections hold sentences that reference findings
({text, findings: [ids]}) instead of bare finding ids, because a brief sentence can combine findings."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourcePrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefer: list[Literal["official", "wire", "regional"]] = []
    exclude: list[str] = Field(default=[], max_length=20)


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    topic: str = Field(min_length=3, max_length=200)
    questions: list[str] = Field(min_length=1, max_length=5)
    date_from: dt.date = Field(alias="from")
    date_to: dt.date = Field(alias="to")
    geography: list[str] = Field(default=[], max_length=10)          # ISO 3166-1 alpha-3
    entities: list[str] = Field(default=[], max_length=10)
    sources: SourcePrefs = SourcePrefs()
    language: Literal["en"] = "en"                                   # "ar" arrives with stage 2
    format: Literal["brief"] = "brief"
    purpose: Literal["live", "eval", "dev"] = "live"                 # budget the calls are charged to

    @field_validator("questions")
    @classmethod
    def _q_len(cls, v):
        if any(not (5 <= len(q) <= 300) for q in v):
            raise ValueError("each question must be 5-300 characters")
        return v

    @field_validator("geography")
    @classmethod
    def _iso3(cls, v):
        if any(len(c) != 3 or not c.isalpha() or not c.isupper() for c in v):
            raise ValueError("geography uses ISO 3166-1 alpha-3 codes")
        return v

    @model_validator(mode="after")
    def _dates(self):
        if self.date_to < self.date_from:
            raise ValueError("'to' is before 'from'")
        if (self.date_to - self.date_from).days > 366:
            raise ValueError("date range is longer than 12 months")
        return self


class Question(BaseModel):
    id: str
    text: str
    default: str
    options: list[str]


class Accepted(BaseModel):
    request_id: str
    report_id: str
    status: Literal["queued", "delivered"]
    status_url: str
    cached: bool = False


class NeedsClarification(BaseModel):
    request_id: str
    status: Literal["needs_clarification"]
    questions: list[Question]
    answers_url: str


class SourceRef(BaseModel):
    outlet: str
    tier: Literal["A", "B", "C"]
    url: str
    published: dt.datetime
    passage: str
    first_hand: bool
    origin_group: int | None
    verdict: Literal["supports", "contradicts"]


class Reliability(BaseModel):
    best_tier: Literal["A", "B", "C"]
    primary_source: bool
    low_reliability: bool
    reason: str


class Finding(BaseModel):
    id: str
    claim_id: int
    text: str
    label: Literal["corroborated", "single_source", "disputed", "insufficient"]
    origin_groups: int
    reliability: Reliability
    sources: list[SourceRef]


class Sentence(BaseModel):
    text: str
    findings: list[str]


class StageState(BaseModel):
    stage: str
    status: str
    attempts: int


class ReportOut(BaseModel):
    report_id: str
    request_id: str
    status: str
    publication: str
    version: int
    ai_generated: bool = True
    review_status: Literal["not_reviewed", "reviewed"]
    stages: list[StageState] = []
    title: str | None = None
    findings: list[Finding] = []
    sections: dict[str, list[Sentence] | list[str]] = {}
    statistics: list[dict] = []
    exports: dict[str, str] = {}
    versions: dict[str, str] = {}
    method: dict = {}
    notes: list[str] = []
    cost_usd: float = 0.0
    duration_s: int | None = None
    supersedes: str | None = None
    superseded_by: str | None = None


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: int | None = None
    reason: str = Field(min_length=5, max_length=1000)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool
    note: str = Field(min_length=3, max_length=1000)
    corrected_text: str | None = Field(default=None, max_length=600)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field(min_length=3, max_length=1000)


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holder_name: str = Field(min_length=2, max_length=100)
    holder_email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["requester", "reviewer", "admin"] = "requester"
    rate_per_hour: int = Field(default=10, ge=1, le=1000)


class ErrorBody(BaseModel):
    code: str
    message: str
    trace_id: str


class ErrorOut(BaseModel):
    error: ErrorBody
