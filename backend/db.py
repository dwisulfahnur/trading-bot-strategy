"""
MongoDB connection helpers.

Collections:
  users   — {_id: user_id (hex uuid), email, password_hash, created_at}
  results — {_id: result_id (file stem), user_id, strategy, created_at,
              symbol, parameters, results}
"""

import os
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "backtest_xauusd")

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    return _client


def get_db():
    return get_client()[DB_NAME]


def get_users() -> Collection:
    col = get_db()["users"]
    col.create_index([("email", ASCENDING)], unique=True, background=True)
    return col


def get_results() -> Collection:
    col = get_db()["results"]
    col.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)], background=True)
    return col
