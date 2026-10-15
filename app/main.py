"""Employee Attendance & Analytics API for the HROne engineering assignment.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Only MONGO_URI and MONGO_DB are configuration inputs. A local .env is optional;
real environment variables take precedence because load_dotenv does not override them.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=2500,
    connectTimeoutMS=2500,
    maxPoolSize=50,
)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
EMP_CODE_PATTERN = r"^EMP\d{4,6}$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
_indexes_ready = False


# ---------------------------------------------------------------------------
# MongoDB indexes and readiness
# ---------------------------------------------------------------------------
def ensure_indexes() -> None:
    """Create required indexes idempotently; uniqueness is the concurrency guard."""
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employees_joined_department",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_code",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_code_punchin",
    )


@app.on_event("startup")
def startup_indexes() -> None:
    global _indexes_ready
    # Do not prevent the process from starting if MongoDB is temporarily down:
    # /health will return 503 until the database is reachable.
    try:
        db.command("ping")
        ensure_indexes()
        _indexes_ready = True
    except PyMongoError:
        logger.exception("MongoDB unavailable at startup; indexes will be retried on health checks")


@app.get("/health")
def health() -> dict:
    global _indexes_ready
    try:
        db.command("ping")
        if not _indexes_ready:
            ensure_indexes()
            _indexes_ready = True
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc


# ---------------------------------------------------------------------------
# Validation models
# ---------------------------------------------------------------------------
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(InputModel):
    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @model_validator(mode="after")
    def shifts_must_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(InputModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="after")
    def timestamp_cannot_be_explicitly_null(self) -> "PunchInIn":
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null when supplied")
        return self


class PunchOutIn(InputModel):
    emp_code: str = Field(min_length=1)
    punched_at: EpochMillis | None = None

    @model_validator(mode="after")
    def timestamp_cannot_be_explicitly_null(self) -> "PunchOutIn":
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null when supplied")
        return self


class RegularizeIn(InputModel):
    status: AttendanceStatus | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def supplied_fields_cannot_be_null(self) -> "RegularizeIn":
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null when supplied")
        return self


# ---------------------------------------------------------------------------
# Time, rounding, and API serialization helpers
# ---------------------------------------------------------------------------
def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    # Integer seconds avoid floating point conversion errors; all instants are
    # deliberately truncated to whole seconds under R1.
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # PyMongo normally returns aware UTC datetimes due to tz_aware=True;
        # treat any legacy naive instant as UTC, never as local machine time.
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000


def api_value(value):
    """Recursively convert BSON datetimes to API epoch milliseconds."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


def public_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def public_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": api_value(doc.get("history", [])),
    }


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(MONTH_PATTERN, month):
        raise HTTPException(422, detail="month must use YYYY-MM format")
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def count_working_days(joined_on: str | date, period_start: date, period_end: date) -> int:
    if isinstance(joined_on, str):
        joined_on = date.fromisoformat(joined_on)
    start = max(joined_on, period_start)
    if start > period_end:
        return 0
    # This is calendar arithmetic only; attendance documents are aggregated in MongoDB.
    days = (period_end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * 5
    first_weekday = start.weekday()
    for offset in range(remainder):
        if (first_weekday + offset) % 7 < 5:
            count += 1
    return count


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = map(int, value.split(":"))
    return hour, minute


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    start_hm = _parse_hhmm(shift_start)
    end_hm = _parse_hhmm(shift_end)
    overnight = end_hm <= start_hm
    local_hm = (local.hour, local.minute)
    if overnight and local_hm < end_hm:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str | date) -> int:
    day = date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    h, m = _parse_hhmm(shift_start)
    shift_start_ist = datetime.combine(day, time(h, m), tzinfo=IST)
    elapsed = (punch_in.astimezone(IST) - shift_start_ist).total_seconds()
    if elapsed <= 10 * 60:
        return 0
    return max(0, math.floor(elapsed / 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    value = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str | date) -> int:
    day = date.fromisoformat(attendance_day) if isinstance(attendance_day, str) else attendance_day
    start_hm = _parse_hhmm(shift_start)
    end_hm = _parse_hhmm(shift_end)
    if end_hm <= start_hm:
        day += timedelta(days=1)
    h, m = end_hm
    shift_end_ist = datetime.combine(day, time(h, m), tzinfo=IST)
    elapsed = (punch_out.astimezone(IST) - shift_end_ist).total_seconds()
    whole_minutes = math.floor(elapsed / 60)
    return whole_minutes if whole_minutes >= 30 else 0


def derived_fields(employee: dict, attendance_day: str, status: str,
                   punch_in: datetime | None, punch_out: datetime | None) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        raise HTTPException(422, detail="presence status requires punch_in")
    late_minutes = compute_late_minutes(punch_in, employee["shift_start"], attendance_day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late_minutes, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, employee["shift_start"], employee["shift_end"], attendance_day)
    return {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime,
        "half_day": work_hours < 4.50,
    }



def is_weekday_expr(date_expr: str) -> dict:
    # MongoDB $dayOfWeek: Sunday=1, Monday=2, ..., Saturday=7.
    return {"$in": [{"$dayOfWeek": date_expr}, [2, 3, 4, 5, 6]]}


def monthly_employee_pipeline(emp_code: str, first_day: str, last_day: str) -> list[dict]:
    weekday = is_weekday_expr({"$dateFromString": {"dateString": "$date"}})
    present_day_value = {
        "$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, weekday]},
                present_day_value,
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first_day: str, last_day: str, department: str | None) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": last_day}}
    if department is not None:
        employee_match["department"] = department

    date_safe = {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}
    weekday = is_weekday_expr(date_safe)
    log_exists = {"$ne": [{"$ifNull": ["$logs.date", None]}, None]}
    present_condition = {"$and": [log_exists, {"$in": ["$logs.status", PRESENCE_STATUSES]}, weekday]}
    present_value = {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}
    average_condition = {"$and": [
        {"$in": ["$logs.status", PRESENCE_STATUSES]},
        {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
    ]}

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$employee_code"]},
                {"$gte": ["$date", first_day]},
                {"$lte": ["$date", last_day]},
            ]}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [present_condition, present_value, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [average_condition, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employee_codes"},
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(first_day: str, last_day: str, limit: int, department: str | None) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict]:
    start_dt = datetime.combine(from_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=UTC)
    from_str, to_str = from_day.isoformat(), to_day.isoformat()
    return [
        # One anchor document ensures $densify also works when there are no logs.
        {"$documents": [{"dateDate": start_dt, "present_count": 0, "late_count": 0}]},
        {"$unionWith": {
            "coll": "attendance_logs",
            "pipeline": [
                {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$addFields": {"dateDate": {"$dateFromString": {"dateString": "$date"}}}},
                {"$group": {
                    "_id": "$dateDate",
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", PRESENCE_STATUSES]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
                {"$project": {"_id": 0, "dateDate": "$_id", "present_count": 1, "late_count": 1}},
            ],
        }},
        {"$group": {
            "_id": "$dateDate",
            "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
        }},
        {"$set": {"dateDate": "$_id"}},
        {"$unset": "_id"},
        {"$densify": {"field": "dateDate", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_string": {"$dateToString": {"date": "$dateDate", "format": "%Y-%m-%d", "timezone": "UTC"}}},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day_string"]}}},
                {"$count": "count"},
            ],
            "as": "headcount_docs",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.count", 0]}, 0]},
            "is_working_day": is_weekday_expr("$dateDate"),
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"dateDate": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"dateDate": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$dateDate", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
    ]


def explain_command(collection: str | int, pipeline: list[dict], verbosity: str = "executionStats") -> dict:
    command_doc = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    result = db.command("explain", command_doc, verbosity=verbosity)
    # Explain plans can contain BSON types; turn them into safe Extended JSON values.
    return json.loads(json_util.dumps(result))


def validate_date_range(from_day: date, to_day: date, max_days: int | None = None) -> None:
    if to_day < from_day:
        raise HTTPException(422, detail="to must be on or after from")
    if max_days is not None and (to_day - from_day).days + 1 > max_days:
        raise HTTPException(422, detail=f"date range must not exceed {max_days} days")


# ---------------------------------------------------------------------------
# Employee endpoints
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": utc_now_seconds(),
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, detail="emp_code already exists") from exc
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [public_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, detail="employee not found")

    punch_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    attendance_day = attendance_date_for(punch_time, employee["shift_start"], employee["shift_end"]).isoformat()
    derived = derived_fields(employee, attendance_day, body.status, punch_time, None)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        **derived,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, detail="already punched in for this date") from exc
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, detail="employee not found")

    punch_time = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_time}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, detail="no punch-in found at or before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="record is already punched out")

    punch_in_time = record["punch_in"]
    elapsed = (punch_time - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, detail="punched_at must be after punch_in and no more than 24 hours later")

    derived = derived_fields(employee, record["date"], record["status"], punch_in_time, punch_time)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_time, **derived}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, detail="record was punched out by another request")
    record.update({"punch_out": punch_time, **derived})
    return public_attendance(record)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: AttendanceStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, detail="date_from must be on or before date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [public_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn) -> dict:
    day_string = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_string})
    if record is None:
        raise HTTPException(404, detail="attendance record not found")

    fields_set = body.model_fields_set
    old_status = record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_status = body.status if "status" in fields_set else old_status
    new_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in fields_set else old_in
    new_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in fields_set else old_out

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(422, detail="ABSENT or LEAVE cannot be supplied with punch times")
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(422, detail="presence status requires punch_in")
        if attendance_date_for(new_in, employee["shift_start"], employee["shift_end"]).isoformat() != day_string:
            raise HTTPException(422, detail="punch_in must belong to the record attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, detail="punch_out must be after punch_in and within 24 hours")

    computed = derived_fields(employee, day_string, new_status, new_in, new_out)
    new_fields = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **computed,
    }
    old_fields = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes", 0) or 0),
        "overtime_minutes": int(record.get("overtime_minutes", 0) or 0),
        "half_day": bool(record.get("half_day", False)),
    }

    changes: dict = {}
    for key, new_value in new_fields.items():
        old_value = old_fields.get(key)
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, detail="request does not change the record")

    history_entry = {
        "at": utc_now_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: a concurrent correction changes history, so only
    # one request based on a given history snapshot can commit.
    filter_query = {
        "_id": record["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
    }
    if "history" in record:
        filter_query["history"] = record["history"]
    else:
        filter_query["history"] = {"$exists": False}
    result = db.attendance_logs.update_one(
        filter_query,
        {"$set": new_fields, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, detail="record changed concurrently; reload and retry")

    record.update(new_fields)
    record["history"] = list(record.get("history", [])) + [history_entry]
    return public_attendance(record)


# ---------------------------------------------------------------------------
# Analytics endpoints (all log calculations are MongoDB aggregation pipelines)
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=MONTH_PATTERN),
) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, detail="employee not found")
    first, last = month_bounds(month)
    result = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, first, last), allowDiskUse=True))
    stats = result[0] if result else {}
    working_days = count_working_days(employee["joined_on"], date.fromisoformat(first), date.fromisoformat(last))
    present_days = float(stats.get("present_days", 0) or 0)
    attendance_pct = round_half_up((present_days / working_days * 100), 4) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(stats.get("leave_days", 0) or 0),
        "late_count": int(stats.get("late_count", 0) or 0),
        "total_late_minutes": int(stats.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: str | None = None,
) -> dict:
    first, last = month_bounds(month)
    docs = db.employees.aggregate(department_summary_pipeline(first, last, department), allowDiskUse=True)
    items = []
    for doc in docs:
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": round_half_up(doc.get("present_days", 0), 2),
            "avg_work_hours": round_half_up(doc.get("avg_work_hours"), 2),
            "late_count": int(doc.get("late_count", 0) or 0),
            "total_late_minutes": int(doc.get("total_late_minutes", 0) or 0),
            "leave_count": int(doc.get("leave_count", 0) or 0),
            "on_duty_count": int(doc.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
) -> dict:
    first, last = month_bounds(month)
    pipeline = leaderboard_pipeline(first, last, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: date = Query(..., alias="from"),
    to_day: date = Query(..., alias="to"),
) -> dict:
    validate_date_range(from_day, to_day, max_days=92)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, detail="department not found")
    pipeline = department_trend_pipeline(department, from_day, to_day)
    docs = list(db.aggregate(pipeline, allowDiskUse=True))
    items = []
    for doc in docs:
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc.get("headcount", 0) or 0),
            "present_count": round_half_up(doc.get("present_count", 0), 2),
            "late_count": int(doc.get("late_count", 0) or 0),
            "attendance_rate": round_half_up(doc.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(doc.get("moving_avg_7d"), 4),
        })
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
ExplainEndpoint = Literal[
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: str | None = None,
    month: str | None = Query(None, pattern=MONTH_PATTERN),
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    from_day: date | None = Query(None, alias="from"),
    to_day: date | None = Query(None, alias="to"),
    status: AttendanceStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, detail="date_from must be on or before date_to")
        query: dict = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from.isoformat()
            if date_to is not None:
                query["date"]["$lte"] = date_to.isoformat()
        if status is not None:
            query["status"] = status
        explain = db.command(
            "explain",
            {"find": "attendance_logs", "filter": query,
             "sort": {"date": -1, "emp_code": 1},
             "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        first, last = month_bounds(month)
        pipeline = monthly_employee_pipeline(emp_code, first, last)
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        first, last = month_bounds(month)
        pipeline = department_summary_pipeline(first, last, department)
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        first, last = month_bounds(month)
        pipeline = leaderboard_pipeline(first, last, limit, department)
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "attendance_logs"

    else:  # department_trend
        if not department or from_day is None or to_day is None:
            raise HTTPException(422, detail="department, from and to are required")
        validate_date_range(from_day, to_day, max_days=92)
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(404, detail="department not found")
        pipeline = department_trend_pipeline(department, from_day, to_day)
        explain = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "attendance_logs"

    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explain))}
