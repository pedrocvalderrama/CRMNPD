from __future__ import annotations
import importlib
import logging
import os
from .logging_conf import configurar_logging
from .migrations import aplicar_migracoes
from .patches import patch_legacy_runtime, refresh_runtime_version
from .settings import aplicar_env
from .web.erros import registrar_handlers

logger = logging.getLogger(__name__)


def create_app():
    aplicar_env()
    configurar_logging()
    legacy = importlib.import_module('crm_npd.legacy_monolith')
    patch_legacy_runtime(legacy)
    legacy.init_db()
    with legacy.conectar() as conn:
        legacy.ensure_users_schema(conn)
        aplicar_migracoes(conn)
        try:
            legacy.force_site_vendor_types(conn)
        except Exception:
            logger.exception("falha ao forcar tipos SITE dos vendedores")
        _primeira_carga_fdb(legacy, conn)
    legacy.ensure_admin_seed()
    registrar_handlers(legacy.app)
    from .web.blueprints import registrar_blueprints
    from .web.relatorios import registrar_rotas
    registrar_blueprints(legacy.app)
    registrar_rotas(legacy)
    refresh_runtime_version(legacy)
    try:
        from crm_npd.sync.cdc import iniciar_cdc_em_background
        from crm_npd.sync.fonte import usando_firebird

        if usando_firebird():
            iniciar_cdc_em_background()
        else:
            logger.info("modo local: somente SQLite; ouvinte CDC desligado")
    except Exception:
        logger.exception("nao foi possivel iniciar o ouvinte CDC do Sisplan")
    return legacy.app


def _primeira_carga_fdb(legacy, conn) -> None:
    """Recarrega o FDB se a base for antiga, de outro alvo, ou ainda nao for Firebird.

    A copia local de ontem nao serve no servidor: o ouvinte so le log novo.
    Com CRM_NPD_FONTE=sqlite (run_local.bat) nao abre o Sisplan.
    """
    from crm_npd.repositories.firebird.conexao import descricao_conexao, disponivel
    from crm_npd.sync.cdc import deve_recarregar_na_subida
    from crm_npd.sync.etl import sincronizar_crm
    from crm_npd.sync.fonte import usando_firebird

    if not usando_firebird():
        logger.info("modo local: somente SQLite; Sisplan nao sera lido")
        return
    if not disponivel():
        logger.error("Sisplan indisponivel; configure host/porta/FDB no .env conforme o manual de acesso.")
        return
    origem = (legacy.get_setting(conn, "sisplan_origem", "") or "").strip()
    synced = (legacy.get_setting(conn, "sisplan_snapshot_synced_at", "") or "").strip()
    alvo_salvo = (legacy.get_setting(conn, "sisplan_database", "") or "").strip()
    alvo_atual = descricao_conexao()
    modo = os.environ.get("SISPLAN_SYNC_ON_START", "stale")
    if not deve_recarregar_na_subida(
        origem=origem,
        synced_at=synced,
        alvo_salvo=alvo_salvo,
        alvo_atual=alvo_atual,
        modo=modo,
    ):
        logger.info("carga FDB em dia (%s); ouvinte assume o restante", synced or "sem data")
        return
    logger.info(
        "carga completa do Sisplan antes do ouvinte (origem=%s synced=%s alvo=%s)",
        origem or "?",
        synced or "nunca",
        alvo_atual,
    )
    try:
        sincronizar_crm(conn)
    except Exception:
        logger.exception(
            "carga completa falhou; o Flask sobe, mas os numeros podem estar desatualizados. "
            "Confira se C:\\Sisplan\\Base\\14215F.FDB existe no host %s e se a porta 3051 responde.",
            alvo_atual,
        )

