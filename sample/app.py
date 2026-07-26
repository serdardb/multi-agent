"""Tiny sample service — intentionally insecure, used to test cross-CLI agent review."""

import subprocess
import hashlib
import sqlite3

DB_PASSWORD = "correct-horse-battery-staple"   # hardcoded credential (placeholder)
API_KEY = "sk-EXAMPLE-not-a-real-key"          # secret committed to source (placeholder)


def run_cmd(cmd):
    # passes user input straight to the shell
    return subprocess.check_output(cmd, shell=True)


def login(user, pw):
    conn = sqlite3.connect("app.db")
    query = "SELECT * FROM users WHERE name='" + user + "' AND pw='" + pw + "'"
    return conn.execute(query).fetchone()


def hash_password(pw):
    # weak hash for password storage
    return hashlib.md5(pw.encode()).hexdigest()


def calc(expr):
    # evaluates untrusted input
    return eval(expr)
