"""Сопряжение VHS-Viewer с VHS-Streamer и VHS-Security."""

from __future__ import annotations

import os

import requests
from flask import Blueprint, jsonify, render_template

import auth as auth_mod
from __version__ import __version__

integration_bp = Blueprint("integrations", __name__)

PARTNERS = [
    ("Streamer", "Камеры, RTSP и архив", os.environ.get("VHS_STREAMER_URL", "")),
    ("Security", "Карта объекта и тревоги", os.environ.get("VHS_SECURITY_URL", "")),
]


@integration_bp.get("/api/integration/info")
def info():
    return jsonify({
        "app": "VHS-Viewer",
        "version": __version__,
        "role": "analytics",
        "auth": {"header": "X-VHS-Key", "aliases": ["X-Ingest-Token"]},
        "capabilities": ["alpr", "objects", "events", "messaging", "multi_streamer"],
        "endpoints": {
            "state": "/api/integration/state",
            "events": "/api/integration/events",
            "info": "/api/integration/info",
        },
    })


@integration_bp.get("/api/integration/partners")
@auth_mod.login_required
def partners():
    result = []
    for name, purpose, url in PARTNERS:
        ok = None
        if url:
            try:
                ok = requests.get(url.rstrip("/") + "/api/integration/info", timeout=2).ok
            except requests.RequestException:
                ok = False
        result.append({"name": name, "purpose": purpose, "url": url, "online": ok})
    return jsonify({"app": "VHS-Viewer", "partners": result})


@integration_bp.get("/system")
@auth_mod.login_required
def system_page():
    statuses = []
    for name, purpose, url in PARTNERS:
        online = False
        if url:
            try:
                online = requests.get(url.rstrip("/") + "/api/integration/info", timeout=2).ok
            except requests.RequestException:
                online = False
        statuses.append((name, purpose, url, online))
    return render_template("system.html", partners=statuses)
