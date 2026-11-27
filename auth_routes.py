"""Phone + OTP login through Twilio Verify, issuing JWTs."""
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
import phonenumbers
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from core import client_ip, current_user, db, ist_day, is_admin_phone, make_token
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
    """Canonical +E.164 form when the number parses (e.g. drops a stray trunk 0: +9109876... -> +919876...)."""
    raw = re.sub(r"[\s\-().]", "", phone)
    try:
        return phonenumbers.format_number(phonenumbers.parse(raw, None), phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return raw


SMS_TYPES = {phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE}


def _check_number(phone: str):
    """Reject numbers that can't get an SMS before asking Twilio (a wrong length is the usual cause: the country code typed twice)."""
    try:
        n = phonenumbers.parse(phone, None)
    except phonenumbers.NumberParseException:
        raise HTTPException(400, "Enter your number with country code, e.g. +919876543210")
    if not phonenumbers.is_valid_number(n):
        raise HTTPException(400, f"{phone} isn't a valid phone number. Check the country and that the country code isn't typed twice")
    if phonenumbers.number_type(n) not in SMS_TYPES:
        raise HTTPException(400, "This looks like a landline. Use a mobile number that can receive SMS")


# What Twilio's error codes mean for the customer
TWILIO_ERRORS = {
    21211: "That phone number isn't valid. Check the country and number",
    21614: "This number can't receive SMS. Use a mobile number",
    21408: "We can't send codes to this country yet",
    60200: "That phone number isn't valid. Check the country and number",
    60203: "Too many codes sent to this number. Wait 10 minutes and try again",
    60410: "Code sending to this number is temporarily blocked. Try again later or contact support",
    60605: "We can't send codes to this country yet",
}


UNSUPPORTED = "Sign-in is available for India, US, UK, UAE and Euro-zone phone numbers only"


def _public(user: dict) -> dict:
    region = region_for_phone(user["phone"]) or {"country": "IN", "currency": "INR"}
    return {"id": user["id"], "phone": user["phone"], "is_admin": is_admin_phone(user["phone"]), "admin_mode": "otp", **region}


async def public_user(user: dict) -> dict:
    """The signed-in user as the app sees it: identity, region, profile details and creator status."""
    c = await db.creators.find_one({"user_id": user["id"]}, {"_id": 0, "status": 1})
    return {
        **_public(user),
        **{k: user.get(k) for k in PROFILE_FIELDS},
        "email_verified": bool(user.get("email") and user.get("email_verified")),
        "creator_status": c["status"] if c else None,  # None | "pending" | "approved" | "rejected"
    }


PROFILE_FIELDS = ("name", "email", "dob", "gender")
GENDERS = ("female", "male", "non_binary", "other", "prefer_not_to_say")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


class ProfileIn(BaseModel):
    """Optional profile details. Send only the fields being changed; an empty string / null clears a field."""
    name: Optional[str] = Field(default=None, max_length=60)
    email: Optional[str] = Field(default=None, max_length=120)
    dob: Optional[str] = Field(default=None, max_length=10)  # YYYY-MM-DD
    gender: Optional[Literal["female", "male", "non_binary", "other", "prefer_not_to_say", ""]] = None

    @field_validator("name")
    @classmethod
    def name_ok(cls, v):
        v = " ".join((v or "").split())
        if v and len(v) < 2:
            raise ValueError("Name is too short")
        return v

    @field_validator("email")
    @classmethod
    def email_ok(cls, v):
        v = (v or "").strip().lower()
        if v and not EMAIL.match(v):
            raise ValueError("Enter a valid email address")
        return v

    @field_validator("dob")
    @classmethod
    def dob_ok(cls, v):
        v = (v or "").strip()
        if not v:
            return v
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise ValueError("Enter your date of birth as DD/MM/YYYY")
        today = date.today()
        age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        if d > today or age > 120:
            raise ValueError("Enter a real date of birth")
        if age < 13:
            raise ValueError("You must be at least 13 to use DropForge")
        return d.isoformat()


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
    _check_number(phone)
    ip = client_ip(request)
    if await db.otp_sends.count_documents({"phone": phone, "at": {"$gte": _since(minutes=10)}}) >= PER_PHONE_10MIN:
        raise HTTPException(429, "Too many codes requested. Try again in 10 minutes")
    if await db.otp_sends.count_documents({"ip": ip, "at": {"$gte": _since(hours=1)}}) >= PER_IP_HOUR:
        raise HTTPException(429, "Too many codes requested from this device. Try again in an hour")
    if await db.otp_sends.count_documents({"at": {"$gte": _since(hours=1)}}) >= GLOBAL_HOUR:
        raise HTTPException(429, "Login codes are busy right now. Please try again shortly")
    if not sms_enabled():
        raise HTTPException(503, "SMS login isn't set up yet. The store owner needs to add Twilio keys")
    sent = await db.otp_sends.insert_one({"phone": phone, "ip": ip, "at": datetime.now(timezone.utc)})
    try:
        v = await run_in_threadpool(lambda: _twilio().verifications.create(to=phone, channel="sms"))
        logger.info("otp sent to %s****%s (verify %s, status %s)", phone[:4], phone[-2:], getattr(v, "sid", "?"), getattr(v, "status", "?"))
    except TwilioRestException as e:
        await db.otp_sends.delete_one({"_id": sent.inserted_id})  # a send Twilio refused doesn't count toward the limits
        logger.warning("twilio send failed for %s****%s: code=%s %s", phone[:4], phone[-2:], e.code, e.msg)
        raise HTTPException(400, TWILIO_ERRORS.get(e.code, "Couldn't send a code to this number. Check it and try again"))
    return {"sent": True, "phone": phone}


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
    raw = re.sub(r"[\s\-().]", "", body.phone)
    if not user and raw != phone:
        # accounts made before numbers were canonicalised (e.g. saved as +9109876...) move to the clean form
        if (await db.users.update_one({"phone": raw}, {"$set": {"phone": phone}})).modified_count:
            user = await db.users.find_one({"phone": phone}, {"_id": 0})
    if not user:
        now = datetime.now(timezone.utc)
        user = {"id": str(uuid.uuid4()), "phone": phone, "created_at": now.isoformat(), "day": ist_day(now)}
        await db.users.insert_one(dict(user))
    return {"token": make_token(user["id"]), "user": await public_user(user)}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return await public_user(user)


@router.put("/me")
async def update_me(body: ProfileIn, user: dict = Depends(current_user)):
    sent = body.model_dump(exclude_unset=True)
    set_ = {k: v for k, v in sent.items() if v}
    unset = {k: "" for k, v in sent.items() if not v}
    if "dob" in sent:  # month-day kept separately so the birthday job can use an index ("06-05")
        if sent["dob"]:
            set_["dob_md"] = sent["dob"][5:]
        else:
            unset["dob_md"] = ""
    if "email" in sent and sent["email"] != user.get("email"):  # a new address has to be verified again
        unset.update({"email_verified": "", "email_verified_at": ""})
    if set_ or unset:
        await db.users.update_one({"id": user["id"]}, {**({"$set": set_} if set_ else {}), **({"$unset": unset} if unset else {})})
    return await public_user(await db.users.find_one({"id": user["id"]}, {"_id": 0}))
