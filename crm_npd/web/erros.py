"""Tratamento centralizado de erros HTTP.

Substitui os ``except Exception: pass`` silenciosos por resposta + log.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, render_template_string

logger = logging.getLogger(__name__)

_PAGINA = """
<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>{{ codigo }} · CRM NPD</title>
<style>body{font-family:sans-serif;margin:4rem;color:#222}h1{font-size:1.4rem}</style>
</head><body>
<h1>{{ codigo }} — {{ titulo }}</h1>
<p>{{ detalhe }}</p>
<p><a href="/">Voltar ao inicio</a></p>
</body></html>
"""


def registrar_handlers(app: Flask) -> None:
    """Instala paginas de erro no app Flask.

    Args:
        app: Aplicacao Flask.
    """

    @app.errorhandler(403)
    def _proibido(_erro: Any):
        return render_template_string(
            _PAGINA, codigo=403, titulo="Acesso negado",
            detalhe="Seu usuario nao tem permissao para esta tela.",
        ), 403

    @app.errorhandler(404)
    def _nao_encontrado(_erro: Any):
        return render_template_string(
            _PAGINA, codigo=404, titulo="Pagina nao encontrada",
            detalhe="O endereco nao existe ou foi removido.",
        ), 404

    @app.errorhandler(500)
    def _interno(erro: Any):
        logger.exception("erro.interno %s", erro)
        return render_template_string(
            _PAGINA, codigo=500, titulo="Erro interno",
            detalhe="O erro foi registrado. Tente de novo ou avise o suporte.",
        ), 500
