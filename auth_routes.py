"""Phone + OTP login through Twilio Verify, issuing JWTs."""
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from core import client_ip, current_user, db, is_admin_phone, make_token
from regions import region_for_phone

router = APIRouter(prefix="/api/auth")
logger = logging.getLogger(__name__)
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
# SMS pumping limits (stored in Mongo so they survive restarts and apply across workers)
PER_PHONE_10MIN, PER_IP_HOUR, GLOBAL_HOUR, WRONG_CODES_10MIN = 3, 8, 150, 5


def _since(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def sms_enabled() -> bool:
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE"))


def _twilio():
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).verify.v2.services(
        os.environ["TWILIO_VERIFY_SERVICE"]
    )


def _norm(phone: str) -> str:
    return re.sub(r"[\s\-()]", "", phone)


UNSUPPORTED = "Sign-in is available for India, US, UK, UAE and Euro-zone phone numbers only"


def _public(user: dict) -> dict:
    region = region_for_phone(user["phone"]) or {"country": "IN", "currency": "INR"}
    return {"id": user["id"], "phone": user["phone"], "is_admin": is_admin_phone(user["phone"]), "admin_mode": "otp", **region}


class PhoneBody(BaseModel):
    phone: str = Field(min_length=6, max_length=20)


class VerifyBody(PhoneBody):
    code: str = Field(min_length=4, max_length=10)


@router.post("/send-otp")
async def send_otp(body: PhoneBody, request: Request):
    phone = _norm(body.phone)
    if not E164.match(phone):
        raise HTTPException(400, "Enter your number with country code, e.g. +919876543210")
    if not region_for_phone(phone):
        raise HTTPException(400, UNSUPPORTED)
    ip = client_ip(request)
    if await db.otp_sends.count_documents({"phone": phone, "at": {"$gte": _since(minutes=10)}}) >= PER_PHONE_10MIN:
        raise HTTPException(429, "Too many codes requested. Try again in 10 minutes")
    if await db.otp_sends.count_documents({"ip": ip, "at": {"$gte": _since(hours=1)}}) >= PER_IP_HOUR:
        raise HTTPException(429, "Too many codes requested from this device. Try again in an hour")
    if await db.otp_sends.count_documents({"at": {"$gte": _since(hours=1)}}) >= GLOBAL_HOUR:
        raise HTTPException(429, "Login codes are busy right now. Please try again shortly")
    if not sms_enabled():
        raise HTTPException(503, "SMS login isn't set up yet. The store owner needs to add Twilio keys")
    await db.otp_sends.insert_one({"phone": phone, "ip": ip, "at": datetime.now(timezone.utc)})
    try:
        await run_in_threadpool(lambda: _twilio().verifications.create(to=phone, channel="sms"))
    except TwilioRestException as e:
        logger.warning("twilio send failed: %s", e)
        raise HTTPException(400, "Couldn't send a code to this number")
    return {"sent": True}


@router.post("/verify-otp")
async def verify_otp(body: VerifyBody):
    phone = _norm(body.phone)
    code = body.code.strip()
    if not region_for_phone(phone):
        raise HTTPException(400, UNSUPPORTED)
    if await db.otp_fails.count_documents({"phone": phone, "at": {"$gte": _since(minutes=10)}}) >= WRONG_CODES_10MIN:
        raise HTTPException(429, "Too many wrong codes. Try again in 10 minutes")
    if not sms_enabled():
        raise HTTPException(503, "SMS login isn't set up yet")
    try:
        check = await run_in_threadpool(lambda: _twilio().verification_checks.create(to=phone, code=code))
        ok = check.status == "approved"
    except TwilioRestException as e:
        logger.warning("twilio verify failed: %s", e)
        ok = False
    if not ok:
        await db.otp_fails.insert_one({"phone": phone, "at": datetime.now(timezone.utc)})
        raise HTTPException(400, "Wrong or expired code")
    user = await db.users.find_one({"phone": phone}, {"_id": 0})
    if not user:
        user = {"id": str(uuid.uuid4()), "phone": phone, "created_at": datetime.now(timezone.utc).isoformat()}
        await db.users.insert_one(dict(user))
    return {"token": make_token(user["id"]), "user": _public(user)}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return _public(user)
