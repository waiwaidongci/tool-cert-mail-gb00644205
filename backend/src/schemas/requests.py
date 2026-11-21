from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False


class TransferRequest(Request):
    dataset_id: int = Field(gt=0)
    station_id: str = Field(min_length=1, max_length=40)
    work_area: str = Field(default='', max_length=60)
    demand_asset_id: str = Field(min_length=1, max_length=40)
    spare_id: str = Field(min_length=1, max_length=40)
    shift_start: datetime
    shift_end: datetime
    report_day: date | None = None


class ReleaseRequest(Request):
    note: str = Field(min_length=1, max_length=300)
