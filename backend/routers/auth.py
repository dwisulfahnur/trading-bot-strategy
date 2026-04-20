"""
Auth endpoints:
  POST /auth/register
  POST /auth/login
  GET  /auth/me
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from backend.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from backend.db import get_users

router = APIRouter(prefix="/auth")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest) -> TokenResponse:
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    users = get_users()
    if users.find_one({"email": req.email}):
        raise HTTPException(400, "Email already registered")
    user_id = uuid.uuid4().hex
    users.insert_one(
        {
            "_id": user_id,
            "email": req.email,
            "password_hash": hash_password(req.password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    token = create_access_token(user_id)
    return TokenResponse(access_token=token, user_id=user_id, email=req.email)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest) -> TokenResponse:
    users = get_users()
    user = users.find_one({"email": req.email})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = create_access_token(user["_id"])
    return TokenResponse(access_token=token, user_id=user["_id"], email=user["email"])


@router.get("/me")
def me(user_id: str = Depends(get_current_user)) -> dict:
    users = get_users()
    user = users.find_one({"_id": user_id}, {"password_hash": 0})
    if not user:
        raise HTTPException(404, "User not found")
    return {"user_id": user["_id"], "email": user["email"]}
