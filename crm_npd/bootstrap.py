from __future__ import annotations
import importlib
from .patches import patch_legacy_runtime, refresh_runtime_version
from .migrations import apply_safe_migrations


def create_app():
    legacy = importlib.import_module('crm_npd.legacy_monolith')
    patch_legacy_runtime(legacy)
    legacy.init_db()
    with legacy.conectar() as conn:
        legacy.ensure_users_schema(conn)
        apply_safe_migrations(conn)
        try:
            legacy.force_site_vendor_types(conn)
        except Exception:
            pass
    legacy.ensure_admin_seed()
    # Importante: não reprocessar redes na inicialização.
    # Isso pode deixar a subida do app lenta ou travar em bases grandes.
    # O reprocessamento passa a ser manual por rota/botão dentro do sistema.
    refresh_runtime_version(legacy)
    return legacy.app
