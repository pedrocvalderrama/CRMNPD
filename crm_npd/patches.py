from __future__ import annotations
import os
from .config import DB_PATH, PROJECT_ROOT, SECRET_KEY
from .db import connect
from .migrations import apply_safe_migrations
from .services.identity import sync_identity_rules_from_jit_history_cached, clear_identity_cache
from .services.instrumentation import install_request_timing
from .services.performance import (
    list_customer_identities_fast,
    build_identity_score_map_fast,
    get_identity_display_name_fast,
    calcular_metricas_recompra_fast,
    cached_chart_wrapper,
    clear_performance_caches,
    summarize_vendor_entities_in_period_fast,
    cached_performance_context_wrapper,
    cached_performance_annual_wrapper,
)


def patch_legacy_runtime(legacy_module) -> None:
    legacy_module.BASE_DIR = str(PROJECT_ROOT)
    legacy_module.DB_PATH = str(DB_PATH)
    legacy_module.SECRET_KEY = SECRET_KEY
    legacy_module.app.secret_key = SECRET_KEY
    legacy_module._crm_cache_db_version = str(os.path.getmtime(DB_PATH)) if os.path.exists(DB_PATH) else '0'

    def conectar_patched():
        return connect(DB_PATH)

    legacy_module.conectar = conectar_patched

    original_init_db = legacy_module.init_db
    def init_db_wrapped():
        original_init_db()
        with legacy_module.conectar() as conn:
            apply_safe_migrations(conn)
    legacy_module.init_db = init_db_wrapped

    # Performance 4.1: substitui rotinas N+1 por leituras em lote + cache invalidado pelo mtime do banco.
    if not hasattr(legacy_module, '_original_list_customer_identities_for_user'):
        legacy_module._original_list_customer_identities_for_user = legacy_module.list_customer_identities_for_user
        legacy_module.list_customer_identities_for_user = lambda conn, user, q='', limit=300: list_customer_identities_fast(legacy_module, conn, user, q=q, limit=limit)

    if not hasattr(legacy_module, '_original_build_identity_score_map'):
        legacy_module._original_build_identity_score_map = legacy_module.build_identity_score_map
        legacy_module.build_identity_score_map = lambda conn, user=None, customer_ids_scope=None: build_identity_score_map_fast(legacy_module, conn, user=user, customer_ids_scope=customer_ids_scope)

    if not hasattr(legacy_module, '_original_get_identity_display_name'):
        legacy_module._original_get_identity_display_name = legacy_module.get_identity_display_name
        legacy_module.get_identity_display_name = lambda conn, identity_key, fallback_customer_id=None: get_identity_display_name_fast(conn, identity_key, fallback_customer_id=fallback_customer_id)

    if not hasattr(legacy_module, '_original_calcular_metricas_recompra_tarefa'):
        legacy_module._original_calcular_metricas_recompra_tarefa = legacy_module.calcular_metricas_recompra_tarefa
        legacy_module.calcular_metricas_recompra_tarefa = lambda conn, cliente_id=None, identity_key='': calcular_metricas_recompra_fast(conn, cliente_id=cliente_id, identity_key=identity_key)

    if hasattr(legacy_module, 'sync_identity_rules_from_jit_history') and not hasattr(legacy_module, '_original_sync_identity_rules_from_jit_history'):
        legacy_module._original_sync_identity_rules_from_jit_history = legacy_module.sync_identity_rules_from_jit_history
        legacy_module.sync_identity_rules_from_jit_history = lambda conn: sync_identity_rules_from_jit_history_cached(legacy_module, conn)

    # Fonte única Novo/Recorrente/Recuperado: Visão do Vendedor e Performance passam
    # obrigatoriamente pelo mesmo serviço em lote, sem regra duplicada por tela.
    if hasattr(legacy_module, 'summarize_vendor_entities_in_period') and not hasattr(legacy_module, '_original_summarize_vendor_entities_in_period'):
        legacy_module._original_summarize_vendor_entities_in_period = legacy_module.summarize_vendor_entities_in_period
        legacy_module.summarize_vendor_entities_in_period = lambda conn, vendor_id, d0, d1: summarize_vendor_entities_in_period_fast(legacy_module, conn, vendor_id, d0, d1)

    # Performance: cache por versão do banco e período. Isso elimina o recálculo de todos
    # os vendedores e de todos os meses do score anual a cada refresh da página.
    if hasattr(legacy_module, '_performance_build_context') and not hasattr(legacy_module, '_original_performance_build_context'):
        legacy_module._original_performance_build_context = legacy_module._performance_build_context
        legacy_module._performance_build_context = cached_performance_context_wrapper(legacy_module._original_performance_build_context)

    if hasattr(legacy_module, '_performance_annual_score_map') and not hasattr(legacy_module, '_original_performance_annual_score_map'):
        legacy_module._original_performance_annual_score_map = legacy_module._performance_annual_score_map
        legacy_module._performance_annual_score_map = cached_performance_annual_wrapper(legacy_module._original_performance_annual_score_map)

    # Gráficos de orçamento/mercado são caros em matplotlib; cache curto torna revisitas instantâneas.
    for fname in ('bar_chart', 'donut_chart', 'brazil_map_chart'):
        if hasattr(legacy_module, fname) and not hasattr(legacy_module, f'_original_{fname}'):
            original = getattr(legacy_module, fname)
            setattr(legacy_module, f'_original_{fname}', original)
            setattr(legacy_module, fname, cached_chart_wrapper(fname, original))

    install_request_timing(legacy_module.app)


def refresh_runtime_version(legacy_module) -> None:
    legacy_module._crm_cache_db_version = str(os.path.getmtime(DB_PATH)) if os.path.exists(DB_PATH) else '0'
    clear_identity_cache()
    clear_performance_caches()
