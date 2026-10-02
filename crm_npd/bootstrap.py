from __future__ import annotations
import importlib
import logging
from .logging_conf import configurar_logging
from .migrations import aplicar_migracoes
from .patches import patch_legacy_runtime, refresh_runtime_version
from .web.erros import registrar_handlers

logger = logging.getLogger(__name__)


def create_app():
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
    legacy.ensure_admin_seed()
    registrar_handlers(legacy.app)
    from .web.blueprints import registrar_blueprints
    registrar_blueprints(legacy.app)
    refresh_runtime_version(legacy)
    return legacy.app
