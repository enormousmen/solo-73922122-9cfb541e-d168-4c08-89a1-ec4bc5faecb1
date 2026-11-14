"""Request / response schemas for the energy reading API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .timeutil import to_microseconds

MAX_MATCH_WINDOW_US = 24 * 60 * 60 * 1_000_000  # 重叠时段最长 24 小时


class ReadingIn(BaseModel):
    """One cumulative meter reading supplied by a gateway."""

    model_config = ConfigDict(extra="forbid")

    meter_id: str = Field(
        ..., min_length=1, max_length=128, description="电表号"
    )
    source_record_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="源记录号；同一号码只允许对应同一条读数",
    )
    read_at: datetime = Field(
        ...,
        description="UTC 时刻，ISO-8601；带时区的值会换算为 UTC，不带时区按 UTC 解释",
    )
    reading: int = Field(
        ..., ge=0, description="电表累计读数（非负整数，单位：度）"
    )
    meter_replaced: bool = Field(
        False, description="该读数起启用新电表；为 true 时与上一读数之间记断点"
    )

    @field_validator("meter_id", "source_record_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("reading", mode="before")
    @classmethod
    def _integral_reading(cls, value: object) -> object:
        # Accept integer literals, integral floats and digit strings; reject
        # booleans, fractions and negative values.
        if isinstance(value, bool):
            raise ValueError("reading must be an integer, not a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("reading must be an integer")
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
            raise ValueError("reading must be a non-negative integer")
        return value

    @field_validator("meter_replaced", mode="before")
    @classmethod
    def _flag(cls, value: object) -> object:
        # Accept true/false and the common gateway encodings 1/0.
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError("meter_replaced must be true, false, 1 or 0")


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readings: list[ReadingIn] = Field(
        ..., min_length=1, max_length=1000, description="一批读数，整批原子入仓"
    )


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="按源记录号撤销；不存在的号码按“无变化”处理",
    )

    @field_validator("source_record_ids")
    @classmethod
    def _ids_not_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("source_record_ids must not contain blank values")
            cleaned.append(value)
        return cleaned


class RecordResult(BaseModel):
    source_record_id: str
    status: Literal["created", "unchanged", "removed", "not_found"]


class MeterSummary(BaseModel):
    meter_id: str
    reading_count: int
    interval_count: int
    usage_interval_count: int
    break_count: int
    usage_total: int
    first_read_at: str | None
    last_read_at: str | None


class MutationResponse(BaseModel):
    commit_seq: int
    changed: bool
    counts: dict[str, int]
    records: list[RecordResult]
    affected_meters: list[MeterSummary]


class ReadingOut(BaseModel):
    source_record_id: str
    read_at: str
    reading: int
    meter_replaced: bool
    commit_seq: int


class IntervalOut(BaseModel):
    from_source_record_id: str
    to_source_record_id: str
    from_read_at: str
    to_read_at: str
    from_reading: int
    to_reading: int
    usage: int | None = Field(
        description="区间用电量；断点区间为 null（不推算该区间用量）"
    )
    is_break: bool
    break_reason: Literal["meter_replaced", "counter_rollback"] | None
    commit_seq: int


class QueryWindow(BaseModel):
    # populate_by_name lets the service build the response with `from_=` while
    # the wire format keeps the `from` alias.
    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    applied: bool


class MeterStateResponse(BaseModel):
    meter_id: str
    commit_seq: int
    window: QueryWindow
    readings: list[ReadingOut]
    intervals: list[IntervalOut]
    breaks: list[IntervalOut]
    usage_total: int


class HealthResponse(BaseModel):
    status: str
    commit_seq: int
    schema_version: int


class ConflictDetail(BaseModel):
    type: str
    source_record_id: str | None
    conflicting_source_record_id: str | None
    detail: str


class ConflictResponse(BaseModel):
    code: Literal["batch_rejected"]
    message: str
    conflicts: list[ConflictDetail]


class ErrorResponse(BaseModel):
    code: str
    message: str


class MatchWindow(BaseModel):
    """UTC overlap period in which the old and new ids are compared."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: datetime = Field(
        ..., alias="from", description="重叠时段起点（含），ISO-8601，按 UTC 解释"
    )
    to: datetime = Field(
        ..., description="重叠时段终点（含），ISO-8601，按 UTC 解释"
    )

    @model_validator(mode="after")
    def _bounds(self) -> "MatchWindow":
        start = to_microseconds(self.from_)
        end = to_microseconds(self.to)
        if end <= start:
            raise ValueError("window.to 必须晚于 window.from")
        if end - start > MAX_MATCH_WINDOW_US:
            raise ValueError("重叠时段最长 24 小时")
        return self


class MatchRequest(BaseModel):
    """Old/new meter-id matching request (read-only)."""

    model_config = ConfigDict(extra="forbid")

    old_meter_ids: list[str] = Field(
        ..., min_length=2, max_length=6, description="旧上报编号，2～6 个且互异"
    )
    new_meter_ids: list[str] = Field(
        ...,
        min_length=2,
        max_length=6,
        description="新上报编号，数量与旧号相同，互异且不得与旧号重复",
    )
    window: MatchWindow

    @field_validator("old_meter_ids", "new_meter_ids")
    @classmethod
    def _clean_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("meter ids must not be blank")
            cleaned.append(value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("meter ids must be distinct")
        return cleaned

    @model_validator(mode="after")
    def _consistent(self) -> "MatchRequest":
        if len(self.old_meter_ids) != len(self.new_meter_ids):
            raise ValueError("old_meter_ids 与 new_meter_ids 的数量必须相同")
        duplicated = sorted(set(self.old_meter_ids) & set(self.new_meter_ids))
        if duplicated:
            raise ValueError(f"旧号与新号必须互异: {duplicated}")
        return self


class MatchEvidenceSegment(BaseModel):
    """One consecutive equal-usage segment backing a matched pair."""

    from_read_at: str
    to_read_at: str
    usage: int
    old_from_source_record_id: str
    old_to_source_record_id: str
    new_from_source_record_id: str
    new_to_source_record_id: str


class MatchedPair(BaseModel):
    old_meter_id: str
    new_meter_id: str
    evidence: list[MatchEvidenceSegment] = Field(
        description="连续三段相同用量的共同有效区间（含双方源记录号）"
    )


class MatchResponse(BaseModel):
    status: Literal[
        "matched", "insufficient_evidence", "no_complete_matching", "ambiguous"
    ] = Field(
        description=(
            "matched=全局一对一对应唯一；insufficient_evidence=证据不足；"
            "no_complete_matching=候选拼不成完整对应；ambiguous=存在多种对应"
        )
    )
    commit_seq: int = Field(description="本次比较所用的已提交快照序号")
    window: QueryWindow
    pairs: list[MatchedPair] = Field(
        description="仅 status=matched 时返回全部配对；其余情况为空，不猜测部分配对"
    )
    reason: str | None = Field(description="未唯一匹配时的原因说明")
    details: dict[str, Any] | None = Field(
        default=None, description="补充诊断信息（缺候选的编号、候选对数等）"
    )
