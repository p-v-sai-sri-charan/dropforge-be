import asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
import core
mdb = AsyncMongoMockClient()["t_indexes"]
import db_indexes, migrations, push_routes
import pytest


@pytest.fixture(autouse=True, scope="module")
def _this_modules_db():
    core.db = mdb
    for m in (db_indexes, migrations, push_routes):
        m.db = mdb


run = lambda co: asyncio.new_event_loop().run_until_complete(co)


def test_unique_indexes_and_duplicate_safety():
    run(mdb.users.insert_many([{"id": "a", "phone": "+919800000001"}, {"id": "b", "phone": "+919800000001"}]))  # same phone twice
    r = run(db_indexes.ensure_all())
    assert "users.phone" in r["not_unique_because_of_duplicates"]  # kept as a normal index, startup not blocked
    info = run(mdb.users.index_information())
    by_keys = {tuple(k for k, _ in v["key"]): v for v in info.values()}
    assert by_keys[("id",)].get("unique") is True and not by_keys[("phone",)].get("unique")
    run(mdb.users.insert_one({"id": "c", "phone": "+919800000003"}))
    with pytest.raises(Exception):
        run(mdb.users.insert_one({"id": "c", "phone": "+919800000004"}))  # unique id is enforced


def test_backfill_day_dob_and_push_opt_out():
    run(mdb.orders.insert_one({"id": "o1", "created_at": "2026-11-27T20:00:00+00:00"}))  # 01:30 on the 28th in India
    run(mdb.events.insert_one({"anon_id": "v", "at": datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)}))
    run(mdb.users.insert_one({"id": "d", "phone": "+919800000005", "dob": "1999-06-05", "push_promotions": False, "created_at": "2026-01-01T00:00:00+00:00"}))
    run(mdb.push_tokens.insert_many([{"token": "t1", "user_id": "d"}, {"token": "t2", "user_id": "c"}]))
    run(migrations.run())
    assert run(mdb.orders.find_one({"id": "o1"}))["day"] == "2026-11-28"
    assert run(mdb.events.find_one({"anon_id": "v"}))["day"] == "2026-11-27"
    assert run(mdb.users.find_one({"id": "d"}))["dob_md"] == "06-05"
    assert run(mdb.push_tokens.find_one({"token": "t1"}))["promo"] is False and "promo" not in run(mdb.push_tokens.find_one({"token": "t2"}))
    assert run(mdb.push_tokens.count_documents(push_routes.PROMO)) == 1
    assert run(migrations.run())["orders.day"] == 0  # nothing left to do the second time
