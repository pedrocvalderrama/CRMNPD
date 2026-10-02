"""Decoradores de autorizacao.

Substituem os checks espalhados nas rotas do monolito. O monolito ainda tem
as funcoes originais; rotas extraidas devem usar estes decoradores.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import abort, redirect, session, url_for


def usuario_atual() -> dict[str, Any] | None:
    """Devolve o usuario da sessao, ou None se nao autenticado."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return {
        "id": user_id,
        "role": (session.get("role") or "").strip().lower(),
        "name": session.get("name") or "",
    }


def login_required(view: Callable) -> Callable:
    """Exige sessao autenticada."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def roles_required(*roles: str) -> Callable:
    """Exige que o usuario tenha um dos papeis informados."""

    permitidos = {r.strip().lower() for r in roles}

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            atual = (session.get("role") or "").strip().lower()
            if atual not in permitidos:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view: Callable) -> Callable:
    """Atalho para papel admin."""
    return roles_required("admin")(view)


def gestor_required(view: Callable) -> Callable:
    """Atalho para admin ou gestor."""
    return roles_required("admin", "gestor")(view)
