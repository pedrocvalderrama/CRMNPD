
from __future__ import annotations
import re
import unicodedata
from ..config import CACHE_TTL_SECONDS
from .cache import TTLCache

_identity_cache = TTLCache()

STOPWORDS = {
    "LJ","LOJA","FILIAL","UNIDADE","MATRIZ","SHOPPING",
    "LTDA","EIRELI","ME","MEI","S","A","SA","EPP","SPE",
    "RESTAURANTE","PIZZARIA"
}

def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch))

def limpar_nome(nome):
    nome = _strip_accents(nome or "").upper()
    nome = re.sub(r"[^A-Z0-9 ]+", " ", nome)
    nome = re.sub(r"\bLOJA\s*\d+\b", " ", nome)
    nome = re.sub(r"\bLJ\s*\d+\b", " ", nome)
    nome = re.sub(r"\bN\s*\d+\b", " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip()
    return nome

def gerar_identity_key(nome):
    nome = limpar_nome(nome)
    parts = []
    for p in nome.split():
        if p in STOPWORDS:
            continue
        if p.isdigit():
            continue
        if len(p) == 1:
            continue
        parts.append(p)
    if not parts:
        return nome
    return " ".join(parts[:3]).strip()

def gerar_identidade(nome):
    return gerar_identity_key(nome)

def agrupar_clientes(clientes):
    mapa = {}
    for c in clientes:
        nome = c.get("nome") if isinstance(c, dict) else getattr(c, "nome", "")
        key = gerar_identity_key(nome)
        row = mapa.setdefault(key, {"nome": key, "filiais": [], "qtd_filiais": 0})
        row["filiais"].append(c)
        row["qtd_filiais"] += 1
    return list(mapa.values())

def sync_identity_rules_from_jit_history_cached(legacy_module, conn):
    # Reprocessamento é uma mutação do banco, não uma leitura: deve rodar SEMPRE.
    # A versão 4.1 podia pular o segundo reprocessamento por causa do cache estático.
    result = legacy_module._original_sync_identity_rules_from_jit_history(conn)
    try:
        conn.commit()
    except Exception:
        pass
    _identity_cache.clear()
    return result

def clear_identity_cache(*args, **kwargs):
    _identity_cache.clear()
