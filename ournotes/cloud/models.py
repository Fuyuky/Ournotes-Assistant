"""Versioned, strict final-result and service request contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_serializer, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Card(Model):
    id: StrictInt = Field(gt=0)
    kind: Literal["member", "support"]
    rarity: Literal["SSR", "SR"]
    title: str = Field(max_length=500)
    name: str = Field(max_length=500)


class CardCount(Model):
    card: Card
    count: StrictInt = Field(gt=0)


class Counts(Model):
    SSR: StrictInt = Field(ge=0)
    SR: StrictInt = Field(ge=0)
    R: StrictInt = Field(default=0, ge=0)


class Totals(Model):
    member: Counts
    support: Counts


class ActivitySnapshot(Model):
    id: str = Field(min_length=1, max_length=100)
    pool_key: str = Field(min_length=1, max_length=100)
    gacha_id: StrictInt = Field(gt=0)
    name: str = Field(max_length=500)
    label: str = Field(max_length=500)
    pickup_card_keys: list[str] = Field(max_length=1000)

    @model_validator(mode='after')
    def identities(self):
        import re
        if (len(self.pickup_card_keys) != len(set(self.pickup_card_keys))
                or any(not re.fullmatch(r'(member|support):[1-9]\d*', key) for key in self.pickup_card_keys)):
            raise ValueError('Invalid activity pickup identities')
        return self


class PresetSnapshot(Model):
    key: str = Field(min_length=1, max_length=100)
    version: StrictInt | None = Field(default=None, ge=0)
    plan: list[dict] = Field(min_length=1, max_length=100000)

    @model_validator(mode='after')
    def validate_plan(self):
        import re
        for entry in self.plan:
            if (set(entry) - {'pool_key', 'resource', 'pulls', 'until_owned', 'fallback_pool_key'}
                    or not isinstance(entry.get('pool_key'), str)
                    or not re.fullmatch(r'[a-z0-9-]{1,100}', entry['pool_key'])
                    or entry.get('resource') not in ('stars', 'ticket')
                    or type(entry.get('pulls')) is not int or not 1 <= entry['pulls'] <= 1000):
                raise ValueError('Invalid preset plan snapshot')
        return self


class Result(Model):
    schema_version: Literal[1]
    account_id: str = Field(min_length=1, max_length=128)
    index: StrictInt = Field(ge=0)
    email: str = Field(min_length=3, max_length=254)
    cards: list[CardCount] = Field(max_length=1000)
    totals: Totals
    pull_count: StrictInt = Field(gt=0, le=100000)
    pool_keys: list[str] = Field(default_factory=list, max_length=100)
    draw_labels: list[str] = Field(default_factory=list, max_length=100)
    used: StrictBool
    activity_id: str | None = Field(default=None, max_length=100)
    activity: ActivitySnapshot | None = None
    preset_snapshot: PresetSnapshot | None = None

    @model_serializer(mode='wrap')
    def compatible_payload(self, handler):
        value = handler(self)
        if not self.pool_keys:
            value.pop('pool_keys', None)
        for key in ('activity_id', 'activity', 'preset_snapshot'):
            if key not in self.model_fields_set:
                value.pop(key, None)
        return value


    @model_validator(mode="after")
    def complete(self):
        counts, identities = {}, set()
        for entry in self.cards:
            key = (entry.card.kind, entry.card.id)
            if key in identities:
                raise ValueError("Duplicate card identity")
            identities.add(key)
            rarity_key = (entry.card.kind, entry.card.rarity)
            counts[rarity_key] = counts.get(rarity_key, 0) + entry.count
        total = 0
        for kind in ("member", "support"):
            for rarity, count in getattr(self.totals, kind).model_dump().items():
                if rarity != "R" and counts.get((kind, rarity), 0) != count:
                    raise ValueError("Card counts disagree with totals")
                total += count
        if total != self.pull_count:
            raise ValueError("Final result totals disagree with pull_count")
        return self


class Allocation(Model):
    request_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    channel: Literal["test", "formal"] = "formal"


class Upload(Model):
    event_id: str = Field(min_length=1, max_length=128)
    version: StrictInt = Field(gt=0)
    result: Result


class Used(Model):
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: StrictInt = Field(gt=0)
    used: StrictBool


class Usage(Model):
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: StrictInt = Field(gt=0)
    target_status: Literal['available', 'booked', 'used']


class TicketRequest(Model):
    request_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    alias: str = Field(max_length=254)
    timeout: StrictInt = Field(default=120, ge=30, le=300)


class TicketRetry(Model):
    request_id: str = Field(min_length=1, max_length=128)
    reason: Literal['game_rejected', 'expired', 'input_misdirected', 'proxy_limit']
    timeout: StrictInt = Field(default=120, ge=30, le=300)


class Publish(Model):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: StrictInt = Field(ge=0)
    limit: StrictInt | None = Field(default=None, gt=0, le=100000)


class ResultSubset(Model):
    account_ids: list[str] = Field(min_length=1, max_length=200)


class HighWater(Model):
    test_next_index: StrictInt = Field(ge=8, le=1 << 20)
    formal_next_index: StrictInt = Field(ge=1 << 20)
    evidence: str = Field(min_length=8, max_length=4000)


class Historical(Model):
    request_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    channel: Literal["test", "formal"]
    result: Result
