"""Blueprints finos. As rotas ainda vivem no monolito; estes modulos
existem para a extracao gradual sem quebrar o mapa de URLs.
"""

from __future__ import annotations

from flask import Blueprint

clientes_bp = Blueprint("clientes_v2", __name__, url_prefix="")
vendedores_bp = Blueprint("vendedores_v2", __name__, url_prefix="")
tarefas_bp = Blueprint("tarefas_v2", __name__, url_prefix="")
importacoes_bp = Blueprint("importacoes_v2", __name__, url_prefix="")


def registrar_blueprints(app) -> None:
    """Registra os blueprints reservados. Ainda sem rotas, para nao colidir."""
    app.register_blueprint(clientes_bp)
    app.register_blueprint(vendedores_bp)
    app.register_blueprint(tarefas_bp)
    app.register_blueprint(importacoes_bp)
