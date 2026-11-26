"""Request schemas.

Every string field is stripped before validation, matching the comparison
rule ("remove leading/trailing whitespace").  Unknown fields are rejected so
that clients cannot accidentally smuggle state (e.g. ``is_public``).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class DetailIn(InputModel):
    code: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=500)

    @field_validator("code", "label", "answer")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class ItemCreate(InputModel):
    title: str = Field(min_length=1, max_length=120)
    public_description: str = Field(min_length=1, max_length=2000)
    found_at: date
    found_location: str = Field(min_length=1, max_length=200)
    details: list[DetailIn] = Field(default_factory=list, max_length=50)

    @field_validator("title", "public_description", "found_location")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value

    def detail_codes(self) -> list[str]:
        return [d.code for d in self.details]


class DetailAdd(DetailIn):
    pass


class CorrectionIn(InputModel):
    answer: str = Field(min_length=1, max_length=500)

    @field_validator("answer")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class StaffCreate(InputModel):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class CounterCreate(InputModel):
    name: str = Field(min_length=1, max_length=60)
    code: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("name", "code")
    @classmethod
    def not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("不能为空白")
        return value


class OperatorCreate(StaffCreate):
    # Omitted -> main desk (总台).  May be a counter id or unique code.
    counter_id: str | None = Field(default=None, min_length=1, max_length=64)


class StaffCounterUpdate(InputModel):
    counter_id: str = Field(min_length=1, max_length=64)


class DispatchCreate(InputModel):
    box_no: str = Field(min_length=1, max_length=64)
    # Counter id or unique code of the receiving counter.
    destination_counter_id: str = Field(min_length=1, max_length=64)
    # Internal item ids (public LF- codes are also accepted for convenience).
    item_ids: list[str] = Field(min_length=1)

    @field_validator("box_no", "destination_counter_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class ScannedLabelIn(InputModel):
    # Box number printed on the label (may differ from the box being opened
    # when an item was mis-packed).
    box_no: str = Field(min_length=1, max_length=64)
    # Item identifier printed on the label: public code or internal id.
    item_code: str | None = Field(default=None, min_length=1, max_length=64)
    item_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("box_no")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value

    @model_validator(mode="after")
    def require_one_item_identifier(self):
        if not (self.item_code or self.item_id):
            raise ValueError("标签必须提供 item_code 或 item_id")
        return self


class ReceiptCreate(InputModel):
    # May legitimately be empty: nothing physical was found in the box.
    scanned_labels: list[ScannedLabelIn] = Field(default_factory=list)


class ClaimantCreate(InputModel):
    display_name: str = Field(
        min_length=1,
        max_length=60,
        description="化名即可，请勿填写真实个人数据",
    )

    @field_validator("display_name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class AnswerSubmit(InputModel):
    public_code: str = Field(min_length=1, max_length=32)
    detail_code: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=500)

    @field_validator("public_code", "detail_code", "text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class BookingCreate(InputModel):
    claimant_id: str = Field(min_length=1, max_length=64)
    # Canonical slot id (``slot_YYYYMMDDTHHMMZ``) or an ISO instant on a UTC
    # full/half hour.  Must not have ended yet.
    slot_start: str = Field(
        min_length=1,
        max_length=64,
        description="接待时段：UTC 整点或半点开始的 30 分钟；接受时段 id 或 ISO 时间",
    )


class RosterEntryIn(InputModel):
    staff_id: str = Field(min_length=1, max_length=64)
    # Counter ids or codes where this person may receive during the slot.
    counter_ids: list[str] = Field(min_length=1)


class RosterSet(InputModel):
    # Replaces the slot roster as a whole; an empty list clears the roster.
    entries: list[RosterEntryIn] = Field(default_factory=list, max_length=200)


class CheckInCreate(InputModel):
    # The rostered duty person who actually received this booking.
    reception_staff_id: str = Field(min_length=1, max_length=64)


class LegacySlotAssign(InputModel):
    # Assign a slot once to an existing slotless (legacy) active booking.
    slot_start: str = Field(min_length=1, max_length=64)


class DeliveryCreate(InputModel):
    claimant_id: str = Field(min_length=1, max_length=64)
