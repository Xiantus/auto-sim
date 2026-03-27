"""auth.py — Authentication blueprint (login / register / logout)."""

import json
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import check_password_hash, generate_password_hash

import db

CONFIG_PATH = Path(__file__).parent / "config.json"

auth_bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_invite_code() -> str:
    try:
        return json.loads(CONFIG_PATH.read_text()).get("invite_code", "")
    except Exception:
        return ""


def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.get("/login")
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@auth_bp.post("/login")
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = db.get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Invalid username or password.")
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))


@auth_bp.get("/register")
def register():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("register.html")


@auth_bp.post("/register")
def register_post():
    username    = request.form.get("username", "").strip()
    password    = request.form.get("password", "")
    invite_code = request.form.get("invite_code", "").strip()

    invite = _load_invite_code()
    if not invite:
        return render_template("register.html", error="No invite code configured on this server.")
    if invite_code != invite:
        return render_template("register.html", error="Invalid invite code.")
    if not username or not password:
        return render_template("register.html", error="Username and password are required.")
    if len(username) > 32:
        return render_template("register.html", error="Username must be 32 characters or fewer.")
    if db.get_user_by_username(username):
        return render_template("register.html", error="Username already taken.")

    db.create_user(username, generate_password_hash(password))
    user = db.get_user_by_username(username)
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
