
# CRM NPD Embalagens - Single-file Flask + SQLite
# Foco: Importação CSV (Pedidos + Faturamento), Dashboard (ciclo 26->25), Vendedores com Comissão e Gráficos

from __future__ import annotations

import base64
import csv
import io
import os
import re
import sqlite3
from pathlib import Path
import statistics
import unicodedata
import json
import hashlib
import math
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import (
    Flask, request, redirect, url_for, flash, session,
    render_template_string, send_file, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

# Refatoracao em andamento: as formulas de negocio sairam deste arquivo para
# crm_npd/metrics, onde sao funcoes puras, tipadas e testadas. As funcoes abaixo
# viraram adaptadores finos: elas traduzem sqlite3.Row/dict para os tipos do
# dominio e delegam o calculo. Nenhum numero muda nesse processo, o que e
# garantido pela suite golden-master em tests/golden.
from crm_npd.domain import (
    EstatisticasIdentidade,
    EtapaFunil,
    FaixaScore,
    LinhaComissao,
    Periodo,
    StatusManualCliente,
)
from crm_npd.domain.status_os import codigo_status_os, rotulo_status_os
from crm_npd.metrics import comissao as f_comissao
from crm_npd.metrics import funil as f_funil
from crm_npd.metrics import percentuais as f_pct
from crm_npd.metrics import periodos as f_periodos
from crm_npd.metrics import recompra as f_recompra
from crm_npd.metrics import score_cliente as f_score_cliente
from crm_npd.metrics import score_vendedor as f_score_vendedor

import matplotlib
matplotlib.use("Agg")  # server-side
import matplotlib.pyplot as plt

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# -----------------------------
# Matplotlib global style (Mercos-like)
# -----------------------------
plt.rcParams.update({
    "figure.facecolor": "#f8fbff",
    "axes.facecolor": "#f8fbff",
    "axes.edgecolor": "#e5ebf4",
    "axes.labelcolor": "#53657a",
    "axes.titleweight": "bold",
    "axes.titlesize": 12,
    "axes.labelsize": 9,
    "axes.grid": False,
    "grid.color": "#dce5f1",
    "grid.linestyle": "--",
    "grid.linewidth": 0.7,
    "grid.alpha": 0.55,
    "xtick.color": "#718096",
    "ytick.color": "#718096",
    "font.size": 9.5,
    "legend.frameon": False,
    "legend.fontsize": 8.5,
    "lines.linewidth": 2.7,
    "lines.markersize": 4,
})


APP_TITLE = "CRM NPD Embalagens"
DB_FILENAME = "crm_npd.db"
SECRET_KEY = os.environ.get("CRM_NPD_SECRET", "crm_npd_secret_key_change_me")

import sys # Certifique-se de importar sys no topo

# ... (logo após os imports)

# Ajuste para garantir que o banco fique na mesma pasta do executável
if getattr(sys, 'frozen', False):
    # Se estiver rodando como .exe (congelado)
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Fonte única de dados: o banco oficial fica na raiz do projeto, igual ao
    # crm_npd.config e ao app.py. Evita existir um segundo crm_npd.db dentro do
    # pacote com dados antigos/diferentes.
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

DB_PATH = os.path.join(BASE_DIR, DB_FILENAME)

app = Flask(__name__)
app.secret_key = SECRET_KEY


# -----------------------------
# DB helpers
# -----------------------------
def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA cache_size = -64000;")
        conn.execute("PRAGMA busy_timeout = 30000;")
    except Exception:
        pass
    return conn


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    with conectar() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")

        # -----------------------------
        # USERS
        # -----------------------------
        conn.execute("""
          CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              nome TEXT NOT NULL,
              email TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL DEFAULT 'user',
              senha_hash TEXT NOT NULL,
              ativo INTEGER NOT NULL DEFAULT 1,
              vendor_codigo TEXT,
              created_at TEXT NOT NULL
          );
        """)

        # -----------------------------
        # TASKS E HISTÓRICO
        # -----------------------------
        conn.execute("""
          CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              vendor_id INTEGER NOT NULL,
              cliente_id INTEGER,
              titulo TEXT NOT NULL,
              descricao TEXT,
              status TEXT NOT NULL DEFAULT 'ABERTA',
              criada_por TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT,
              FOREIGN KEY(vendor_id) REFERENCES vendors(id),
              FOREIGN KEY(cliente_id) REFERENCES customers(id)
          );
        """)

        conn.execute("""
          CREATE TABLE IF NOT EXISTS task_history(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              status_anterior TEXT,
              status_novo TEXT,
              observacao TEXT,
              alterado_por TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
          );
        """)

        # -----------------------------
        # IA DO CRM - BASE DE CONHECIMENTO, REGRAS, FEEDBACK E AUDITORIA
        # -----------------------------
        conn.execute("""
          CREATE TABLE IF NOT EXISTS ia_knowledge(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              categoria TEXT NOT NULL DEFAULT 'geral',
              pergunta TEXT NOT NULL,
              resposta TEXT NOT NULL,
              permissao TEXT NOT NULL DEFAULT 'all',
              palavras_chave TEXT,
              ativo INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
          );
        """)
        conn.execute("""
          CREATE TABLE IF NOT EXISTS ia_feedback(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pergunta TEXT NOT NULL,
              resposta TEXT,
              acertou INTEGER,
              correcao TEXT,
              usuario TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
          );
        """)
        conn.execute("""
          CREATE TABLE IF NOT EXISTS ia_audit_log(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              usuario TEXT,
              role TEXT,
              pergunta TEXT NOT NULL,
              resposta TEXT,
              fonte TEXT,
              permissao_aplicada TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
          );
        """)
        conn.execute("""
          CREATE TABLE IF NOT EXISTS crm_manual_overrides(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tipo TEXT NOT NULL,
              chave TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}',
              criado_por TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(tipo, chave)
          );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_identity_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL UNIQUE,
            identity_key TEXT NOT NULL,
            identity_nome TEXT NOT NULL,
            tipo_vinculo TEXT NOT NULL DEFAULT 'CLIENTE', -- MATRIZ | FILIAL | CLIENTE
            matriz_customer_id INTEGER,
            origem TEXT NOT NULL DEFAULT 'MANUAL',        -- MANUAL | AUTO_JIT | IMPORT
            observacao TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS import_identity_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            customer_id INTEGER NOT NULL,
            customer_nome TEXT NOT NULL,
            customer_codigo TEXT,
            condicao_entrega TEXT,
            candidato_customer_id INTEGER,
            candidato_nome TEXT,
            status TEXT NOT NULL DEFAULT 'PENDENTE',   -- PENDENTE | RESOLVIDO | IGNORADO
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
        """)

        # -----------------------------
        # METAS E HISTÓRICO DE METAS
        # -----------------------------
        conn.execute("""
          CREATE TABLE IF NOT EXISTS metas_vendedores(
              vendor_id INTEGER PRIMARY KEY,
              meta_manual REAL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(vendor_id) REFERENCES vendors(id) ON DELETE CASCADE
          );
        """)

        conn.execute("""
          CREATE TABLE IF NOT EXISTS goals_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              periodo TEXT NOT NULL,      -- Formato: 'YYYY-MM'
              tipo_entidade TEXT,         -- PEDIDOS_*/FATURAMENTO_*/VENDEDOR (legado: EQUIPE_*)
              entity_id INTEGER,          -- ID do vendedor (se for meta individual)
              valor REAL NOT NULL,
              updated_at TEXT,
              UNIQUE(periodo, tipo_entidade, entity_id)
          );
        """)

        conn.execute("""
          CREATE TABLE IF NOT EXISTS ciclos_faturamento(
              periodo TEXT PRIMARY KEY,
              dia_fechamento INTEGER NOT NULL DEFAULT 24,
              updated_at TEXT NOT NULL
          );
        """)

        # -----------------------------
        # VENDEDORES E CLIENTES
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS vendors(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            nome TEXT NOT NULL,
            tipo TEXT DEFAULT 'Externo',
            ativo INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(codigo, nome)
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT,
            nome TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(codigo, nome)
        );
        """)

        # -----------------------------
        # REDES / FILIAIS
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_groups(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_group_members(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(group_id, customer_id),
            FOREIGN KEY(group_id) REFERENCES customer_groups(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
        );
        """)

        # -----------------------------
        # LOG DE IMPORTAÇÕES
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS imports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,               -- PEDIDOS | FATURAMENTO | ORÇAMENTO
            arquivo TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            linhas INTEGER NOT NULL DEFAULT 0,
            registros INTEGER NOT NULL DEFAULT 0
        );
        """)

        # -----------------------------
        # PEDIDOS E FATURAMENTO
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            pedido TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL DEFAULT 0,
            quantidade REAL DEFAULT 0,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(import_id) REFERENCES imports(id) ON DELETE SET NULL,
            FOREIGN KEY(cliente_id) REFERENCES customers(id) ON DELETE SET NULL,
            FOREIGN KEY(vendor_id) REFERENCES vendors(id) ON DELETE SET NULL
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER,
            numero TEXT,
            data_emissao TEXT,
            cliente_id INTEGER,
            vendor_id INTEGER,
            valor REAL DEFAULT 0,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(import_id) REFERENCES imports(id) ON DELETE SET NULL,
            FOREIGN KEY(cliente_id) REFERENCES customers(id) ON DELETE SET NULL,
            FOREIGN KEY(vendor_id) REFERENCES vendors(id) ON DELETE SET NULL
        );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidez (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER,
                vendor_id INTEGER,
                customer_id INTEGER,
                pedido TEXT,
                titulo TEXT,
                cliente_codigo TEXT,
                cliente_nome TEXT,
                valor_pago REAL DEFAULT 0,
                valor_liq REAL DEFAULT 0,
                data_pagto TEXT,
                responsavel TEXT,
                created_at TEXT,
                FOREIGN KEY(import_id) REFERENCES imports(id),
                FOREIGN KEY(vendor_id) REFERENCES vendors(id),
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidez_vendor_data ON liquidez(vendor_id, data_pagto)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidez_pedido ON liquidez(pedido)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidez_cliente ON liquidez(cliente_codigo)")

        # -----------------------------
        # ORÇAMENTOS (Unificado)
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orcamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT UNIQUE,
            cliente_nome TEXT,
            pedido_id TEXT,
            vendedor_id TEXT,
            vendedor_nome TEXT,
            data_inicio TEXT,
            data_validade TEXT,
            status TEXT,
            desc_status TEXT,
            tipo_contato TEXT,
            valor_total REAL DEFAULT 0,
            uf TEXT,
            motivo TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # -----------------------------
        # SETTINGS E ÍNDICES
        # -----------------------------
        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orc_vendedor ON orcamentos(vendedor_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orc_data ON orcamentos(data_inicio)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_vendor ON orders(vendor_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id)")
            # Garante coluna tipo se a tabela vendors já existia
            conn.execute("ALTER TABLE vendors ADD COLUMN tipo TEXT DEFAULT 'Externo'")
        except:
            pass

        conn.commit()

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN customer_id INTEGER")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN pedido TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN titulo TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN responsavel TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN valor_pago REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN valor_liq REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN data_pagto TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN cliente_codigo TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN cliente_nome TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN created_at TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN medida_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN cliente_nome_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN vendor_nome_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN dedupe_key TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN grupo_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN tipo_item_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN papel_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN preco_ref REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN valor_item_ref REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN situacao_pedido TEXT")
        except:
            pass
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN motivo_pedido TEXT")
        except:
            pass
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN desc_motivo_pedido TEXT")
        except:
            pass
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN pedido_cancelado INTEGER DEFAULT 0")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_dedupe_key ON orders(dedupe_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_grupo_ref ON orders(grupo_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_tipo_item_ref ON orders(tipo_item_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_pedido_data ON orders(pedido, data_emissao)")

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN medida_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN cliente_nome_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN vendor_nome_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN dedupe_key TEXT")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_dedupe_key ON invoices(dedupe_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_numero_data ON invoices(numero, data_emissao)")

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN medida_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE liquidez ADD COLUMN dedupe_key TEXT")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidez_dedupe_key ON liquidez(dedupe_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_liquidez_data_valor ON liquidez(data_pagto, valor_pago, valor_liq)")

        try:
            conn.execute("ALTER TABLE orcamentos ADD COLUMN medida_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orcamentos ADD COLUMN cliente_nome_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orcamentos ADD COLUMN vendor_nome_ref TEXT")
        except:
            pass
        
        try:
            conn.execute("ALTER TABLE orcamentos ADD COLUMN import_id INTEGER")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orcamentos ADD COLUMN dedupe_key TEXT")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_orcamentos_dedupe_key ON orcamentos(dedupe_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orcamentos_numero_data ON orcamentos(numero, data_inicio)")

        try:
            conn.execute("ALTER TABLE imports ADD COLUMN novos INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE imports ADD COLUMN substituidos INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE imports ADD COLUMN duplicados_arquivo INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE customers ADD COLUMN fantasia TEXT")
        except:
            pass

        
        # -----------------------------
        # BLOCO JIT / REDE / FILIAL
        # -----------------------------
        try:
            conn.execute("ALTER TABLE customers ADD COLUMN rede_nome TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE customers ADD COLUMN rede_key TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN condicao_entrega TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN is_jit INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN rede_nome TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN rede_key TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN is_jit INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN rede_nome TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN rede_key TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE customer_groups ADD COLUMN matriz_customer_id INTEGER")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_rede_key ON customers(rede_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_pedido ON orders(pedido)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_rede_key ON orders(rede_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_rede_key ON invoices(rede_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_vendor_cliente_data ON invoices(vendor_id, cliente_id, data_emissao)")

        try:
            conn.execute("ALTER TABLE customers ADD COLUMN fantasia TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE customers ADD COLUMN grupo_id INTEGER")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN condicao_entrega TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE orders ADD COLUMN is_jit INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN is_jit INTEGER DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN comissionavel INTEGER DEFAULT 1")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_grupo_id ON customers(grupo_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_is_jit ON orders(is_jit)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_medida_ref ON orders(medida_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_cliente_medida ON orders(cliente_id, medida_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_is_jit ON invoices(is_jit)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_comissionavel ON invoices(comissionavel)")
        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN grupo_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN tipo_item_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN papel_ref TEXT")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN preco_ref REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN valor_item_ref REAL DEFAULT 0")
        except:
            pass

        try:
            conn.execute("ALTER TABLE invoices ADD COLUMN produto_codigo_ref TEXT")
        except:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_pedido ON invoices(pedido)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_produto_codigo_ref ON invoices(produto_codigo_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_grupo_ref ON invoices(grupo_ref)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_group_members_group ON customer_group_members(group_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_group_members_customer ON customer_group_members(customer_id)")

        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN identity_key TEXT")
        except:
            pass

        conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_status_overrides(
            identity_key TEXT PRIMARY KEY,
            identity_nome TEXT,
            status_manual TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_status_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_key TEXT NOT NULL,
            identity_nome TEXT,
            status_anterior TEXT,
            status_novo TEXT NOT NULL,
            changed_by TEXT,
            changed_role TEXT,
            created_at TEXT NOT NULL
        );
        """)

        # Recalcula a flag antiga is_jit com a REGRA ATUAL.
        # 98 - JIT precisa permanecer como matriz/base; não pode ser excluído por flag antiga.
        try:
            conn.execute("""
                UPDATE orders
                   SET is_jit = CASE
                       WHEN UPPER(REPLACE(TRIM(COALESCE(condicao_entrega,'')),' ','')) LIKE '05%JIT%IMEDIATO%'
                       THEN 1 ELSE 0 END
            """)
        except Exception:
            pass

def ensure_admin_seed() -> None:
    with conectar() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if not row or int(row["n"]) == 0:
            conn.execute(
                "INSERT INTO users(nome,email,role,senha_hash,ativo,created_at) VALUES(?,?,?,?,?,?)",
                ("Admin NPD", "admin@npd.local", "admin", generate_password_hash("npdadmin"), 1, now_iso())
            )
            conn.commit()

            # --- BLOCO 1: ATUALIZAÇÃO DE BANCO E DATAS ---

def ensure_vendors_schema(conn):
    """Garante colunas de equipe e ciclo de vida na tabela vendors."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(vendors)").fetchall()}
    if "tipo" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN tipo TEXT DEFAULT 'Externo'")
    if "performance_ativo" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN performance_ativo INTEGER NOT NULL DEFAULT 0")
    if "inativado_em" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN inativado_em TEXT")
    if "inativado_motivo" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN inativado_motivo TEXT")
    if "reativado_em" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN reativado_em TEXT")
    conn.commit()

def periodo_mes_calendario(ref: Optional[date] = None) -> Tuple[date, date]:
    """Adaptador: ver crm_npd.metrics.periodos.mes_calendario.

    Esta funcao estava definida duas vezes no arquivo (aqui e perto do fim), com
    implementacoes diferentes para o mesmo resultado. A segunda sobrescrevia esta
    em tempo de import. Agora existe uma unica definicao, delegando a formula.
    """
    mes = f_periodos.mes_calendario(ref or date.today())
    return mes.inicio, mes.fim


def faturamento_mes_calendario(conn, ref: Optional[date] = None, ate_hoje: bool = True) -> Dict[str, Any]:
    """Fonte única do faturamento do mês-calendário.

    Usa exclusivamente ``invoices`` (relatório 731) e nunca pedidos, ciclo de
    comissão ou orçamento. Para o mês atual, limita a leitura até hoje. Para
    mês futuro, retorna zero. Retorna também a última data existente na base
    para permitir auditoria visual/diagnóstico sem criar uma segunda regra.
    """
    ref = ref or date.today()
    d0, d1 = periodo_mes_calendario(ref)
    hoje = date.today()
    if d0 > hoje:
        end_ = None
    elif ate_hoje:
        end_ = min(d1, hoje)
    else:
        end_ = d1

    max_row = conn.execute("SELECT MAX(date(data_emissao)) AS dt FROM invoices WHERE data_emissao IS NOT NULL").fetchone()
    max_data = (max_row["dt"] if max_row else None)
    if end_ is None:
        return {"inicio": d0, "fim": None, "valor": 0.0, "qtd": 0, "ultima_data_base": max_data}

    row = conn.execute("""
        SELECT COUNT(*) AS qtd, COALESCE(SUM(valor),0) AS valor
        FROM invoices
        WHERE data_emissao IS NOT NULL
          AND date(data_emissao) BETWEEN date(?) AND date(?)
    """, (d0.isoformat(), end_.isoformat())).fetchone()
    return {
        "inicio": d0, "fim": end_,
        "valor": float((row["valor"] if row else 0) or 0),
        "qtd": int((row["qtd"] if row else 0) or 0),
        "ultima_data_base": max_data,
    }

def ensure_users_schema(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if "vendor_codigo" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN vendor_codigo TEXT")
    if "created_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
    conn.commit()

def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None

    with conectar() as conn:
        row = conn.execute(
            "SELECT id,nome,email,role,ativo,vendor_codigo FROM users WHERE id=?",
            (uid,)
        ).fetchone()

    return dict(row) if row else None


def login_required(view_func):
    def wrapper(*args, **kwargs):
        u = get_current_user()
        if not u or int(u["ativo"]) != 1:
            session.pop("user_id", None)
            flash("Sessão expirada. Faça login novamente.", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper

def month_range(ref: Optional[date] = None) -> Tuple[date, date]:
    """1º dia do mês -> último dia do mês"""
    ref = ref or date.today()
    start = date(ref.year, ref.month, 1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = nxt - timedelta(days=1)
    return start, end


def business_days_between(d0: date, d1: date) -> int:
    """Adaptador: ver crm_npd.metrics.periodos.dias_uteis."""
    if d1 < d0:
        return 0
    return f_periodos.dias_uteis(Periodo(inicio=d0, fim=d1))


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return default


def parse_int(x: Any, default: int = 0) -> int:
    return safe_int(x, default)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return default




def safe_pct(parte: float, total: float, default: float = 0.0) -> float:
    """Adaptador: ver crm_npd.metrics.percentuais.percentual."""
    return f_pct.percentual(parte, total, default)


def pct_delta(atual: float, anterior: float, default_zero_base: float = 0.0) -> float:
    """Adaptador: ver crm_npd.metrics.percentuais.variacao_percentual."""
    return f_pct.variacao_percentual(atual, anterior, default_zero_base)


def mercos_meta_panel(faturado: float, meta: float, dias_uteis_restantes: int) -> Dict[str, Any]:
    """Adaptador: ver crm_npd.metrics.percentuais.painel_meta."""
    painel = f_pct.painel_meta(faturado, meta, dias_uteis_restantes)
    return {"pct": painel.pct, "falta": painel.falta, "por_dia": painel.por_dia}

def get_user_vendor_id(conn, user) -> Optional[int]:
    """
    Versão BLINDADA: Traz todos os vendedores e filtra no Python.
    Resolve problemas de SQL não achando códigos por formatação.
    """
    if not user or user["role"] in ["admin", "gestor"]:
        return None
    
    # 1. Pega código do usuário
    try:
        raw_uc = user["vendor_codigo"]
    except (IndexError, KeyError, AttributeError):
        raw_uc = ""

    user_code_str = str(raw_uc or "").strip()
    
    if not user_code_str:
        return -1 

    # 2. Normaliza código do usuário (Tira zeros e espaços)
    # Ex: "09044" vira "9044"
    user_norm = user_code_str.lstrip("0")

    # 3. Busca TODOS os vendedores (sem WHERE para garantir que nada fique de fora)
    vendors = conn.execute("SELECT id, codigo, nome FROM vendors").fetchall()
    
    print(f"--- DEBUG VINCULO ({user['nome']}) ---")
    print(f"Tentando vincular código user: '{user_code_str}' (Norm: '{user_norm}')")
    print(f"Total de vendedores no banco: {len(vendors)}")

    for v in vendors:
        # Pega o código do banco, força string e limpa
        db_code_raw = str(v["codigo"] or "").strip()
        
        # Se o vendedor não tem código, pula
        if not db_code_raw:
            continue
            
        db_norm = db_code_raw.lstrip("0")
        
        # Debug para você ver o que o sistema está lendo (se precisar)
        # print(f"Checando contra: '{v['nome']}' -> Cód Banco: '{db_code_raw}'")

        # COMPARAÇÃO 1: Texto Exato
        if user_code_str == db_code_raw:
            print(f"-> SUCESSO (Exato): {v['nome']} (ID {v['id']})")
            return int(v["id"])
            
        # COMPARAÇÃO 2: Normalizada (Sem zeros)
        if user_norm == db_norm and user_norm != "":
            print(f"-> SUCESSO (Normalizado): {v['nome']} (ID {v['id']})")
            return int(v["id"])

    print(f"-> FALHA CRÍTICA: Código '{user_code_str}' não encontrou par.")
    return -1

def get_identity_invoices(conn, identity_key: str):
    """
    Retorna o histórico de faturamento da identidade comercial no modelo novo:
    MATRIZ / FILIAL / CLIENTE.
    """
    if not identity_key or identity_key == "SEM_IDENTIDADE":
        return []

    rows = conn.execute("""
        SELECT
            i.id,
            i.cliente_id,
            i.data_emissao,
            COALESCE(i.valor, 0) AS valor,
            COALESCE(c.fantasia, c.nome, '—') AS cliente
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.cliente_id
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) = ?
          AND i.data_emissao IS NOT NULL
        ORDER BY date(i.data_emissao) ASC, i.id ASC
    """, (identity_key,)).fetchall()

    return rows


def clamp_score(v, min_v=0, max_v=100):
    """Adaptador: ver crm_npd.metrics.score_cliente.limitar_score."""
    return f_score_cliente.limitar_score(v, float(min_v), float(max_v))


def score_faixa(score: float) -> dict:
    """Adaptador: ver crm_npd.metrics.score_cliente.faixa_de_score."""
    faixa = f_score_cliente.faixa_de_score(score)
    return {"faixa": str(faixa), "label": faixa.label, "cor": faixa.cor}

def soft_color_from_bootstrap(color_name: str) -> str:
    palette = {
        "success": "rgba(25, 135, 84, 0.10)",
        "primary": "rgba(13, 110, 253, 0.10)",
        "info": "rgba(13, 202, 240, 0.10)",
        "warning": "rgba(255, 193, 7, 0.12)",
        "secondary": "rgba(108, 117, 125, 0.10)",
        "danger": "rgba(220, 53, 69, 0.10)",
    }
    return palette.get((color_name or "").strip().lower(), "rgba(13, 110, 253, 0.06)")


def soft_border_from_bootstrap(color_name: str) -> str:
    palette = {
        "success": "rgba(25, 135, 84, 0.28)",
        "primary": "rgba(13, 110, 253, 0.28)",
        "info": "rgba(13, 202, 240, 0.28)",
        "warning": "rgba(255, 193, 7, 0.30)",
        "secondary": "rgba(108, 117, 125, 0.20)",
        "danger": "rgba(220, 53, 69, 0.28)",
    }
    return palette.get((color_name or "").strip().lower(), "rgba(13, 110, 253, 0.15)")

def get_identity_sales_stats(conn, customer_ids: List[int], ref: Optional[date] = None) -> dict:
    """
    Estatísticas comerciais da identidade:
    - faturamento total
    - pedidos
    - última compra
    - intervalo mediano de recompra
    - atraso atual
    """
    if not customer_ids:
        return {
            "valor_total": 0.0,
            "pedidos": 0,
            "ultima_data": None,
            "intervalo_mediano": None,
            "atraso_atual": None,
        }

    placeholders = ",".join("?" for _ in customer_ids)

    rows = conn.execute(f"""
        SELECT
            data_emissao,
            valor
        FROM invoices
        WHERE cliente_id IN ({placeholders})
          AND COALESCE(valor, 0) > 0
          AND data_emissao IS NOT NULL
        ORDER BY data_emissao ASC
    """, customer_ids).fetchall()

    datas = []
    valor_total = 0.0

    for r in rows:
        d = try_parse_date(r["data_emissao"])
        if d:
            try:
                datas.append(date.fromisoformat(d))
            except:
                pass
    
        try:
            valor_total += float(r["valor"] or 0)
        except:
            pass

    datas = f_recompra.datas_unicas_ordenadas(datas)
    pedidos = len(datas)
    ultima_data = datas[-1] if datas else None

    intervalos = f_recompra.intervalos_entre_compras(datas)

    intervalo_mediano = None
    if intervalos:
        ordered = sorted(intervalos)
        n = len(ordered)
        meio = n // 2
        if n % 2 == 1:
            intervalo_mediano = float(ordered[meio])
        else:
            intervalo_mediano = (ordered[meio - 1] + ordered[meio]) / 2.0

    if ref is None:
        ref = date.today()

    atraso_atual = None
    if ultima_data:
        atraso_atual = (ref - ultima_data).days

    return {
        "valor_total": float(valor_total),
        "pedidos": int(pedidos),
        "ultima_data": ultima_data,
        "intervalo_mediano": intervalo_mediano,
        "atraso_atual": atraso_atual,
    }

def _stats_para_dominio(stats: dict) -> EstatisticasIdentidade:
    """Converte o dicionario de estatisticas do monolito no tipo do dominio."""
    ultima = stats.get("ultima_data")
    if isinstance(ultima, str) and ultima.strip():
        try:
            ultima = date.fromisoformat(ultima.strip()[:10])
        except ValueError:
            ultima = None
    elif not isinstance(ultima, date):
        ultima = None
    return EstatisticasIdentidade(
        valor_total=float(stats.get("valor_total") or 0),
        pedidos=int(stats.get("pedidos") or 0),
        intervalo_mediano=stats.get("intervalo_mediano"),
        atraso_atual=stats.get("atraso_atual"),
        ultima_data=ultima,
    )


def calc_frequency_score(intervalo_mediano: Optional[float], pedidos: int) -> float:
    """Adaptador: ver crm_npd.metrics.score_cliente.score_frequencia."""
    return f_score_cliente.score_frequencia(intervalo_mediano, pedidos)


def calc_repurchase_score(stats: dict) -> float:
    """Adaptador: ver crm_npd.metrics.score_cliente.score_recompra."""
    return f_score_cliente.score_recompra(_stats_para_dominio(stats))


def calc_value_score(valor_cliente: float, valor_ref_max: float) -> float:
    """Adaptador: ver crm_npd.metrics.score_cliente.score_valor."""
    return f_score_cliente.score_valor(valor_cliente, valor_ref_max)


def montar_score_identidade(stats: dict, valor_max: float) -> dict:
    """Monta o registro de score de uma identidade no formato esperado pelas telas.

    Fonte unica do calculo. Antes da refatoracao, este bloco estava duplicado em
    ``_build_identity_score_map_global_fast`` e em ``build_identity_score_map``,
    com a mesma formula escrita duas vezes.
    """
    dominio = _stats_para_dominio(stats)
    resultado = f_score_cliente.calcular_score(dominio, valor_max)
    faixa = resultado.faixa
    return {
        "score": resultado.score,
        "score_frequencia": resultado.frequencia,
        "score_recompra": resultado.recompra,
        "score_valor": resultado.valor,
        "faixa": str(faixa),
        "label": faixa.label,
        "cor": faixa.cor,
        "valor_total": float(stats.get("valor_total") or 0),
        "pedidos": int(stats.get("pedidos") or 0),
        "intervalo_mediano": stats.get("intervalo_mediano"),
        "atraso_atual": stats.get("atraso_atual"),
        "ultima_data": stats.get("ultima_data"),
    }


def _build_identity_score_map_global_fast(conn) -> Dict[str, dict]:
    """Versão em lote do score de identidades para uso global.

    A implementação antiga fazia milhares de SELECTs (um por cliente e depois
    vários por identidade). Em bases com 5k+ clientes isso fazia a atualização
    de tarefas levar minutos. Esta versão lê o faturamento uma única vez e
    mantém exatamente a mesma fórmula de score.
    """
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key,
            i.data_emissao AS data_emissao,
            COALESCE(i.valor, 0) AS valor
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE COALESCE(i.cliente_id,0) > 0
          AND COALESCE(i.valor,0) > 0
          AND i.data_emissao IS NOT NULL
          AND TRIM(COALESCE(i.data_emissao,'')) <> ''
        ORDER BY identity_key, date(i.data_emissao), i.id
    """).fetchall()

    ref = date.today()
    agg: Dict[str, dict] = {}
    for r in rows:
        ident = (r["identity_key"] or "").strip()
        if not ident or ident == "SEM_IDENTIDADE":
            continue
        item = agg.setdefault(ident, {"valor_total": 0.0, "datas": []})
        item["valor_total"] += float(r["valor"] or 0)
        d = try_parse_date(r["data_emissao"] or "")
        if d:
            try:
                item["datas"].append(date.fromisoformat(d))
            except Exception:
                pass

    raw: Dict[str, dict] = {}
    valor_max = 0.0
    for ident, item in agg.items():
        datas = f_recompra.datas_unicas_ordenadas(item["datas"])
        pedidos = len(datas)
        ultima_data = datas[-1] if datas else None
        intervalos = [(datas[i] - datas[i-1]).days for i in range(1, len(datas))]
        intervalo_mediano = None
        if intervalos:
            ordered = sorted(intervalos)
            n = len(ordered)
            meio = n // 2
            if n % 2 == 1:
                intervalo_mediano = float(ordered[meio])
            else:
                intervalo_mediano = (ordered[meio - 1] + ordered[meio]) / 2.0
        atraso_atual = (ref - ultima_data).days if ultima_data else None
        stats = {
            "valor_total": float(item["valor_total"] or 0),
            "pedidos": int(pedidos),
            "ultima_data": ultima_data,
            "intervalo_mediano": intervalo_mediano,
            "atraso_atual": atraso_atual,
        }
        raw[ident] = stats
        valor_max = max(valor_max, stats["valor_total"])

    return {
        identity_key: montar_score_identidade(stats, valor_max)
        for identity_key, stats in raw.items()
    }


def build_identity_score_map(conn, user=None, customer_ids_scope: Optional[List[int]] = None) -> Dict[str, dict]:
    """
    Gera score por identidade comercial com régua de negócio real.
    Não usa mais normalização 'melhor da carteira = 100'.
    O score final passa a refletir:
    - frequência real de recompra
    - situação atual vs padrão do cliente
    - valor faturado
    """
    # Caminho global em lote: evita N+1 de milhares de consultas durante a
    # atualização automática de tarefas e nas telas administrativas.
    if user is None and not customer_ids_scope:
        return _build_identity_score_map_global_fast(conn)

    ref = date.today()

    role = ((user["role"] if user and "role" in user else "") or "").strip().lower()
    vendor_lock = get_user_vendor_id(conn, user) if user else None

    params = []
    vendor_sql = ""

    if customer_ids_scope:
        placeholders_scope = ",".join("?" for _ in customer_ids_scope)
        scope_sql = f" AND c.id IN ({placeholders_scope}) "
        params.extend(customer_ids_scope)
    else:
        scope_sql = ""

    if user and role not in ("admin", "gestor") and vendor_lock:
        vendor_sql = """
            AND EXISTS (
                SELECT 1
                FROM invoices i
                WHERE i.cliente_id = c.id
                  AND i.vendor_id = ?
            )
        """
        params.append(vendor_lock)

    customers = conn.execute(f"""
        SELECT c.id
        FROM customers c
        WHERE 1=1
        {scope_sql}
        {vendor_sql}
    """, params).fetchall()

    identities = {}
    for r in customers:
        cid = int(r["id"])
        identity_key = get_customer_identity_key(conn, cid)
        if not identity_key:
            continue
        identities.setdefault(identity_key, set()).add(cid)

    raw = {}
    valor_max = 0.0

    for identity_key, ids_set in identities.items():
        ids_list = sorted(ids_set)
        stats = get_identity_sales_stats(conn, ids_list, ref=ref)
        raw[identity_key] = stats
        valor_max = max(valor_max, float(stats["valor_total"] or 0))

    return {
        identity_key: montar_score_identidade(stats, valor_max)
        for identity_key, stats in raw.items()
    }

def get_task_priority_from_score(score_data: dict) -> dict:
    """Adaptador: ver crm_npd.metrics.score_cliente.prioridade_tarefa."""
    bruta = str(score_data.get("faixa") or "D").upper()
    try:
        faixa = FaixaScore(bruta)
    except ValueError:
        faixa = FaixaScore.FRIO

    prioridade = f_score_cliente.prioridade_tarefa(
        faixa,
        score_data.get("atraso_atual"),
        score_data.get("intervalo_mediano"),
    )
    return {
        "prioridade": str(prioridade),
        "cor": prioridade.cor,
        "ordem": prioridade.ordem,
        "motivo": f_score_cliente.MOTIVO_PRIORIDADE[prioridade],
    }

def enrich_identity_score_for_ui(score_data: dict) -> dict:
    sc = dict(score_data or {})
    pr = get_task_priority_from_score(sc)

    sc["prioridade"] = pr.get("prioridade", "BAIXA")
    sc["prioridade_cor"] = pr.get("cor", "secondary")
    sc["prioridade_ordem"] = pr.get("ordem", 9)
    sc["prioridade_motivo"] = pr.get("motivo", "")

    return sc

def summarize_identity_repurchase(conn, identity_key: str, ref: date):
    """
    Resume recompra por identidade comercial.
    """
    rows = get_identity_invoices(conn, identity_key)
    datas = []

    for r in rows:
        d = (r["data_emissao"] or "").strip()
        if d:
            try:
                datas.append(datetime.strptime(d, "%Y-%m-%d").date())
            except:
                pass

    datas = sorted(list(set(datas)))

    if len(datas) == 0:
        return {
            "intervalo_mediano": 0.0,
            "atraso_atual": 0.0,
            "ultima_compra": None,
            "compras_qtd": 0
        }

    if len(datas) == 1:
        ultima = datas[-1]
        atraso = max(0, (ref - ultima).days)
        return {
            "intervalo_mediano": 0.0,
            "atraso_atual": float(atraso),
            "ultima_compra": ultima,
            "compras_qtd": 1
        }

    intervalos = []
    for i in range(1, len(datas)):
        intervalos.append((datas[i] - datas[i - 1]).days)

    try:
        intervalo_mediano = statistics.median(intervalos) if intervalos else 0.0
    except:
        intervalo_mediano = 0.0

    ultima = datas[-1]
    atraso = max(0, (ref - ultima).days)

    return {
        "intervalo_mediano": float(intervalo_mediano or 0.0),
        "atraso_atual": float(atraso or 0.0),
        "ultima_compra": ultima,
        "compras_qtd": len(datas)
    }

def list_overdue_identities(conn, ref=None):
    ref = ref or date.today()
    rows = conn.execute("""
        SELECT i.cliente_id,
               i.data_emissao,
               COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) AS identity_key,
               COALESCE(cir.identity_nome, '') AS identity_nome_raw,
               c.codigo AS customer_codigo,
               c.nome AS customer_nome,
               c.fantasia AS customer_fantasia
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.cliente_id
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE i.data_emissao IS NOT NULL
          AND COALESCE(i.vendor_id,0) > 0
          AND COALESCE(i.cliente_id,0) > 0
        ORDER BY date(i.data_emissao) ASC, i.id ASC
    """).fetchall()
    agrupado = {}
    for r in rows:
        ident = (r['identity_key'] or '').strip()
        if not ident or ident == 'SEM_IDENTIDADE':
            continue
        try:
            d = datetime.strptime((r['data_emissao'] or '').strip(), '%Y-%m-%d').date()
        except Exception:
            continue
        identity_raw = (r['identity_nome_raw'] or '').strip()
        nome_seguro = identity_raw if identity_raw and not is_generic_customer_name(identity_raw) else safe_customer_display_name(
            r['customer_nome'], r['customer_fantasia'], r['customer_codigo'], r['cliente_id']
        )
        item = agrupado.setdefault(ident, {'cliente_id': int(r['cliente_id'] or 0), 'nome': nome_seguro, 'datas': []})
        item['datas'].append(d)
    out = []
    for ident, item in agrupado.items():
        datas = sorted(set(item['datas']))
        if len(datas) < 2:
            continue
        intervalos = [(datas[i] - datas[i-1]).days for i in range(1, len(datas))]
        intervalo_mediano = float(statistics.median(intervalos)) if intervalos else 0.0
        atraso_atual = float(max(0, (ref - datas[-1]).days))
        if intervalo_mediano > 0 and atraso_atual > intervalo_mediano:
            out.append({
                'identity_key': ident,
                'cliente_id': item['cliente_id'],
                'nome': item['nome'],
                'intervalo_mediano': intervalo_mediano,
                'atraso_atual': atraso_atual,
                'atraso_excedente': atraso_atual - intervalo_mediano,
            })
    out.sort(key=lambda x: x['atraso_excedente'], reverse=True)
    return out


def deduplicate_open_tasks(conn, actor_name: str = 'Sistema') -> int:
    rows = conn.execute("""
        SELECT id, vendor_id, COALESCE(identity_key, '') AS identity_key, status
        FROM tasks
        WHERE status IN ('ABERTA', 'EM CONTATO', 'AGUARDANDO RESPOSTA', 'NEGOCIANDO')
          AND COALESCE(identity_key, '') <> ''
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
    """).fetchall()
    seen = {}
    removed = 0
    for r in rows:
        ident = (r['identity_key'] or '').strip()
        if not ident:
            continue
        if ident not in seen:
            seen[ident] = int(r['id'])
            continue
        conn.execute("UPDATE tasks SET status='CANCELADA', updated_at=? WHERE id=?", (now_iso(), int(r['id'])))
        conn.execute("""
            INSERT INTO task_history (task_id, status_anterior, status_novo, observacao, alterado_por, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(r['id']),
            normalize_task_status(r['status'] or 'ABERTA'),
            'CANCELADA',
            'Tarefa duplicada cancelada automaticamente para manter apenas uma tarefa aberta por identidade comercial.',
            actor_name,
            now_iso(),
        ))
        removed += 1
    return removed


def check_repurchase_tasks(conn):
    """
    Gera tarefas automáticas de recompra por identidade comercial.
    Mantém apenas uma tarefa aberta por identidade e sempre no último responsável.
    """
    hoje = date.today()
    score_map = build_identity_score_map(conn)
    overdue = list_overdue_identities(conn, ref=hoje)
    vendor_map = build_last_vendor_map(conn)
    existentes_rows = conn.execute("""
        SELECT id, COALESCE(identity_key, '') AS identity_key
        FROM tasks
        WHERE status IN ('ABERTA', 'EM CONTATO', 'AGUARDANDO RESPOSTA', 'NEGOCIANDO')
          AND COALESCE(identity_key, '') <> ''
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
    """).fetchall()
    existentes = {}
    for r in existentes_rows:
        ident = (r['identity_key'] or '').strip()
        if ident and ident not in existentes:
            existentes[ident] = int(r['id'])

    ts = now_iso()
    for item in overdue:
        identity_key = (item['identity_key'] or '').strip()
        if not identity_key:
            continue
        vendor_id = int(vendor_map.get(identity_key) or 0)
        if vendor_id <= 0:
            continue

        existente_id = existentes.get(identity_key)

        sc = score_map.get(identity_key, {})
        prio = get_task_priority_from_score(sc)
        titulo = f"[{prio['prioridade']}] Cliente fora da média de recompra: {(item['nome'] or 'Cliente').strip()}"
        descricao = (
            f"{prio['motivo']}\n"
            f"Intervalo médio: {float(item['intervalo_mediano'] or 0):.0f} dias\n"
            f"Atraso atual: {float(item['atraso_atual'] or 0):.0f} dias\n"
            f"Excedente: {float(item['atraso_excedente'] or 0):.0f} dias"
        )

        if existente_id:
            conn.execute("UPDATE tasks SET vendor_id=?, titulo=?, descricao=?, updated_at=? WHERE id=?", (vendor_id, titulo, descricao, ts, existente_id))
            continue

        create_task_unificada(
            conn=conn,
            vendor_id=vendor_id,
            titulo=titulo,
            descricao=descricao,
            cliente_id=int(item['cliente_id'] or 0) if int(item['cliente_id'] or 0) > 0 else None,
            identity_key=identity_key,
            criada_por='Sistema',
            status='ABERTA',
        )



def cancel_stale_auto_repurchase_tasks(conn, actor_name: str = "Sistema") -> int:
    """Cancela somente tarefas automáticas cuja identidade não existe mais no faturamento.

    É especialmente importante após corrigir cadastros genéricos do Sisplan: uma
    identidade antiga como CLIENTE:<id genérico> pode deixar de ter qualquer NF
    depois que as linhas são religadas ao código correto do cliente.
    """
    valid_rows = conn.execute("""
        SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key),''), 'CLIENTE:' || i.cliente_id) AS identity_key
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id=i.cliente_id
        WHERE COALESCE(i.cliente_id,0) > 0
    """).fetchall()
    valid = {(r["identity_key"] or "").strip() for r in valid_rows if (r["identity_key"] or "").strip()}

    rows = conn.execute("""
        SELECT id, status, COALESCE(identity_key,'') AS identity_key, COALESCE(titulo,'') AS titulo, COALESCE(criada_por,'') AS criada_por
        FROM tasks
        WHERE status IN ('ABERTA','EM CONTATO','AGUARDANDO RESPOSTA','NEGOCIANDO')
          AND COALESCE(identity_key,'') <> ''
          AND (
              UPPER(COALESCE(criada_por,''))='SISTEMA'
              OR UPPER(COALESCE(titulo,'')) LIKE '%RECOMPRA%'
              OR UPPER(COALESCE(titulo,'')) LIKE '%FORA DA M%DIA%'
          )
    """).fetchall()
    cancelled = 0
    ts = now_iso()
    for r in rows:
        ident = (r["identity_key"] or "").strip()
        if not ident or ident in valid:
            continue
        old_status = normalize_task_status(r["status"] or "ABERTA")
        conn.execute("UPDATE tasks SET status='CANCELADA', updated_at=? WHERE id=?", (ts, int(r["id"])))
        conn.execute("""
            INSERT INTO task_history(task_id,status_anterior,status_novo,observacao,alterado_por,created_at)
            VALUES(?,?,?,?,?,?)
        """, (
            int(r["id"]), old_status, 'CANCELADA',
            'Tarefa automática encerrada após correção/revinculação do cliente no Sisplan.',
            actor_name, ts
        ))
        cancelled += 1
    return cancelled


def refresh_repurchase_tasks_after_sisplan(conn, actor_name: str = "Sistema") -> Dict[str, int]:
    """Atualiza tarefas depois de importação/reprocessamento do Sisplan."""
    cancelled = cancel_stale_auto_repurchase_tasks(conn, actor_name=actor_name)
    check_repurchase_tasks(conn)
    moved = normalize_open_tasks_to_last_vendor(conn, actor_name=actor_name)
    dedup = deduplicate_open_tasks(conn, actor_name=actor_name)
    return {"cancelled": int(cancelled), "moved": int(moved), "dedup": int(dedup)}


# Metas de equipe por eixo. Legado EQUIPE_* = pedidos (antes da separação).
META_EIXOS = ("pedidos", "faturamento")
_META_TIPOS = {
    "pedidos": ("PEDIDOS_INTERNA", "PEDIDOS_EXTERNO"),
    "faturamento": ("FATURAMENTO_INTERNA", "FATURAMENTO_EXTERNO"),
}
_META_LEGADO_PEDIDOS = ("EQUIPE_INTERNA", "EQUIPE_EXTERNO")


def ensure_goals_table(conn):
    """Cria tabela para armazenar histórico de metas por mês."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            periodo TEXT NOT NULL,      -- Formato: 'YYYY-MM'
            tipo_entidade TEXT,         -- PEDIDOS_*/FATURAMENTO_*/VENDEDOR (legado: EQUIPE_*)
            entity_id INTEGER,          -- ID do vendedor (se for meta individual)
            valor REAL NOT NULL,
            updated_at TEXT,
            UNIQUE(periodo, tipo_entidade, entity_id)
        );
    """)
    conn.commit()

def set_goal_history(conn, periodo: str, tipo: str, valor: float, entity_id: int = 0):
    """Define uma meta para um mês específico."""
    conn.execute("""
        INSERT INTO goals_history (periodo, tipo_entidade, entity_id, valor, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(periodo, tipo_entidade, entity_id) 
        DO UPDATE SET valor=excluded.valor, updated_at=excluded.updated_at
    """, (periodo, tipo, entity_id, valor, now_iso()))

def get_goal_history(conn, periodo: str, tipo: str, entity_id: int = 0) -> float:
    """Busca a meta de um mês específico."""
    row = conn.execute("""
        SELECT valor FROM goals_history 
        WHERE periodo=? AND tipo_entidade=? AND entity_id=?
    """, (periodo, tipo, entity_id)).fetchone()
    return float(row["valor"]) if row else 0.0


def _goal_history_existe(conn, periodo: str, tipo: str, entity_id: int = 0) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM goals_history
        WHERE periodo=? AND tipo_entidade=? AND entity_id=?
        """,
        (periodo, tipo, entity_id),
    ).fetchone()
    return row is not None


def get_meta_equipe(conn, periodo: str, eixo: str) -> tuple[float, float]:
    """Meta Interna/Externa do mês para pedidos ou faturamento.

    Pedidos: se ainda nao houver PEDIDOS_*, usa o legado EQUIPE_*.
    """
    chave = (eixo or "pedidos").strip().lower()
    if chave not in _META_TIPOS:
        chave = "pedidos"
    tipo_int, tipo_ext = _META_TIPOS[chave]
    if chave == "pedidos" and not (
        _goal_history_existe(conn, periodo, tipo_int)
        or _goal_history_existe(conn, periodo, tipo_ext)
    ):
        tipo_int, tipo_ext = _META_LEGADO_PEDIDOS
    return (
        get_goal_history(conn, periodo, tipo_int),
        get_goal_history(conn, periodo, tipo_ext),
    )


def set_meta_equipe(conn, periodo: str, eixo: str, interna: float, externa: float) -> None:
    """Grava meta Interna/Externa no eixo pedidos ou faturamento."""
    chave = (eixo or "pedidos").strip().lower()
    if chave not in _META_TIPOS:
        raise ValueError(f"Eixo de meta invalido: {eixo}")
    tipo_int, tipo_ext = _META_TIPOS[chave]
    set_goal_history(conn, periodo, tipo_int, float(interna or 0.0))
    set_goal_history(conn, periodo, tipo_ext, float(externa or 0.0))

# Inicializa a tabela na primeira execução
with conectar() as conn:
    ensure_goals_table(conn)


# -----------------------------
# Parsing helpers
# -----------------------------
def norm(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("\ufeff", "")
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_header(h: str) -> str:
    h = norm(h).lower()
    h = h.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
    h = h.replace("é", "e").replace("ê", "e").replace("í", "i").replace("ó", "o").replace("ô", "o").replace("ú", "u")
    h = re.sub(r"[^a-z0-9_ ]+", "", h).strip()
    h = h.replace(" ", "_")
    return h


def try_parse_date(s: str) -> Optional[str]:
    s = norm(s)
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    # tenta YYYYMMDD
    if re.fullmatch(r"\d{8}", s):
        try:
            return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            return None
    return None


def parse_br_money(s: Any) -> float:
    if s is None:
        return 0.0
    txt = str(s).strip()
    if not txt:
        return 0.0
    txt = txt.replace("R$", "").replace(" ", "")
    # padrão BR: 1.234,56
    if "," in txt and txt.count(",") == 1:
        txt = txt.replace(".", "").replace(",", ".")
    else:
        # se vier 1234.56, mantém
        txt = txt.replace(",", "")
    try:
        return float(txt)
    except Exception:
        return 0.0


def parse_float(s: Any) -> float:
    if s is None:
        return 0.0
    txt = str(s).strip()
    if not txt:
        return 0.0
    txt = txt.replace(" ", "")
    if "," in txt and txt.count(",") == 1 and "." not in txt:
        txt = txt.replace(",", ".")
    txt = txt.replace(",", "")
    try:
        return float(txt)
    except Exception:
        return 0.0


def split_codigo_nome(s: str) -> Tuple[Optional[str], str]:
    s = norm(s)
    if not s:
        return None, ""
    # padrões: "09044 - AMANDA ..." ou "09044-AMANDA" ou "09044  AMANDA"
    m = re.match(r"^\s*(\d+)\s*[-–]\s*(.+)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^\s*(\d+)\s+(.+)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, s


def read_csv_dicts(file_bytes: bytes) -> Tuple[List[str], List[Dict[str, str]]]:
    # tenta preservar o encoding original do CSV (muitos arquivos vêm em latin1)
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = file_bytes.decode("utf-8", errors="replace")

    # sniff delimiter
    sample = text[:4096]
    delim = ";"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t")
        delim = dialect.delimiter
    except Exception:
        pass

    f = io.StringIO(text)
    reader = csv.DictReader(f, delimiter=delim)
    headers = reader.fieldnames or []
    rows = []
    for r in reader:
        rows.append({(k or ""): (v or "") for k, v in r.items()})
    return headers, rows


def read_csv_sample_dicts(file_bytes: bytes, max_rows: int = 200) -> Tuple[List[str], List[Dict[str, str]]]:
    """Lê cabeçalho + pequena amostra sem percorrer o CSV inteiro.

    Usado somente para detectar o tipo do relatório antes da importação. Assim
    os quatro arquivos grandes não são parseados duas vezes.
    """
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = file_bytes.decode("utf-8", errors="replace")

    sample = text[:4096]
    delim = ";"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t")
        delim = dialect.delimiter
    except Exception:
        pass

    f = io.StringIO(text)
    reader = csv.DictReader(f, delimiter=delim)
    headers = reader.fieldnames or []
    rows = []
    for i, r in enumerate(reader):
        rows.append({(k or ""): (v or "") for k, v in r.items()})
        if i + 1 >= int(max_rows or 200):
            break
    return headers, rows


def ensure_ciclos_faturamento_table(conn):
    """Garante a tabela de fechamento manual do ciclo de faturamento."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ciclos_faturamento(
            periodo TEXT PRIMARY KEY,
            dia_fechamento INTEGER NOT NULL DEFAULT 24,
            updated_at TEXT NOT NULL
        );
    """)


def get_dia_fechamento_faturamento(conn, periodo: str, default: int = 24) -> int:
    """Retorna o dia de fechamento do ciclo de faturamento para o mês informado."""
    ensure_ciclos_faturamento_table(conn)
    row = conn.execute("SELECT dia_fechamento FROM ciclos_faturamento WHERE periodo=?", (periodo,)).fetchone()
    try:
        dia = int(row["dia_fechamento"]) if row and row["dia_fechamento"] is not None else int(default)
    except Exception:
        dia = int(default)
    return max(1, min(31, dia))


def set_dia_fechamento_faturamento(conn, periodo: str, dia_fechamento: int) -> None:
    """Salva manualmente o dia de fechamento do faturamento para um mês."""
    ensure_ciclos_faturamento_table(conn)
    dia = max(1, min(31, int(dia_fechamento or 24)))
    conn.execute("""
        INSERT INTO ciclos_faturamento(periodo, dia_fechamento, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(periodo) DO UPDATE SET
            dia_fechamento=excluded.dia_fechamento,
            updated_at=excluded.updated_at
    """, (periodo, dia, now_iso()))


def _safe_date_with_day(year: int, month: int, day: int) -> date:
    """Adaptador: ver crm_npd.metrics.periodos.dia_valido_no_mes."""
    return f_periodos.dia_valido_no_mes(year, month, day)


def periodo_ciclo_faturamento_manual(ref: Optional[date] = None) -> Tuple[date, date]:
    """Ciclo de faturamento do mês de referência com fechamento editável por mês.

    Ex.: Abril com fechamento 25 => 26/03 a 25/04.
    Sem configuração manual, o padrão é 26/mês anterior a 25/mês atual.

    A leitura do dia de fechamento continua aqui porque depende do banco; o
    cálculo das datas em si está em crm_npd.metrics.periodos.ciclo_faturamento,
    testável sem banco.
    """
    ref = ref or date.today()
    periodo = ref.strftime("%Y-%m")
    try:
        with conectar() as conn:
            dia = get_dia_fechamento_faturamento(
                conn, periodo, default=f_periodos.DIA_FECHAMENTO_PADRAO
            )
    except Exception:
        dia = f_periodos.DIA_FECHAMENTO_PADRAO
    ciclo = f_periodos.ciclo_faturamento(ref, dia)
    return ciclo.inicio, ciclo.fim


# -----------------------------
# Business rules: Período 25 -> 24 (último fechado)
# -----------------------------
def ultimo_periodo_fechado(ref: Optional[date] = None) -> Tuple[date, date]:
    """Retorna o ciclo de faturamento fechado para o mês de referência.

    Usa a mesma regra global configurada em Configurar Metas:
    se não houver fechamento manual, usa 26/mês anterior até 25/mês atual.
    """
    return periodo_ciclo_faturamento_manual(ref or date.today())


def periodo_em_andamento_25_ate_hoje(ref: Optional[date] = None) -> Tuple[date, date]:
    """Período financeiro em andamento usando o mesmo fechamento configurável.

    Mantém o nome antigo por compatibilidade, mas não usa mais 25 fixo.
    Se o mês já passou do fechamento, retorna o ciclo fechado completo.
    Se ainda não passou, retorna do início do ciclo até hoje.
    """
    ref = ref or date.today()
    start_, end_ = periodo_ciclo_faturamento_manual(ref)
    return start_, min(ref, end_)


def periodo_ciclo_25_24_que_contem_hoje(ref: Optional[date] = None) -> Tuple[date, date]:
    """Retorna o ciclo de faturamento do mês de referência.

    Mantém o nome antigo para não quebrar rotas existentes, mas agora respeita
    o fechamento manual salvo em Configurar Metas.
    """
    return periodo_ciclo_faturamento_manual(ref)

def cliente_metrics(conn, cliente_id):
    today = date.today()
    c0, c1 = periodo_ciclo_25_24_que_contem_hoje()

    # AGORA BUSCA DE INVOICES (FATURAMENTO)
    rows = conn.execute("""
        SELECT i.data_emissao, i.valor, v.nome AS vendedor, i.vendor_id
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE i.cliente_id=?
          AND i.data_emissao IS NOT NULL
        ORDER BY date(i.data_emissao)
    """, (cliente_id,)).fetchall()

    if not rows:
        return {
            "total_ciclo": 0, "pedidos_ciclo": 0, "dias_ultima": None,
            "intervalo": None, "status": "SEM HISTÓRICO", "transferido": False,
            "novo_empresa": True, "vendedores": [], "projecao": 0,
            "ultima_compra": None
        }

    datas = [date.fromisoformat(r["data_emissao"]) for r in rows]
    primeira = datas[0]
    ultima = datas[-1]
    dias_ultima = (today - ultima).days

    diffs = [(datas[i] - datas[i-1]).days for i in range(1, len(datas))]
    intervalo = int(statistics.median(diffs)) if diffs else None

    if not intervalo: status = "NOVO"
    elif dias_ultima <= intervalo: status = "ATIVO"
    elif dias_ultima <= intervalo * 1.3: status = "EM RISCO"
    else: status = "INATIVO"

    # Totais baseados em Faturamento
    total_ciclo = conn.execute("""
        SELECT COALESCE(SUM(valor),0) FROM invoices
        WHERE cliente_id=? AND date(data_emissao) BETWEEN date(?) AND date(?)
    """, (cliente_id, c0.isoformat(), c1.isoformat())).fetchone()[0]

    pedidos_ciclo = conn.execute("""
        SELECT COUNT(*) FROM invoices
        WHERE cliente_id=? AND date(data_emissao) BETWEEN date(?) AND date(?)
    """, (cliente_id, c0.isoformat(), c1.isoformat())).fetchone()[0]

    dias_passados = business_days_between(c0, today)
    dias_totais = business_days_between(c0, c1)
    projecao = (total_ciclo / dias_passados * dias_totais) if dias_passados > 0 else 0

    vendedores = list({r["vendedor"] for r in rows if r["vendedor"]})

    # Lógica da Última Compra (Baseado na última Nota Fiscal)
    last_inv = rows[-1]
    ultima_valor = float(last_inv["valor"] or 0)
    ultima_vend_id = last_inv["vendor_id"]
    
    pct_meta = 0.0
    if ultima_vend_id:
        mv = meta_vendedor(conn, ultima_vend_id)
        if mv > 0:
            pct_meta = (ultima_valor / mv) * 100

    dados_ultima = {
        "valor": ultima_valor,
        "data": last_inv["data_emissao"],
        "vendedor": last_inv["vendedor"],
        "pct_meta": pct_meta
    }

    return {
        "total_ciclo": total_ciclo,
        "pedidos_ciclo": pedidos_ciclo,
        "dias_ultima": dias_ultima,
        "intervalo": intervalo,
        "status": status,
        "transferido": (len(vendedores) > 1),
        "novo_empresa": (primeira >= c0),
        "vendedores": vendedores,
        "projecao": projecao,
        "ultima_compra": dados_ultima
    }

def cliente_metrics_multi(conn, customer_ids: List[int]):
    if not customer_ids:
        return {}

    placeholders = ",".join("?" for _ in customer_ids)

    row = conn.execute(f"""
        SELECT
            COUNT(*) AS pedidos,
            SUM(valor) AS faturamento
        FROM invoices
        WHERE cliente_id IN ({placeholders})
    """, customer_ids).fetchone()

    return {
        "pedidos": int(row["pedidos"] or 0),
        "faturamento": float(row["faturamento"] or 0)
    }

def vendor_repurchase_metrics(conn, vendor_id: int, ref=None):
    """
    Métricas de recompra por identidade comercial:
    GRUPO > REDE > CLIENTE
    """
    ref = ref or date.today()

    rows = conn.execute("""
        SELECT DISTINCT
            i.cliente_id
        FROM invoices i
        WHERE i.vendor_id = ?
          AND i.data_emissao IS NOT NULL
    """, (vendor_id,)).fetchall()

    identidades = {}
    for r in rows:
        cid = r["cliente_id"]
        ident = get_customer_identity_key(conn, cid)
        if ident and ident != "SEM_IDENTIDADE":
            identidades[ident] = cid

    atrasos = []
    intervalos = []
    atrasados_qtd = 0

    for ident, cid in identidades.items():
        resumo = summarize_identity_repurchase(conn, ident, ref)

        intervalo_mediano = float(resumo["intervalo_mediano"] or 0.0)
        atraso_atual = float(resumo["atraso_atual"] or 0.0)

        if intervalo_mediano > 0:
            intervalos.append(intervalo_mediano)

            # regra de atraso:
            # se já passou do intervalo mediano, consideramos atrasado
            if atraso_atual > intervalo_mediano:
                atrasados_qtd += 1
                atrasos.append(atraso_atual - intervalo_mediano)

    atraso_mediano = statistics.median(atrasos) if atrasos else 0.0
    intervalo_mediano_geral = statistics.median(intervalos) if intervalos else 0.0

    return {
        "atraso_mediano": float(atraso_mediano or 0.0),
        "intervalo_mediano": float(intervalo_mediano_geral or 0.0),
        "atrasados_qtd": int(atrasados_qtd or 0),
    }

def get_last_vendor_for_identity(conn, identity_key: str) -> int:
    if not identity_key or identity_key == "SEM_IDENTIDADE":
        return 0

    row = conn.execute("""
        WITH movs AS (
            SELECT
                i.vendor_id AS vendor_id,
                date(i.data_emissao) AS mov_date,
                COALESCE(i.id, 0) AS mov_id
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE i.data_emissao IS NOT NULL
              AND COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) = ?
              AND COALESCE(i.vendor_id, 0) > 0

            UNION ALL

            SELECT
                o.vendor_id AS vendor_id,
                date(o.data_emissao) AS mov_date,
                COALESCE(o.id, 0) AS mov_id
            FROM orders o
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = o.cliente_id
            WHERE o.data_emissao IS NOT NULL
              AND COALESCE(cir.identity_key, 'CLIENTE:' || o.cliente_id) = ?
              AND COALESCE(o.vendor_id, 0) > 0
        )
        SELECT vendor_id
        FROM movs
        WHERE vendor_id IS NOT NULL
        ORDER BY mov_date DESC, mov_id DESC
        LIMIT 1
    """, (identity_key, identity_key)).fetchone()
    return int(row["vendor_id"] or 0) if row else 0


def build_last_vendor_map(conn) -> Dict[str, int]:
    rows = conn.execute("""
        WITH movs AS (
            SELECT
                COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) AS identity_key,
                i.vendor_id AS vendor_id,
                date(i.data_emissao) AS mov_date,
                COALESCE(i.id, 0) AS mov_id
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE i.data_emissao IS NOT NULL
              AND COALESCE(i.vendor_id, 0) > 0

            UNION ALL

            SELECT
                COALESCE(cir.identity_key, 'CLIENTE:' || o.cliente_id) AS identity_key,
                o.vendor_id AS vendor_id,
                date(o.data_emissao) AS mov_date,
                COALESCE(o.id, 0) AS mov_id
            FROM orders o
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = o.cliente_id
            WHERE o.data_emissao IS NOT NULL
              AND COALESCE(o.vendor_id, 0) > 0
        )
        SELECT identity_key, vendor_id, mov_date, mov_id
        FROM movs
        WHERE COALESCE(identity_key, '') <> ''
        ORDER BY identity_key ASC, mov_date DESC, mov_id DESC
    """).fetchall()

    out: Dict[str, int] = {}
    for r in rows:
        ident = (r['identity_key'] or '').strip()
        if not ident or ident in out:
            continue
        out[ident] = int(r['vendor_id'] or 0)
    return out


def get_vendor_overdue_identities(conn, vendor_id: int, ref=None):
    ref = ref or date.today()

    rows = conn.execute("""
        SELECT DISTINCT cliente_id
        FROM invoices
        WHERE data_emissao IS NOT NULL
          AND COALESCE(vendor_id, 0) > 0
    """
    ).fetchall()

    identidades = {}
    for r in rows:
        cid = int(r["cliente_id"] or 0)
        if cid <= 0:
            continue
        ident = get_customer_identity_key(conn, cid)
        if not ident or ident == "SEM_IDENTIDADE":
            continue
        if get_last_vendor_for_identity(conn, ident) != int(vendor_id):
            continue
        identidades.setdefault(ident, cid)

    out = []

    for ident, cid in identidades.items():
        info = get_customer_identity_row(conn, cid)
        resumo = summarize_identity_repurchase(conn, ident, ref)

        intervalo = float(resumo["intervalo_mediano"] or 0.0)
        atraso = float(resumo["atraso_atual"] or 0.0)

        if intervalo > 0 and atraso > intervalo:
            nome = info.get("identity_nome") or conn.execute("""
                SELECT COALESCE(fantasia, nome, '—') AS nome
                FROM customers
                WHERE id = ?
                LIMIT 1
            """, (cid,)).fetchone()["nome"]

            out.append({
                "identity_key": ident,
                "cliente_id": cid,
                "nome": nome,
                "intervalo_mediano": intervalo,
                "atraso_atual": atraso,
                "atraso_excedente": atraso - intervalo,
            })

    out.sort(key=lambda x: x["atraso_excedente"], reverse=True)
    return out


# -----------------------------
# Commission rules
# -----------------------------
@dataclass
class CommissionLine:
    cliente: str
    data: str
    valor: float
    base_pct: float
    bonus_pct: float
    comissao: float
    is_novo: bool


def commission_rates(v_total_period: float, is_new_customer: bool, base_pct: float = 0.01) -> Tuple[float, float]:
    bonus = 0.0
    if is_new_customer:
        bonus += 0.01  # +1% NOVO
    # +1% excedente acima de 100k no período
    # aplicado no resumo (não por linha), mas para exibição a gente deixa como "bônus" no resumo
    return base_pct, bonus


def format_brl(v: float) -> str:
    try:
        # formata "1.234,56"
        s = f"{v:,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return f"R$ {v}"


# -----------------------------
# UI template base (Bootstrap 5)
# -----------------------------
_BASE_HTML_PATH = Path(__file__).resolve().parent.parent / "templates" / "base.html"
BASE_HTML = _BASE_HTML_PATH.read_text(encoding="utf-8")



def render_page(title: str, body_html: str, active: str) -> str:
    user = get_current_user()
    ticker_messages = []
    if user:
        try:
            with conectar() as conn:
                ticker_messages = _goal_achievement_ticker(conn, limit=6)
        except Exception:
            ticker_messages = []
    
    nav_groups = [
        {
            "label": "Visão geral", "icon": "bi-grid-1x2",
            "items": [
                {"label": "Dashboard", "endpoint": "dashboard", "icon": "bi-speedometer2"},
                {"label": "Desempenho", "endpoint": "desempenho", "icon": "bi-graph-up"},
                {"label": "A Faturar", "endpoint": "a_faturar", "icon": "bi-receipt-cutoff"},
                {"label": "Análise Histórica", "endpoint": "analise_historica", "icon": "bi-calendar3-range"},
                {"label": "Relatórios", "endpoint": "relatorios", "icon": "bi-file-earmark-spreadsheet"},
            ],
        },
        {
            "label": "Comercial", "icon": "bi-briefcase",
            "items": [
                {"label": "Oportunidades", "endpoint": "leads", "icon": "bi-lightning-charge"},
                {"label": "Funil", "endpoint": "funil", "icon": "bi-kanban"},
                {"label": "Clientes", "endpoint": "clientes", "icon": "bi-buildings"},
                {"label": "Tarefas", "endpoint": "tarefas", "icon": "bi-check2-square"},
                {"label": "Orçamentos", "endpoint": "orcamentos_detalhe", "icon": "bi-file-earmark-text"},
            ],
        },
        {
            "label": "Equipe", "icon": "bi-people",
            "items": [
                {"label": "Vendedores", "endpoint": "vendedores", "icon": "bi-person-lines-fill"},
            ],
        },
        {
            "label": "Inteligência", "icon": "bi-graph-up-arrow",
            "items": [
                {"label": "Mercado", "endpoint": "estudo_mercado", "icon": "bi-graph-up-arrow"},
                {"label": "Diagnóstico", "endpoint": "diagnostico", "icon": "bi-activity"},
                {"label": "IA", "endpoint": "ia_assistente", "icon": "bi-stars"},
                {"label": "Site", "endpoint": "site_analytics", "icon": "bi-globe2"},
                {"label": "Medidas", "endpoint": "medidas", "icon": "bi-rulers"},
            ],
        },
        {
            "label": "Sistema", "icon": "bi-gear",
            "items": [
                {"label": "Atualizar Sisplan", "endpoint": "importar_csv", "icon": "bi-arrow-repeat"},
                {"label": "Importações", "endpoint": "importacoes", "icon": "bi-database-check"},
                {"label": "Usuários", "endpoint": "usuarios", "icon": "bi-shield-lock"},
                {"label": "Metas", "endpoint": "settings", "icon": "bi-bullseye"},
            ],
        },
    ]

    if user and user["role"] == "admin":
        nav_groups[2]["items"].append({"label": "Performance", "endpoint": "performance_time", "icon": "bi-bar-chart-line"})

    if user and user["role"] not in ["admin", "gestor"]:
        nav_groups = [
            {
                "label": "Minha rotina", "icon": "bi-person-workspace",
                "items": [
                    {"label": "Minha Visão", "endpoint": "home", "icon": "bi-speedometer2"},
                    {"label": "Oportunidades", "endpoint": "leads", "icon": "bi-lightning-charge"},
                    {"label": "Funil", "endpoint": "funil", "icon": "bi-kanban"},
                    {"label": "Tarefas", "endpoint": "tarefas", "icon": "bi-check2-square"},
                    {"label": "Clientes", "endpoint": "clientes", "icon": "bi-buildings"},
                ],
            },
            {
                "label": "Ferramentas", "icon": "bi-tools",
                "items": [
                    {"label": "IA", "endpoint": "ia_assistente", "icon": "bi-stars"},
                    {"label": "Medidas", "endpoint": "medidas", "icon": "bi-rulers"},
                ],
            },
        ]

    for group in nav_groups:
        group["active"] = any(item.get("endpoint") == active for item in group.get("items", []))

    return render_template_string(
        BASE_HTML,
        title=title,
        body=body_html,
        app_title=APP_TITLE,
        nav_items=[],
        nav_groups=nav_groups,
        active=active,
        user=user,
        ticker_messages=ticker_messages
    )


# -----------------------------
# SITE helpers
# -----------------------------
SITE_VENDOR_NAMES = {
    "SITE",
    "AMANDA IZABEL BILYK",
    "ANA BEATRIZ ABDU MENEZES",
}

def is_site_vendor_name(nome: str) -> bool:
    clean = norm(nome or "")
    return clean in SITE_VENDOR_NAMES

def force_site_vendor_types(conn: sqlite3.Connection) -> None:
    """Garante que as vendedoras que pertencem ao SITE fiquem marcadas como tipo Site.
    Roda de forma segura na inicialização e não apaga nenhuma outra informação.
    """
    try:
        ensure_vendor_type_column(conn)
        # SITE precisa ficar fora das metas Interno/Externo.
        # Inclui a linha sintética "SITE" e as duas vendedoras do canal SITE.
        for nome in ("SITE", "AMANDA IZABEL BILYK", "ANA BEATRIZ ABDU MENEZES"):
            conn.execute(
                "UPDATE vendors SET tipo='Site' WHERE UPPER(TRIM(COALESCE(nome,''))) = ?",
                (nome,)
            )
        # Interno/Externo fica na tela de Vendedores; nao forcar ANFARMA no boot.
        conn.commit()
    except Exception:
        pass


def get_site_vendor_row(conn: sqlite3.Connection):
    return conn.execute("""
        SELECT *
        FROM vendors
        WHERE UPPER(TRIM(COALESCE(nome, ''))) = 'SITE'
           OR UPPER(TRIM(COALESCE(codigo, ''))) = 'SITE'
           OR UPPER(TRIM(COALESCE(nome, ''))) IN ('AMANDA IZABEL BILYK', 'ANA BEATRIZ ABDU MENEZES')
           OR UPPER(TRIM(COALESCE(tipo, ''))) = 'SITE'
        ORDER BY id
        LIMIT 1
    """).fetchone()


def get_site_vendor_id(conn: sqlite3.Connection) -> Optional[int]:
    row = get_site_vendor_row(conn)
    return int(row["id"]) if row else None


def is_site_vendor_id(conn: sqlite3.Connection, vendor_id: Optional[int]) -> bool:
    if not vendor_id:
        return False
    row = conn.execute("SELECT nome, codigo, tipo FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    if not row:
        return False
    return (str(row["tipo"] or "").strip().upper() == "SITE") or is_site_vendor_name(row["nome"] or row["codigo"] or "")


def sql_site_match(alias: str, vendor_id_field: str = 'vendor_id', vendor_nome_field: Optional[str] = 'vendor_nome_ref', join_alias: Optional[str] = 'v', responsavel_field: Optional[str] = None) -> str:
    parts = [f"(? > 0 AND COALESCE({alias}.{vendor_id_field},0) = ?)"]
    if vendor_nome_field:
        parts.append(f"UPPER(TRIM(COALESCE({alias}.{vendor_nome_field}, ''))) = 'SITE'")
    if responsavel_field:
        parts.append(f"UPPER(TRIM(COALESCE({alias}.{responsavel_field}, ''))) = 'SITE'")
    if join_alias:
        parts.append(f"UPPER(TRIM(COALESCE({join_alias}.nome, ''))) = 'SITE'")
        parts.append(f"UPPER(TRIM(COALESCE({join_alias}.codigo, ''))) = 'SITE'")
    return '(' + ' OR '.join(parts) + ')'


def site_sql_params(site_vendor_id: Optional[int]) -> tuple[int, int]:
    sid = int(site_vendor_id or 0)
    return (sid, sid)

def _norm_jit_text_py(valor: str) -> str:
    """Normalização simples para comparar CONDICAO_ENTREGA/GRUPO sem depender de acento/espaço."""
    txt = (valor or "").strip().upper()
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def texto_indica_cancelamento_py(*valores) -> bool:
    txt = " ".join(str(v or "") for v in valores)
    txt = _norm_jit_text_py(txt)
    if not txt:
        return False
    return any(p in txt for p in ("CANCELADO", "CANCELADA", "CANCELAMENTO", "CLIENTE CANCELOU", "CANC PELO CLIENTE"))


def sql_pedido_cancelado_cond(alias: str = "o") -> str:
    a = alias
    txt = f"UPPER(TRIM(COALESCE({a}.situacao_pedido,'') || ' ' || COALESCE({a}.motivo_pedido,'') || ' ' || COALESCE({a}.desc_motivo_pedido,'')))"
    return f"(COALESCE({a}.pedido_cancelado,0)=1 OR {txt} LIKE '%CANCELADO%' OR {txt} LIKE '%CANCELADA%' OR {txt} LIKE '%CANCELAMENTO%' OR {txt} LIKE '%CLIENTE CANCELOU%')"


def sql_pedido_valido_meta_cond(alias: str = "o") -> str:
    return f"(NOT {sql_pedido_cancelado_cond(alias)} AND NOT {sql_jit_picado_cond(alias)})"


def condicao_bloqueia_comissao_jit(condicao: str, grupo: str = "") -> bool:
    """Regra global de exclusão de JIT picado.

    MANTÉM 98 - JIT, pois é pedido matriz/base.
    EXCLUI:
      - 05 - JIT IMEDIATO
      - 03 - IMEDIATA somente quando GRUPO for CARTUCHOS PERSONALIZADOS ou SACOLAS PERSONALIZADAS
    """
    c = _norm_jit_text_py(condicao)
    g = _norm_jit_text_py(grupo)
    if not c:
        return False
    if c.startswith("05") and "JIT" in c and "IMEDIATO" in c:
        return True
    if c.startswith("03") and "IMEDIATA" in c:
        return True
    return False


def sql_jit_picado_cond(alias: str = "o") -> str:
    """Condição SQL da regra global de JIT picado.

    Não usa a coluna is_jit para excluir, porque em versões antigas ela podia marcar 98 - JIT.
    A regra oficial depende de CONDICAO_ENTREGA + GRUPO_REF do CSV de PEDIDOS.
    """
    a = alias
    cond_ns = f"UPPER(REPLACE(TRIM(COALESCE({a}.condicao_entrega,'')),' ',''))"
    return (
        f"(({cond_ns} LIKE '05%JIT%IMEDIATO%'))"
    )


def sql_excluir_jit_picado(alias: str = "o") -> str:
    """Filtro SQL padrão para excluir somente JIT picado, mantendo 98 - JIT."""
    a = alias
    jit_cond = sql_jit_picado_cond(a)
    return (
        f"AND {sql_pedido_valido_meta_cond(a)} "
        f"AND TRIM(COALESCE({a}.pedido,'')) NOT IN ("
        f"SELECT TRIM(COALESCE(oj.pedido,'')) FROM orders oj "
        f"WHERE TRIM(COALESCE(oj.pedido,'')) <> '' AND (NOT {sql_pedido_valido_meta_cond('oj')})"
        f")"
    )


def sql_pedidos_realizados_excel_cond(alias: str = "o", join_alias: str = "v") -> str:
    """Pedidos realizados = MANUAL_REGRAS_PEDIDOS_SISPLAN.

    98-JIT e CLICHES ja saem na extração. 05-JIT IMEDIATO entra.
    join_alias permanece na assinatura: chamadas antigas passam o alias do vendor.
    """
    a = alias
    _ = join_alias
    cond_ns = f"UPPER(REPLACE(TRIM(COALESCE({a}.condicao_entrega,'')),' ',''))"
    grupo = f"UPPER(COALESCE({a}.grupo_ref,''))"
    return (
        f"TRIM(COALESCE({a}.pedido,'')) <> '' "
        f"AND {cond_ns} NOT LIKE '%98%JIT%' "
        f"AND {grupo} NOT LIKE '%CLICHES%'"
    )


def sql_site_exclusion_cond(join_alias: str = "v") -> str:
    """Exclui SITE quando a tela é de time interno/externo."""
    j = join_alias
    return f"COALESCE(UPPER(TRIM({j}.nome)), '') <> 'SITE' AND COALESCE(UPPER(TRIM({j}.tipo)), '') <> 'SITE'"


def pedidos_oficiais_summary_sql(start_param: str = "?", end_param: str = "?", vendor_filter: str = "", include_site: bool = True) -> str:
    """SQL padrão para somar PEDIDOS igual ao Dashboard/Excel.
    Retorna qtd e valor usando VALOR_ITEM por linha válida.
    """
    site_filter = "" if include_site else " AND " + sql_site_exclusion_cond('v')
    vf = f" AND {vendor_filter}" if vendor_filter else ""
    return f"""
        SELECT
            COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd,
            COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS valor
        FROM orders o
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date({start_param}) AND date({end_param})
          AND TRIM(COALESCE(o.pedido,'')) <> ''
          {site_filter}
          {vf}
          AND {sql_pedidos_realizados_excel_cond('o','v')}
    """


def invoice_is_commission_blocked_as_jit(conn, invoice_row: sqlite3.Row) -> bool:
    """Compatibilidade: identifica JIT picado pela regra de PEDIDOS.

    IMPORTANTE: isto NÃO deve bloquear comissão. A função permanece para telas
    antigas que precisem saber se a origem é JIT picado, mas faturamento e
    comissão continuam contando normalmente.
    """
    pedido = norm((invoice_row["pedido"] if "pedido" in invoice_row.keys() else '') or '')
    cliente_id = int((invoice_row["cliente_id"] if "cliente_id" in invoice_row.keys() else 0) or 0)
    vendor_id = int((invoice_row["vendor_id"] if "vendor_id" in invoice_row.keys() else 0) or 0)

    if pedido:
        rows = conn.execute("""
            SELECT condicao_entrega, grupo_ref, COALESCE(is_jit,0) AS is_jit, cliente_id, vendor_id
            FROM orders
            WHERE TRIM(COALESCE(pedido,'')) = TRIM(COALESCE(?,''))
            ORDER BY
              CASE WHEN COALESCE(cliente_id,0)=? THEN 0 ELSE 1 END,
              CASE WHEN COALESCE(vendor_id,0)=? THEN 0 ELSE 1 END,
              id ASC
        """, (pedido, cliente_id, vendor_id)).fetchall()
        if rows:
            return any(condicao_bloqueia_comissao_jit(r["condicao_entrega"] or '', r["grupo_ref"] or '') for r in rows)

    return False


def invoice_tem_qualquer_jit(conn, invoice_row: sqlite3.Row) -> bool:
    """True se a nota/faturamento veio de qualquer pedido JIT.

    Regra comercial final:
    - JIT faturado entra em faturamento e comissão normalmente;
    - JIT NÃO gera bônus de cliente NOVO (+1%).
    - Separado da regra de meta, que exclui só JIT picado.
    """
    try:
        if "is_jit" in invoice_row.keys() and int(invoice_row["is_jit"] or 0) == 1:
            return True
    except Exception:
        pass

    pedido = norm((invoice_row["pedido"] if "pedido" in invoice_row.keys() else '') or '')
    cliente_id = int((invoice_row["cliente_id"] if "cliente_id" in invoice_row.keys() else 0) or 0)
    vendor_id = int((invoice_row["vendor_id"] if "vendor_id" in invoice_row.keys() else 0) or 0)

    if not pedido:
        return False

    rows = conn.execute("""
        SELECT condicao_entrega, grupo_ref, COALESCE(is_jit,0) AS is_jit, cliente_id, vendor_id
        FROM orders
        WHERE TRIM(COALESCE(pedido,'')) = TRIM(COALESCE(?,''))
        ORDER BY
          CASE WHEN COALESCE(cliente_id,0)=? THEN 0 ELSE 1 END,
          CASE WHEN COALESCE(vendor_id,0)=? THEN 0 ELSE 1 END,
          id ASC
    """, (pedido, cliente_id, vendor_id)).fetchall()

    for r in rows:
        cond = norm(r["condicao_entrega"] or "").upper()
        if "JIT" in cond:
            return True
        try:
            if int(r["is_jit"] or 0) == 1:
                return True
        except Exception:
            pass
    return False


def _info_pedido_comissao(conn, pedido: str, cliente_id: int = 0, vendor_id: int = 0) -> dict:
    """Cabecalho do pedido usado na comissao: motivo Sisplan e condicao de entrega."""
    pedido = (pedido or "").strip()
    if not pedido:
        return {"motivo_pedido": "", "desc_motivo_pedido": "", "condicao_entrega": ""}
    row = conn.execute(
        """
        SELECT
            COALESCE(motivo_pedido,'') AS motivo_pedido,
            COALESCE(desc_motivo_pedido,'') AS desc_motivo_pedido,
            COALESCE(condicao_entrega,'') AS condicao_entrega
        FROM orders
        WHERE TRIM(COALESCE(pedido,'')) = TRIM(?)
        ORDER BY
          CASE WHEN COALESCE(cliente_id,0)=? THEN 0 ELSE 1 END,
          CASE WHEN COALESCE(vendor_id,0)=? THEN 0 ELSE 1 END,
          id ASC
        LIMIT 1
        """,
        (pedido, int(cliente_id or 0), int(vendor_id or 0)),
    ).fetchone()
    if not row:
        return {"motivo_pedido": "", "desc_motivo_pedido": "", "condicao_entrega": ""}
    return {
        "motivo_pedido": row["motivo_pedido"] or "",
        "desc_motivo_pedido": row["desc_motivo_pedido"] or "",
        "condicao_entrega": row["condicao_entrega"] or "",
    }


def invoice_tem_jit_imediato(conn, invoice_row: sqlite3.Row) -> bool:
    """True se a nota veio de pedido 05 - JIT IMEDIATO.

    So este JIT anula o bonus de cliente novo. 98-JIT e outros nao.
    """
    pedido = norm((invoice_row["pedido"] if "pedido" in invoice_row.keys() else "") or "")
    cliente_id = int((invoice_row["cliente_id"] if "cliente_id" in invoice_row.keys() else 0) or 0)
    vendor_id = int((invoice_row["vendor_id"] if "vendor_id" in invoice_row.keys() else 0) or 0)
    info = _info_pedido_comissao(conn, pedido, cliente_id, vendor_id)
    return f_comissao.condicao_jit_imediato(info["condicao_entrega"])


def site_liquidez_total_mes(conn, site_vendor_id: Optional[int], d0: date, d1: date) -> float:
    site_params = site_sql_params(site_vendor_id)
    row = conn.execute(f"""
        SELECT COALESCE(SUM(COALESCE(l.valor_pago, l.valor_liq, 0)), 0) AS total
        FROM liquidez l
        LEFT JOIN vendors v ON v.id = l.vendor_id
        WHERE {sql_site_match('l', vendor_nome_field=None, join_alias='v', responsavel_field='responsavel')}
          AND l.data_pagto IS NOT NULL
          AND date(l.data_pagto) BETWEEN date(?) AND date(?)
    """, site_params + (d0.isoformat(), d1.isoformat())).fetchone()
    total = float((row["total"] if row else 0) or 0)
    if total > 0:
        return total

    row = conn.execute(f"""
        SELECT COALESCE(SUM(COALESCE(l.valor_pago, l.valor_liq, 0)), 0) AS total
        FROM liquidez l
        WHERE l.data_pagto IS NOT NULL
          AND date(l.data_pagto) BETWEEN date(?) AND date(?)
          AND (
              (l.pedido IS NOT NULL AND l.pedido IN (
                  SELECT DISTINCT o.pedido
                  FROM orders o
                  LEFT JOIN vendors v ON v.id = o.vendor_id
                  WHERE {sql_site_match('o', join_alias='v')}
                    AND o.data_emissao IS NOT NULL
                    AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              ))
              OR
              (l.titulo IS NOT NULL AND l.titulo IN (
                  SELECT DISTINCT i.numero
                  FROM invoices i
                  LEFT JOIN vendors v ON v.id = i.vendor_id
                  WHERE {sql_site_match('i', join_alias='v')}
                    AND i.data_emissao IS NOT NULL
                    AND date(i.data_emissao) BETWEEN date(?) AND date(?)
              ))
          )
    """, (d0.isoformat(), d1.isoformat()) + site_params + (d0.isoformat(), d1.isoformat()) + site_params + (d0.isoformat(), d1.isoformat())).fetchone()
    return float((row["total"] if row else 0) or 0)


def score_priority_explanation(score_data: dict) -> str:
    sc = enrich_identity_score_for_ui(score_data or {})
    return (
        f"Score {sc.get('score', 0)} | "
        f"Freq {sc.get('score_frequencia', 0)} | "
        f"Recompra {sc.get('score_recompra', 0)} | "
        f"Valor {sc.get('score_valor', 0)} | "
        f"Prioridade {sc.get('prioridade', 'BAIXA')} · {sc.get('prioridade_motivo', '')}"
    ).strip()


# -----------------------------
# Executive helpers
# -----------------------------
def month_bounds(d: date) -> Tuple[date, date]:
    start = date(d.year, d.month, 1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


def year_bounds(d: date) -> Tuple[date, date]:
    return date(d.year, 1, 1), date(d.year, 12, 31)


def projection_month(current_total: float, days_elapsed: int, total_days: int) -> float:
    """Adaptador: ver crm_npd.metrics.percentuais.projecao_linear."""
    return f_pct.projecao_linear(current_total, days_elapsed, total_days)

def fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def _brazil_map_chart_fallback(data_by_uf: Dict[str, float], title: str = "") -> str:
    """Mapa interno simplificado por UF. Mantém a tela funcionando mesmo sem GeoPandas/internet."""
    uf_pos = {
        "AC": (-73.5, -9.5), "AM": (-64.5, -4.0), "RR": (-61.3, 2.0), "RO": (-63.0, -10.8),
        "PA": (-52.5, -4.5), "AP": (-51.7, 1.2), "TO": (-48.3, -10.3), "MA": (-45.0, -5.0),
        "PI": (-42.8, -7.0), "CE": (-39.5, -5.2), "RN": (-36.8, -5.8), "PB": (-36.2, -7.2),
        "PE": (-37.8, -8.3), "AL": (-36.7, -9.7), "SE": (-37.4, -10.6), "BA": (-41.8, -12.8),
        "MT": (-56.0, -13.5), "MS": (-54.5, -20.5), "GO": (-49.8, -16.0), "DF": (-47.9, -15.8),
        "MG": (-44.5, -18.8), "ES": (-40.3, -19.6), "RJ": (-43.2, -22.4), "SP": (-48.0, -22.2),
        "PR": (-51.0, -24.6), "SC": (-50.5, -27.2), "RS": (-53.2, -30.0),
    }
    fig, ax = plt.subplots(figsize=(10.5, 8.5))
    total = float(sum(data_by_uf.values()) or 0)
    max_val = max([float(v or 0) for v in data_by_uf.values()] or [1])
    for uf, (x, y) in uf_pos.items():
        val = float(data_by_uf.get(uf, 0) or 0)
        pct = (val / total * 100) if total else 0
        size = 80 + (900 * (val / max_val if max_val else 0))
        alpha = 0.25 + (0.65 * (val / max_val if max_val else 0))
        ax.scatter(x, y, s=size, alpha=alpha, edgecolors="#0f172a", linewidths=0.7)
        label = uf if val <= 0 else f"{uf}\n{pct:.1f}%"
        ax.text(x, y, label, ha="center", va="center", fontsize=8.5, fontweight="bold", color="#0f172a")
    ax.set_xlim(-75.5, -33.5)
    ax.set_ylim(-34.0, 5.0)
    ax.invert_yaxis()
    ax.axis("off")
    ax.set_title(title or "Distribuição por UF", fontsize=15, fontweight="bold", pad=12)
    ax.text(0.5, 0.02, "Mapa simplificado interno — usado automaticamente quando o mapa geográfico completo não está disponível.",
            transform=ax.transAxes, ha="center", fontsize=8, color="#64748b")
    plt.tight_layout()
    return fig_to_base64(fig)

def brazil_map_chart(data_by_uf: Dict[str, float], title: str, cmap='YlOrRd') -> str:
    """Mapa regional 100% local.

    A versão anterior tentava baixar um GeoJSON do GitHub a cada primeira
    renderização/cache miss. Em um CRM local isso podia segurar a página de
    Orçamentos por vários segundos quando a internet estava lenta ou bloqueada.
    O mapa interno elimina a dependência de rede e mantém o relatório instantâneo.
    """
    return _brazil_map_chart_fallback(data_by_uf, title)

def _short_money(v: float) -> str:
    v = float(v or 0)
    av = abs(v)
    if av >= 1_000_000:
        return f"R$ {v/1_000_000:.1f} mi".replace(".", ",")
    if av >= 1_000:
        return f"R$ {v/1_000:.1f} mil".replace(".", ",")
    return f"R$ {v:,.0f}".replace(",", ".")


def _chart_clean_axes(ax, grid_axis="y"):
    for spine in ["top", "right", "left", "bottom"]:
        ax.spines[spine].set_visible(False)
    ax.set_axisbelow(True)
    if grid_axis:
        ax.grid(True, axis=grid_axis, color="#dfe7f1", alpha=.58, linestyle="--", linewidth=.7)
    ax.tick_params(axis="both", length=0, labelcolor="#718096", labelsize=8.5)


def bar_chart(labels: List[str], values: List[float], title: str) -> str:
    """Comparação categórica. Automaticamente vira horizontal quando há muitas categorias."""
    labels = [str(x or "-") for x in labels]
    values = [float(v or 0) for v in values]
    if not labels:
        return ""
    horizontal = len(labels) >= 6 or any(len(x) > 12 for x in labels)
    if horizontal:
        pairs = sorted(zip(labels, values), key=lambda x: x[1], reverse=True)[:10]
        labs = [p[0] for p in pairs][::-1]
        vals = [p[1] for p in pairs][::-1]
        fig_h = max(3.8, .48 * len(labs) + .8)
        fig, ax = plt.subplots(figsize=(8.4, fig_h))
        bars = ax.barh(labs, vals, color="#3b82f6", edgecolor="none", height=.62)
        _chart_clean_axes(ax, "x")
        vmax = max(vals) if vals else 1
        ax.set_xlim(0, vmax * 1.28 if vmax > 0 else 1)
        for b, v in zip(bars, vals):
            label = _short_money(v) if max(vals or [0]) >= 1000 else f"{v:,.0f}".replace(",", ".")
            ax.text(v + vmax*.018, b.get_y()+b.get_height()/2, label, va="center", ha="left", fontsize=8.5, fontweight="bold", color="#21364f")
    else:
        fig, ax = plt.subplots(figsize=(8.4, 4.0))
        bars = ax.bar(labels, values, color="#3b82f6", edgecolor="none", width=.58)
        _chart_clean_axes(ax, "y")
        vmax=max(values) if values else 1
        ax.set_ylim(0, vmax*1.18 if vmax>0 else 1)
        for b, v in zip(bars, values):
            label = _short_money(v) if vmax >= 1000 else f"{v:,.0f}".replace(",", ".")
            ax.text(b.get_x()+b.get_width()/2, v+vmax*.025, label, ha="center", va="bottom", fontsize=8.5, fontweight="bold", color="#21364f")
    fig.patch.set_facecolor("#f8fbff")
    ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.7)
    return fig_to_base64(fig)


def rank_chart_horizontal(labels: List[str], values: List[float], money: bool = True) -> str:
    labels = [str(x or "-") for x in labels]
    values = [float(v or 0) for v in values]
    pairs = sorted(zip(labels, values), key=lambda x: x[1], reverse=True)[:8]
    if not pairs:
        return ""
    labs=[p[0] for p in pairs][::-1]; vals=[p[1] for p in pairs][::-1]
    fig_h=max(4.0, .54*len(labs)+.8)
    fig,ax=plt.subplots(figsize=(8.4,fig_h))
    bars=ax.barh(labs,vals,color="#3b82f6",edgecolor="none",height=.62)
    _chart_clean_axes(ax,"x")
    vmax=max(vals) if vals else 1
    ax.set_xlim(0,vmax*1.32 if vmax>0 else 1)
    for bar,val in zip(bars,vals):
        label=_short_money(val) if money else f"{int(val):,}".replace(",", ".")
        ax.text(val+vmax*.018,bar.get_y()+bar.get_height()/2,label,va="center",ha="left",fontsize=8.8,fontweight="bold",color="#20364f")
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.8)
    return fig_to_base64(fig)


def regional_value_distribution_chart(data_by_uf: Dict[str, float]) -> str:
    items=[(str(uf or "").upper().strip(),float(v or 0)) for uf,v in (data_by_uf or {}).items() if str(uf or "").strip() and float(v or 0)>0]
    items.sort(key=lambda x:x[1],reverse=True)
    if not items:return ""
    total=sum(v for _,v in items) or 1
    top=items[:10]
    if len(items)>10:
        outros=sum(v for _,v in items[10:])
        if outros>0: top.append(("DEMAIS",outros))
    labels=[x[0] for x in top][::-1]; values=[x[1] for x in top][::-1]
    fig_h=max(4.3,.46*len(labels)+1)
    fig,ax=plt.subplots(figsize=(8.8,fig_h))
    bars=ax.barh(labels,values,color="#3b82f6",edgecolor="none",height=.6)
    _chart_clean_axes(ax,"x")
    vmax=max(values) if values else 1
    ax.set_xlim(0,vmax*1.38)
    for bar,val in zip(bars,values):
        pct=val/total*100
        ax.text(val+vmax*.02,bar.get_y()+bar.get_height()/2,f"{_short_money(val)}  ·  {pct:.1f}%",va="center",ha="left",fontsize=8.5,fontweight="bold",color="#20364f")
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.8)
    return fig_to_base64(fig)


def regional_conversion_chart(rows: List[dict], title: str) -> str:
    if not rows:return ""
    rows=list(rows)[:6]
    rows=sorted(rows,key=lambda r:float(r.get("taxa") or 0))
    labels=[str(r.get("uf") or "").upper() for r in rows]
    rates=[float(r.get("taxa") or 0) for r in rows]
    fig,ax=plt.subplots(figsize=(6.8,max(2.8,.52*len(rows)+.6)))
    bars=ax.barh(labels,rates,color="#3b82f6",edgecolor="none",height=.56)
    _chart_clean_axes(ax,"x")
    ax.set_xlim(0,105)
    for bar,r,rate in zip(bars,rows,rates):
        conv=int(r.get("convertidos") or 0); total=int(r.get("total") or 0)
        ax.text(min(rate+2,101),bar.get_y()+bar.get_height()/2,f"{rate:.1f}%  ·  {conv}/{total}",va="center",ha="left",fontsize=8.5,fontweight="bold",color="#20364f")
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.7)
    return fig_to_base64(fig)


def campanha_medida_chart(rows: List[sqlite3.Row], total_pedidos: int) -> str:
    if not rows:return ""
    prepared=[]; seen=set(); base=float(total_pedidos or 0)
    for r in rows:
        medida=str((r["medida"] if "medida" in r.keys() else None) or "SEM MEDIDA").strip()
        qtd=float((r["qtd_pedidos"] if "qtd_pedidos" in r.keys() else 0) or 0)
        pct=float(r["pct_pedidos"] or 0) if "pct_pedidos" in r.keys() else ((qtd/base*100) if base else 0)
        if medida in seen:continue
        seen.add(medida); prepared.append({"medida":medida,"qtd":qtd,"pct":pct})
    prepared=prepared[:10]
    labels=[x["medida"][:22] for x in prepared][::-1]; vals=[x["qtd"] for x in prepared][::-1]; pcts=[x["pct"] for x in prepared][::-1]
    fig,ax=plt.subplots(figsize=(9,max(4,.5*len(labels)+.8)))
    bars=ax.barh(labels,vals,color="#3b82f6",edgecolor="none",height=.6)
    _chart_clean_axes(ax,"x")
    vmax=max(vals) if vals else 1
    ax.set_xlim(0,vmax*1.30 if vmax>0 else 1)
    for b,v,pct in zip(bars,vals,pcts):
        ax.text(v+vmax*.02,b.get_y()+b.get_height()/2,f"{int(v)} pedidos · {pct:.1f}%",va="center",ha="left",fontsize=8.5,fontweight="bold",color="#20364f")
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.8)
    return fig_to_base64(fig)


def status_distribution_chart(labels: List[str], values: List[float]) -> str:
    """Distribuição de fases com muitas categorias: barras horizontais são mais legíveis que pizza."""
    pairs=[(str(l or "Sem status"),float(v or 0)) for l,v in zip(labels,values) if float(v or 0)>0]
    pairs=sorted(pairs,key=lambda x:x[1],reverse=True)
    if not pairs:return ""
    total=sum(v for _,v in pairs) or 1
    pairs=pairs[:8]
    labs=[p[0] for p in pairs][::-1]; vals=[p[1] for p in pairs][::-1]
    fig,ax=plt.subplots(figsize=(8.4,max(3.7,.52*len(labs)+.8)))
    bars=ax.barh(labs,vals,color="#3b82f6",edgecolor="none",height=.58)
    _chart_clean_axes(ax,"x")
    vmax=max(vals) if vals else 1
    ax.set_xlim(0,vmax*1.36)
    for b,v in zip(bars,vals):
        pct=v/total*100
        ax.text(v+vmax*.018,b.get_y()+b.get_height()/2,f"{int(v)}  ·  {pct:.1f}%",va="center",ha="left",fontsize=8.7,fontweight="bold",color="#20364f")
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.8)
    return fig_to_base64(fig)


def donut_chart(labels: List[str], values: List[float], title: str) -> str:
    labels=[str(x or "Sem status") for x in labels]
    values=[float(v or 0) for v in values]
    pairs=[(l,v) for l,v in zip(labels,values) if v>0]
    if not pairs:return ""
    # Nunca usar pizza para uma lista extensa de categorias.
    if len(pairs)>6:
        return status_distribution_chart([p[0] for p in pairs],[p[1] for p in pairs])
    labels=[p[0] for p in pairs]; values=[p[1] for p in pairs]
    semantic={
        "PEDIDO APROVADO":"#16a34a","CONVERTIDO EM PEDIDO":"#16a34a","GANHO":"#16a34a",
        "ORCAMENTO EM NEGOCIACAO":"#0ea5e9","EM NEGOCIAÇÃO":"#0ea5e9","NEGOCIAÇÃO":"#0ea5e9",
        "AGUARDANDO APROVAÇÃO":"#8b5cf6","CONTATO / RETORNO":"#f59e0b","NOVO ORÇAMENTO":"#2563eb",
        "CANCELADO PELO CLIENTE":"#ef4444","PERDIDO / CANCELADO":"#ef4444","PERDIDO":"#ef4444",
        "NOVO":"#2563eb","REPETIÇÃO":"#f59e0b","FATURADOS":"#16a34a","PENDENTES":"#f97316"
    }
    fallback=["#3b82f6","#14b8a6","#8b5cf6","#f59e0b","#ef4444","#64748b"]
    colors=[semantic.get(l.upper(),fallback[i%len(fallback)]) for i,l in enumerate(labels)]
    fig,ax=plt.subplots(figsize=(6.6,4.4))
    def pct_fmt(p): return f"{p:.1f}%" if p>=4 else ""
    wedges,_,autos=ax.pie(values,startangle=90,colors=colors,wedgeprops=dict(width=.28,edgecolor="#f8fbff",linewidth=2),autopct=pct_fmt,pctdistance=.84,textprops=dict(fontsize=8,fontweight="bold",color="#41566f"))
    total=sum(values)
    ax.text(0,.06,f"{int(total)}",ha="center",va="center",fontsize=29,fontweight="900",color="#10233d")
    center_label="Orçamentos" if "fase" in title.lower() or "orça" in title.lower() else "Pedidos"
    ax.text(0,-.14,center_label,ha="center",va="center",fontsize=9,color="#718096",fontweight="bold")
    ax.legend(wedges,labels,loc="lower center",bbox_to_anchor=(.5,-.12),ncol=2,frameon=False,fontsize=7.8,handlelength=1.5,columnspacing=1.2)
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.7)
    return fig_to_base64(fig)


def line_chart(labels: List[str], series: List[Tuple[str, List[float]]], title: str) -> str:
    fig,ax=plt.subplots(figsize=(9.4,4.15))
    colors=["#2563eb","#10b981","#8b5cf6","#f59e0b"]
    x=list(range(len(labels)))
    for i,(name,values) in enumerate(series):
        n=min(len(labels),len(values)); vals=[float(v or 0) for v in values[:n]]; xx=x[:n]; c=colors[i%len(colors)]
        ax.plot(xx,vals,label=name,color=c,linewidth=2.6,solid_capstyle="round")
        ax.fill_between(xx,vals,0,color=c,alpha=.055)
        if n<=14:
            ax.scatter(xx,vals,s=18,color="#ffffff",edgecolor=c,linewidth=1.5,zorder=3)
    _chart_clean_axes(ax,"y")
    step=max(1,len(labels)//12)
    ticks=x[::step]; ax.set_xticks(ticks); ax.set_xticklabels(labels[::step],fontsize=8)
    ax.legend(loc="upper left",ncol=min(3,len(series)),frameon=False,fontsize=8.5)
    ax.margins(x=.01)
    fig.patch.set_facecolor("#f8fbff"); ax.set_facecolor("#f8fbff")
    plt.tight_layout(pad=.8)
    return fig_to_base64(fig)


def desempenho_evolucao_chart(
    labels: List[str],
    series_ref: Tuple[str, List[float]],
    series_cmp: Tuple[str, List[float]],
) -> str:
    """Linha mensal: referência (cinza) vs comparação (teal da bolinha)."""
    import math
    fig, ax = plt.subplots(figsize=(8.6, 3.9))
    x = list(range(len(labels)))

    def _plot(name: str, values: List[float], color: str, z: int):
        vals = []
        for v in values[: len(labels)]:
            try:
                fv = float(v)
            except Exception:
                fv = float("nan")
            vals.append(fv if not (isinstance(fv, float) and math.isnan(fv)) else float("nan"))
        while len(vals) < len(labels):
            vals.append(float("nan"))
        ax.plot(x, vals, label=name, color=color, linewidth=2.4, solid_capstyle="round", zorder=z)
        xs = [i for i, v in enumerate(vals) if not math.isnan(v)]
        ys = [vals[i] for i in xs]
        if xs:
            ax.scatter(xs, ys, s=42, color="#ffffff", edgecolor=color, linewidth=1.8, zorder=z + 1)

    _plot(series_ref[0], series_ref[1], "#94a3b8", 2)
    _plot(series_cmp[0], series_cmp[1], "#0e7490", 3)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8.2, color="#8a98a9")
    ax.tick_params(axis="y", labelsize=8, colors="#8a98a9")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: _short_money(v) if v else "0"))
    ax.grid(axis="y", linestyle="--", linewidth=0.8, color="#d7e1ec", alpha=0.9)
    ax.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#e2e8f0")
    ax.legend(loc="upper right", frameon=False, fontsize=8.2, labelcolor="#334155")
    ax.margins(x=0.02)
    ymax = 0.0
    for vals in (series_ref[1], series_cmp[1]):
        for v in vals:
            try:
                fv = float(v)
            except Exception:
                continue
            if not math.isnan(fv):
                ymax = max(ymax, fv)
    ax.set_ylim(0, ymax * 1.18 if ymax > 0 else 1)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    plt.tight_layout(pad=0.7)
    return fig_to_base64(fig)


# -----------------------------
# Upserts
# -----------------------------
def upsert_vendor(conn: sqlite3.Connection, raw_vendor: str, cache: Optional[dict] = None) -> Optional[int]:
    from crm_npd.sync.etl import carteira_do_responsavel
    raw_vendor = carteira_do_responsavel(raw_vendor) or raw_vendor
    raw_vendor = norm(raw_vendor)
    if not raw_vendor:
        return None
    codigo, nome = split_codigo_nome(raw_vendor)
    nome = nome or raw_vendor
    codigo_key = (codigo or "").strip()

    if cache is not None:
        vid = cache.get("vendors_by_code_name", {}).get((codigo_key, nome))
        if vid:
            return int(vid)
        if codigo_key:
            vid = cache.get("vendors_by_code", {}).get(codigo_key)
            if vid:
                return int(vid)
        vid = cache.get("vendors_by_name", {}).get(nome)
        if vid:
            return int(vid)
        alt_nome = nome.lstrip(". ").strip()
        if alt_nome and alt_nome != nome:
            vid = cache.get("vendors_by_name", {}).get(alt_nome)
            if vid:
                return int(vid)

    row = conn.execute("SELECT id FROM vendors WHERE codigo IS ? AND nome=?", (codigo, nome)).fetchone()
    if row:
        vid = int(row["id"])
    elif codigo_key:
        row = conn.execute(
            "SELECT id FROM vendors WHERE TRIM(COALESCE(codigo,''))=?",
            (codigo_key,),
        ).fetchone()
        vid = int(row["id"]) if row else None
    else:
        vid = None
    if not vid:
        nomes = [nome]
        alt = nome.lstrip(". ").strip()
        if alt and alt not in nomes:
            nomes.append(alt)
        row = None
        for candidato in nomes:
            row = conn.execute("SELECT id FROM vendors WHERE nome=?", (candidato,)).fetchone()
            if row:
                break
        if row:
            vid = int(row["id"])
        else:
            conn.execute("INSERT INTO vendors(codigo,nome,ativo,created_at) VALUES(?,?,?,?)", (codigo, nome, 1, now_iso()))
            vid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    if codigo_key:
        atual = conn.execute("SELECT codigo FROM vendors WHERE id=?", (vid,)).fetchone()
        if atual and not str(atual["codigo"] or "").strip():
            conn.execute("UPDATE vendors SET codigo=? WHERE id=?", (codigo_key, vid))

    if cache is not None:
        cache.setdefault("vendors_by_code_name", {})[(codigo_key, nome)] = vid
        cache.setdefault("vendors_by_name", {})[nome] = vid
        if codigo_key:
            cache.setdefault("vendors_by_code", {})[codigo_key] = vid
    return vid


GENERIC_CUSTOMER_LABELS = {
    "", "CLIENTE", "CLIENT", "CLIENTE FINAL", "CONSUMIDOR", "CONSUMIDOR FINAL",
    "NAO INFORMADO", "NÃO INFORMADO", "SEM NOME", "SEM CADASTRO", "NAO CADASTRADO",
    "NÃO CADASTRADO", "N/A", "NA", "-", ".", "0"
}


def _customer_name_key(value: Any) -> str:
    txt = norm(str(value or "")).strip().upper()
    txt = "".join(ch for ch in unicodedata.normalize("NFKD", txt) if not unicodedata.combining(ch))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


_GENERIC_CUSTOMER_LABEL_KEYS = frozenset(_customer_name_key(x) for x in GENERIC_CUSTOMER_LABELS)

def is_generic_customer_name(value: Any) -> bool:
    """True quando o Sisplan trouxe apenas um rótulo genérico, e não o nome do cliente."""
    key = _customer_name_key(value)
    if key in _GENERIC_CUSTOMER_LABEL_KEYS:
        return True
    if re.fullmatch(r"CLIENTE\s*#?\s*\d*", key):
        return True
    if re.fullmatch(r"CLIENTE\s+PEDIDO\s+\d+", key):
        return True
    if key in {"CLIENTE NAO IDENTIFICADO", "CLIENTE NÃO IDENTIFICADO"}:
        return True
    if re.fullmatch(r"CONSUMIDOR\s*#?\s*\d*", key):
        return True
    return False


def _clean_customer_candidate(value: Any) -> str:
    txt = norm(str(value or "")).strip()
    if not txt:
        return ""
    codigo, nome = split_codigo_nome(txt)
    if codigo and nome:
        txt = nome.strip()
    txt = re.sub(r"\s+", " ", txt).strip(" -–;|")
    if not txt or is_generic_customer_name(txt) or txt.isdigit():
        return ""
    return txt


def _extract_customer_code(value: Any) -> str:
    txt = norm(str(value or "")).strip()
    if not txt:
        return ""
    codigo, _ = split_codigo_nome(txt)
    if codigo:
        return codigo.strip()
    # campo dedicado de código costuma vir somente numérico
    if re.fullmatch(r"\d{2,}", txt):
        return txt
    return ""


def safe_customer_display_name(nome: Any = "", fantasia: Any = "", codigo: Any = "", customer_id: Any = 0) -> str:
    """Nome de exibição que nunca degrada para o rótulo puro ``Cliente``."""
    for value in (fantasia, nome):
        clean = _clean_customer_candidate(value)
        if clean:
            return clean
    code = norm(str(codigo or "")).strip()
    if code:
        return f"Cliente {code}"
    try:
        cid = int(customer_id or 0)
    except Exception:
        cid = 0
    return f"Cliente #{cid}" if cid else "Cliente não identificado"


def resolve_customer_name_for_import(conn, customer_code: str = "", pedido: str = "", candidates: Optional[Iterable[Any]] = None) -> str:
    """Resolve o nome real do cliente antes do upsert.

    Ordem de confiança:
      1) campos do CSV que contenham um nome não genérico;
      2) cadastro já conhecido pelo mesmo código Sisplan;
      3) liquidez pelo código;
      4) outro relatório ligado ao mesmo pedido.

    Nunca devolve somente ``Cliente``. Quando o Sisplan não fornece nenhum nome
    recuperável, usa ``Cliente <codigo>`` para não colapsar clientes diferentes.
    """
    customer_code = norm(customer_code or "").strip()
    pedido = norm(pedido or "").strip()

    # 1) O próprio arquivo é a fonte principal, desde que não seja rótulo genérico.
    for value in (candidates or []):
        clean = _clean_customer_candidate(value)
        if clean:
            return clean

    # 2) Cadastro existente com o mesmo código.
    if customer_code:
        row = conn.execute("""
            SELECT fantasia, nome
            FROM customers
            WHERE TRIM(COALESCE(codigo,'')) = TRIM(?)
            ORDER BY id DESC
            LIMIT 1
        """, (customer_code,)).fetchone()
        if row:
            for value in (row["fantasia"], row["nome"]):
                clean = _clean_customer_candidate(value)
                if clean:
                    return clean

        # 3) Liquidez é muito útil porque normalmente carrega código + razão/fantasia.
        try:
            rows = conn.execute("""
                SELECT cliente_nome
                FROM liquidez
                WHERE TRIM(COALESCE(cliente_codigo,'')) = TRIM(?)
                  AND TRIM(COALESCE(cliente_nome,'')) <> ''
                ORDER BY id DESC
                LIMIT 25
            """, (customer_code,)).fetchall()
            for row in rows:
                clean = _clean_customer_candidate(row["cliente_nome"])
                if clean:
                    return clean
        except Exception:
            pass

    # 4) Cruza PEDIDOS/FATURAMENTO/LIQUIDEZ pelo número do pedido.
    if pedido:
        try:
            rows = conn.execute("""
                SELECT nome FROM (
                    SELECT cliente_nome_ref AS nome, id AS ord FROM orders
                    WHERE TRIM(COALESCE(pedido,'')) = TRIM(?)
                    UNION ALL
                    SELECT cliente_nome_ref AS nome, id + 100000000 AS ord FROM invoices
                    WHERE TRIM(COALESCE(pedido,'')) = TRIM(?)
                    UNION ALL
                    SELECT cliente_nome AS nome, id + 200000000 AS ord FROM liquidez
                    WHERE TRIM(COALESCE(pedido,'')) = TRIM(?)
                    UNION ALL
                    SELECT cliente_nome AS nome, id + 300000000 AS ord FROM orcamentos
                    WHERE TRIM(COALESCE(pedido_id,'')) = TRIM(?)
                ) x
                WHERE TRIM(COALESCE(nome,'')) <> ''
                ORDER BY ord DESC
                LIMIT 50
            """, (pedido, pedido, pedido, pedido)).fetchall()
            for row in rows:
                clean = _clean_customer_candidate(row["nome"])
                if clean:
                    return clean
        except Exception:
            pass

    if customer_code:
        return f"Cliente {customer_code}"
    if pedido:
        return f"Cliente pedido {pedido}"
    return "Cliente não identificado"


def build_customer_upsert_value(customer_code: str, customer_name: str) -> str:
    customer_code = norm(customer_code or "").strip()
    customer_name = norm(customer_name or "").strip()
    if customer_code and customer_name:
        return f"{customer_code} - {customer_name}"
    return customer_name or customer_code


def upsert_customer(conn, raw_cliente, fantasia=None, codigo_hint: str = "", cache: Optional[dict] = None):
    """Upsert de cliente resistente a rótulos genéricos do Sisplan.

    Quando ``cache`` é fornecido, a regra é idêntica à versão tradicional, mas
    clientes já vistos no mesmo pacote são resolvidos em memória.
    """
    raw_cliente = norm(raw_cliente)
    fantasia = norm(fantasia or "")
    codigo_hint = norm(codigo_hint or "").strip()

    if not raw_cliente and not codigo_hint:
        return None

    codigo_raw, nome_raw = split_codigo_nome(raw_cliente)
    codigo = (codigo_hint or codigo_raw or "").strip()
    nome = _clean_customer_candidate(nome_raw or raw_cliente)
    fantasia_clean = _clean_customer_candidate(fantasia)

    cached_row = None
    if cache is not None and codigo:
        cached_row = cache.get("customers_by_code", {}).get(codigo)

    if codigo and not nome:
        row = cached_row
        if row is None:
            rr = conn.execute("""
                SELECT id, nome, fantasia, COALESCE(codigo,'') codigo
                FROM customers
                WHERE TRIM(COALESCE(codigo,'')) = TRIM(?)
                ORDER BY id DESC
                LIMIT 1
            """, (codigo,)).fetchone()
            row = dict(rr) if rr else None
        if row:
            known = _clean_customer_candidate(row.get("fantasia")) or _clean_customer_candidate(row.get("nome"))
            if known:
                nome = known

    if not nome:
        nome = f"Cliente {codigo}" if codigo else "Cliente não identificado"
    if not fantasia_clean:
        fantasia_clean = nome

    # 1) Código Sisplan é a chave principal.
    row = cached_row
    if row is None and codigo:
        rr = conn.execute("""
            SELECT id, nome, fantasia, codigo
            FROM customers
            WHERE TRIM(COALESCE(codigo,'')) = TRIM(?)
            ORDER BY id
            LIMIT 1
        """, (codigo,)).fetchone()
        row = dict(rr) if rr else None

    if row:
        cid = int(row["id"])
        current_nome = (row.get("nome") or "").strip()
        current_fantasia = (row.get("fantasia") or "").strip()
        novo_nome = nome if (is_generic_customer_name(current_nome) or current_nome.lower().startswith("cliente não identificado") or current_nome.lower().startswith("cliente pedido ")) and not is_generic_customer_name(nome) else current_nome
        nova_fantasia = fantasia_clean if (not current_fantasia or is_generic_customer_name(current_fantasia) or current_fantasia.lower().startswith("cliente não identificado") or current_fantasia.lower().startswith("cliente pedido ")) and not is_generic_customer_name(fantasia_clean) else current_fantasia
        novo_nome = novo_nome or nome
        nova_fantasia = nova_fantasia or fantasia_clean
        # Evita UPDATE redundante em todas as linhas do mesmo cliente.
        if novo_nome != current_nome or nova_fantasia != current_fantasia:
            conn.execute("UPDATE customers SET nome=?, fantasia=? WHERE id=?", (novo_nome, nova_fantasia, cid))
        item = {"id": cid, "codigo": codigo or (row.get("codigo") or ""), "nome": novo_nome, "fantasia": nova_fantasia}
        if cache is not None:
            if item["codigo"]:
                cache.setdefault("customers_by_code", {})[str(item["codigo"]).strip()] = item
            for value in (novo_nome, nova_fantasia):
                clean = _clean_customer_candidate(value)
                if clean:
                    cache.setdefault("customers_by_name", {})[_customer_name_key(clean)] = item
        return cid

    # 2) Nome exato somente quando é nome real.
    if not is_generic_customer_name(nome) and not nome.lower().startswith("cliente não identificado") and not nome.lower().startswith("cliente pedido "):
        name_key = _customer_name_key(nome)
        row = cache.get("customers_by_name", {}).get(name_key) if cache is not None else None
        if row is None:
            rr = conn.execute("""
                SELECT id, nome, codigo, fantasia
                FROM customers
                WHERE UPPER(TRIM(COALESCE(nome,''))) = UPPER(TRIM(?))
                   OR UPPER(TRIM(COALESCE(fantasia,''))) = UPPER(TRIM(?))
                ORDER BY CASE WHEN TRIM(COALESCE(codigo,'')) <> '' THEN 0 ELSE 1 END, id
                LIMIT 1
            """, (nome, nome)).fetchone()
            row = dict(rr) if rr else None
        if row:
            cid = int(row["id"])
            current_code = (row.get("codigo") or "").strip()
            current_nome = (row.get("nome") or "").strip()
            current_fantasia = (row.get("fantasia") or "").strip()
            new_code = current_code or codigo
            new_nome = nome if (not current_nome or is_generic_customer_name(current_nome)) else current_nome
            new_fantasia = fantasia_clean if (not current_fantasia or is_generic_customer_name(current_fantasia)) else current_fantasia
            if (new_code, new_nome, new_fantasia) != (current_code, current_nome, current_fantasia):
                conn.execute("UPDATE customers SET codigo=?, nome=?, fantasia=? WHERE id=?", (new_code or None, new_nome, new_fantasia, cid))
            item = {"id": cid, "codigo": new_code, "nome": new_nome, "fantasia": new_fantasia}
            if cache is not None:
                if new_code:
                    cache.setdefault("customers_by_code", {})[new_code] = item
                for value in (new_nome, new_fantasia):
                    clean = _clean_customer_candidate(value)
                    if clean:
                        cache.setdefault("customers_by_name", {})[_customer_name_key(clean)] = item
            return cid

    # 3) Novo cadastro.
    conn.execute("INSERT INTO customers(codigo, nome, fantasia, created_at) VALUES(?, ?, ?, ?)", (codigo or None, nome, fantasia_clean or None, now_iso()))
    cid = int(conn.execute("SELECT last_insert_rowid() id").fetchone()["id"])
    item = {"id": cid, "codigo": codigo, "nome": nome, "fantasia": fantasia_clean}
    if cache is not None:
        if codigo:
            cache.setdefault("customers_by_code", {})[codigo] = item
        for value in (nome, fantasia_clean):
            clean = _clean_customer_candidate(value)
            if clean:
                cache.setdefault("customers_by_name", {})[_customer_name_key(clean)] = item
    return cid


def repair_generic_customer_names(conn) -> Dict[str, int]:
    """Repara nomes genéricos já existentes sem alterar histórico financeiro.

    O reparo cruza os quatro relatórios Sisplan pelo customer_id, código e pedido.
    Registros que continuam sem nome real recebem identificador com código/pedido,
    nunca o rótulo puro ``Cliente``.
    """
    stats = {"resolved": 0, "identified": 0, "unresolved": 0, "identity_rules": 0, "refs": 0}
    rows = conn.execute("SELECT id, COALESCE(codigo,'') codigo, COALESCE(nome,'') nome, COALESCE(fantasia,'') fantasia FROM customers ORDER BY id").fetchall()

    for c in rows:
        cid = int(c["id"])
        current_good = _clean_customer_candidate(c["fantasia"]) or _clean_customer_candidate(c["nome"])
        current_is_bad = not bool(current_good)
        # Também corrige placeholders antigos 'Cliente 123', se houver uma fonte melhor.
        placeholder = _customer_name_key(c["fantasia"] or c["nome"]).startswith("CLIENTE ")
        if not current_is_bad and not placeholder:
            continue

        code = (c["codigo"] or "").strip()
        candidates: List[str] = []

        for table, idcol, namecol in (
            ("orders", "cliente_id", "cliente_nome_ref"),
            ("invoices", "cliente_id", "cliente_nome_ref"),
            ("liquidez", "customer_id", "cliente_nome"),
        ):
            try:
                src = conn.execute(f"SELECT {namecol} AS nome FROM {table} WHERE {idcol}=? AND TRIM(COALESCE({namecol},''))<>'' ORDER BY id DESC LIMIT 50", (cid,)).fetchall()
                candidates.extend((r["nome"] or "") for r in src)
            except Exception:
                pass

        if not code:
            try:
                r = conn.execute("SELECT cliente_codigo FROM liquidez WHERE customer_id=? AND TRIM(COALESCE(cliente_codigo,''))<>'' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                if r:
                    code = (r["cliente_codigo"] or "").strip()
            except Exception:
                pass

        # Pedidos ligados a este customer_id podem apontar para um nome bom em outro relatório.
        try:
            ped_rows = conn.execute("""
                SELECT DISTINCT TRIM(pedido) AS pedido
                FROM (
                    SELECT pedido FROM orders WHERE cliente_id=?
                    UNION ALL SELECT pedido FROM invoices WHERE cliente_id=?
                    UNION ALL SELECT pedido FROM liquidez WHERE customer_id=?
                )
                WHERE TRIM(COALESCE(pedido,''))<>''
                LIMIT 100
            """, (cid, cid, cid)).fetchall()
            for pr in ped_rows:
                pname = resolve_customer_name_for_import(conn, customer_code=code, pedido=pr["pedido"], candidates=candidates)
                if _clean_customer_candidate(pname):
                    candidates.insert(0, pname)
                    break
        except Exception:
            pass

        resolved = resolve_customer_name_for_import(conn, customer_code=code, candidates=candidates)
        real = _clean_customer_candidate(resolved)
        if real:
            display = real
            stats["resolved"] += 1
        else:
            display = f"Cliente {code}" if code else f"Cliente #{cid}"
            stats["unresolved"] += 1

        # Se resolveu um código que ainda não estava no cadastro, preserva-o.
        conn.execute("""
            UPDATE customers
               SET codigo = CASE WHEN TRIM(COALESCE(codigo,''))='' AND TRIM(?)<>'' THEN ? ELSE codigo END,
                   nome = ?,
                   fantasia = CASE WHEN TRIM(COALESCE(fantasia,''))='' OR UPPER(TRIM(COALESCE(fantasia,'')))='CLIENTE' OR UPPER(TRIM(COALESCE(fantasia,''))) LIKE 'CLIENTE #%'
                                   THEN ? ELSE fantasia END
             WHERE id=?
        """, (code, code, display, display, cid))
        stats["identified"] += 1

        for table, idcol, namecol in (
            ("orders", "cliente_id", "cliente_nome_ref"),
            ("invoices", "cliente_id", "cliente_nome_ref"),
            ("liquidez", "customer_id", "cliente_nome"),
        ):
            try:
                cur = conn.execute(f"""
                    UPDATE {table}
                       SET {namecol}=?
                     WHERE {idcol}=?
                       AND (TRIM(COALESCE({namecol},''))='' OR UPPER(TRIM(COALESCE({namecol},'')))='CLIENTE')
                """, (display, cid))
                stats["refs"] += int(cur.rowcount or 0)
            except Exception:
                pass

        try:
            cur = conn.execute("""
                UPDATE customer_identity_rules
                   SET identity_nome=?, updated_at=CURRENT_TIMESTAMP
                 WHERE customer_id=?
                   AND (TRIM(COALESCE(identity_nome,''))='' OR UPPER(TRIM(COALESCE(identity_nome,'')))='CLIENTE' OR UPPER(TRIM(COALESCE(identity_nome,''))) LIKE 'CLIENTE #%')
            """, (display, cid))
            stats["identity_rules"] += int(cur.rowcount or 0)
        except Exception:
            pass

        try:
            conn.execute("UPDATE tasks SET cliente_nome_cache=? WHERE cliente_id=? AND (TRIM(COALESCE(cliente_nome_cache,''))='' OR UPPER(TRIM(COALESCE(cliente_nome_cache,'')))='CLIENTE')", (display, cid))
        except Exception:
            pass

    return stats


# -----------------------------
# Importers
# -----------------------------
def map_row(row: Dict[str, str]) -> Dict[str, str]:
    return {normalize_header(k): (v or "") for k, v in row.items()}


def compile_row_mapper(headers: Iterable[str]):
    """Normaliza os cabeçalhos uma única vez por CSV.

    Antes cada linha refazia regex/acentuação de todos os nomes de coluna. Em
    relatórios Sisplan com dezenas de milhares de linhas isso significava
    milhões de normalizações repetidas. Mantém exatamente as mesmas chaves de
    ``map_row``, mas reduz muito o custo da importação.
    """
    key_map = {(h or ""): normalize_header(h or "") for h in (headers or [])}

    def _mapper(row: Dict[str, str]) -> Dict[str, str]:
        out = {}
        for k, v in row.items():
            raw_key = (k or "")
            nk = key_map.get(raw_key)
            if nk is None:
                nk = normalize_header(raw_key)
                key_map[raw_key] = nk
            out[nk] = (v or "")
        return out

    return _mapper


def build_import_entity_cache(conn: sqlite3.Connection) -> dict:
    """Cache local de clientes/vendedores usado somente durante uma importação.

    Não altera a regra de identidade; apenas evita milhares de SELECTs idênticos
    para o mesmo cliente e vendedor dentro do mesmo pacote.
    """
    cache = {
        "customers_by_code": {},
        "customers_by_name": {},
        "vendors_by_name": {},
        "vendors_by_code": {},
        "vendors_by_code_name": {},
    }
    for r in conn.execute("SELECT id, COALESCE(codigo,'') codigo, COALESCE(nome,'') nome, COALESCE(fantasia,'') fantasia FROM customers ORDER BY id").fetchall():
        item = {"id": int(r["id"]), "codigo": (r["codigo"] or "").strip(), "nome": (r["nome"] or "").strip(), "fantasia": (r["fantasia"] or "").strip()}
        if item["codigo"]:
            cache["customers_by_code"].setdefault(item["codigo"], item)
        for value in (item["nome"], item["fantasia"]):
            clean = _clean_customer_candidate(value)
            if clean:
                cache["customers_by_name"].setdefault(_customer_name_key(clean), item)

    for r in conn.execute("SELECT id, COALESCE(codigo,'') codigo, COALESCE(nome,'') nome FROM vendors ORDER BY id").fetchall():
        item = {"id": int(r["id"]), "codigo": (r["codigo"] or "").strip(), "nome": (r["nome"] or "").strip()}
        if item["nome"]:
            cache["vendors_by_name"].setdefault(item["nome"], item["id"])
        if item["codigo"]:
            cache["vendors_by_code"].setdefault(item["codigo"], item["id"])
        cache["vendors_by_code_name"].setdefault((item["codigo"], item["nome"]), item["id"])
    return cache

def first_not_empty(*values) -> str:
    for v in values:
        if v is None:
            continue
        txt = str(v).strip()
        if txt:
            return txt
    return ""


def normalize_key_text(s: Any) -> str:
    txt = norm(str(s or "")).lower()
    txt = (txt.replace("ç", "c")
              .replace("ã", "a").replace("á", "a").replace("à", "a").replace("â", "a")
              .replace("é", "e").replace("ê", "e")
              .replace("í", "i")
              .replace("ó", "o").replace("ô", "o")
              .replace("ú", "u"))
    txt = re.sub(r"[^a-z0-9]+", "", txt)
    return txt


def normalize_measure_text(s: Any) -> str:
    txt = norm(str(s or "")).lower()
    if not txt:
        return ""

    txt = txt.replace("×", "x").replace("*", "x").replace("/", "x")
    txt = txt.replace(" cm", "").replace("cm", "")
    txt = txt.replace(",", ".")
    txt = txt.replace(" ", "")
    txt = re.sub(r"[^0-9.x]", "", txt)
    txt = re.sub(r"x+", "x", txt)
    txt = txt.strip("x.")
    return txt


def money_key(v: Any) -> str:
    try:
        return f"{float(v or 0):.2f}"
    except Exception:
        return "0.00"




def dedupe_row_signature(rr: Dict[str, Any], campos_preferenciais: Optional[List[str]] = None) -> str:
    """Assinatura estável para reforçar dedup quando o CSV não traz um ID claro."""
    try:
        if campos_preferenciais:
            pares = []
            for k in campos_preferenciais:
                v = rr.get(k)
                if v not in (None, ""):
                    pares.append(f"{normalize_key_text(k)}={normalize_key_text(v)}")
        else:
            pares = [
                f"{normalize_key_text(k)}={normalize_key_text(v)}"
                for k, v in sorted((rr or {}).items(), key=lambda item: str(item[0]))
                if v not in (None, "")
            ]
        base = "|".join(pares).strip()
        if not base:
            return ""
        return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:20]
    except Exception:
        return ""

def build_dedupe_key(*args, **kwargs) -> str:
    """Monta chave de deduplicação de forma tolerante.

    Aceita chamadas posicionais ou nomeadas e ignora campos extras vazios.
    Isso evita erros do tipo "multiple values for argument" em importações.
    """
    campos = [
        "tipo",
        "medida_ref",
        "data_ref",
        "valor_ref",
        "documento_ref",
        "cliente_ref",
        "responsavel_ref",
    ]
    values = {}
    extras = []

    for idx, arg in enumerate(args):
        if idx < len(campos):
            values[campos[idx]] = arg
        else:
            extras.append(arg)

    for nome in campos:
        if nome in kwargs and nome not in values:
            values[nome] = kwargs.pop(nome)

    # se vier repetido por nome e posição, mantém o posicional
    for nome in list(kwargs.keys()):
        if nome in campos:
            kwargs.pop(nome, None)

    extra_refs = kwargs.pop("extra_refs", None)
    if extra_refs:
        if isinstance(extra_refs, (list, tuple, set)):
            extras.extend(list(extra_refs))
        else:
            extras.append(extra_refs)

    extras.extend(kwargs.values())

    parts = [
        normalize_key_text(values.get("tipo")),
        normalize_measure_text(values.get("medida_ref")),
        norm(str(values.get("data_ref") or "")).strip(),
        money_key(values.get("valor_ref")),
        normalize_key_text(values.get("documento_ref")),
        normalize_key_text(values.get("cliente_ref")),
        normalize_key_text(values.get("responsavel_ref")),
    ]
    for extra in extras:
        parts.append(normalize_key_text(extra))
    return "|".join(parts)

# -----------------------------
# JIT / REDE / FILIAL helpers
# -----------------------------

# Preencha aqui os casos conhecidos da sua empresa.
# A chave pode ser a fantasia/filial como aparece no CSV
# e o valor deve ser o nome da rede-mãe que o financeiro considera.
REDE_ALIASES_MANUAIS = {
    "GUACAMOLE NITEROI": "GUACAMOLE",
    "GUACAMOLE RJ JD BOTANICO": "GUACAMOLE",
    "GUACAMOLE BALNEARIO CAMBORIU": "GUACAMOLE",
    "GUACAMOLE NORDESTE": "GUACAMOLE",
    "NACHO MAN": "NACHO MAN",
    "SUSHIAKI CURITIBA": "SUSHIAKI",
    "GUAQUITO SACO GRANDE": "GUAQUITO",
}

PALAVRAS_LIXO_REDE = {
    "LTDA", "EIRELI", "ME", "MEI", "S", "A", "SA", "EPP", "EIRELI", "MATRIZ",
    "FILIAL", "UNIDADE", "LOJA", "SHOPPING", "SEDE", "FRANQUIA", "RESTAURANTE",
    "LANCHONETE", "PADARIA", "CAFETERIA", "BAR", "BISTRO", "BURGER"
}

SIGLAS_CURTAS_REDE = {
    "SC", "PR", "RS", "SP", "RJ", "MG", "DF", "GO", "BA", "ES", "MT", "MS",
    "PE", "CE", "PB", "RN", "AL", "SE", "PI", "MA", "PA", "AM", "RO", "RR",
    "AP", "TO", "AC"
}

def limpar_nome_para_rede(txt: str) -> str:
    txt = norm(txt or "").upper().strip()
    if not txt:
        return ""

    # se vier "00001 - CLIENTE XYZ", tira o código
    _, nome_sem_codigo = split_codigo_nome(txt)
    txt = nome_sem_codigo or txt

    txt = txt.replace("/", " ").replace("-", " ")
    txt = re.sub(r"[^\w\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    tokens = [t for t in txt.split() if t]
    tokens = [t for t in tokens if t not in PALAVRAS_LIXO_REDE]

    return " ".join(tokens).strip()


def eh_jit(condicao_entrega: str) -> bool:
    txt = norm(condicao_entrega or "").upper()
    return "JIT" in txt


def eh_jit_matriz(condicao_entrega: str) -> bool:
    txt = norm(condicao_entrega or "").upper().strip()
    txt_sem_espaco = re.sub(r"\s+", "", txt)
    return txt_sem_espaco.startswith("98-") and "JIT" in txt_sem_espaco or txt_sem_espaco.startswith("98") and "JIT" in txt_sem_espaco


def normalizar_doc(valor: str) -> str:
    txt = norm(valor or "").strip()
    if not txt:
        return ""
    return txt


def gerar_candidatos_pedido(valor: str):
    txt = normalizar_doc(valor)
    if not txt:
        return []

    candidatos = {txt}

    sem_zero = txt.lstrip("0")
    if sem_zero:
        candidatos.add(sem_zero)

    if "/" in txt:
        parte = txt.split("/", 1)[0].strip()
        if parte:
            candidatos.add(parte)
            candidatos.add(parte.lstrip("0"))

    return [c for c in candidatos if c]


def descobrir_rede(raw_cliente: str = "", fantasia: str = "", condicao_entrega: str = ""):
    base = fantasia or raw_cliente or ""
    base_limpa = limpar_nome_para_rede(base)

    if not base_limpa:
        return "", ""

    # 1) prioridade total: aliases manuais
    base_norm = norm(base_limpa).upper()
    for apelido, rede_principal in REDE_ALIASES_MANUAIS.items():
        apelido_norm = norm(apelido).upper().strip()
        if apelido_norm and apelido_norm in base_norm:
            rede_nome = rede_principal.strip().upper()
            return rede_nome, normalize_key_text(rede_nome)

    tokens = [t for t in base_limpa.split() if t]

    if not tokens:
        return "", ""

    # 2) Se for JIT, geralmente o primeiro nome é a marca/rede principal
    if eh_jit_matriz(condicao_entrega):
        rede_nome = tokens[0]
        return rede_nome, normalize_key_text(rede_nome)

    # 3) Heurística conservadora
    if len(tokens) == 1:
        rede_nome = tokens[0]
    elif tokens[1] in SIGLAS_CURTAS_REDE:
        rede_nome = tokens[0]
    elif len(tokens[1]) <= 2:
        rede_nome = tokens[0]
    elif tokens[1] in {"MATRIZ", "FILIAL", "NORDESTE", "NORTE", "SUL", "LESTE", "OESTE"}:
        rede_nome = tokens[0]
    else:
        rede_nome = " ".join(tokens[:2])

    return rede_nome, normalize_key_text(rede_nome)

def eh_jit(condicao_entrega: str) -> bool:
    txt = norm(condicao_entrega or "").upper().strip()
    return "JIT" in txt


def eh_jit_matriz(condicao_entrega: str) -> bool:
    txt = norm(condicao_entrega or "").upper().strip()
    txt_sem_espaco = re.sub(r"\s+", "", txt)
    return (txt_sem_espaco.startswith("98-") or txt_sem_espaco.startswith("98")) and "JIT" in txt_sem_espaco


def normalizar_nome_grupo(txt: str) -> str:
    txt = norm(txt or "").upper().strip()
    if not txt:
        return ""

    txt = txt.replace("/", " ").replace("-", " ")
    txt = re.sub(r"[^\w\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    lixo = {
        "LTDA", "EIRELI", "ME", "MEI", "SA", "S", "A",
        "MATRIZ", "FILIAL", "UNIDADE", "LOJA"
    }
    tokens = [t for t in txt.split() if t and t not in lixo]

    if not tokens:
        return ""

    # regra simples inicial:
    # pega até as duas primeiras palavras, mas normalmente a primeira já identifica a rede
    if len(tokens) >= 2 and len(tokens[1]) > 2:
        return " ".join(tokens[:2]).strip()
    return tokens[0].strip()

def nome_base_matriz_jit(txt: str) -> str:
    txt = norm(txt or "").upper().strip()
    if not txt:
        return ""

    txt = txt.replace("/", " ").replace("-", " ")
    txt = re.sub(r"[^\w\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()

    lixo = {
        "LTDA", "EIRELI", "ME", "MEI", "SA", "S", "A",
        "MATRIZ", "FILIAL", "UNIDADE", "LOJA", "NOVO", "NOVA"
    }

    tokens = [t for t in txt.split() if t and t not in lixo]

    if not tokens:
        return ""

    # nomes compostos que devem continuar juntos
    compostos = {
        "NACHO MAN",
        "PEDIDOS 10",
        "NOSSA COZINHA",
        "TRATTORIA CARBONE",
        "GENERAL LYY",
        "DANIELA RORATO",
        "CARIOCA CALCADOS",
        "CARIOCA SPORTS",
        "FOOD TO",
        "GEORGE SEGUROS"
    }

    if len(tokens) >= 2:
        dois = f"{tokens[0]} {tokens[1]}".strip()
        if dois in compostos:
            return dois

        # se o segundo token for número, mantém os dois
        if any(ch.isdigit() for ch in tokens[1]):
            return dois

        # se o primeiro for muito curto, mantém os dois
        if len(tokens[0]) <= 3:
            return dois

    return tokens[0]

def escolher_cliente_matriz(conn, base_nome: str):
    """
    Escolhe a matriz mais provável da rede:
    prioridade para cliente que veio de pedido JIT
    e nome/fantasia mais curto e mais limpo.
    """
    base_nome = norm(base_nome or "").upper().strip()
    if not base_nome:
        return None

    rows = conn.execute("""
        SELECT DISTINCT
            c.id,
            c.nome,
            COALESCE(c.fantasia, '') AS fantasia,
            CASE
              WHEN EXISTS (
                SELECT 1
                FROM orders o
                WHERE o.cliente_id = c.id
                  AND (
                    UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega, '')),' ','')) LIKE '98%JIT%'
                  )
              ) THEN 1
              ELSE 0
            END AS eh_jit
        FROM customers c
        WHERE
            UPPER(COALESCE(c.fantasia, c.nome, '')) = ?
            OR UPPER(COALESCE(c.fantasia, c.nome, '')) LIKE ?
        ORDER BY eh_jit DESC,
                 LENGTH(COALESCE(c.fantasia, c.nome, '')) ASC,
                 c.id ASC
    """, (base_nome, f"{base_nome}%")).fetchall()

    if not rows:
        return None

    # preferimos o mais curto e que tenha vindo de JIT
    return rows[0]

def get_or_create_customer_group(conn, nome_grupo: str, matriz_customer_id=None):
    nome_grupo = norm(nome_grupo or "").upper().strip()
    if not nome_grupo:
        return None

    row = conn.execute(
        "SELECT id, matriz_customer_id FROM customer_groups WHERE UPPER(nome)=UPPER(?) LIMIT 1",
        (nome_grupo,)
    ).fetchone()

    if row:
        gid = int(row["id"])
        if matriz_customer_id and not row["matriz_customer_id"]:
            conn.execute("""
                UPDATE customer_groups
                   SET matriz_customer_id = ?
                 WHERE id = ?
            """, (int(matriz_customer_id), gid))
        return gid

    conn.execute(
        "INSERT INTO customer_groups(nome, created_at, matriz_customer_id) VALUES(?, ?, ?)",
        (nome_grupo, now_iso(), int(matriz_customer_id) if matriz_customer_id else None)
    )
    return int(conn.execute("SELECT last_insert_rowid() id").fetchone()["id"])


def vincular_cliente_ao_grupo(conn, customer_id: int, group_id: int):
    if not customer_id or not group_id:
        return

    conn.execute("""
        INSERT OR IGNORE INTO customer_group_members(group_id, customer_id, created_at)
        VALUES(?, ?, ?)
    """, (group_id, customer_id, now_iso()))

    conn.execute("""
        UPDATE customers
           SET grupo_id = ?
         WHERE id = ?
    """, (group_id, customer_id))


def autopreencher_filiais_existentes(conn):
    """
    Usa os clientes que apareceram em pedidos JIT como base de matriz.
    Para cada base, cria UM grupo só, define UMA matriz e vincula
    todas as filiais parecidas nela.
    """
    matrizes = conn.execute("""
        SELECT DISTINCT
            c.id AS customer_id,
            COALESCE(c.fantasia, c.nome, '') AS nome_ref
        FROM orders o
        JOIN customers c ON c.id = o.cliente_id
        WHERE UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega, '')),' ','')) LIKE '98%JIT%'
    """).fetchall()

    bases = []
    vistos = set()

    for m in matrizes:
        base = nome_base_matriz_jit(m["nome_ref"] or "")
        if base and len(base) >= 3:
            chave = base.upper().strip()
            if chave not in vistos:
                vistos.add(chave)
                bases.append(base)

    todos = conn.execute("""
        SELECT id, nome, COALESCE(fantasia,'') AS fantasia
        FROM customers
        ORDER BY nome
    """).fetchall()

    for base in bases:
        matriz = escolher_cliente_matriz(conn, base)
        matriz_id = int(matriz["id"]) if matriz else None

        gid = get_or_create_customer_group(conn, base, matriz_customer_id=matriz_id)
        if not gid:
            continue

        if matriz_id:
            vincular_cliente_ao_grupo(conn, matriz_id, gid)

        base_up = base.upper().strip()

        for c in todos:
            alvo = (c["fantasia"] or c["nome"] or "").upper().strip()
            if not alvo:
                continue

            # tudo que começa com a base vai para a mesma matriz
            if (
                alvo == base_up
                or alvo.startswith(base_up + " ")
                or alvo.startswith(base_up + "-")
                or alvo.startswith(base_up + " -")
            ):
                vincular_cliente_ao_grupo(conn, int(c["id"]), gid)



def buscar_grupo_do_cliente(conn, customer_id: int):
    if not customer_id:
        return None

    row = conn.execute("""
        SELECT g.id, g.nome
        FROM customer_groups g
        JOIN customer_group_members m ON m.group_id = g.id
        WHERE m.customer_id = ?
        LIMIT 1
    """, (customer_id,)).fetchone()

    return row


def procurar_grupo_existente_por_nome(conn, nome_cliente: str, fantasia: str = ""):
    base = fantasia or nome_cliente or ""
    base = normalizar_nome_grupo(base)
    if not base:
        return None

    row = conn.execute("""
        SELECT id, nome
        FROM customer_groups
        WHERE UPPER(nome) = UPPER(?)
        LIMIT 1
    """, (base,)).fetchone()

    if row:
        return row

    return None


def extrair_codigo_produto(produto: str) -> str:
    txt = (produto or "").strip()
    if not txt:
        return ""
    return txt.split(" - ", 1)[0].strip()

def escolher_vendedor_importacao(rr: dict, preferir_responsavel: bool = True) -> str:
    nome_resp = first_not_empty(rr.get("nome_responsavel"), rr.get("responsavel")).strip()
    representante = first_not_empty(rr.get("representante"), rr.get("representate")).strip()
    vendedor = first_not_empty(rr.get("vendedor"), rr.get("vend")).strip()
    # SITE é exceção: precisa cair no vendedor SITE, não no responsável operacional
    if "SITE" in norm(representante).upper().split():
        return "SITE"
    if preferir_responsavel:
        return first_not_empty(nome_resp, vendedor, representante).strip()
    return first_not_empty(representante, nome_resp, vendedor).strip()

def buscar_contexto_pedido_para_faturamento(conn, pedido: str, cliente_nome: str = "") -> dict:
    pedido = (pedido or "").strip()
    if not pedido:
        return {}
    candidatos = gerar_candidatos_pedido(pedido)
    if not candidatos:
        return {}
    placeholders = ",".join(["?"] * len(candidatos))
    params = list(candidatos)
    sql = f"""
        SELECT
            o.pedido,
            o.cliente_id,
            o.vendor_id,
            COALESCE(o.vendor_nome_ref, '') AS vendor_nome_ref,
            CASE WHEN UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega,'')),' ','')) LIKE '98%JIT%' THEN 1 ELSE 0 END AS is_jit,
            COALESCE(o.rede_nome, '') AS rede_nome,
            COALESCE(o.rede_key, '') AS rede_key,
            COALESCE(o.grupo_ref, '') AS grupo_ref,
            COALESCE(o.tipo_item_ref, '') AS tipo_item_ref,
            COALESCE(o.papel_ref, '') AS papel_ref,
            COALESCE(o.preco_ref, 0) AS preco_ref,
            COALESCE(o.valor_item_ref, 0) AS valor_item_ref,
            COALESCE(o.medida_ref, '') AS medida_ref,
            COALESCE(c.fantasia, c.nome, '') AS cliente_nome
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        WHERE COALESCE(o.pedido, '') IN ({placeholders})
        ORDER BY
            CASE WHEN UPPER(TRIM(COALESCE(c.fantasia, c.nome, ''))) = UPPER(TRIM(?)) THEN 0 ELSE 1 END,
            COALESCE(o.valor_item_ref, 0) DESC,
            o.id DESC
        LIMIT 1
    """
    params.append(cliente_nome or "")
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def build_order_context_cache(conn: sqlite3.Connection) -> Dict[str, dict]:
    """Pré-carrega o melhor contexto de cada pedido para o faturamento.

    No Sisplan o número do pedido identifica um único cliente. A versão anterior
    fazia uma consulta complexa para cada pedido encontrado no faturamento; este
    cache executa uma única leitura ordenada e reaproveita o resultado para todas
    as linhas da NF.
    """
    cache: Dict[str, dict] = {}
    rows = conn.execute("""
        SELECT
            o.pedido,
            o.cliente_id,
            o.vendor_id,
            COALESCE(o.vendor_nome_ref, '') AS vendor_nome_ref,
            CASE WHEN UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega,'')),' ','')) LIKE '98%JIT%' THEN 1 ELSE 0 END AS is_jit,
            COALESCE(o.rede_nome, '') AS rede_nome,
            COALESCE(o.rede_key, '') AS rede_key,
            COALESCE(o.grupo_ref, '') AS grupo_ref,
            COALESCE(o.tipo_item_ref, '') AS tipo_item_ref,
            COALESCE(o.papel_ref, '') AS papel_ref,
            COALESCE(o.preco_ref, 0) AS preco_ref,
            COALESCE(o.valor_item_ref, 0) AS valor_item_ref,
            COALESCE(o.medida_ref, '') AS medida_ref,
            COALESCE(c.fantasia, c.nome, '') AS cliente_nome
        FROM orders o
        LEFT JOIN customers c ON c.id=o.cliente_id
        WHERE TRIM(COALESCE(o.pedido,'')) <> ''
        ORDER BY COALESCE(o.valor_item_ref,0) DESC, o.id DESC
    """).fetchall()
    for r in rows:
        item = dict(r)
        pedido = str(item.get("pedido") or "").strip()
        if not pedido:
            continue
        # Primeiro registro vence porque a query já prioriza maior valor do item
        # e o registro mais novo, mesma regra prática do lookup anterior.
        for cand in gerar_candidatos_pedido(pedido):
            cache.setdefault(cand, item)
    return cache

def import_pedidos_csv(file_bytes: bytes, filename: str) -> Tuple[int, int, int]:
    headers, rows = read_csv_dicts(file_bytes)

    with conectar() as conn:
        conn.execute(
            "INSERT INTO imports(tipo,arquivo,imported_at,linhas,registros,novos,substituidos,duplicados_arquivo) VALUES(?,?,?,?,?,?,?,?)",
            ("PEDIDOS", filename, now_iso(), len(rows), 0, 0, 0, 0)
        )
        import_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        novos = 0
        substituidos = 0
        duplicados_arquivo = 0
        row_mapper = compile_row_mapper(headers)
        entity_cache = build_import_entity_cache(conn)
        existing_dedupe = {r["dedupe_key"] for r in conn.execute("SELECT dedupe_key FROM orders WHERE TRIM(COALESCE(dedupe_key,''))<>''").fetchall()}
        jit_customers_seen = set()
        pending_customers_seen = set()
        import_ts = now_iso()
        pending_deletes = []
        pending_inserts = []

        # Guarda contagem das chaves vistas no próprio CSV.
        # IMPORTANTE: no PEDIDOS, duas linhas realmente válidas podem ser idênticas
        # em pedido/medida/valor/quantidade. Por isso, a 2ª ocorrência recebe sufixo
        # estável por ordem no arquivo, em vez de substituir/apagar a anterior.
        seen_in_file = {}

        for r in rows:
            rr = row_mapper(r)

            pedido = first_not_empty(
                rr.get("pedido"),
                rr.get("num_pedido"),
                rr.get("pedido_numero"),
                rr.get("numero_pedido")
            ).strip()

            data_emissao = try_parse_date(first_not_empty(
                rr.get("data"),
                rr.get("emissao"),
                rr.get("data_emissao"),
                rr.get("emissao_pedido"),
                rr.get("dt_emissao")
            ))

            fantasia_cliente = first_not_empty(
                rr.get("fantasia")
            ).strip()

            raw_cliente_dedupe = first_not_empty(
                fantasia_cliente,
                rr.get("fantasia"),
                rr.get("cliente_nome"),
                rr.get("razao_social"),
                rr.get("cliente_razao_social"),
                rr.get("nome_fantasia")
            ).strip()
            raw_cliente = raw_cliente_dedupe

            cliente_csv_bruto = first_not_empty(
                rr.get("cliente"),
                rr.get("cod_cliente"),
                rr.get("codigo_cliente")
            ).strip()

            customer_codigo = ""
            cliente_razao_do_csv = ""

            if " - " in cliente_csv_bruto:
                partes_cliente = cliente_csv_bruto.split(" - ", 1)
                customer_codigo = (partes_cliente[0] or "").strip()
                cliente_razao_do_csv = (partes_cliente[1] or "").strip()
            else:
                customer_codigo = _extract_customer_code(cliente_csv_bruto) or cliente_csv_bruto.strip()

            # O campo FANTASIA de alguns layouts do Sisplan vem literalmente como "Cliente".
            # Nesses casos, recuperamos o nome real pelos demais campos/código/pedido.
            raw_cliente = resolve_customer_name_for_import(
                conn,
                customer_code=customer_codigo,
                pedido=pedido,
                candidates=[
                    fantasia_cliente,
                    rr.get("cliente_nome"),
                    rr.get("razao_social"),
                    rr.get("cliente_razao_social"),
                    rr.get("nome_fantasia"),
                    cliente_razao_do_csv,
                    cliente_csv_bruto,
                ],
            )
            fantasia_cliente = _clean_customer_candidate(fantasia_cliente) or raw_cliente

            raw_vendor = escolher_vendedor_importacao(rr, preferir_responsavel=True)

            valor_pedido = parse_br_money(first_not_empty(
                rr.get("valor_ped"),
                rr.get("valor_pedido"),
                rr.get("valor_total"),
                rr.get("total"),
                rr.get("valor"),
                rr.get("valor_liquido"),
                "0"
            ))

            valor_item = parse_br_money(first_not_empty(
                rr.get("valor_item"),
                rr.get("subtotal"),
                rr.get("preco_total"),
                rr.get("valor"),
                "0"
            ))

            valor = valor_pedido if float(valor_pedido or 0) > 0 else valor_item

            quantidade = parse_float(first_not_empty(
                rr.get("quantidade"),
                rr.get("qtde"),
                rr.get("qtd"),
                rr.get("qtd_total"),
                rr.get("qtde_total"),
                "0"
            ))

            medida_ref = first_not_empty(
                rr.get("medida"),
                rr.get("formato"),
                rr.get("tamanho"),
                rr.get("dimensao"),
                rr.get("dimensoes")
            ).strip()

            grupo_ref = first_not_empty(
                rr.get("grupo")
            ).strip()

            tipo_item_ref = first_not_empty(
                rr.get("tipo_item_pedido"),
                rr.get("tipo_item"),
                rr.get("tipo")
            ).strip()

            papel_ref = first_not_empty(
                rr.get("papel")
            ).strip()

            preco_ref = parse_br_money(first_not_empty(
                rr.get("preco"),
                rr.get("valor_unitario"),
                rr.get("valor_unit"),
                "0"
            ))

            condicao_entrega = first_not_empty(
                rr.get("condicao_entrega"),
                rr.get("condicao de entrega"),
                rr.get("cond_entrega")
            ).strip()

            situacao_pedido = first_not_empty(
                rr.get("situacao"), rr.get("situacao_pedido"), rr.get("status"),
                rr.get("status_pedido"), rr.get("desc_situacao"), rr.get("situacao_desc")
            ).strip()
            motivo_pedido = first_not_empty(
                rr.get("motivo"), rr.get("cod_motivo"), rr.get("codigo_motivo")
            ).strip()
            desc_motivo_pedido = first_not_empty(
                rr.get("descmotivo"), rr.get("desc_motivo"), rr.get("descricao_motivo"),
                rr.get("motivo_descricao"), rr.get("motivo_desc")
            ).strip()
            pedido_cancelado_flag = 1 if texto_indica_cancelamento_py(
                situacao_pedido, motivo_pedido, desc_motivo_pedido
            ) else 0

            condicao_entrega_norm = norm(condicao_entrega)
            is_jit_flag = 1 if condicao_bloqueia_comissao_jit(condicao_entrega_norm, grupo_ref) else 0

            dedupe_key = build_dedupe_key(
                "PEDIDOS",
                medida_ref,
                data_emissao or "",
                valor,
                pedido,
                raw_cliente_dedupe,
                raw_vendor,
                grupo_ref,
                tipo_item_ref,
                papel_ref,
                quantidade,
                preco_ref,
                valor_item,
                condicao_entrega,
                customer_codigo,
                cliente_razao_do_csv,
            )

            # Se nem consegue montar chave, pula
            if not dedupe_key:
                continue

            # CORREÇÃO DEFINITIVA 15/05/2026:
            # O Sisplan pode exportar duas linhas legítimas IGUAIS no mesmo pedido
            # (ex.: mesmo pedido, mesma medida, mesmo valor_item). Antes o CRM apagava
            # a linha anterior porque o dedupe_key era igual. Agora mantemos a ocorrência
            # da linha dentro do arquivo, com sufixo estável por ordem de aparição.
            base_dedupe_key = dedupe_key
            ocorrencia = int(seen_in_file.get(base_dedupe_key, 0) or 0) + 1
            seen_in_file[base_dedupe_key] = ocorrencia
            if ocorrencia > 1:
                dedupe_key = f"{base_dedupe_key}|OCC:{ocorrencia}"

            cliente_upsert = build_customer_upsert_value(customer_codigo, raw_cliente)
            cliente_id = upsert_customer(conn, cliente_upsert, fantasia_cliente, codigo_hint=customer_codigo, cache=entity_cache)
            vendor_id = upsert_vendor(conn, raw_vendor, cache=entity_cache)

            if eh_jit_matriz(condicao_entrega_norm) and cliente_id not in jit_customers_seen:
                nome_identidade = fantasia_cliente or raw_cliente or f"Cliente {cliente_id}"
                upsert_identity_rule_as_matriz(
                    conn,
                    customer_id=cliente_id,
                    identity_nome=nome_identidade,
                    origem="AUTO_JIT"
                )
                jit_customers_seen.add(cliente_id)

            if not eh_jit_matriz(condicao_entrega_norm):
                nome_busca_pendencia = (fantasia_cliente or raw_cliente or "").strip()
                pending_key = (int(cliente_id or 0), _customer_name_key(nome_busca_pendencia))

                # A pendência é por cliente/identidade, não por linha do pedido.
                # Consultar uma vez evita repetir a mesma busca centenas de vezes.
                if nome_busca_pendencia and pending_key not in pending_customers_seen:
                    pending_customers_seen.add(pending_key)
                    candidatos = conn.execute("""
                        SELECT
                            c.id,
                            COALESCE(c.fantasia, c.nome, '') AS nome_base
                        FROM customers c
                        WHERE c.id <> ?
                          AND UPPER(TRIM(COALESCE(c.fantasia, c.nome, ''))) = UPPER(TRIM(?))
                        ORDER BY c.id
                        LIMIT 10
                    """, (
                        cliente_id,
                        nome_busca_pendencia,
                    )).fetchall()

                    for cand in candidatos:
                        create_import_identity_pending(
                            conn,
                            customer_id=cliente_id,
                            customer_nome=nome_busca_pendencia,
                            customer_codigo=customer_codigo,
                            condicao_entrega=condicao_entrega,
                            candidato_customer_id=int(cand["id"] or 0),
                            candidato_nome=(cand["nome_base"] or "").strip(),
                            import_id=None
                        )

            # 2) JÁ EXISTIA NO BANCO (importação anterior).
            # O conjunto é carregado uma vez; evita um SELECT por linha.
            if dedupe_key in existing_dedupe:
                pending_deletes.append((dedupe_key,))
                substituidos += 1
            else:
                novos += 1
            existing_dedupe.add(dedupe_key)

            pending_inserts.append((
                import_id, pedido or None, data_emissao, cliente_id, vendor_id,
                valor, quantidade, None, import_ts, medida_ref or None,
                raw_cliente or None, raw_vendor or None, dedupe_key,
                condicao_entrega or None, is_jit_flag, grupo_ref or None,
                tipo_item_ref or None, papel_ref or None, preco_ref, valor_item,
                situacao_pedido or None, motivo_pedido or None, desc_motivo_pedido or None,
                pedido_cancelado_flag
            ))

        if pending_deletes:
            conn.executemany("DELETE FROM orders WHERE dedupe_key = ?", pending_deletes)
        if pending_inserts:
            conn.executemany("""
                INSERT INTO orders(
                    import_id, pedido, data_emissao, cliente_id, vendor_id,
                    valor, quantidade, raw_json, created_at,
                    medida_ref, cliente_nome_ref, vendor_nome_ref, dedupe_key,
                    condicao_entrega, is_jit, grupo_ref, tipo_item_ref, papel_ref, preco_ref, valor_item_ref,
                    situacao_pedido, motivo_pedido, desc_motivo_pedido, pedido_cancelado
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, pending_inserts)

        registros_processados = novos + substituidos

        conn.execute(
            "UPDATE imports SET registros=?, novos=?, substituidos=?, duplicados_arquivo=? WHERE id=?",
            (registros_processados, novos, substituidos, duplicados_arquivo, import_id)
        )
        conn.commit()

        return import_id, len(rows), registros_processados
    
def import_liquidez_csv(file_bytes: bytes, filename: str) -> Tuple[int, int, int]:
    headers, rows = read_csv_dicts(file_bytes)

    with conectar() as conn:
        conn.execute(
            "INSERT INTO imports(tipo,arquivo,imported_at,linhas,registros,novos,substituidos,duplicados_arquivo) VALUES(?,?,?,?,?,?,?,?)",
            ("LIQUIDEZ", filename, now_iso(), len(rows), 0, 0, 0, 0)
        )
        import_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        novos = 0
        substituidos = 0
        duplicados_arquivo = 0

        row_mapper = compile_row_mapper(headers)
        entity_cache = build_import_entity_cache(conn)
        existing_dedupe = {r["dedupe_key"] for r in conn.execute("SELECT dedupe_key FROM liquidez WHERE TRIM(COALESCE(dedupe_key,''))<>''").fetchall()}
        import_ts = now_iso()
        pending_deletes = []
        pending_inserts = []

        # Preserva todas as linhas do 733. Títulos diferentes podem ficar iguais
        # depois da normalização (ex.: 0070074/2-1 x 007007421), portanto a
        # ocorrência dentro do arquivo faz parte da chave estável.
        seen_in_file = {}

        for r in rows:
            rr = row_mapper(r)

            # REGRA FIXA: SEMPRE RESPONSAVEL, exceto SITE que vem por NOME_REPRESENTANTE/REPRESENTANTE
            nome_representante = first_not_empty(
                rr.get("nome_representante"),
                rr.get("representante"),
                rr.get("nome representante"),
                rr.get("nome_do_representante")
            ).strip()
            raw_vendor = first_not_empty(
                rr.get("nome_responsavel"),
                rr.get("responsavel")
            ).strip()
            if is_site_vendor_name(nome_representante):
                raw_vendor = "SITE"

            fantasia_cliente = first_not_empty(
                rr.get("fantasia")
            ).strip()

            raw_cliente_source = first_not_empty(
                fantasia_cliente,
                rr.get("fantasia"),
                rr.get("cliente"),
                rr.get("cliente_nome"),
                rr.get("razao_social")
            ).strip()
            raw_cliente = raw_cliente_source

            pedido = first_not_empty(
                rr.get("pedido"),
                rr.get("num_pedido"),
                rr.get("numero_pedido")
            ).strip()

            titulo = first_not_empty(
                rr.get("titulo"),
                rr.get("duplicata"),
                rr.get("num_duplicata"),
                rr.get("numero_duplicata"),
                rr.get("nro_duplicata"),
                rr.get("documento"),
                rr.get("doc"),
                rr.get("num_doc"),
                rr.get("numero_documento"),
                rr.get("titulo_receber"),
                rr.get("num_titulo"),
                rr.get("numero_titulo"),
                rr.get("fatura"),
                rr.get("parcela"),
                rr.get("nf"),
                rr.get("nota"),
                rr.get("numero_nota")
            ).strip()

            data_pagto = try_parse_date(first_not_empty(
                rr.get("dt_pagto"),
                rr.get("data_pagto"),
                rr.get("pagamento"),
                rr.get("data_pagamento"),
                rr.get("baixa")
            ))

            valor_pago = parse_br_money(first_not_empty(
                rr.get("valor_pago"),
                rr.get("valor"),
                rr.get("valor_recebido"),
                rr.get("valorrecebido"),
                "0"
            ))

            valor_liq = parse_br_money(first_not_empty(
                rr.get("valor_liq"),
                rr.get("valor_liquido"),
                rr.get("liquido"),
                "0"
            ))

            valor_ref = valor_pago if float(valor_pago or 0) > 0 else valor_liq

            cliente_codigo = first_not_empty(
                rr.get("codcli"),
                rr.get("cod_cli"),
                rr.get("codigo_cliente"),
                rr.get("cod_cliente"),
                rr.get("cliente_codigo"),
                rr.get("codigo"),
                rr.get("cod")
            ).strip()

            medida_ref = first_not_empty(
                rr.get("medida"),
                rr.get("formato"),
                rr.get("tamanho"),
                rr.get("dimensao"),
                rr.get("dimensoes")
            ).strip()

            cliente_nome_dedupe = raw_cliente_source
            if " - " in raw_cliente_source:
                partes = raw_cliente_source.split(" - ", 1)
                if not cliente_codigo:
                    cliente_codigo = partes[0].strip()
                cliente_nome_dedupe = partes[1].strip()

            # Resolve o nome real antes de gravar. Código + pedido permitem cruzar os outros relatórios.
            cliente_nome_salvar = resolve_customer_name_for_import(
                conn,
                customer_code=cliente_codigo,
                pedido=pedido,
                candidates=[
                    fantasia_cliente,
                    rr.get("cliente_nome"),
                    rr.get("razao_social"),
                    rr.get("nome_fantasia"),
                    cliente_nome_dedupe,
                    rr.get("cliente"),
                ],
            )
            fantasia_cliente = _clean_customer_candidate(fantasia_cliente) or cliente_nome_salvar
            cliente_bruto_upsert = build_customer_upsert_value(cliente_codigo, cliente_nome_salvar)

            # DEDUP LIQUIDEZ - chave reforçada e menos agressiva
            # Antes usava pedido OU titulo. Se o pedido vinha preenchido, títulos/duplicatas
            # diferentes do mesmo pedido podiam cair na mesma chave e a liquidez ficava menor.
            # Agora o título/duplicata tem prioridade e os dois documentos entram na chave.
            data_vencimento_ref = try_parse_date(first_not_empty(
                rr.get("dt_vencimento"),
                rr.get("data_vencimento"),
                rr.get("vencimento"),
                rr.get("data_vencto"),
                rr.get("vencto")
            ))
            parcela_ref = first_not_empty(
                rr.get("parcela"),
                rr.get("seq"),
                rr.get("sequencia"),
                rr.get("sequencial"),
                rr.get("item"),
                rr.get("linha")
            ).strip()
            documento_ref = titulo or pedido

            chave_fragil_sem_titulo = not bool(titulo)
            assinatura_linha = dedupe_row_signature(rr) if chave_fragil_sem_titulo else ""

            dedupe_key = build_dedupe_key(
                tipo="LIQUIDEZ",
                medida_ref=medida_ref,
                data_ref=data_pagto or "",
                valor_ref=valor_ref,
                documento_ref=documento_ref,
                cliente_ref=cliente_codigo or cliente_nome_dedupe,
                responsavel_ref=raw_vendor,
                extra_refs=[
                    f"PED:{pedido}" if pedido else "",
                    f"TIT:{titulo}" if titulo else "",
                    f"CLI:{cliente_codigo}" if cliente_codigo else "",
                    f"VP:{money_key(valor_pago)}",
                    f"VL:{money_key(valor_liq)}",
                    f"VENC:{data_vencimento_ref}" if data_vencimento_ref else "",
                    f"PARC:{parcela_ref}" if parcela_ref else "",
                    f"REPR:{nome_representante}" if nome_representante else "",
                    f"ROW:{assinatura_linha}" if assinatura_linha else "",
                ]
            )

            if not dedupe_key:
                continue

            # Não descarte colisões de normalização no próprio 733. Mantemos
            # OCC:2, OCC:3... para que o total do banco seja o total do CSV.
            base_dedupe_key = dedupe_key
            ocorrencia = int(seen_in_file.get(base_dedupe_key, 0) or 0) + 1
            seen_in_file[base_dedupe_key] = ocorrencia
            if ocorrencia > 1:
                dedupe_key = f"{base_dedupe_key}|OCC:{ocorrencia}"

            customer_id = upsert_customer(conn, cliente_bruto_upsert, fantasia_cliente or cliente_nome_salvar, codigo_hint=cliente_codigo, cache=entity_cache)
            vendor_id = upsert_vendor(conn, raw_vendor, cache=entity_cache)

            # 2) JÁ EXISTIA NO BANCO — cache evita SELECT por linha.
            if dedupe_key in existing_dedupe:
                pending_deletes.append((dedupe_key,))
                substituidos += 1
            else:
                novos += 1
            existing_dedupe.add(dedupe_key)

            pending_inserts.append((
                import_id, vendor_id, customer_id, pedido or None, titulo or None,
                cliente_codigo or None, cliente_nome_salvar or None, float(valor_pago or 0),
                float(valor_liq or 0), data_pagto, raw_vendor, import_ts, medida_ref or None, dedupe_key
            ))

        if pending_deletes:
            conn.executemany("DELETE FROM liquidez WHERE dedupe_key = ?", pending_deletes)
        if pending_inserts:
            conn.executemany("""
                INSERT INTO liquidez(
                    import_id, vendor_id, customer_id, pedido, titulo,
                    cliente_codigo, cliente_nome, valor_pago, valor_liq,
                    data_pagto, responsavel, created_at, medida_ref, dedupe_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, pending_inserts)

        registros_processados = novos + substituidos

        conn.execute(
            "UPDATE imports SET registros=?, novos=?, substituidos=?, duplicados_arquivo=? WHERE id=?",
            (registros_processados, novos, substituidos, duplicados_arquivo, import_id)
        )
        conn.commit()

        return import_id, len(rows), registros_processados
    

def import_faturamento_csv(file_bytes: bytes, filename: str) -> Tuple[int, int, int]:
    headers, rows = read_csv_dicts(file_bytes)

    with conectar() as conn:
        conn.execute(
            "INSERT INTO imports(tipo,arquivo,imported_at,linhas,registros,novos,substituidos,duplicados_arquivo) VALUES(?,?,?,?,?,?,?,?)",
            ("FATURAMENTO", filename, now_iso(), len(rows), 0, 0, 0, 0)
        )
        import_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        novos = 0
        substituidos = 0
        duplicados_arquivo = 0

        row_mapper = compile_row_mapper(headers)
        entity_cache = build_import_entity_cache(conn)
        existing_dedupe = {r["dedupe_key"] for r in conn.execute("SELECT dedupe_key FROM invoices WHERE TRIM(COALESCE(dedupe_key,''))<>''").fetchall()}
        contexto_pedido_cache = build_order_context_cache(conn)
        import_ts = now_iso()
        pending_deletes = []
        pending_inserts = []

        # Conta ocorrências por assinatura. O 731 pode conter duas linhas
        # legitimamente idênticas; elas precisam ser preservadas.
        seen_in_file = {}

        for r in rows:
            rr = row_mapper(r)

            numero = first_not_empty(
                rr.get("numero"),
                rr.get("nf"),
                rr.get("nota"),
                rr.get("numnota"),
                rr.get("numero_nota")
            ).strip()

            data_emissao = try_parse_date(first_not_empty(
                rr.get("data"),
                rr.get("data_emissao"),
                rr.get("emissao"),
                rr.get("dt_emissao")
            ))

            fantasia_cliente = first_not_empty(
                rr.get("fantasia")
            ).strip()

            raw_cliente_dedupe = first_not_empty(
                fantasia_cliente,
                rr.get("nome_fantasia"),
                rr.get("cliente_nome"),
                rr.get("razao_social"),
            ).strip()

            cliente_csv_bruto = first_not_empty(
                rr.get("cliente"), rr.get("cod_cliente"), rr.get("codigo_cliente"), rr.get("codcli"), rr.get("cod_cli")
            ).strip()
            customer_codigo = first_not_empty(
                rr.get("cod_cliente"), rr.get("codigo_cliente"), rr.get("codcli"), rr.get("cod_cli"), rr.get("cliente_codigo")
            ).strip()
            cliente_razao_do_csv = ""
            cod_composto, nome_composto = split_codigo_nome(cliente_csv_bruto)
            if cod_composto and not customer_codigo:
                customer_codigo = cod_composto
            if nome_composto and nome_composto != cliente_csv_bruto:
                cliente_razao_do_csv = nome_composto
            if not customer_codigo:
                customer_codigo = _extract_customer_code(cliente_csv_bruto)

            raw_cliente = resolve_customer_name_for_import(
                conn,
                customer_code=customer_codigo,
                pedido=first_not_empty(rr.get("pedido"), rr.get("num_pedido"), rr.get("numero_pedido")).strip(),
                candidates=[
                    fantasia_cliente,
                    rr.get("nome_fantasia"),
                    rr.get("cliente_nome"),
                    rr.get("razao_social"),
                    cliente_razao_do_csv,
                    cliente_csv_bruto,
                ],
            )
            fantasia_cliente = _clean_customer_candidate(fantasia_cliente) or raw_cliente

            raw_vendor = escolher_vendedor_importacao(rr, preferir_responsavel=True)

            valor = parse_br_money(first_not_empty(
                rr.get("valor"),
                rr.get("valor_total"),
                rr.get("total"),
                rr.get("valor_nota"),
                "0"
            ))

            quantidade = parse_float(first_not_empty(
                rr.get("quantidade"),
                rr.get("qtde"),
                rr.get("qtd"),
                "0"
            ))

            # PRECO pertence ao próprio 731 e ajuda a distinguir linhas sem
            # depender do contexto herdado do relatório 721.
            preco_csv = parse_br_money(first_not_empty(
                rr.get("preco"),
                rr.get("valor_unitario"),
                rr.get("valor_unit"),
                "0"
            ))

            pedido = first_not_empty(
                rr.get("pedido"),
                rr.get("num_pedido"),
                rr.get("numero_pedido")
            ).strip()

            produto = first_not_empty(
                rr.get("produto"),
                rr.get("descricao"),
                rr.get("item")
            ).strip()
            produto_codigo = extrair_codigo_produto(produto)

            formato = first_not_empty(
                rr.get("formato"),
                rr.get("medida"),
                rr.get("tamanho"),
                rr.get("dimensao"),
                rr.get("dimensoes")
            ).strip()

            contexto_pedido = {}
            for pedido_cand in gerar_candidatos_pedido(pedido):
                contexto_pedido = contexto_pedido_cache.get(pedido_cand) or {}
                if contexto_pedido:
                    break
            # Fallback raro para um pedido recém-chegado com formato incomum.
            if pedido and not contexto_pedido:
                contexto_pedido = buscar_contexto_pedido_para_faturamento(conn, pedido, raw_cliente)
                if contexto_pedido:
                    for pedido_cand in gerar_candidatos_pedido(pedido):
                        contexto_pedido_cache[pedido_cand] = contexto_pedido
            grupo_ref = (contexto_pedido.get("grupo_ref") or "").strip()
            tipo_item_ref = (contexto_pedido.get("tipo_item_ref") or "").strip()
            papel_ref = (contexto_pedido.get("papel_ref") or "").strip()
            preco_ref = float(contexto_pedido.get("preco_ref") or 0)
            valor_item_ref = float(contexto_pedido.get("valor_item_ref") or 0)
            is_jit = int(contexto_pedido.get("is_jit") or 0)
            rede_nome = (contexto_pedido.get("rede_nome") or "").strip()
            rede_key = (contexto_pedido.get("rede_key") or "").strip()
            if not formato:
                formato = (contexto_pedido.get("medida_ref") or "").strip()

            # A chave de FATURAMENTO precisa depender somente dos campos do
            # próprio 731. Antes ela herdava formato/grupo/nome do 721; quando
            # esse contexto mudava, a mesma nota podia entrar de novo com outra
            # chave e inflar os totais acumulados.
            dedupe_key = build_dedupe_key(
                tipo="FATURAMENTO",
                medida_ref="",
                data_ref=data_emissao or "",
                valor_ref=valor,
                documento_ref=numero or pedido,
                cliente_ref=pedido,
                responsavel_ref=produto_codigo or produto,
                extra_refs=[
                    f"PROD:{produto}",
                    f"QTD:{quantidade}",
                    f"PRECO:{money_key(preco_csv)}",
                ],
            )

            if not dedupe_key:
                continue

            # Preserva ocorrências realmente repetidas do 731, mas gera sempre
            # a mesma sequência OCC:2, OCC:3... a cada fotografia importada.
            base_dedupe_key = dedupe_key
            ocorrencia = int(seen_in_file.get(base_dedupe_key, 0) or 0) + 1
            seen_in_file[base_dedupe_key] = ocorrencia
            if ocorrencia > 1:
                dedupe_key = f"{base_dedupe_key}|OCC:{ocorrencia}"

            cliente_upsert = build_customer_upsert_value(customer_codigo, raw_cliente)
            cliente_id = upsert_customer(conn, cliente_upsert, fantasia_cliente, codigo_hint=customer_codigo, cache=entity_cache)
            if contexto_pedido.get("vendor_id"):
                vendor_id = int(contexto_pedido["vendor_id"])
                raw_vendor = (contexto_pedido.get("vendor_nome_ref") or raw_vendor or "").strip() or raw_vendor
            else:
                vendor_id = upsert_vendor(conn, raw_vendor, cache=entity_cache)

            # 2) JÁ EXISTIA NO BANCO — cache evita SELECT por linha.
            if dedupe_key in existing_dedupe:
                pending_deletes.append((dedupe_key,))
                substituidos += 1
            else:
                novos += 1
            existing_dedupe.add(dedupe_key)

            pending_inserts.append((
                import_id, numero or None, data_emissao, cliente_id, vendor_id,
                valor, quantidade, pedido or None, produto or None, formato or None,
                import_ts, formato or None, raw_cliente or None, raw_vendor or None, dedupe_key,
                is_jit, rede_nome or None, rede_key or None, grupo_ref or None, tipo_item_ref or None,
                papel_ref or None, preco_ref, valor_item_ref, produto_codigo or None
            ))

        if pending_deletes:
            conn.executemany("DELETE FROM invoices WHERE dedupe_key = ?", pending_deletes)
        if pending_inserts:
            conn.executemany("""
                INSERT INTO invoices(
                    import_id, numero, data_emissao, cliente_id, vendor_id,
                    valor, quantidade, pedido, produto, formato,
                    created_at, medida_ref, cliente_nome_ref, vendor_nome_ref, dedupe_key,
                    is_jit, rede_nome, rede_key, grupo_ref, tipo_item_ref, papel_ref, preco_ref, valor_item_ref, produto_codigo_ref
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, pending_inserts)

        registros_processados = novos + substituidos

        conn.execute(
            "UPDATE imports SET registros=?, novos=?, substituidos=?, duplicados_arquivo=? WHERE id=?",
            (registros_processados, novos, substituidos, duplicados_arquivo, import_id)
        )
        conn.commit()

        return import_id, len(rows), registros_processados
    
def import_orcamentos_csv(file_bytes: bytes, filename: str) -> Tuple[int, int, int]:
    """Importa o relatório 104 mantendo uma linha por orçamento.

    Além dos campos já usados pelo CRM, preserva os dados do 104 necessários
    para a tela detalhada (quantidade, valor base, observações, datas e status).
    A lógica existente de identificação do orçamento continua baseada em NUMERO/dedupe_key.
    """
    headers, rows = read_csv_dicts(file_bytes)

    with conectar() as conn:
        conn.execute(
            "INSERT INTO imports(tipo,arquivo,imported_at,linhas,registros,novos,substituidos,duplicados_arquivo) VALUES(?,?,?,?,?,?,?,?)",
            ("ORCAMENTO", filename, now_iso(), len(rows), 0, 0, 0, 0)
        )
        import_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        novos = 0
        substituidos = 0
        duplicados_arquivo = 0
        row_mapper = compile_row_mapper(headers)
        seen_in_file = set()

        for r in rows:
            rr = row_mapper(r)
            numero = first_not_empty(rr.get("numero"), rr.get("orcamento"), rr.get("num_orcamento"), rr.get("numero_orcamento")).strip()
            if not numero:
                continue

            cliente_nome_dedupe = first_not_empty(
                rr.get("nome"), rr.get("cliente_nome"), rr.get("razao_social"),
                rr.get("nome_fantasia"), rr.get("cliente"), rr.get("fantasia")
            ).strip()
            vendedor_nome = first_not_empty(rr.get("nomeven"), rr.get("responsavel"), rr.get("vendedor"), rr.get("vendedor_nome")).strip()
            vendedor_id = first_not_empty(
                rr.get("vendedor_id"), rr.get("codigo_vendedor"), rr.get("cod_vendedor"), rr.get("cod_ven"), rr.get("codven")
            ).strip()
            pedido_id = first_not_empty(rr.get("pedido"), rr.get("pedido_id"), rr.get("num_pedido"), rr.get("numero_pedido")).strip()

            cliente_csv_bruto = first_not_empty(rr.get("cliente"), rr.get("cod_cliente"), rr.get("codigo_cliente")).strip()
            customer_codigo = first_not_empty(
                rr.get("cod_cliente"), rr.get("codigo_cliente"), rr.get("cliente_codigo"), rr.get("codcli"), rr.get("cod_cli")
            ).strip()
            cod_composto, nome_composto = split_codigo_nome(cliente_csv_bruto)
            if cod_composto and not customer_codigo:
                customer_codigo = cod_composto
            if not customer_codigo:
                customer_codigo = _extract_customer_code(cliente_csv_bruto)
            cliente_nome = resolve_customer_name_for_import(
                conn, customer_code=customer_codigo, pedido=pedido_id,
                candidates=[
                    rr.get("nome_fantasia"), rr.get("fantasia"), rr.get("nome"), rr.get("cliente_nome"),
                    rr.get("razao_social"), nome_composto if nome_composto != cliente_csv_bruto else "", cliente_csv_bruto,
                ],
            )

            data_inicio = try_parse_date(first_not_empty(rr.get("data_inicio"), rr.get("data"), rr.get("emissao"), rr.get("dt_inicio")))
            data_validade = try_parse_date(first_not_empty(rr.get("data_validade"), rr.get("validade"), rr.get("dt_validade")))
            data_prev = try_parse_date(first_not_empty(rr.get("dt_prev"), rr.get("data_prev"), rr.get("previsao")))
            data_retorno = try_parse_date(first_not_empty(rr.get("dt_retorno"), rr.get("data_retorno"), rr.get("retorno")))
            # O Sisplan usa 30/12/1899 como sentinela de "sem data". Não expor isso ao gestor.
            if data_prev and str(data_prev) < "1901-01-01":
                data_prev = None
            if data_retorno and str(data_retorno) < "1901-01-01":
                data_retorno = None

            # 104: VALORT é o total final; VALOR é a base antes de acréscimos.
            valor_total = parse_br_money(first_not_empty(rr.get("valort"), rr.get("valor_total"), rr.get("total"), rr.get("valor"), "0"))
            valor_base = parse_br_money(first_not_empty(rr.get("valor"), rr.get("valor_base"), "0"))
            quantidade_orc = parse_float(first_not_empty(rr.get("quantidade"), rr.get("qtde"), rr.get("qtd"), "0"))

            status = first_not_empty(rr.get("status"), rr.get("fase"), rr.get("etapa")).strip()
            desc_status = first_not_empty(rr.get("desc_status"), rr.get("descricao_status"), rr.get("descricao"), rr.get("motivo")).strip()
            tipo_contato = first_not_empty(rr.get("tipo_contato"), rr.get("contato"), rr.get("canal")).strip()
            uf = first_not_empty(rr.get("cod_uf"), rr.get("uf"), rr.get("estado"), rr.get("estado_uf"), rr.get("sigla_uf"), rr.get("uf_cliente")).strip().upper()
            observacao = first_not_empty(rr.get("observacao")).strip()
            obs = first_not_empty(rr.get("obs")).strip()
            # Mantém a compatibilidade com a tela existente: quando não há motivo
            # estruturado, ela já usava observação/OBS como contexto comercial.
            motivo = first_not_empty(
                rr.get("descmotivo"), rr.get("desc_motivo"), rr.get("descricao_motivo"), rr.get("motivo_desc"),
                rr.get("motivo"), rr.get("codmotivo"), rr.get("cod_motivo"), observacao, obs
            ).strip()
            status_os = first_not_empty(rr.get("status_os")).strip()
            prospeccao = first_not_empty(rr.get("prospeccao")).strip()
            desc_prospeccao = first_not_empty(rr.get("desc_prospeccao")).strip()
            funcionario = first_not_empty(rr.get("funcionario")).strip()
            nome_funcionario = first_not_empty(rr.get("nome_funcionario")).strip()
            cidade = first_not_empty(rr.get("nome_1"), rr.get("cidade")).strip()
            medida_ref = first_not_empty(rr.get("medida"), rr.get("formato"), rr.get("tamanho"), rr.get("dimensao"), rr.get("dimensoes")).strip()

            dedupe_key = build_dedupe_key(
                tipo="ORCAMENTO", medida_ref=medida_ref, data_ref=data_inicio or "", valor_ref=valor_total,
                documento_ref=numero, cliente_ref=cliente_nome_dedupe, responsavel_ref=vendedor_nome,
            )
            if not dedupe_key:
                continue
            if dedupe_key in seen_in_file:
                duplicados_arquivo += 1
                continue
            seen_in_file.add(dedupe_key)

            existente = conn.execute("SELECT * FROM orcamentos WHERE numero=? LIMIT 1", (numero,)).fetchone()
            if not existente:
                existente = conn.execute("SELECT * FROM orcamentos WHERE dedupe_key=? LIMIT 1", (dedupe_key,)).fetchone()

            values = {
                "numero": numero or None, "cliente_nome": cliente_nome or None, "pedido_id": pedido_id or None,
                "vendedor_id": vendedor_id or None, "vendedor_nome": vendedor_nome or None,
                "data_inicio": data_inicio, "data_validade": data_validade, "status": status or None,
                "desc_status": desc_status or None, "tipo_contato": tipo_contato or None,
                "valor_total": float(valor_total or 0), "uf": uf or None, "motivo": motivo or None,
                "medida_ref": medida_ref or None, "cliente_nome_ref": cliente_nome or None,
                "vendor_nome_ref": vendedor_nome or None, "quantidade": float(quantidade_orc or 0),
                "valor_base": float(valor_base or 0), "observacao": observacao or None, "obs": obs or None,
                "data_prev": data_prev, "data_retorno": data_retorno, "status_os": status_os or None,
                "prospeccao": prospeccao or None, "desc_prospeccao": desc_prospeccao or None,
                "funcionario": funcionario or None, "nome_funcionario": nome_funcionario or None,
                "cliente_codigo": customer_codigo or None, "cidade": cidade or None, "dedupe_key": dedupe_key,
            }

            if not existente:
                cols = ["import_id", "created_at"] + list(values.keys())
                vals = [import_id, now_iso()] + [values[k] for k in values]
                conn.execute(
                    f"INSERT INTO orcamentos ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", vals
                )
                novos += 1
                continue

            mudou = False
            for key, newval in values.items():
                oldval = existente[key] if key in existente.keys() else None
                if key in ("valor_total", "quantidade", "valor_base"):
                    if abs(float(oldval or 0) - float(newval or 0)) > 0.0001:
                        mudou = True; break
                elif (str(oldval or "").strip() != str(newval or "").strip()):
                    mudou = True; break
            if not mudou:
                duplicados_arquivo += 1
                continue

            set_sql = ",".join(["import_id=?"] + [f"{k}=?" for k in values])
            params = [import_id] + [values[k] for k in values] + [existente["id"]]
            conn.execute(f"UPDATE orcamentos SET {set_sql} WHERE id=?", params)
            substituidos += 1

        registros_processados = novos + substituidos
        conn.execute(
            "UPDATE imports SET registros=?, novos=?, substituidos=?, duplicados_arquivo=? WHERE id=?",
            (registros_processados, novos, substituidos, duplicados_arquivo, import_id)
        )
        conn.commit()
        return import_id, len(rows), registros_processados

# -----------------------------
# Views
# -----------------------------


def infer_product_family(produto_base: str) -> str:
    base = norm(produto_base or '').upper().strip()
    if not base:
        return 'Sem produto'
    # PAPEL SEDA precisa ser identificado antes dos agrupamentos genéricos.
    # No Sisplan ele aparece principalmente como GRUPO_REF='PAPEL SEDA'.
    if 'PAPEL SEDA' in base or ('PAPEL' in base and 'SEDA' in base):
        return 'Papel Seda'
    if 'SACOLA' in base:
        return 'Sacola'
    if 'CARTUCHO' in base:
        return 'Cartucho'
    if 'BOBINA' in base:
        return 'Bobina'
    if 'ENVELOPE' in base:
        return 'Envelope'
    if 'CAIXA' in base:
        return 'Caixa'
    if 'BAGKRAFT' in base:
        return 'Bagkraft'
    return (base[:50] + '...') if len(base) > 50 else base


def infer_order_product_family(grupo_ref: str = '', tipo_item_ref: str = '', papel_ref: str = '', medida_ref: str = '') -> str:
    """Classifica as famílias presentes em um pedido do Sisplan.

    Um mesmo pedido pode ter linhas de clichê + Sacola/Cartucho/Papel Seda, ou
    até mais de uma família. Por isso preservamos todas as famílias comerciais
    relevantes em vez de deixar um item auxiliar (como CLICHES) esconder o produto.
    """
    parts = [grupo_ref, tipo_item_ref, papel_ref, medida_ref]
    base = ' '.join(norm(x or '').upper().strip() for x in parts if str(x or '').strip())
    if not base:
        return 'Sem produto'

    familias = []
    if 'SACOLA' in base:
        familias.append('Sacola')
    if 'CARTUCHO' in base:
        familias.append('Cartucho')
    if 'PAPEL SEDA' in base or ('PAPEL' in base and 'SEDA' in base):
        familias.append('Papel Seda')
    if familias:
        return ' + '.join(familias)

    return infer_product_family(grupo_ref or tipo_item_ref or papel_ref or medida_ref)


def end_of_month(d: date) -> date:
    """Adaptador: ver crm_npd.metrics.periodos.ultimo_dia_do_mes."""
    return f_periodos.ultimo_dia_do_mes(d)


def get_ref_date_param(param_name: str = 'data_ref') -> date:
    raw = (request.args.get(param_name) or '').strip()
    if not raw:
        return date.today()
    for fmt in ('%Y-%m-%d', '%Y-%m'):
        try:
            dt = datetime.strptime(raw, fmt).date()
            if fmt == '%Y-%m':
                return end_of_month(date(dt.year, dt.month, 1))
            return dt
        except Exception:
            continue
    return date.today()




# -----------------------------
# IA / Assistente do CRM
# -----------------------------
MESES_PT = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "março": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}


def _fmt_brl(v: float) -> str:
    try:
        return f"R$ {float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def _parse_mes_ano(texto: str) -> tuple[int, int] | tuple[None, None]:
    q = norm(texto or "")
    hoje = date.today()
    if "este mes" in q or "esse mes" in q or "mes atual" in q or "mês atual" in texto.lower():
        return hoje.month, hoje.year
    if "mes passado" in q or "mês passado" in texto.lower():
        d = (hoje.replace(day=1) - timedelta(days=1))
        return d.month, d.year

    m = re.search(r'\b(0?[1-9]|1[0-2])[/-](20\d{2})\b', q)
    if m:
        return int(m.group(1)), int(m.group(2))

    ano_match = re.search(r'\b(20\d{2})\b', q)
    ano = int(ano_match.group(1)) if ano_match else hoje.year

    for nome_mes, num_mes in MESES_PT.items():
        if re.search(rf'\b{re.escape(nome_mes)}\b', q):
            return num_mes, ano

    return None, None


def _periodo_mes(mes: int, ano: int) -> tuple[str, str]:
    ini = date(ano, mes, 1)
    if mes == 12:
        fim = date(ano + 1, 1, 1)
    else:
        fim = date(ano, mes + 1, 1)
    return ini.isoformat(), fim.isoformat()


def _get_month_name_pt(mes: int) -> str:
    nomes = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    if 1 <= mes <= 12:
        return nomes[mes-1]
    return "mês inválido"


def _sql_scalar(conn, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    if not row:
        return 0
    try:
        return list(row)[0]
    except Exception:
        return row[0] if row else 0




CRM_RULES_LOCAL = {
    "responsavel": "Para comissão, carteira e atribuição comercial o CRM deve usar RESPONSAVEL/NOME_RESPONSAVEL. REPRESENTANTE só deve ser usado em exceções específicas do canal SITE.",
    "jit": "Pedido JIT/matriz pode contar para meta/pedidos conforme a tela, mas não deve inflar faturamento/comissão de filial. Filial IMEDIATA conta para faturamento/comissão quando aplicável.",
    "carteira": "Transferências manuais de carteira, tarefas, matriz/filial e classificação devem ser persistidas e reaplicadas após excluir/importar CSV novamente.",
    "novo_recompra": "Cliente novo vem do pedido no Sisplan (MOTIVO 10 — PEDIDO PRIMEIRA COMPRA). JIT IMEDIATO anula. Gestor/admin pode forçar NOVO ou RECOMPRA.",
    "seguranca": "Vendedor só pode consultar sua própria carteira/desempenho. Gestor e admin podem consultar visão ampliada. Comissão de outro vendedor não deve aparecer para usuário comum.",
}

IA_KNOWLEDGE_SEED = [
    ("usuarios", "Como cadastrar um novo usuário?", "Para cadastrar usuário, entre em **Usuários** no menu lateral e clique em **Novo usuário** ou preencha o formulário da tela. Informe nome, e-mail, perfil, senha e, se for vendedor, vincule o código/vendedor do Sisplan. Depois salve e peça para a pessoa acessar com o e-mail cadastrado.", "admin", "cadastrar usuario novo usuário criar login vendedores gestor admin"),
    ("usuarios", "Como alterar ou desativar um usuário?", "Entre em **Usuários**, localize o usuário e use as ações disponíveis para editar, ativar/desativar ou ajustar vínculo com vendedor. Essa função deve ficar restrita a admin/gestor conforme regra da empresa.", "gestor", "alterar desativar usuario bloquear login"),
    ("importacao", "Como importar pedidos?", "Entre em **Atualizar Sisplan**. Na importação inteligente, selecione de uma vez os CSVs de **PEDIDOS, FATURAMENTO, ORÇAMENTO e LIQUIDEZ**; o CRM identifica cada arquivo automaticamente, importa na ordem correta e reprocessa redes/matrizes uma única vez no final. Se precisar corrigir só PEDIDOS, use a importação avulsa.", "all", "importar pedidos csv sisplan atualizar dados pacote"),
    ("importacao", "Como importar faturamento?", "Entre em **Atualizar Sisplan** e, de preferência, selecione os quatro relatórios juntos. O CRM detecta o arquivo de **FATURAMENTO** automaticamente. Para uma correção pontual, use a seção **Importação avulsa**.", "all", "importar faturamento notas invoices csv atualizar sisplan"),
    ("importacao", "Como importar liquidez?", "Entre em **Atualizar Sisplan** e selecione os relatórios exportados. O CRM reconhece **LIQUIDEZ** automaticamente; para uma correção isolada, use **Importação avulsa**. A liquidez é usada principalmente em regras financeiras/comerciais já configuradas no CRM.", "gestor", "importar liquidez comissão externos atualizar sisplan"),
    ("orcamentos", "Como ver orçamento detalhe?", "Entre em **Orçamentos Detalhe**, selecione mês e vendedor. A tela mostra quantidade por fase, percentual e valor financeiro de cada status de orçamento.", "all", "orçamento detalhe fase negociação aprovado"),
    ("oportunidades", "Como cadastrar um novo lead?", "Você **não precisa cadastrar lead manualmente**. O CRM usa o relatório de **ORÇAMENTOS do Sisplan** como fonte única. Entre em **Oportunidades** para ver os orçamentos transformados em oportunidades e em **Funil** para acompanhar as etapas. Para atualizar, use **Atualizar Sisplan**.", "all", "novo lead cadastrar oportunidade sisplan orçamento funil"),
    ("funil", "Como funciona o funil comercial?", "O **Funil** é uma visão automática e somente leitura dos status de orçamento do Sisplan. Ele não cria uma base paralela. Se a negociação mudar de fase, atualize o status no Sisplan e depois rode **Atualizar Sisplan** no CRM.", "all", "funil etapa orçamento sisplan oportunidade negociação"),
    ("clientes", "Como mesclar matriz ou grupo?", "Entre em **Clientes** e use **Mesclar grupo/matriz**. Selecione uma ou várias origens/filiais e escolha um único destino. O sistema passa a tratar as origens como filiais manuais da matriz escolhida.", "gestor", "mesclar grupo matriz filial rede clientes"),
    ("clientes", "Como transferir cliente para outro vendedor?", "A transferência manual de cliente/carteira deve ser feita pela ação administrativa da tela de clientes/vendedores. Depois da transferência, o cliente deve sumir da carteira antiga e aparecer na carteira do novo vendedor, mantendo o override salvo mesmo após reimportações.", "gestor", "transferir cliente carteira vendedor"),
    ("tarefas", "Como transferir tarefas?", "Entre em **Tarefas**, clique em **Transferir Tarefas**, selecione as tarefas desejadas com checkbox, escolha o vendedor de destino e confirme. A alteração deve ficar registrada e persistida.", "gestor", "transferir tarefas checkbox vendedor"),
    ("tarefas", "Como usar o filtro de atraso de recompra?", "Na tela **Tarefas**, ative o filtro de atraso da média de recompra. O CRM ordena os clientes do mais atrasado para o menos atrasado e mostra quantos dias estão fora da média de recompra.", "all", "atraso recompra média tarefas fora da média"),
    ("tarefas", "O que significa cliente atrasado na média de recompra?", "Significa que o cliente costuma recomprar em um intervalo médio e já passou desse prazo. Exemplo: se a média é 30 dias e está há 40 dias sem comprar, aparece como **10 dias fora da média de recompra**.", "all", "cliente atrasado média recompra"),
    ("comissao", "Qual é a base de cálculo da comissão?", "Regra principal: vendedores internos usam faturamento; externos usam liquidez quando configurado; o CRM deve atribuir por **RESPONSAVEL/NOME_RESPONSAVEL**, não por REPRESENTANTE. Comissão de outro vendedor é informação restrita.", "gestor", "base cálculo comissão responsavel representante liquidez faturamento"),
    ("jit", "O que é JIT no CRM?", "JIT normalmente representa pedido/matriz programado. No CRM, JIT precisa ser tratado com cuidado para não duplicar meta, faturamento ou comissão. O botão **Ignorar JIT** em tarefas ajuda a esconder itens que não devem entrar no acompanhamento operacional normal.", "all", "jit matriz imediata filial condicao entrega"),
    ("dashboard", "Como conferir pedidos de um vendedor?", "Na tela **Vendedores**, selecione o mês e clique em **Ver pedidos** no card de Pedidos Realizado. A lista deve mostrar os pedidos do período e ajuda a auditar divergências.", "all", "ver pedidos vendedor realizado mês"),
    ("auditoria", "Por que meus pedidos não batem?", "As causas mais comuns são: vendedor vindo de coluna errada, pedido cancelado entrando, dedup agressivo, diferença entre ciclo de pedidos e ciclo de faturamento, JIT/IMEDIATA, cliente transferido manualmente ou CSV com colunas diferentes. Use a auditoria de pedidos por vendedor para ver o que entrou e o que foi ignorado.", "gestor", "pedidos não batem divergência cecilia auditoria"),
    ("score", "Como funciona o score do cliente?", "O score busca indicar qualidade/risco do cliente considerando recompra, valor, frequência e histórico. Cliente com valor alto mas muito tempo sem comprar pode ter risco maior que um cliente menor e recorrente.", "all", "score cliente risco recompra valor frequência"),
    ("seguranca", "O que vendedor pode ver na IA?", "Vendedor pode perguntar sobre sua própria carteira, tarefas, clientes, pedidos, orçamento e desempenho. Não deve ver comissão, carteira ou faturamento de outro vendedor.", "all", "segurança permissão vendedor ia"),
]


def _ia_user_level(user: dict | None) -> str:
    role = (user or {}).get("role") or "user"
    if role == "admin":
        return "admin"
    if role == "gestor":
        return "gestor"
    return "vendedor"


def _ia_permission_allows(permissao: str, level: str) -> bool:
    p = (permissao or "all").lower().strip()
    if p == "all":
        return True
    if level == "admin":
        return True
    if level == "gestor":
        return p in {"gestor", "vendedor", "all"}
    return p in {"vendedor", "all"}


def seed_ia_knowledge_base(conn) -> None:
    try:
        existentes = int(_sql_scalar(conn, "SELECT COUNT(*) FROM ia_knowledge") or 0)
    except Exception:
        return
    if existentes > 0:
        return
    for categoria, pergunta, resposta, permissao, palavras in IA_KNOWLEDGE_SEED:
        conn.execute("""
            INSERT INTO ia_knowledge(categoria, pergunta, resposta, permissao, palavras_chave, ativo)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (categoria, pergunta, resposta, permissao, palavras))


def _ia_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    a = norm(a or "").lower()
    b = norm(b or "").lower()
    if not a or not b:
        return 0.0
    base = SequenceMatcher(None, a, b).ratio()
    ta = set(re.findall(r"[a-z0-9]+", a))
    tb = set(re.findall(r"[a-z0-9]+", b))
    inter = len(ta & tb)
    union = len(ta | tb) or 1
    token = inter / union
    return (base * 0.55) + (token * 0.45)


def _answer_knowledge_base(conn, question: str, user: dict | None = None) -> str | None:
    level = _ia_user_level(user)
    try:
        seed_ia_knowledge_base(conn)
        rows = conn.execute("""
            SELECT categoria, pergunta, resposta, permissao, palavras_chave
            FROM ia_knowledge
            WHERE COALESCE(ativo,1)=1
        """).fetchall()
    except Exception:
        rows = []
    best = None
    best_score = 0.0
    qn = norm(question or "").lower()
    for r in rows:
        if not _ia_permission_allows(r["permissao"], level):
            continue
        texto = " ".join([r["pergunta"] or "", r["palavras_chave"] or "", r["categoria"] or ""])
        score = _ia_similarity(qn, texto)
        kws = set(re.findall(r"[a-z0-9]+", norm(r["palavras_chave"] or "").lower()))
        qt = set(re.findall(r"[a-z0-9]+", qn))
        if kws and qt:
            score += min(0.35, len(kws & qt) * 0.045)
        if score > best_score:
            best_score = score
            best = r
    if best and best_score >= 0.34:
        fonte = f"Base de conhecimento: {best['categoria']} | confiança {best_score:.0%}"
        return f"{best['resposta']}\n\n---\nFonte: {fonte}"
    return None


def _ia_security_block(question: str, user: dict | None = None) -> str | None:
    if not user:
        return None
    level = _ia_user_level(user)
    if level in {"admin", "gestor"}:
        return None
    qn = norm(question or "").lower()
    termos_restritos = [
        "comissao de", "comissão de", "comissao da", "comissão da", "comissao do", "comissão do",
        "ranking de vendedores", "top vendedores", "melhor vendedor", "melhor vendedora", "faturamento geral", "todos vendedores",
        "carteira da", "carteira do", "senha", "api key", "usuarios", "usuários", "criar usuario", "cadastrar usuario"
    ]
    if any(t in qn for t in termos_restritos):
        return "Por segurança, essa informação é restrita para gestor/administrador. Como vendedor, posso te ajudar com sua própria carteira, clientes, tarefas, pedidos, orçamentos e dúvidas de uso do sistema."
    return None


def _ia_log(conn, user: dict | None, pergunta: str, resposta: str, fonte: str = "local") -> None:
    try:
        conn.execute("""
            INSERT INTO ia_audit_log(usuario, role, pergunta, resposta, fonte, permissao_aplicada)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ((user or {}).get("email") or (user or {}).get("nome"), (user or {}).get("role"), pergunta, resposta[:4000], fonte, _ia_user_level(user)))
    except Exception:
        pass

def _system_explanation(question: str) -> str | None:
    q = norm(question)
    explicacoes = [
        (["base de calculo", "base de calculo da comissao", "base de cálculo da comissão", "comissao"],
         """**Base de cálculo da comissão no CRM**

- Vendedores **internos** usam o **faturamento** importado na tabela `invoices`.
- Vendedores **externos** usam a **liquidez** importada na tabela `liquidez`.
- O canal **SITE** aparece nos painéis, mas não entra como vendedor com meta/comissão padrão.
- A apuração mensal do CRM respeita as regras do próprio sistema e as importações mais recentes."""),
        (["meta", "base da meta", "calculo da meta", "base de cálculo da meta"],
         """**Base da meta**

As metas do CRM são comparadas principalmente contra os **pedidos/faturamento do período selecionado**, conforme a tela e a lógica interna já existente no sistema. Em painéis comerciais, o CRM usa o período ativo da consulta e os dados consolidados do banco."""),
        (["ticket medio", "ticket médio"],
         """**Ticket médio**

O ticket médio comercial de **pedidos** é calculado como **soma de VALOR_ITEM dos pedidos válidos ÷ quantidade de pedidos únicos válidos**. Para faturamento/comissão, o ticket usa notas/faturamento conforme o ciclo financeiro."""),
        (["conversao", "conversão"],
         """**Conversão**

Na tela Mercado, a conversão é estimada como a relação entre o volume financeiro realizado e a base de orçamentos disponível/importada no sistema para o período comparado."""),
        (["ultimo responsavel", "último responsável", "responsavel do cliente"],
         """**Último responsável do cliente**

As tarefas automáticas devem ser atribuídas ao **último responsável** encontrado no histórico mais recente do cliente, evitando duplicidade para vendedores antigos."""),
        (["como usar", "como mexer", "ajuda", "tutorial", "ensine", "manual", "treinamento"],
         """**Ajuda rápida do CRM**

- **Dashboard**: visão geral de pedidos, faturamento, metas e evolução.
- **Mercado**: funil, churn, concentração, regiões e mix.
- **Vendedores / Minha Visão**: desempenho, meta, comissão, clientes e orçamentos do vendedor.
- **Tarefas**: acompanhamento de recompra, contatos e negociações por fase. Use status, busca, vendedor e o filtro **Ignorar JIT** quando quiser esconder pedidos programados.
- **Orçamentos Detalhe**: mostra abertos, fechados e valor total criado.
- **Importar CSV**: entrada dos arquivos do Sisplan. Depois confira Dashboard, Mercado e Importações.

Você pode perguntar: “qual meu faturamento este mês?”, “como vejo minhas tarefas?”, “o que significa conversão?”, “quais clientes estão sem recompra?” ou “como funciona a comissão?”."""),
    ]
    for termos, resposta in explicacoes:
        if any(t in q for t in termos):
            return resposta
    return None


def _find_vendor_by_question(conn, question: str):
    q = norm(question or "").lower()
    vendors = conn.execute("""
        SELECT v.id, v.nome,
               (SELECT COUNT(*) FROM orders o WHERE o.vendor_id=v.id) +
               (SELECT COUNT(*) FROM invoices i WHERE i.vendor_id=v.id) AS mov_count
        FROM vendors v
        WHERE COALESCE(v.ativo,1)=1
        ORDER BY mov_count DESC, v.nome ASC
    """).fetchall()
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    best = None
    best_score = 0
    best_inter = 0
    for v in vendors:
        nome = (v["nome"] or "").strip()
        n = norm(nome).lower()
        if not n:
            continue
        score = 0
        toks = set(re.findall(r"[a-z0-9]+", n))
        inter = len(q_tokens & toks)
        if n in q:
            score += 100
        elif inter >= 2:
            score += inter * 10
        elif inter == 1:
            # permite encontrar vendedor por primeiro nome quando a pergunta é natural
            comuns = {'de','da','do','das','dos','e','a','o','um','uma','para','por','com','que','quanto','quantos','cliente','clientes','anos','trabalha','aqui'}
            hit = list(q_tokens & toks)[0]
            if len(hit) >= 4 and hit not in comuns:
                score += 12
            else:
                continue
        else:
            continue
        if score > best_score or (score == best_score and (inter > best_inter or int(v['mov_count'] or 0) > (int(best['mov_count'] or 0) if best else 0))):
            best_score = score
            best_inter = inter
            best = v
    return best if best_score >= 12 else None


def _market_snapshot(conn, ref_date: date | None = None) -> dict:
    ref_date = ref_date or date.today()
    ano_atual = ref_date.year
    ano_anterior = ano_atual - 1
    ytd_ini = date(ano_atual, 1, 1)
    ytd_fim = ref_date
    ytd_ly_ini = date(ano_anterior, 1, 1)
    ytd_ly_fim = date(ano_anterior, ref_date.month, min(ref_date.day, end_of_month(date(ano_anterior, ref_date.month, 1)).day))
    mes_ini = date(ref_date.year, ref_date.month, 1)
    mes_fim = end_of_month(ref_date)
    mes_ly_ini = date(ano_anterior, ref_date.month, 1)
    mes_ly_fim = end_of_month(mes_ly_ini)
    dias_passados = max(1, (ref_date - mes_ini).days + 1)

    fat_ytd = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
    fat_ytd_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
    fat_mes = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (mes_ini.isoformat(), ref_date.isoformat())).fetchone()
    fat_mes_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (mes_ly_ini.isoformat(), mes_ly_fim.isoformat())).fetchone()
    ped_ytd = conn.execute(pedidos_oficiais_summary_sql(), (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
    ped_mes = conn.execute(pedidos_oficiais_summary_sql(), (mes_ini.isoformat(), ref_date.isoformat())).fetchone()
    orc_ytd = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)", (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
    orc_ytd_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)", (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
    conv_ytd = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) <> '' OR UPPER(TRIM(COALESCE(desc_status,''))) = 'PEDIDO APROVADO' THEN 1 ELSE 0 END) AS fechados
        FROM orcamentos
        WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
    conv_ytd_ly = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) <> '' OR UPPER(TRIM(COALESCE(desc_status,''))) = 'PEDIDO APROVADO' THEN 1 ELSE 0 END) AS fechados
        FROM orcamentos
        WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
    ticket_ytd = float((fat_ytd['valor'] or 0)) / max(1, int(fat_ytd['qtd'] or 0))
    ticket_ytd_ly = float((fat_ytd_ly['valor'] or 0)) / max(1, int(fat_ytd_ly['qtd'] or 0))
    projecao_mes = (float(fat_mes['valor'] or 0) / dias_passados) * mes_fim.day if dias_passados else 0.0
    churn_count = conn.execute("""
        WITH base_ly AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        ),
        base_cy AS (
            SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        )
        SELECT COUNT(*) AS qtd
        FROM base_ly ly
        LEFT JOIN base_cy cy ON cy.chave = ly.chave
        WHERE cy.chave IS NULL
    """, (f'{ano_anterior}-01-01', f'{ano_anterior}-12-31', f'{ano_atual}-01-01', ref_date.isoformat())).fetchone()
    return {
        'ref_date': ref_date,
        'ano_atual': ano_atual,
        'ano_anterior': ano_anterior,
        'fat_ytd_val': float(fat_ytd['valor'] or 0),
        'fat_ytd_ly_val': float(fat_ytd_ly['valor'] or 0),
        'fat_mes_val': float(fat_mes['valor'] or 0),
        'fat_mes_ly_val': float(fat_mes_ly['valor'] or 0),
        'orc_ytd_qtd': int(orc_ytd['qtd'] or 0),
        'orc_ytd_ly_qtd': int(orc_ytd_ly['qtd'] or 0),
        'ped_ytd_qtd': int(ped_ytd['qtd'] or 0) if 'ped_ytd' in locals() else 0,
        'ped_ytd_val': float(ped_ytd['valor'] or 0) if 'ped_ytd' in locals() else 0.0,
        'ped_mes_qtd': int(ped_mes['qtd'] or 0) if 'ped_mes' in locals() else 0,
        'ped_mes_val': float(ped_mes['valor'] or 0) if 'ped_mes' in locals() else 0.0,
        'conv_pct': safe_pct(float(conv_ytd['fechados'] or 0), float(conv_ytd['total'] or 0)),
        'conv_pct_ly': safe_pct(float(conv_ytd_ly['fechados'] or 0), float(conv_ytd_ly['total'] or 0)),
        'ticket_ytd': ticket_ytd,
        'ticket_ytd_ly': ticket_ytd_ly,
        'projecao_mes': projecao_mes,
        'churn_total': int(churn_count['qtd'] or 0),
    }




def _ia_count_current_clients_for_vendor(conn, vendor_id: int) -> dict:
    """Conta carteira atual pelo último responsável encontrado em pedidos/faturamento.
    Usa rede_key quando existe para não duplicar filial/matriz; cai para cliente_id/nome quando não existe.
    """
    try:
        row = conn.execute("""
            WITH movs AS (
                SELECT
                    COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0), 'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave,
                    COALESCE(NULLIF(TRIM(rede_nome),''), NULLIF(TRIM(cliente_nome_ref),''), 'Cliente sem nome') AS cliente,
                    vendor_id,
                    date(data_emissao) AS data_mov,
                    COALESCE(valor,0) AS valor
                FROM orders
                WHERE vendor_id IS NOT NULL AND COALESCE(data_emissao,'') <> ''
                UNION ALL
                SELECT
                    COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0), 'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave,
                    COALESCE(NULLIF(TRIM(rede_nome),''), NULLIF(TRIM(cliente_nome_ref),''), 'Cliente sem nome') AS cliente,
                    vendor_id,
                    date(data_emissao) AS data_mov,
                    COALESCE(valor,0) AS valor
                FROM invoices
                WHERE vendor_id IS NOT NULL AND COALESCE(data_emissao,'') <> ''
            ), ult AS (
                SELECT m.*
                FROM movs m
                JOIN (
                    SELECT chave, MAX(data_mov) AS ultima_data
                    FROM movs
                    GROUP BY chave
                ) x ON x.chave=m.chave AND x.ultima_data=m.data_mov
            ), carteira AS (
                SELECT chave, MAX(cliente) AS cliente, MAX(data_mov) AS ultima_data
                FROM ult
                WHERE vendor_id=?
                GROUP BY chave
            )
            SELECT COUNT(*) AS qtd, MIN(ultima_data) AS mais_antigo, MAX(ultima_data) AS mais_recente
            FROM carteira
        """, (int(vendor_id),)).fetchone()
        top = conn.execute("""
            WITH movs AS (
                SELECT
                    COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0), 'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave,
                    COALESCE(NULLIF(TRIM(rede_nome),''), NULLIF(TRIM(cliente_nome_ref),''), 'Cliente sem nome') AS cliente,
                    vendor_id,
                    date(data_emissao) AS data_mov,
                    COALESCE(valor,0) AS valor
                FROM orders
                WHERE vendor_id IS NOT NULL AND COALESCE(data_emissao,'') <> ''
                UNION ALL
                SELECT
                    COALESCE(NULLIF(TRIM(rede_key),''), 'CID:' || COALESCE(cliente_id,0), 'N:' || UPPER(TRIM(COALESCE(cliente_nome_ref,'')))) AS chave,
                    COALESCE(NULLIF(TRIM(rede_nome),''), NULLIF(TRIM(cliente_nome_ref),''), 'Cliente sem nome') AS cliente,
                    vendor_id,
                    date(data_emissao) AS data_mov,
                    COALESCE(valor,0) AS valor
                FROM invoices
                WHERE vendor_id IS NOT NULL AND COALESCE(data_emissao,'') <> ''
            ), ult AS (
                SELECT m.*
                FROM movs m
                JOIN (SELECT chave, MAX(data_mov) AS ultima_data FROM movs GROUP BY chave) x
                  ON x.chave=m.chave AND x.ultima_data=m.data_mov
            ), carteira AS (
                SELECT chave, MAX(cliente) AS cliente, MAX(data_mov) AS ultima_data
                FROM ult
                WHERE vendor_id=?
                GROUP BY chave
            )
            SELECT cliente, ultima_data
            FROM carteira
            ORDER BY ultima_data DESC, cliente ASC
            LIMIT 10
        """, (int(vendor_id),)).fetchall()
        return {"qtd": int((row or {})["qtd"] or 0), "mais_antigo": (row or {})["mais_antigo"] if row else None, "mais_recente": (row or {})["mais_recente"] if row else None, "top": top}
    except Exception:
        # fallback simples por clientes vinculados diretamente em pedidos/faturamento
        row = conn.execute("""
            SELECT COUNT(*) AS qtd FROM (
                SELECT DISTINCT cliente_id FROM orders WHERE vendor_id=? AND cliente_id IS NOT NULL
                UNION
                SELECT DISTINCT cliente_id FROM invoices WHERE vendor_id=? AND cliente_id IS NOT NULL
            ) x
        """, (int(vendor_id), int(vendor_id))).fetchone()
        return {"qtd": int(row["qtd"] or 0) if row else 0, "mais_antigo": None, "mais_recente": None, "top": []}


def _ia_vendor_tenure_answer(conn, vendor) -> str | None:
    if not vendor:
        return None
    vid = int(vendor["id"])
    nome = vendor["nome"]
    datas = []
    labels = []
    try:
        u = conn.execute("""
            SELECT created_at FROM users
            WHERE UPPER(nome) LIKE UPPER(?) OR UPPER(email) LIKE UPPER(?) OR COALESCE(vendor_codigo,'') = COALESCE((SELECT codigo FROM vendors WHERE id=?),'')
            ORDER BY created_at ASC LIMIT 1
        """, (f"%{nome.split()[0]}%", f"%{nome.split()[0]}%", vid)).fetchone()
        if u and u["created_at"]:
            datas.append(str(u["created_at"])[:10]); labels.append("cadastro de usuário")
    except Exception:
        pass
    try:
        v = conn.execute("SELECT created_at FROM vendors WHERE id=?", (vid,)).fetchone()
        if v and v["created_at"]:
            datas.append(str(v["created_at"])[:10]); labels.append("cadastro de vendedor")
    except Exception:
        pass
    try:
        d = conn.execute("""
            SELECT MIN(data_emissao) AS primeira FROM (
                SELECT data_emissao FROM orders WHERE vendor_id=? AND COALESCE(data_emissao,'') <> ''
                UNION ALL
                SELECT data_emissao FROM invoices WHERE vendor_id=? AND COALESCE(data_emissao,'') <> ''
            ) x
        """, (vid, vid)).fetchone()
        if d and d["primeira"]:
            datas.append(str(d["primeira"])[:10]); labels.append("primeiro pedido/faturamento no CRM")
    except Exception:
        pass
    datas_validas = sorted([d for d in datas if re.match(r"\d{4}-\d{2}-\d{2}", d)])
    if not datas_validas:
        return f"Não encontrei no CRM uma data confiável de cadastro/admissão da **{nome}**. O CRM não deve inventar tempo de empresa sem essa data cadastrada."
    primeira = datas_validas[0]
    try:
        d0 = datetime.strptime(primeira, "%Y-%m-%d").date()
        dias = (date.today() - d0).days
        anos = dias // 365
        meses = (dias % 365) // 30
        tempo = f"{anos} ano(s) e {meses} mês(es)" if anos else f"{meses} mês(es)"
    except Exception:
        tempo = "tempo não calculado"
    return (
        f"O CRM não tem uma tabela formal de RH/admissão. Pela base do CRM, a primeira data encontrada para **{nome}** é **{primeira}** "
        f"({', '.join(labels)}). Isso representa aproximadamente **{tempo}** até hoje.\n\n"
        f"Se você quiser precisão de tempo de casa, o ideal é adicionar um campo **data_admissao** no cadastro de usuários/vendedores."
    )

def _ia_performance_answer(conn, question: str, user: dict | None = None) -> str | None:
    """Responde perguntas de Performance com os próprios cálculos da tela.

    A IA não deve mandar o gestor procurar uma informação que o banco já possui.
    Primeiro devolve o resultado; depois indica onde conferir.
    """
    q = norm(question or "")
    gatilhos = (
        "melhor vendedor", "melhor vendedora", "destaque interno", "destaque externo",
        "quem performa", "quem esta melhor", "quem está melhor", "tabela de performance",
        "score dos vendedores", "ranking performance", "performance do time",
    )
    vendor = _find_vendor_by_question(conn, question)
    quer_vendor = bool(vendor and any(t in q for t in ("performance", "score", "desempenho", "como esta", "como está")))
    if not quer_vendor and not any(g in q for g in gatilhos):
        return None

    role = (user or {}).get("role")
    if role not in ("admin", "gestor"):
        return None

    mes, ano = _parse_mes_ano(question)
    hoje = date.today()
    mes = mes or hoje.month
    ano = ano or hoje.year
    data_ref = f"{ano:04d}-{mes:02d}"
    ctx = _performance_build_context(conn, data_ref, include_customer_mix=True)
    rows = [r for r in ctx["rows"] if int(r.get("id") or 0) > 0 and (r.get("tipo") or "").upper() in ("INTERNO", "EXTERNO")]

    if vendor:
        r = next((x for x in rows if int(x.get("id") or 0) == int(vendor["id"])), None)
        if r:
            return (
                f"Na **Performance de {_get_month_name_pt(mes)}/{ano}**, **{r['nome']}** está com **score {float(r.get('score') or 0):.0f}/100**. "
                f"No período: **{int(r.get('orc_qtd') or 0)} orçamentos** ({_fmt_brl(r.get('orc_valor') or 0)}), "
                f"**{float(r.get('conv_pct') or 0):.1f}% de conversão**, **{int(r.get('ped_qtd') or 0)} pedidos**, "
                f"**{_fmt_brl(r.get('fat_valor') or 0)} faturados**, **{int(r.get('clientes_novos') or 0)} novos**, "
                f"**{int(r.get('clientes_repeticao') or 0)} recorrentes** e **{int(r.get('clientes_recuperados') or 0)} recuperados**.\n\n"
                f"Você pode conferir em **Performance → {r['nome']}**, mas estes são os números calculados pela própria tela."
            )

    tipo = None
    if "intern" in q:
        tipo = "INTERNO"
    elif "extern" in q:
        tipo = "EXTERNO"
    filtradas = [r for r in rows if not tipo or (r.get("tipo") or "").upper() == tipo]
    if not filtradas:
        return f"Não encontrei vendedores com atividade na Performance de {_get_month_name_pt(mes)}/{ano}."
    filtradas.sort(key=lambda r: (float(r.get("score") or 0), int(r.get("orc_qtd") or 0), float(r.get("fat_valor") or 0)), reverse=True)
    top = filtradas[0]
    label_time = "time interno" if tipo == "INTERNO" else ("time externo" if tipo == "EXTERNO" else "time")
    linhas = []
    for pos, r in enumerate(filtradas[:3], 1):
        linhas.append(
            f"{pos}. **{r['nome']}** — score **{float(r.get('score') or 0):.0f}**, "
            f"{int(r.get('orc_qtd') or 0)} orçamentos, {float(r.get('conv_pct') or 0):.1f}% conversão, "
            f"{_fmt_brl(r.get('fat_valor') or 0)} faturados"
        )
    return (
        f"Com base no **score da tela Performance** de **{_get_month_name_pt(mes)}/{ano}**, o destaque do **{label_time}** é "
        f"**{top['nome']}**, com **{float(top.get('score') or 0):.0f}/100**. Ela/ele fez **{int(top.get('orc_qtd') or 0)} orçamentos**, "
        f"teve **{float(top.get('conv_pct') or 0):.1f}% de conversão**, **{int(top.get('ped_qtd') or 0)} pedidos** e "
        f"**{_fmt_brl(top.get('fat_valor') or 0)} de faturamento**.\n\n"
        + "**Top 3 do mesmo recorte:**\n" + "\n".join(linhas) +
        "\n\nPara conferir visualmente: **Performance** e selecione o mesmo mês/time."
    )


def _answer_data_question(conn, question: str, user: dict | None = None) -> str | None:
    q = norm(question)
    mes, ano = _parse_mes_ano(question)
    vendor = _find_vendor_by_question(conn, question)

    perf_answer = _ia_performance_answer(conn, question, user=user)
    if perf_answer:
        return perf_answer

    # Perguntas básicas que precisam somar A+B no banco, sem depender da OpenAI
    if vendor and (('quantos clientes' in q) or ('qtd clientes' in q) or ('carteira' in q and 'cliente' in q)):
        dados = _ia_count_current_clients_for_vendor(conn, int(vendor['id']))
        linhas = []
        for r in (dados.get('top') or [])[:8]:
            linhas.append(f"- {r['cliente']} — última movimentação: {r['ultima_data'] or '-'}")
        extra = ("\n\nÚltimos clientes movimentados na carteira atual:\n" + "\n".join(linhas)) if linhas else ""
        return (
            f"Pela regra de **carteira atual pelo último responsável**, **{vendor['nome']}** tem **{dados['qtd']} cliente(s)/grupo(s)** no CRM.\n\n"
            f"Critério usado: peguei pedidos + faturamento, agrupei por rede/cliente e considerei como dono atual o vendedor da movimentação mais recente."
            f"{extra}"
        )

    if vendor and (('primeiro pedido' in q) or ('desde o primeiro' in q) or ('historico completo' in q) or ('histórico completo' in question.lower())):
        row = conn.execute(f"""
            SELECT MIN(o.data_emissao) AS primeira_data,
                   COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd_pedidos,
                   COUNT(DISTINCT o.cliente_id) AS qtd_clientes,
                   COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS valor
            FROM orders o
            LEFT JOIN vendors v ON v.id=o.vendor_id
            WHERE o.vendor_id=?
              AND {sql_pedidos_realizados_excel_cond('o','v')}
        """, (int(vendor['id']),)).fetchone()
        return (
            f"Baseado **desde o primeiro pedido registrado** de **{vendor['nome']}** no CRM: primeira data **{row['primeira_data'] or '-'}**, "
            f"**{int(row['qtd_pedidos'] or 0)} pedido(s)**, **{int(row['qtd_clientes'] or 0)} cliente(s)** e "
            f"**{_fmt_brl(row['valor'] or 0)}** em pedidos."
        )

    if vendor and (('quantos anos' in q) or ('tempo de empresa' in q) or ('trabalha aqui' in q) or ('tempo trabalha' in q)):
        ans = _ia_vendor_tenure_answer(conn, vendor)
        if ans:
            return ans

    if ('como faço' in q or 'como faco' in q or 'como saber' in q) and 'quantos clientes' in q:
        if vendor:
            dados = _ia_count_current_clients_for_vendor(conn, int(vendor['id']))
            return (
                f"Para saber quantos clientes um vendedor tem, use a regra de **carteira atual pelo último responsável**. "
                f"No caso de **{vendor['nome']}**, encontrei **{dados['qtd']} cliente(s)/grupo(s)**.\n\n"
                f"Também dá para conferir pela tela **Vendedores** ou pela rota **Diagnóstico**, pesquisando o nome do vendedor."
            )
        return "Para saber quantos clientes um vendedor tem, me diga o nome do vendedor. Exemplo: **quantos clientes a Amanda Longarai tem?**"

    if ('top vendedores' in q or 'ranking de vendedores' in q or 'melhores vendedores' in q) and ('pedido' in q or 'pedidos' in q or 'vendas' in q or 'realizado' in q):
        ano_match = re.search(r'\b(20\d{2})\b', q)
        ano_ref = int(ano_match.group(1)) if ano_match else date.today().year
        mes_ref, ano_mes = _parse_mes_ano(question)
        if mes_ref and ano_mes:
            ini, fim_excl = _periodo_mes(mes_ref, ano_mes)
            fim = (datetime.strptime(fim_excl, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
            titulo = f"Top vendedores por pedidos em {_get_month_name_pt(mes_ref)}/{ano_mes}"
        else:
            ini, fim = f"{ano_ref}-01-01", f"{ano_ref}-12-31"
            titulo = f"Top vendedores por pedidos em {ano_ref}"
        rows = conn.execute(f"""
            SELECT COALESCE(v.nome, 'Sem vendedor') AS vendedor,
                   COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS valor,
                   COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd
            FROM orders o
            LEFT JOIN vendors v ON v.id=o.vendor_id
            WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_site_exclusion_cond('v')}
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY COALESCE(v.nome, 'Sem vendedor')
            ORDER BY valor DESC, vendedor ASC
            LIMIT 10
        """, (ini, fim)).fetchall()
        if rows:
            linhas = [f"- **{r['vendedor']}**: {_fmt_brl(r['valor'])} ({int(r['qtd'] or 0)} pedidos)" for r in rows]
            return f"**{titulo}**\n\n" + "\n".join(linhas)

    if ('top vendedores' in q or 'ranking de vendedores' in q or 'melhores vendedores' in q) and ('faturamento' in q or 'fat' in q):
        ano_match = re.search(r'\b(20\d{2})\b', q)
        ano_ref = int(ano_match.group(1)) if ano_match else date.today().year
        mes_ref, ano_mes = _parse_mes_ano(question)
        if mes_ref and ano_mes:
            ini, fim = _periodo_mes(mes_ref, ano_mes)
            titulo = f"Top vendedores por faturamento em {_get_month_name_pt(mes_ref)}/{ano_mes}"
        else:
            ini, fim = f"{ano_ref}-01-01", f"{ano_ref+1}-01-01"
            titulo = f"Top vendedores por faturamento em {ano_ref}"
        rows = conn.execute("""
            SELECT COALESCE(v.nome, 'Sem vendedor') AS vendedor, COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor
            FROM invoices i
            LEFT JOIN vendors v ON v.id = i.vendor_id
            WHERE i.data_emissao >= ? AND i.data_emissao < ?
            GROUP BY COALESCE(v.nome, 'Sem vendedor')
            ORDER BY valor DESC, vendedor ASC
            LIMIT 10
        """, (ini, fim)).fetchall()
        if rows:
            linhas = [f"- **{r['vendedor']}**: {_fmt_brl(r['valor'])}" for r in rows]
            return f"**{titulo}**\n\n" + "\n".join(linhas)

    if ('mercado' in q and ('resuma' in q or 'resumo' in q or 'como estamos' in q or 'situacao' in q or 'situação' in question.lower())) or ('estudo de mercado' in q):
        s = _market_snapshot(conn, date.today())
        fat_delta = pct_delta(s['fat_ytd_val'], s['fat_ytd_ly_val'])
        orc_delta = pct_delta(s['orc_ytd_qtd'], s['orc_ytd_ly_qtd'])
        conv_delta = s['conv_pct'] - s['conv_pct_ly']
        ticket_delta = pct_delta(s['ticket_ytd'], s['ticket_ytd_ly'])
        direcao = 'queda' if fat_delta < 0 else 'alta'
        return (
            f"**Resumo executivo do mercado / CRM**\n\n"
            f"- Faturamento YTD: **{_fmt_brl(s['fat_ytd_val'])}**, com **{direcao} de {abs(fat_delta):.1f}%** vs {s['ano_anterior']}.\n"
            f"- Pedidos YTD: **{_fmt_brl(s.get('ped_ytd_val', 0))}** em **{s.get('ped_ytd_qtd', 0)}** pedido(s) válidos.\n"
            f"- Orçamentos YTD: **{s['orc_ytd_qtd']}**, variação de **{orc_delta:.1f}%** vs {s['ano_anterior']}.\n"
            f"- Conversão: **{s['conv_pct']:.1f}%** ({conv_delta:+.1f} p.p. vs ano anterior).\n"
            f"- Ticket médio: **{_fmt_brl(s['ticket_ytd'])}** ({ticket_delta:+.1f}% vs ano anterior).\n"
            f"- Churn acumulado: **{s['churn_total']}** clientes/redes que compraram em {s['ano_anterior']} e ainda não voltaram em {s['ano_atual']}.\n"
            f"- Mês atual até hoje: **{_fmt_brl(s['fat_mes_val'])}**. Projeção linear do mês: **{_fmt_brl(s['projecao_mes'])}**."
        )

    if ('pedido' in q or 'pedidos' in q or 'realizado' in q or 'vendas' in q) and mes and ano:
        ini, fim_excl = _periodo_mes(mes, ano)
        fim_date = (datetime.strptime(fim_excl, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        params = [ini, fim_date]
        vendor_filter = ''
        if vendor:
            vendor_filter = 'o.vendor_id = ?'
            params.append(int(vendor['id']))
        row = conn.execute(pedidos_oficiais_summary_sql(vendor_filter=vendor_filter, include_site=True), tuple(params)).fetchone()
        total = float(row['valor'] or 0)
        qtd = int(row['qtd'] or 0)
        ticket = total / qtd if qtd else 0
        if vendor:
            return f"Os pedidos de **{vendor['nome']}** em **{_get_month_name_pt(mes)}/{ano}** somaram **{_fmt_brl(total)}**, com **{qtd}** pedido(s) válidos e ticket médio de **{_fmt_brl(ticket)}**."
        return f"Os pedidos de **{_get_month_name_pt(mes)}/{ano}** somaram **{_fmt_brl(total)}**, com **{qtd}** pedido(s) válidos e ticket médio de **{_fmt_brl(ticket)}**."

    if ('faturamento' in q or 'fat' in q) and mes and ano:
        ini, fim = _periodo_mes(mes, ano)
        params = [ini, fim]
        where_vendor = ''
        if vendor:
            where_vendor = ' AND vendor_id = ?'
            params.append(int(vendor['id']))
        total = float(_sql_scalar(conn, f"SELECT COALESCE(SUM(valor),0) FROM invoices WHERE data_emissao >= ? AND data_emissao < ?{where_vendor}", tuple(params)) or 0)
        qtd = int(_sql_scalar(conn, f"SELECT COUNT(*) FROM invoices WHERE data_emissao >= ? AND data_emissao < ?{where_vendor}", tuple(params)) or 0)
        if vendor:
            return f"O faturamento de **{vendor['nome']}** em **{_get_month_name_pt(mes)}/{ano}** foi **{_fmt_brl(total)}**, com **{qtd}** lançamento(s) de faturamento no período."
        return f"O faturamento de **{_get_month_name_pt(mes)}/{ano}** foi **{_fmt_brl(total)}**, com **{qtd}** lançamento(s) de faturamento no período."

    if ('orcamento' in q or 'orçamento' in q) and mes and ano:
        ini, fim = _periodo_mes(mes, ano)
        params = [ini, fim]
        where_vendor = ''
        if vendor:
            where_vendor = ' AND vendedor_id = ?'
            params.append(int(vendor['id']))
        qtd = int(_sql_scalar(conn, f"SELECT COUNT(*) FROM orcamentos WHERE COALESCE(data_inicio,'') >= ? AND COALESCE(data_inicio,'') < ?{where_vendor}", tuple(params)) or 0)
        total = float(_sql_scalar(conn, f"SELECT COALESCE(SUM(valor_total),0) FROM orcamentos WHERE COALESCE(data_inicio,'') >= ? AND COALESCE(data_inicio,'') < ?{where_vendor}", tuple(params)) or 0)
        if vendor:
            return f"Em **{_get_month_name_pt(mes)}/{ano}**, **{vendor['nome']}** registrou **{qtd}** orçamento(s), somando **{_fmt_brl(total)}**."
        return f"Em **{_get_month_name_pt(mes)}/{ano}**, o CRM registrou **{qtd}** orçamento(s), somando **{_fmt_brl(total)}** em valor orçado."

    if ('ticket medio' in q or 'ticket médio' in q) and mes and ano:
        ini, fim = _periodo_mes(mes, ano)
        params = [ini, fim]
        where_vendor = ''
        if vendor:
            where_vendor = ' AND vendor_id = ?'
            params.append(int(vendor['id']))
        total = float(_sql_scalar(conn, f"SELECT COALESCE(SUM(valor),0) FROM invoices WHERE data_emissao >= ? AND data_emissao < ?{where_vendor}", tuple(params)) or 0)
        qtd = int(_sql_scalar(conn, f"SELECT COUNT(*) FROM invoices WHERE data_emissao >= ? AND data_emissao < ?{where_vendor}", tuple(params)) or 0)
        ticket = (total / qtd) if qtd else 0
        if vendor:
            return f"O ticket médio de **{vendor['nome']}** em **{_get_month_name_pt(mes)}/{ano}** foi **{_fmt_brl(ticket)}**, considerando **{qtd}** venda(s)/nota(s)."
        return f"O ticket médio de **{_get_month_name_pt(mes)}/{ano}** foi **{_fmt_brl(ticket)}**, considerando **{qtd}** venda(s)/nota(s) e faturamento total de **{_fmt_brl(total)}**."

    if 'top perdas' in q or 'maiores perdas' in q or 'clientes perdidos' in q:
        rows = conn.execute("""
            WITH base_ly AS (
                SELECT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave,
                       MAX(COALESCE(NULLIF(TRIM(cir.identity_nome), ''), NULLIF(TRIM(i.rede_nome), ''), NULLIF(TRIM(i.cliente_nome_ref), ''), 'Cliente sem nome')) AS cliente,
                       COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_ly
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
                GROUP BY 1
            ),
            base_cy AS (
                SELECT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave,
                       COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_cy
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
                GROUP BY 1
            )
            SELECT ly.cliente, ly.valor_ly, COALESCE(cy.valor_cy,0) AS valor_cy, (ly.valor_ly - COALESCE(cy.valor_cy,0)) AS perda
            FROM base_ly ly
            LEFT JOIN base_cy cy ON cy.chave = ly.chave
            WHERE COALESCE(cy.valor_cy,0) <= 0
            ORDER BY ly.valor_ly DESC
            LIMIT 10
        """, (f"{date.today().year-1}-01-01", f"{date.today().year-1}-12-31", f"{date.today().year}-01-01", date.today().isoformat())).fetchall()
        if not rows:
            return 'Não encontrei clientes perdidos neste recorte.'
        linhas=[f"- **{r['cliente']}**: {_fmt_brl(r['valor_ly'])} em {date.today().year-1} | {_fmt_brl(r['valor_cy'])} em {date.today().year} | perda {_fmt_brl(r['perda'])}" for r in rows]
        return "**Top clientes perdidos / sem recompra no ano atual**\n\n" + "\n".join(linhas)

    if "faturamento anual" in q or (("faturamento" in q or 'fat' in q) and re.search(r'\b20\d{2}\b', q) and not mes):
        ano_match = re.search(r'\b(20\d{2})\b', q)
        if ano_match:
            ano = int(ano_match.group(1))
            ini = f"{ano}-01-01"
            fim = f"{ano+1}-01-01"
            params = [ini, fim]
            where_vendor = ''
            if vendor:
                where_vendor = ' AND vendor_id = ?'
                params.append(int(vendor['id']))
            total = float(_sql_scalar(conn, f"SELECT COALESCE(SUM(valor),0) FROM invoices WHERE data_emissao >= ? AND data_emissao < ?{where_vendor}", tuple(params)) or 0)
            if vendor:
                return f"O faturamento anual de **{vendor['nome']}** em **{ano}** foi **{_fmt_brl(total)}**."
            return f"O faturamento anual de **{ano}** foi **{_fmt_brl(total)}**."

    if 'nao compraram' in q or 'não compraram' in question.lower() or 'churn' in q:
        ano_atual = date.today().year
        ano_anterior = ano_atual - 1
        total = int(_sql_scalar(conn, """
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT i1.cliente_id
                FROM invoices i1
                WHERE i1.data_emissao >= ? AND i1.data_emissao < ?
                  AND i1.cliente_id IS NOT NULL
                  AND i1.cliente_id NOT IN (
                      SELECT DISTINCT i2.cliente_id
                      FROM invoices i2
                      WHERE i2.data_emissao >= ? AND i2.data_emissao < ?
                        AND i2.cliente_id IS NOT NULL
                  )
            ) t
        """, (f"{ano_anterior}-01-01", f"{ano_atual}-01-01", f"{ano_atual}-01-01", f"{ano_atual+1}-01-01")) or 0)
        return f"Até agora, o CRM identifica **{total}** cliente(s) que compraram em **{ano_anterior}** e ainda **não compraram em {ano_atual}**."

    if 'ultimas importacoes' in q or 'últimas importações' in question.lower() or 'ultimas importações' in q:
        rows = conn.execute("""
            SELECT tipo, arquivo, imported_at, registros
            FROM imports
            ORDER BY imported_at DESC
            LIMIT 8
        """).fetchall()
        if not rows:
            return "Ainda não há importações registradas no sistema."
        linhas = [f"- **{r['tipo']}** | {r['arquivo']} | {r['imported_at']} | registros: {int(r['registros'] or 0)}" for r in rows]
        return "**Últimas importações do CRM**\n\n" + "\n".join(linhas)

    if ('top vendedores' in q or 'ranking de vendedores' in q or 'melhores vendedores' in q) and ('faturamento' in q or 'fat' in q):
        ano_match = re.search(r'\b(20\d{2})\b', q)
        ano_ref = int(ano_match.group(1)) if ano_match else date.today().year
        mes_ref, ano_mes = _parse_mes_ano(question)
        if mes_ref and ano_mes:
            ini, fim = _periodo_mes(mes_ref, ano_mes)
            titulo = f"Top vendedores por faturamento em {_get_month_name_pt(mes_ref)}/{ano_mes}"
        else:
            ini, fim = f"{ano_ref}-01-01", f"{ano_ref+1}-01-01"
            titulo = f"Top vendedores por faturamento em {ano_ref}"
        rows = conn.execute("""
            SELECT COALESCE(v.nome, 'Sem vendedor') AS vendedor, COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor
            FROM invoices i
            LEFT JOIN vendors v ON v.id = i.vendor_id
            WHERE i.data_emissao >= ? AND i.data_emissao < ?
            GROUP BY COALESCE(v.nome, 'Sem vendedor')
            ORDER BY valor DESC, vendedor ASC
            LIMIT 10
        """, (ini, fim)).fetchall()
        if rows:
            linhas = [f"- **{r['vendedor']}**: {_fmt_brl(r['valor'])}" for r in rows]
            return f"**{titulo}**\n\n" + "\n".join(linhas)

    if ('cliente' in q and ('faturamento' in q or 'historico' in q or 'histórico' in question.lower())):
        nome_match = re.search(r'cliente\s+(.+)$', question, re.I)
        if nome_match:
            nome = nome_match.group(1).strip()
            row = conn.execute("""
                SELECT COALESCE(c.fantasia, c.nome, 'Cliente sem nome') AS cliente,
                       COUNT(i.id) AS qtd,
                       COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor,
                       MAX(i.data_emissao) AS ultima_compra
                FROM invoices i
                JOIN customers c ON c.id = i.cliente_id
                WHERE UPPER(COALESCE(c.fantasia, c.nome)) LIKE UPPER(?)
                GROUP BY COALESCE(c.fantasia, c.nome, 'Cliente sem nome')
                ORDER BY valor DESC, ultima_compra DESC
                LIMIT 1
            """, (f"%{nome}%",)).fetchone()
            if row:
                return (
                    f"O cliente **{row['cliente']}** soma **{_fmt_brl(row['valor'])}** em faturamento histórico, "
                    f"com **{int(row['qtd'] or 0)}** nota(s) e última compra em **{row['ultima_compra'] or '-'}**."
                )

    if ('tarefas' in q or 'kanban' in q) and ('abertas' in q or 'pendentes' in q or 'atrasadas' in q):
        where_vendor = ''
        params = []
        if vendor:
            where_vendor = ' AND t.vendor_id = ?'
            params.append(int(vendor['id']))
        row = conn.execute(f"""
            SELECT COUNT(*) AS qtd
            FROM tasks t
            WHERE t.status IN ('ABERTA', 'EM CONTATO', 'AGUARDANDO RESPOSTA', 'NEGOCIANDO'){where_vendor}
        """, tuple(params)).fetchone()
        qtd = int(row['qtd'] or 0) if row else 0
        if vendor:
            return f"**{vendor['nome']}** tem **{qtd}** tarefa(s) aberta(s)/pendente(s) no Kanban."
        return f"O CRM tem **{qtd}** tarefa(s) aberta(s)/pendente(s) no Kanban."

    if 'ultimo responsavel' in q or 'último responsável' in question.lower():
        nome_match = re.search(r'cliente\s+(.+)$', question, re.I)
        if nome_match:
            nome = nome_match.group(1).strip()
            row = conn.execute("""
                SELECT COALESCE(c.fantasia, c.nome) AS cliente_nome, v.nome AS vendedor_nome, MAX(o.data_emissao) AS ultima_data
                FROM orders o
                JOIN customers c ON c.id = o.cliente_id
                LEFT JOIN vendors v ON v.id = o.vendor_id
                WHERE UPPER(COALESCE(c.fantasia, c.nome)) LIKE UPPER(?)
                GROUP BY c.id, COALESCE(c.fantasia, c.nome), v.nome
                ORDER BY ultima_data DESC
                LIMIT 1
            """, (f"%{nome}%",)).fetchone()
            if row:
                vendedor = row['vendedor_nome'] or 'Sem responsável vinculado'
                return f"O último responsável encontrado para **{row['cliente_nome']}** é **{vendedor}**, com movimentação mais recente em **{row['ultima_data'] or '-'}**."

    return None


CRM_AI_PROVIDER_DEFAULT = "gemini"
CRM_AI_GEMINI_MODEL_DEFAULT = "gemini-3.5-flash-lite"
CRM_AI_OPENAI_MODEL_DEFAULT = "gpt-5.6-luna"
CRM_AI_MODEL_DEFAULT = CRM_AI_GEMINI_MODEL_DEFAULT

CRM_ASSISTANT_MANUAL = r"""
Você é o assistente interno do CRM NPD Embalagens. Este CRM usa dados exportados do Sisplan como fonte principal.

MAPA DO SISTEMA
- Dashboard: visão executiva de pedidos, faturamento, metas, evolução, pipeline e alertas.
- Oportunidades: visão automática dos orçamentos do Sisplan. Não existe cadastro manual de lead; a tela transforma ORÇAMENTO em oportunidade comercial sem duplicar a fonte de dados.
- Funil: visão somente leitura derivada dos status do Sisplan. Alterações de etapa são feitas no Sisplan e aparecem no CRM após a atualização dos CSVs.
- Clientes: carteira consolidada por cliente/rede/matriz, score, prioridade e acesso ao Perfil 360.
- Tarefas: acompanhamento comercial por fase. Permite contato, observação, histórico, transferência por identidade e filtro de atraso de recompra. O filtro Ignorar JIT remove casos programados que não devem entrar no acompanhamento normal.
- Vendedores / Minha Visão: desempenho individual, meta, pedidos, faturamento, comissão quando permitido, clientes e orçamentos.
- Orçamentos: análise dos orçamentos importados do Sisplan, fases, valores, conversões, regiões e pedido vinculado.
- Mercado: análise agregada, churn, concentração, evolução, ticket e indicadores comerciais.
- Diagnóstico: auditoria para investigar divergências, carteira, tarefas e regras de negócio.
- Site: visão específica do canal SITE.
- Medidas: pesquisa formatos/medidas e clientes que compram cada formato.
- Atualizar Sisplan: importa os quatro CSVs (PEDIDOS, FATURAMENTO, ORÇAMENTO e LIQUIDEZ). Na importação inteligente, o usuário seleciona todos os arquivos de uma vez, o CRM detecta o tipo e processa em lote.
- Importações: histórico dos lotes, quantidade de linhas, registros, novos, atualizados, duplicados, pendências e exclusão de lote.
- Usuários: administração de acessos e vínculo de vendedor. Restrito a perfis autorizados.
- Metas/Configurações: metas mensais e fechamento do ciclo de faturamento.
- IA do CRM: copiloto conectado a um provedor externo quando configurado (Gemini Free Tier ou OpenAI). Tira dúvidas livres sobre menus, regras, fluxo de trabalho e treinamento de novos vendedores; para números, preserva as consultas calculadas pelo próprio CRM e as permissões do usuário.

REGRAS IMPORTANTES
- A atribuição comercial usa RESPONSÁVEL/NOME_RESPONSÁVEL como regra principal. REPRESENTANTE só é usado em exceções do canal SITE.
- Pedidos JIT/matriz precisam ser tratados para não duplicar meta, faturamento ou comissão. Filial IMEDIATA pode contar conforme a regra comercial já implementada.
- Transferências manuais de carteira, tarefas e vínculos de matriz/filial devem persistir mesmo após nova importação.
- Cliente novo vem do pedido no Sisplan (MOTIVO 10 — PEDIDO PRIMEIRA COMPRA). JIT IMEDIATO anula. Overrides auditados quando aplicável.
- Vendedor comum só pode consultar a própria carteira/desempenho. Gestor/admin podem consultar visão ampliada.
- Pedidos, faturamento, orçamento e liquidez são dados do Sisplan; não crie números ou fatos que não estejam no contexto fornecido.
- Oportunidades e Funil NÃO são uma base paralela: são leituras do relatório ORÇAMENTO do Sisplan. Nunca instrua o vendedor a cadastrar lead manualmente.

COMO ORIENTAR UM NOVO VENDEDOR
- Responda como um instrutor de sistema: diga em qual menu entrar, o que procurar e qual ação executar.
- Se houver mais de um caminho, comece pelo mais simples.
- Explique termos do CRM em linguagem comercial, sem jargão técnico de programação.
- Se a pergunta envolver uma permissão que o vendedor não possui, explique que a ação precisa de gestor/admin e ofereça o caminho que o vendedor pode usar.
- Se não houver informação suficiente, diga o que falta em vez de inventar.
"""


def _crm_ai_provider(conn) -> str:
    provider = (os.environ.get("CRM_NPD_AI_PROVIDER", "").strip() or get_setting(conn, "crm_ai_provider", CRM_AI_PROVIDER_DEFAULT).strip() or CRM_AI_PROVIDER_DEFAULT).lower()
    return provider if provider in {"gemini", "openai"} else CRM_AI_PROVIDER_DEFAULT


def _crm_ai_api_key(conn, provider: str | None = None) -> str:
    provider = (provider or _crm_ai_provider(conn)).lower()
    if provider == "gemini":
        return (os.environ.get("GEMINI_API_KEY", "").strip() or get_setting(conn, "gemini_api_key", "").strip())
    return (os.environ.get("OPENAI_API_KEY", "").strip() or get_setting(conn, "openai_api_key", "").strip())


def _crm_ai_model(conn, provider: str | None = None) -> str:
    provider = (provider or _crm_ai_provider(conn)).lower()
    if provider == "gemini":
        default = CRM_AI_GEMINI_MODEL_DEFAULT
        env = os.environ.get("CRM_NPD_GEMINI_MODEL", "").strip()
        saved = get_setting(conn, "gemini_model", default).strip()
        # Migração transparente: modelos 2.5 deixaram de estar disponíveis para
        # novas chaves. Não deixa uma configuração antiga prender o CRM num 404.
        if saved in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            saved = default
    else:
        default = CRM_AI_OPENAI_MODEL_DEFAULT
        env = os.environ.get("CRM_NPD_AI_MODEL", "").strip()
        saved = get_setting(conn, "crm_ai_model", default).strip()
    return env or saved or default


def _crm_ai_base_url(conn) -> str:
    """Usado apenas pelo provedor OpenAI/compatível."""
    return (os.environ.get("OPENAI_BASE_URL", "").strip() or get_setting(conn, "openai_base_url", "").strip())


def _crm_ai_client(conn):
    if OpenAI is None:
        return None
    key = _crm_ai_api_key(conn, "openai")
    if not key:
        return None
    kwargs = {"api_key": key, "timeout": 45.0, "max_retries": 1}
    base_url = _crm_ai_base_url(conn)
    if base_url:
        kwargs["base_url"] = base_url.rstrip("/") + "/"
    return OpenAI(**kwargs)


def _crm_ai_is_configured(conn) -> bool:
    provider = _crm_ai_provider(conn)
    if provider == "gemini":
        return bool(_crm_ai_api_key(conn, "gemini"))
    return bool(_crm_ai_api_key(conn, "openai") and OpenAI is not None)


def _crm_ai_safe_error(exc, api_key: str = "") -> str:
    """Mensagem útil para o admin, sem vazar credenciais nem headers."""
    raw = str(exc or "Erro desconhecido").strip()
    if api_key:
        raw = raw.replace(api_key, "[CHAVE OCULTA]")
    raw = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer [OCULTO]", raw)
    raw = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}\b", "[CHAVE OCULTA]", raw)
    raw = raw[:650]
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    cls = exc.__class__.__name__.lower() if exc else ""
    if status == 400:
        return f"Requisição recusada (400). Confira modelo/provedor. Detalhe: {raw}"
    if status == 401 or "authentication" in cls:
        return "Autenticação recusada (401). A chave não foi aceita pelo provedor configurado."
    if status == 403 or "permission" in cls:
        return "Acesso recusado (403). A chave existe, mas não tem permissão para este recurso/modelo."
    if status == 404 or "notfound" in cls or "not_found" in cls:
        return f"Recurso/modelo não encontrado (404). Confira o modelo/provedor. Detalhe: {raw}"
    if status == 429 or "ratelimit" in cls or "rate_limit" in cls:
        return "Limite da API atingido (429). Aguarde a cota renovar ou confira os limites do projeto."
    if "connection" in cls or "timeout" in cls or isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return f"Falha de conexão com a API. Confira internet, firewall/proxy e provedor. Detalhe: {raw}"
    return f"Falha da API{f' ({status})' if status else ''}: {raw}"


def _crm_ai_gemini_generate(api_key: str, model: str, instructions: str, input_text: str) -> str:
    """Chamada REST ao Gemini. Tenta o modelo escolhido e fallbacks do Free Tier."""
    requested = (model or CRM_AI_GEMINI_MODEL_DEFAULT).strip()
    candidates = []
    for m in [requested, "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"]:
        if m and m not in candidates:
            candidates.append(m)

    last_error = None
    for candidate in candidates:
        model_q = urllib.parse.quote(candidate, safe="-._")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_q}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": input_text}]}],
            "generationConfig": {"temperature": 0.2}
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            parts = []
            for cand in data.get("candidates") or []:
                content = cand.get("content") or {}
                for part in content.get("parts") or []:
                    if isinstance(part, dict) and part.get("text"):
                        parts.append(str(part.get("text")))
            text_out = "\n".join(parts).strip()
            if text_out:
                return text_out
            feedback = data.get("promptFeedback") or data.get("error") or {}
            raise RuntimeError(f"Gemini respondeu sem texto no modelo {candidate}. {json.dumps(feedback, ensure_ascii=False)[:500]}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:650]
            except Exception:
                detail = str(e)
            err = RuntimeError(detail or str(e))
            setattr(err, "status_code", getattr(e, "code", None))
            last_error = err
            # Modelo não disponível/permitido: tenta os modelos gratuitos de fallback.
            if getattr(e, "code", None) in (400, 403, 404) and candidate != candidates[-1]:
                continue
            raise err
        except Exception as e:
            last_error = e
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Nenhum modelo Gemini disponível para teste.")

def _crm_ai_relevant_kb(conn, question: str, user: dict | None = None, limit: int = 8) -> str:
    level = _ia_user_level(user)
    try:
        seed_ia_knowledge_base(conn)
        rows = conn.execute("""
            SELECT categoria, pergunta, resposta, permissao, palavras_chave
            FROM ia_knowledge
            WHERE COALESCE(ativo,1)=1
        """).fetchall()
    except Exception:
        rows = []
    ranked = []
    qn = norm(question or "").lower()
    for r in rows:
        if not _ia_permission_allows(r["permissao"], level):
            continue
        texto = " ".join([r["pergunta"] or "", r["palavras_chave"] or "", r["categoria"] or ""])
        score = _ia_similarity(qn, texto)
        ranked.append((score, r))
    ranked.sort(key=lambda x: x[0], reverse=True)
    parts = []
    for score, r in ranked[:limit]:
        if score < 0.08 and parts:
            continue
        parts.append(f"[{r['categoria']}] {r['pergunta']}\n{r['resposta']}")
    return "\n\n".join(parts)


def _crm_ai_user_context(conn, user: dict | None) -> dict:
    hoje = date.today()
    level = _ia_user_level(user)
    base = {
        "hoje": hoje.isoformat(),
        "perfil": level,
        "usuario": (user or {}).get("nome"),
    }

    if level in {"admin", "gestor"}:
        try:
            base["mercado"] = _market_snapshot(conn, hoje)
        except Exception:
            pass
        base["clientes_total"] = int(_sql_scalar(conn, "SELECT COUNT(*) FROM customers") or 0)
        base["vendedores_total"] = int(_sql_scalar(conn, "SELECT COUNT(*) FROM vendors WHERE COALESCE(ativo,1)=1") or 0)
        base["tarefas_abertas"] = int(_sql_scalar(conn, "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('CONCLUÍDA','CANCELADA')") or 0)
        # Entrega à IA os números reais da tela Performance do mês atual para que
        # ela responda a pergunta do gestor, em vez de apenas mandar procurar a tela.
        try:
            pctx = _performance_build_context(conn, hoje.strftime("%Y-%m"), include_customer_mix=True)
            prows = [r for r in pctx["rows"] if int(r.get("id") or 0) > 0 and (r.get("tipo") or "").upper() in ("INTERNO", "EXTERNO")]
            prows.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
            base["performance_mes_atual"] = [
                {
                    "vendedor": r.get("nome"), "tipo": r.get("tipo"), "score": float(r.get("score") or 0),
                    "orcamentos": int(r.get("orc_qtd") or 0), "valor_orcado": float(r.get("orc_valor") or 0),
                    "conversao_pct": float(r.get("conv_pct") or 0), "pedidos": int(r.get("ped_qtd") or 0),
                    "faturado": float(r.get("fat_valor") or 0), "clientes_novos": int(r.get("clientes_novos") or 0),
                    "clientes_recorrentes": int(r.get("clientes_repeticao") or 0),
                    "clientes_recuperados": int(r.get("clientes_recuperados") or 0),
                }
                for r in prows[:20]
            ]
        except Exception as e:
            base["performance_context_error"] = str(e)[:180]
    else:
        vid = get_user_vendor_id(conn, user)
        base["vendor_id"] = vid
        if vid:
            v = conn.execute("SELECT nome FROM vendors WHERE id=?", (vid,)).fetchone()
            base["vendedor"] = v["nome"] if v else None
            mes_ini = hoje.replace(day=1).isoformat()
            prox = (hoje.replace(day=28) + timedelta(days=4)).replace(day=1).isoformat()
            p = conn.execute("""
                SELECT COUNT(DISTINCT TRIM(COALESCE(pedido,''))) AS qtd,
                       COALESCE(SUM(COALESCE(NULLIF(valor_item_ref,0), valor,0)),0) AS valor
                FROM orders
                WHERE vendor_id=? AND data_emissao>=? AND data_emissao<?
            """, (vid, mes_ini, prox)).fetchone()
            f = conn.execute("""
                SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor
                FROM invoices
                WHERE vendor_id=? AND data_emissao>=? AND data_emissao<?
            """, (vid, mes_ini, prox)).fetchone()
            t = conn.execute("""
                SELECT COUNT(*) AS qtd FROM tasks
                WHERE vendor_id=? AND status NOT IN ('CONCLUÍDA','CANCELADA')
            """, (vid,)).fetchone()
            base["mes_atual"] = {
                "pedidos_qtd": int(p["qtd"] or 0),
                "pedidos_valor": float(p["valor"] or 0),
                "faturamento_qtd": int(f["qtd"] or 0),
                "faturamento_valor": float(f["valor"] or 0),
                "tarefas_abertas": int(t["qtd"] or 0),
            }

    try:
        base["ultimas_importacoes"] = [dict(r) for r in conn.execute("""
            SELECT tipo, arquivo, imported_at, registros
            FROM imports ORDER BY imported_at DESC LIMIT 4
        """).fetchall()]
    except Exception:
        pass
    return base


def _crm_ai_call(conn, question: str, user: dict | None = None, authoritative_answer: str | None = None, rule_answer: str | None = None):
    provider = _crm_ai_provider(conn)
    api_key = _crm_ai_api_key(conn, provider)
    if not api_key:
        return None, f"Nenhuma API Key foi configurada para {provider.upper()}."
    if provider == "openai" and OpenAI is None:
        return None, "A biblioteca OpenAI não está instalada/atualizada neste computador."
    try:
        model = _crm_ai_model(conn, provider)
        contexto = _crm_ai_user_context(conn, user)
        kb = _crm_ai_relevant_kb(conn, question, user=user)

        instructions = (
            "Você é a IA interna do CRM NPD Embalagens. Responda em português do Brasil. "
            "Seu objetivo principal é treinar e apoiar vendedores no uso do CRM e responder análises comerciais dentro das permissões. "
            "Use SOMENTE o manual, a base de conhecimento e os dados de contexto fornecidos. Não invente números, telas, botões ou regras. "
            "Quando houver uma resposta autoritativa calculada pelo CRM, preserve todos os números, datas e nomes exatamente como recebidos; você pode apenas explicar melhor. "
            "REGRA OBRIGATÓRIA: quando a pergunta pedir um número, nome, ranking, comparação ou diagnóstico e esse dado estiver no contexto/resposta autoritativa, responda PRIMEIRO com a resposta objetiva. Só depois diga onde conferir na tela. Nunca responda apenas 'acesse a tela' quando o CRM já forneceu o dado. "
            "Para dúvidas operacionais, responda com passos curtos e práticos. Para análise, dê leitura e próxima ação. "
            "Nunca revele API key, senha, dados de outro vendedor a usuário vendedor ou detalhes técnicos internos desnecessários."
        )
        input_text = (
            f"MANUAL INTERNO:\n{CRM_ASSISTANT_MANUAL}\n\n"
            f"BASE DE CONHECIMENTO RELEVANTE:\n{kb or 'Sem trecho específico.'}\n\n"
            f"CONTEXTO PERMITIDO DO USUÁRIO:\n{json.dumps(contexto, ensure_ascii=False, default=str)}\n\n"
            f"RESPOSTA AUTORITATIVA DO CRM (se houver):\n{authoritative_answer or 'Nenhuma.'}\n\n"
            f"REGRA/EXPLICAÇÃO LOCAL (se houver):\n{rule_answer or 'Nenhuma.'}\n\n"
            f"PERGUNTA DO USUÁRIO:\n{question}"
        )

        if provider == "gemini":
            txt = _crm_ai_gemini_generate(api_key, model, instructions, input_text)
            return txt.strip(), None

        client = _crm_ai_client(conn)
        if client is None:
            return None, "Cliente OpenAI indisponível. Confira a biblioteca e a chave."
        try:
            resp = client.responses.create(
                model=model,
                instructions=instructions,
                input=input_text,
            )
            txt = getattr(resp, "output_text", "") or ""
            if txt.strip():
                return txt.strip(), None
            raise RuntimeError("A API respondeu sem texto pela Responses API.")
        except Exception:
            if not _crm_ai_base_url(conn):
                raise
            chat = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": input_text},
                ],
                temperature=0.2,
            )
            txt = ""
            try:
                txt = chat.choices[0].message.content or ""
            except Exception:
                txt = ""
            if txt.strip():
                return txt.strip(), None
            return None, "O endpoint respondeu sem texto nas APIs Responses e Chat Completions."
    except Exception as e:
        err = _crm_ai_safe_error(e, api_key)
        try:
            print(f"[CRM][IA][{provider}] {err}")
        except Exception:
            pass
        return None, err


def _try_openai_response(conn, question: str, user: dict | None = None, authoritative_answer: str | None = None, rule_answer: str | None = None) -> str | None:
    texto, _erro = _crm_ai_call(conn, question, user=user, authoritative_answer=authoritative_answer, rule_answer=rule_answer)
    return texto


def responder_ia_crm(question: str, user: dict | None = None) -> str:
    question = (question or "").strip()
    if not question:
        return "Digite uma pergunta para eu analisar o CRM."

    with conectar() as conn:
        try:
            seed_ia_knowledge_base(conn)
            conn.commit()
        except Exception:
            pass

        bloqueio = _ia_security_block(question, user)
        if bloqueio:
            _ia_log(conn, user, question, bloqueio, "seguranca")
            conn.commit()
            return bloqueio

        # Restrições adicionais para vendedor quando citar explicitamente outro vendedor.
        vendedor_restrito = bool(user and user.get("role") not in ["admin", "gestor"])
        if vendedor_restrito:
            my_vid = get_user_vendor_id(conn, user)
            mentioned = _find_vendor_by_question(conn, question)
            if mentioned and (not my_vid or int(mentioned["id"]) != int(my_vid)):
                resposta = "Por segurança, vendedores só podem consultar dados da própria carteira, clientes, tarefas e desempenho. Para dados de outro vendedor, peça a um gestor ou administrador."
                _ia_log(conn, user, question, resposta, "seguranca_vendedor")
                conn.commit()
                return resposta
            qn = norm(question)
            if any(t in qn for t in ["top vendedores", "ranking de vendedores", "melhores vendedores", "comissao da", "comissão da"]):
                resposta = "Por segurança, essa consulta é restrita a gestor/administrador. Posso te ajudar com sua própria meta, clientes, tarefas, orçamentos e desempenho."
                _ia_log(conn, user, question, resposta, "seguranca_vendedor")
                conn.commit()
                return resposta

        question_consulta = question
        if vendedor_restrito:
            my_vid = get_user_vendor_id(conn, user)
            if my_vid and my_vid > 0:
                vrow = conn.execute("SELECT nome FROM vendors WHERE id=?", (my_vid,)).fetchone()
                if vrow and vrow["nome"] and vrow["nome"] not in question_consulta:
                    question_consulta = question_consulta + " " + vrow["nome"]

        # Calculadoras locais continuam sendo a fonte de verdade para números.
        dado = _answer_data_question(conn, question_consulta, user=user)
        explicacao = _system_explanation(question)
        kb = _answer_knowledge_base(conn, question, user=user)

        # Com chave configurada, a IA real recebe o manual + contexto + respostas autoritativas locais.
        # Se a API falhar, o erro fica visível para o ADMIN em vez de ser mascarado como "sem chave".
        ai_error = None
        provider = _crm_ai_provider(conn)
        if _crm_ai_is_configured(conn):
            resposta_ai, ai_error = _crm_ai_call(
                conn,
                question,
                user=user,
                authoritative_answer=dado,
                rule_answer=explicacao or kb,
            )
            if resposta_ai:
                set_setting(conn, "crm_ai_last_error", "")
                set_setting(conn, "crm_ai_last_ok", now_iso())
                set_setting(conn, "crm_ai_last_model", _crm_ai_model(conn, provider))
                _ia_log(conn, user, question, resposta_ai, provider)
                conn.commit()
                return resposta_ai
            if ai_error:
                set_setting(conn, "crm_ai_last_error", ai_error)

        def _with_ai_status(texto):
            if ai_error and user and user.get("role") == "admin":
                return f"⚠️ IA externa configurada, mas a chamada falhou: {ai_error}\n\n{texto}"
            return texto

        # Fallback offline mantém o CRM útil mesmo sem internet/API.
        if dado:
            resposta = _with_ai_status(dado)
            _ia_log(conn, user, question, resposta, "sql_local")
            conn.commit()
            return resposta
        if kb:
            resposta = _with_ai_status(kb)
            _ia_log(conn, user, question, resposta, "base_conhecimento")
            conn.commit()
            return resposta
        if explicacao:
            resposta = _with_ai_status(explicacao)
            _ia_log(conn, user, question, resposta, "regras_locais")
            conn.commit()
            return resposta

        if ai_error:
            resposta = _with_ai_status("Enquanto a API não responde, o CRM continua disponível no modo local para consultas que eu consiga calcular com segurança.")
        elif _crm_ai_is_configured(conn):
            resposta = "A IA está configurada, mas não retornou texto. Use Configurar IA → Testar conexão para ver o diagnóstico."
        else:
            resposta = "A IA externa ainda não está configurada. O modo local continua respondendo somente o que está cadastrado e o que o CRM consegue calcular diretamente no banco."
        _ia_log(conn, user, question, resposta, "fallback")
        conn.commit()
        return resposta


@app.route('/ia/configuracao', methods=['GET', 'POST'])
@login_required
def ia_configuracao():
    user = get_current_user()
    if not user or user.get('role') != 'admin':
        flash('A configuração da IA é restrita ao administrador.', 'warning')
        return redirect(url_for('ia_assistente'))

    with conectar() as conn:
        if request.method == 'POST':
            acao = (request.form.get('acao') or 'salvar').strip()
            provider_form = (request.form.get('crm_ai_provider') or _crm_ai_provider(conn) or CRM_AI_PROVIDER_DEFAULT).strip().lower()
            if provider_form not in {'gemini', 'openai'}:
                provider_form = CRM_AI_PROVIDER_DEFAULT

            if acao == 'remover':
                if provider_form == 'gemini':
                    set_setting(conn, 'gemini_api_key', '')
                else:
                    set_setting(conn, 'openai_api_key', '')
                conn.commit()
                flash(f'Chave da IA ({provider_form.upper()}) removida do CRM.', 'success')
                return redirect(url_for('ia_configuracao'))

            if acao == 'testar':
                set_setting(conn, 'crm_ai_provider', provider_form)
                conn.commit()
                if not _crm_ai_api_key(conn, provider_form):
                    flash('Salve uma API Key antes de testar.', 'warning')
                elif provider_form == 'openai' and OpenAI is None:
                    flash('A biblioteca OpenAI deste computador está ausente ou desatualizada. Execute ATUALIZAR_DEPENDENCIAS.bat e teste novamente.', 'warning')
                else:
                    teste, erro = _crm_ai_call(conn, 'Teste de conexão. Responda somente: CONEXAO OK', user=user)
                    if teste:
                        set_setting(conn, 'crm_ai_last_error', '')
                        set_setting(conn, 'crm_ai_last_ok', now_iso())
                        set_setting(conn, 'crm_ai_last_model', _crm_ai_model(conn, provider_form))
                        flash(f'Conexão com a IA testada com sucesso: {provider_form.upper()} · {_crm_ai_model(conn, provider_form)}.', 'success')
                    else:
                        set_setting(conn, 'crm_ai_last_error', erro or 'Não foi possível obter resposta da API.')
                        flash(erro or 'Não foi possível obter resposta da API.', 'danger')
                    conn.commit()
                return redirect(url_for('ia_configuracao'))

            key = (request.form.get('api_key') or '').strip()
            provider = provider_form
            set_setting(conn, 'crm_ai_provider', provider)
            if provider == 'gemini':
                model = (request.form.get('gemini_model') or CRM_AI_GEMINI_MODEL_DEFAULT).strip()
                if key:
                    set_setting(conn, 'gemini_api_key', key)
                set_setting(conn, 'gemini_model', model or CRM_AI_GEMINI_MODEL_DEFAULT)
                set_setting(conn, 'crm_ai_last_error', '')
                set_setting(conn, 'crm_ai_last_ok', '')
            else:
                model = (request.form.get('crm_ai_model') or CRM_AI_OPENAI_MODEL_DEFAULT).strip()
                base_url = (request.form.get('openai_base_url') or '').strip()
                if key:
                    set_setting(conn, 'openai_api_key', key)
                set_setting(conn, 'crm_ai_model', model or CRM_AI_OPENAI_MODEL_DEFAULT)
                set_setting(conn, 'openai_base_url', base_url)
            conn.commit()
            flash('Configuração da IA salva. Use “Testar conexão” para validar.', 'success')
            return redirect(url_for('ia_configuracao'))

        provider = _crm_ai_provider(conn)
        current_key = _crm_ai_api_key(conn, provider)
        model = _crm_ai_model(conn, provider)
        base_url = _crm_ai_base_url(conn)
        sdk_ok = OpenAI is not None
        has_key = bool(current_key)
        configured = _crm_ai_is_configured(conn)
        masked = ('••••••••' + current_key[-4:]) if len(current_key) >= 4 else ('configurada' if current_key else '')

    body = render_template_string(r"""
    <div class="row justify-content-center">
      <div class="col-12 col-xl-8">
        <div class="d-flex justify-content-between align-items-center mb-3">
          <div><div class="h3 mb-1">Configuração da IA</div><div class="text-secondary">Gemini Free Tier é o padrão desta versão. OpenAI continua disponível como alternativa.</div></div>
          <a class="btn btn-outline-secondary" href="{{ url_for('ia_assistente') }}">Voltar</a>
        </div>
        <div class="cardx p-4">
          <div class="alert {{ 'alert-success' if configured else 'alert-warning' }}">
            <b>{% if configured %}IA configurada · {{ provider|upper }}{% else %}IA externa ainda não configurada{% endif %}</b><br>
            <span class="small">{% if has_key %}Chave atual: {{ masked }} · modelo {{ model }}{% else %}Sem chave. O modo local continua funcionando para consultas conhecidas.{% endif %}</span>
            {% if provider=='openai' and has_key and not sdk_ok %}<div class="small mt-1">Execute <b>ATUALIZAR_DEPENDENCIAS.bat</b> para instalar/atualizar a biblioteca OpenAI.</div>{% endif %}
          </div>

          <div class="alert alert-info border-0 small">
            <b>Opção gratuita recomendada:</b> Google Gemini Free Tier. Crie uma chave no Google AI Studio, escolha <b>Gemini (grátis)</b>, cole a chave abaixo e clique em <b>Testar conexão</b>. O CRM usa chamada REST direta, então não precisa instalar SDK do Google.
          </div>

          <form method="post" id="aiConfigForm">
            <input type="hidden" name="acao" value="salvar">
            <div class="mb-3">
              <label class="form-label fw-bold">Provedor</label>
              <select class="form-select" name="crm_ai_provider" id="aiProvider">
                <option value="gemini" {% if provider=='gemini' %}selected{% endif %}>Gemini · Free Tier</option>
                <option value="openai" {% if provider=='openai' %}selected{% endif %}>OpenAI</option>
              </select>
            </div>
            <div class="mb-3">
              <label class="form-label fw-bold">API Key</label>
              <input type="password" class="form-control" name="api_key" autocomplete="new-password" placeholder="Cole uma nova chave apenas se quiser substituir a atual">
              <div class="form-text">A chave fica somente no banco local do CRM e nunca volta a ser exibida inteira.</div>
            </div>
            <div id="geminiFields" class="row g-3" style="display:{{ 'flex' if provider=='gemini' else 'none' }}">
              <div class="col-12"><label class="form-label fw-bold">Modelo Gemini</label><input class="form-control" name="gemini_model" value="{{ model if provider=='gemini' else gemini_default }}" list="gemini-models"><datalist id="gemini-models"><option value="gemini-3.5-flash-lite"><option value="gemini-3.5-flash"><option value="gemini-3.6-flash"></datalist><div class="form-text">Padrão gratuito: {{ gemini_default }}. Se um modelo antigo estiver salvo, o CRM migra automaticamente e tenta modelos atuais compatíveis.</div></div>
            </div>
            <div id="openaiFields" class="row g-3" style="display:{{ 'flex' if provider=='openai' else 'none' }}">
              <div class="col-md-6"><label class="form-label fw-bold">Modelo OpenAI</label><input class="form-control" name="crm_ai_model" value="{{ model if provider=='openai' else openai_default }}" list="crm-ai-models"><datalist id="crm-ai-models"><option value="gpt-5.6-luna"><option value="gpt-5.6-terra"><option value="gpt-5.6-sol"></datalist></div>
              <div class="col-md-6"><label class="form-label fw-bold">Endpoint / Base URL <span class="fw-normal text-muted">(opcional)</span></label><input class="form-control" name="openai_base_url" value="{{ base_url }}" placeholder="Deixe vazio para OpenAI oficial"></div>
            </div>
            <button class="btn btn-primary mt-3"><i class="bi bi-stars me-1"></i>Salvar configuração</button>
          </form>
          {% if has_key %}
          <div class="d-flex gap-2 mt-3 flex-wrap">
            <form method="post" class="m-0"><input type="hidden" name="acao" value="testar"><input type="hidden" name="crm_ai_provider" value="{{ provider }}"><button class="btn btn-outline-primary"><i class="bi bi-wifi me-1"></i>Testar conexão</button></form>
            <form method="post" class="m-0" onsubmit="return confirm('Remover a chave da IA deste CRM?');"><input type="hidden" name="acao" value="remover"><input type="hidden" name="crm_ai_provider" value="{{ provider }}"><button class="btn btn-outline-danger">Remover chave</button></form>
          </div>
          {% endif %}
        </div>
      </div>
    </div>
    <script>
      (function(){
        const p=document.getElementById('aiProvider'), g=document.getElementById('geminiFields'), o=document.getElementById('openaiFields');
        function sync(){const isG=p && p.value==='gemini'; if(g)g.style.display=isG?'flex':'none'; if(o)o.style.display=isG?'none':'flex';}
        p?.addEventListener('change',sync); sync();
      })();
    </script>
    """, configured=configured, has_key=has_key, sdk_ok=sdk_ok, masked=masked, model=model, base_url=base_url, provider=provider,
       gemini_default=CRM_AI_GEMINI_MODEL_DEFAULT, openai_default=CRM_AI_OPENAI_MODEL_DEFAULT)
    return render_page('Configuração da IA', body, 'ia_assistente')


@app.route('/ia', methods=['GET', 'POST'])
@login_required
def ia_assistente():
    historico = session.get('ia_chat_history', [])
    user = get_current_user()

    if request.method == 'POST':
        if request.form.get('acao') == 'limpar':
            session['ia_chat_history'] = []
            return redirect(url_for('ia_assistente'))

        pergunta = (request.form.get('pergunta') or '').strip()
        if pergunta:
            resposta = responder_ia_crm(pergunta, user=user)
            historico = historico + [{
                'pergunta': pergunta,
                'resposta': resposta,
                'quando': now_iso(),
            }]
            historico = historico[-20:]
            session['ia_chat_history'] = historico
        else:
            flash('Digite uma pergunta para a IA.', 'warning')
        return redirect(url_for('ia_assistente'))

    with conectar() as conn:
        configured = _crm_ai_is_configured(conn)
        provider = _crm_ai_provider(conn)
        model = _crm_ai_model(conn, provider)
        ai_last_error = get_setting(conn, "crm_ai_last_error", "").strip()
        ai_last_ok = get_setting(conn, "crm_ai_last_ok", "").strip()
        ai_connected = bool(configured and ai_last_ok and not ai_last_error)

    body = render_template_string(r"""
    <style>
      .ai-hero{background:linear-gradient(135deg,#0d1b2f 0%,#13345b 58%,#0c6d86 100%);color:white;border-radius:20px;padding:22px 24px;box-shadow:0 18px 45px rgba(17,50,84,.18)}
      .ai-status{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:.38rem .7rem;font-size:.75rem;font-weight:800;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.16)}
      .ai-status-dot{width:8px;height:8px;border-radius:50%;background:{{ '#36e38a' if ai_connected else ('#ff5b6e' if configured and ai_last_error else '#ffc857') }}}
      .prompt-chip{border:1px solid #dce6f4;background:#f8fbff;border-radius:12px;padding:10px 12px;font-size:.82rem;cursor:pointer;transition:.15s}.prompt-chip:hover{border-color:#6b9bff;background:#f0f6ff}
      .chat-answer{white-space:pre-line;line-height:1.55}.ai-input{min-height:110px;border-radius:14px}
    </style>
    <div class="ai-hero mb-3 d-flex justify-content-between align-items-start flex-wrap gap-3">
      <div>
        <div class="d-flex align-items-center gap-2 mb-2"><i class="bi bi-stars fs-4"></i><span class="ai-status"><span class="ai-status-dot"></span>{{ ('IA ' ~ provider|upper ~ ' conectada') if ai_connected else (('IA ' ~ provider|upper ~ ' · erro de conexão') if configured and ai_last_error else (('IA ' ~ provider|upper ~ ' configurada') if configured else 'Modo local')) }}</span></div>
        <div class="h3 mb-1 text-white">Copiloto do CRM</div>
        <div style="color:#d8e7f7;max-width:820px">Pergunte como usar qualquer tela, onde encontrar uma informação, como funciona uma regra comercial ou peça uma análise dos dados que o seu perfil pode consultar.</div>
      </div>
      <div class="d-flex gap-2">
        {% if user and user['role']=='admin' %}<a class="btn btn-light btn-sm" href="{{ url_for('ia_configuracao') }}"><i class="bi bi-gear me-1"></i> Configurar IA</a>{% endif %}
        <form method="post" class="m-0"><input type="hidden" name="acao" value="limpar"><button class="btn btn-outline-light btn-sm">Limpar conversa</button></form>
      </div>
    </div>

    {% if not configured %}
    <div class="alert alert-warning border-0 shadow-sm">A IA externa ainda não está configurada. O CRM continua respondendo perguntas conhecidas no modo local. Um administrador pode conectar o Gemini Free Tier ou a OpenAI em <b>Configurar IA</b>.</div>
    {% elif ai_last_error %}
    <div class="alert alert-danger border-0 shadow-sm"><b>A chave está configurada, mas a última chamada da IA falhou.</b><br><span class="small">{{ ai_last_error }}</span><div class="mt-2"><a class="btn btn-sm btn-outline-danger" href="{{ url_for('ia_configuracao') }}">Abrir configuração / testar conexão</a></div></div>
    {% elif not ai_connected %}
    <div class="alert alert-info border-0 shadow-sm">A chave está salva. Clique em <b>Configurar IA → Testar conexão</b> uma vez para validar a comunicação com o provedor.</div>
    {% endif %}

    <div class="row g-3">
      <div class="col-lg-4">
        <div class="cardx p-3 h-100">
          <div class="fw-bold mb-1">Pergunte como um vendedor perguntaria</div>
          <div class="small text-secondary mb-3">Clique em um exemplo para preencher a pergunta.</div>
          <div class="d-grid gap-2">
            <div class="prompt-chip" onclick="fillAi(this)">Como eu vejo quais clientes estão atrasados na recompra?</div>
            <div class="prompt-chip" onclick="fillAi(this)">O que significa JIT e quando devo ignorar?</div>
            <div class="prompt-chip" onclick="fillAi(this)">Como eu acompanho um orçamento que ainda não virou pedido?</div>
            <div class="prompt-chip" onclick="fillAi(this)">Como uso a tela de tarefas no dia a dia?</div>
            <div class="prompt-chip" onclick="fillAi(this)">Qual foi meu faturamento neste mês?</div>
            <div class="prompt-chip" onclick="fillAi(this)">Me explique o CRM como se fosse meu primeiro dia.</div>
          </div>
          <hr>
          <div class="small text-secondary"><b>Modelo:</b> {{ model }}<br>As permissões do usuário continuam valendo dentro da IA.</div>
        </div>
      </div>

      <div class="col-lg-8">
        <div class="cardx p-3 mb-3">
          <form method="post" id="aiForm">
            <label class="form-label fw-semibold">Sua pergunta</label>
            <textarea id="aiQuestion" name="pergunta" class="form-control ai-input" rows="4" placeholder="Ex.: sou novo aqui. Como eu sei quais clientes devo ligar primeiro hoje?"></textarea>
            <div class="mt-3 d-flex justify-content-between align-items-center gap-2 flex-wrap">
              <span class="small text-secondary">A IA usa o manual interno, regras do CRM e somente os dados permitidos para o seu perfil.</span>
              <button class="btn btn-primary px-4"><i class="bi bi-stars me-1"></i> Perguntar à IA</button>
            </div>
          </form>
        </div>

        <div class="cardx p-3">
          <div class="fw-bold mb-3">Conversa</div>
          {% if historico %}
            {% for item in historico|reverse %}
              <div class="border rounded-4 p-3 mb-3" style="background:#fff;">
                <div class="small text-secondary mb-1">{{ item.quando }}</div>
                <div class="fw-semibold mb-2"><i class="bi bi-person me-1"></i> {{ item.pergunta }}</div>
                <div class="chat-answer text-dark"><i class="bi bi-stars me-1 text-primary"></i>{{ item.resposta }}</div>
              </div>
            {% endfor %}
          {% else %}
            <div class="text-secondary py-4 text-center">Faça a primeira pergunta. Pode escrever do seu jeito.</div>
          {% endif %}
        </div>
      </div>
    </div>
    <script>
    function fillAi(el){document.getElementById('aiQuestion').value=el.innerText;document.getElementById('aiQuestion').focus();}
    </script>
    """, historico=historico, configured=configured, provider=provider, model=model, user=user, ai_last_error=ai_last_error, ai_last_ok=ai_last_ok, ai_connected=ai_connected)

    return render_page('IA do CRM', body, 'ia_assistente')


@app.route('/estudo_mercado')
@login_required
def estudo_mercado():
    user = get_current_user()
    if not user or user['role'] not in ['admin', 'gestor']:
        flash('Acesso negado.', 'warning')
        return redirect(url_for('home'))

    ref_date = get_ref_date_param('data_ref')
    ano_atual = ref_date.year
    ano_anterior = ano_atual - 1
    ytd_ini = date(ano_atual, 1, 1)
    ytd_fim = ref_date
    ytd_ly_ini = date(ano_anterior, 1, 1)
    ytd_ly_fim = date(ano_anterior, ref_date.month, min(ref_date.day, end_of_month(date(ano_anterior, ref_date.month, 1)).day))
    mes_ini = date(ref_date.year, ref_date.month, 1)
    mes_fim = end_of_month(ref_date)
    mes_ly_ini = date(ano_anterior, ref_date.month, 1)
    mes_ly_fim = end_of_month(mes_ly_ini)
    dias_passados = max(1, (ref_date - mes_ini).days + 1)
    dias_mes = mes_fim.day

    with conectar() as conn:
        fat_ytd = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
        fat_ytd_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
        fat_mes = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (mes_ini.isoformat(), ref_date.isoformat())).fetchone()
        fat_mes_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor,0)),0) AS valor FROM invoices WHERE date(data_emissao) BETWEEN date(?) AND date(?)", (mes_ly_ini.isoformat(), mes_ly_fim.isoformat())).fetchone()
        # Pedidos oficiais: mesma base do Dashboard/Excel.
        # Total geral inclui SITE; equipe interno/externo deve sempre separar SITE.
        ped_ytd = conn.execute(pedidos_oficiais_summary_sql(), (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
        ped_mes = conn.execute(pedidos_oficiais_summary_sql(), (mes_ini.isoformat(), ref_date.isoformat())).fetchone()
        ped_cliente_rows = conn.execute(f"""
            SELECT cliente,
                   COUNT(DISTINCT pedido) AS pedidos,
                   COALESCE(SUM(valor_item),0) AS valor
            FROM (
                SELECT
                    COALESCE(NULLIF(TRIM(c.rede_nome),''), NULLIF(TRIM(c.fantasia),''), NULLIF(TRIM(c.nome),''), NULLIF(TRIM(o.cliente_nome_ref),''), 'Cliente sem nome') AS cliente,
                    TRIM(COALESCE(o.pedido,'')) AS pedido,
                    COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0) AS valor_item
                FROM orders o
                LEFT JOIN customers c ON c.id = o.cliente_id
                LEFT JOIN vendors v ON v.id = o.vendor_id
                WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND TRIM(COALESCE(o.pedido,'')) <> ''
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
            ) base
            GROUP BY cliente
            ORDER BY pedidos DESC, valor DESC, cliente ASC
            LIMIT 15
        """, (mes_ini.isoformat(), ref_date.isoformat())).fetchall()
        orc_ytd = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)", (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
        orc_ytd_ly = conn.execute("SELECT COUNT(*) AS qtd, COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?)", (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
        conv_ytd = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) <> '' OR UPPER(TRIM(COALESCE(desc_status,''))) = 'PEDIDO APROVADO' THEN 1 ELSE 0 END) AS fechados
            FROM orcamentos
            WHERE date(data_inicio) BETWEEN date(?) AND date(?)
        """, (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchone()
        conv_ytd_ly = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN TRIM(COALESCE(pedido_id,'')) <> '' OR UPPER(TRIM(COALESCE(desc_status,''))) = 'PEDIDO APROVADO' THEN 1 ELSE 0 END) AS fechados
            FROM orcamentos
            WHERE date(data_inicio) BETWEEN date(?) AND date(?)
        """, (ytd_ly_ini.isoformat(), ytd_ly_fim.isoformat())).fetchone()
        churn_rows = conn.execute("""
            WITH base_ly AS (
                SELECT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave,
                       MAX(COALESCE(NULLIF(TRIM(cir.identity_nome), ''), NULLIF(TRIM(i.rede_nome), ''), NULLIF(TRIM(i.cliente_nome_ref), ''), 'Cliente sem nome')) AS cliente,
                       COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_ly
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
                GROUP BY 1
            ),
            base_cy AS (
                SELECT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave,
                       COALESCE(SUM(COALESCE(i.valor,0)),0) AS valor_cy
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
                GROUP BY 1
            )
            SELECT ly.cliente, ly.valor_ly, COALESCE(cy.valor_cy, 0) AS valor_cy,
                   (ly.valor_ly - COALESCE(cy.valor_cy, 0)) AS perda
            FROM base_ly ly
            LEFT JOIN base_cy cy ON cy.chave = ly.chave
            WHERE COALESCE(cy.valor_cy, 0) <= 0
            ORDER BY ly.valor_ly DESC, ly.cliente ASC
            LIMIT 20
        """, (f'{ano_anterior}-01-01', f'{ano_anterior}-12-31', f'{ano_atual}-01-01', ref_date.isoformat())).fetchall()
        churn_count = conn.execute("""
            WITH base_ly AS (
                SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
            ),
            base_cy AS (
                SELECT DISTINCT COALESCE(NULLIF(TRIM(cir.identity_key), ''), 'CLIENTE:' || i.cliente_id) AS chave
                FROM invoices i
                LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
                WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
            )
            SELECT COUNT(*) AS qtd
            FROM base_ly ly
            LEFT JOIN base_cy cy ON cy.chave = ly.chave
            WHERE cy.chave IS NULL
        """, (f'{ano_anterior}-01-01', f'{ano_anterior}-12-31', f'{ano_atual}-01-01', ref_date.isoformat())).fetchone()
        uf_rows = conn.execute("""
            SELECT COALESCE(NULLIF(TRIM(uf), ''), 'SEM UF') AS uf,
                   COUNT(*) AS qtd,
                   COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor
            FROM orcamentos
            WHERE date(data_inicio) BETWEEN date(?) AND date(?)
            GROUP BY COALESCE(NULLIF(TRIM(uf), ''), 'SEM UF')
            ORDER BY valor DESC, qtd DESC, uf ASC
            LIMIT 10
        """, (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchall()
        produto_rows = conn.execute("""
            SELECT COALESCE(NULLIF(TRIM(grupo_ref), ''), NULLIF(TRIM(produto), ''), NULLIF(TRIM(formato), ''), NULLIF(TRIM(medida_ref), ''), 'SEM PRODUTO') AS produto_base,
                   COUNT(*) AS qtd,
                   COALESCE(SUM(COALESCE(valor,0)),0) AS valor
            FROM invoices
            WHERE date(data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY 1
            ORDER BY valor DESC, qtd DESC, produto_base ASC
            LIMIT 60
        """, (ytd_ini.isoformat(), ytd_fim.isoformat())).fetchall()

    fat_ytd_val = float(fat_ytd['valor'] or 0)
    fat_ytd_ly_val = float(fat_ytd_ly['valor'] or 0)
    fat_mes_val = float(fat_mes['valor'] or 0)
    fat_mes_ly_val = float(fat_mes_ly['valor'] or 0)
    ped_ytd_qtd = int(ped_ytd['qtd'] or 0)
    ped_ytd_val = float(ped_ytd['valor'] or 0)
    ped_mes_qtd = int(ped_mes['qtd'] or 0)
    ped_mes_val = float(ped_mes['valor'] or 0)
    ped_cliente_rows_fmt = [{'cliente': r['cliente'], 'pedidos': int(r['pedidos'] or 0), 'valor': float(r['valor'] or 0)} for r in ped_cliente_rows]
    orc_ytd_qtd = int(orc_ytd['qtd'] or 0)
    orc_ytd_ly_qtd = int(orc_ytd_ly['qtd'] or 0)
    conv_pct = safe_pct(float(conv_ytd['fechados'] or 0), float(conv_ytd['total'] or 0))
    conv_pct_ly = safe_pct(float(conv_ytd_ly['fechados'] or 0), float(conv_ytd_ly['total'] or 0))
    ticket_ytd = fat_ytd_val / max(1, int(fat_ytd['qtd'] or 0))
    ticket_ytd_ly = fat_ytd_ly_val / max(1, int(fat_ytd_ly['qtd'] or 0))
    projecao_mes = (fat_mes_val / dias_passados) * dias_mes if dias_passados else 0.0

    familias = {}
    total_produto_valor = 0.0
    for r in produto_rows:
        fam = infer_product_family(r['produto_base'] or '')
        info = familias.setdefault(fam, {'familia': fam, 'qtd': 0, 'valor': 0.0})
        info['qtd'] += int(r['qtd'] or 0)
        info['valor'] += float(r['valor'] or 0)
        total_produto_valor += float(r['valor'] or 0)
    produto_rows_final = sorted(familias.values(), key=lambda x: (-x['valor'], -x['qtd'], x['familia']))[:10]
    for item in produto_rows_final:
        item['pct'] = safe_pct(item['valor'], total_produto_valor)

    total_uf_valor = sum(float(r['valor'] or 0) for r in uf_rows)
    top3_uf_share = safe_pct(sum(float(r['valor'] or 0) for r in uf_rows[:3]), total_uf_valor) if uf_rows else 0.0
    uf_rows_fmt = [{'uf': r['uf'], 'qtd': int(r['qtd'] or 0), 'valor': float(r['valor'] or 0), 'pct': safe_pct(float(r['valor'] or 0), total_uf_valor)} for r in uf_rows]

    fat_delta_ytd = pct_delta(fat_ytd_val, fat_ytd_ly_val)
    orc_delta_qtd = pct_delta(orc_ytd_qtd, orc_ytd_ly_qtd)
    conv_delta = conv_pct - conv_pct_ly
    ticket_delta = pct_delta(ticket_ytd, ticket_ytd_ly)

    insight_faturamento = (
        f"De {ytd_ini.strftime('%d/%m/%Y')} até {ytd_fim.strftime('%d/%m/%Y')}, o faturamento está em {format_brl(fat_ytd_val)} "
        f"contra {format_brl(fat_ytd_ly_val)} no mesmo período de {ano_anterior}. "
        f"Isso representa {'queda' if fat_ytd_val < fat_ytd_ly_val else 'alta'} de {abs(fat_delta_ytd):.1f}%."
    )
    insight_funil = (
        f"No mesmo recorte, a empresa gerou {orc_ytd_qtd} orçamentos contra {orc_ytd_ly_qtd} no ano anterior. "
        f"A conversão está em {conv_pct:.1f}% (ano anterior: {conv_pct_ly:.1f}%)."
    )
    insight_churn = f"Há {int(churn_count['qtd'] or 0)} clientes/redes que compraram em {ano_anterior} e ainda não voltaram a comprar em {ano_atual} até {ref_date.strftime('%d/%m/%Y')}."
    insight_geo = (
        f"A leitura geográfica está baseada nos orçamentos, porque o faturamento importado ainda não carrega UF no banco. "
        f"Os 3 principais estados concentram {top3_uf_share:.1f}% do valor orçado no período."
    ) if uf_rows else 'Ainda não há UF suficiente nos orçamentos para medir concentração geográfica.'
    insight_produto = 'O mix abaixo mostra onde o faturamento está concentrado por famílias de produto.'

    body = render_template_string(r"""
    <div class="page-hero mb-4">
      <div class="d-flex justify-content-between align-items-end flex-wrap gap-3 position-relative" style="z-index:1">
        <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Inteligência de mercado</div><div class="hero-title">Estudo de Mercado</div><div class="hero-sub mt-1">Funil, churn, concentração, mix e ritmo de faturamento em uma visão executiva.</div></div>
        <form method="get" class="d-flex align-items-end gap-2"><div><label class="form-label small fw-bold mb-1" style="color:#b9cbe0">DATA DE REFERÊNCIA</label><input type="date" class="form-control" name="data_ref" value="{{ ref_date.isoformat() }}"></div><button class="btn btn-light">Atualizar</button></form>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-md-6 col-xl-3"><div class="stat-card h-100"><div class="stat-label">Prévia por pedidos</div><div class="stat-value">{{ format_brl(ped_mes_val) }}</div><div class="stat-sub">{{ ped_mes_qtd }} pedido(s) até {{ ref_date.strftime('%d/%m') }}</div></div></div>
      <div class="col-12 col-md-6 col-xl-3"><div class="stat-card h-100"><div class="stat-label">Pedidos YTD</div><div class="stat-value text-primary">{{ format_brl(ped_ytd_val) }}</div><div class="stat-sub">{{ ped_ytd_qtd }} pedido(s) no ano</div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-md-6 col-xl-3"><div class="cardx p-3 h-100"><div class="small text-uppercase muted fw-bold">Faturamento YTD</div><div class="display-6 fw-bold">{{ format_brl(fat_ytd_val) }}</div><div class="small {{ 'text-danger' if fat_delta_ytd < 0 else 'text-success' }}">{{ '▼' if fat_delta_ytd < 0 else '▲' }} {{ '%.1f'|format(fat_delta_ytd|abs) }}% vs ano anterior</div></div></div>
      <div class="col-12 col-md-6 col-xl-3"><div class="cardx p-3 h-100"><div class="small text-uppercase muted fw-bold">Orçamentos YTD</div><div class="display-6 fw-bold">{{ orc_ytd_qtd }}</div><div class="small {{ 'text-danger' if orc_delta_qtd < 0 else 'text-success' }}">{{ '▼' if orc_delta_qtd < 0 else '▲' }} {{ '%.1f'|format(orc_delta_qtd|abs) }}% vs ano anterior</div></div></div>
      <div class="col-12 col-md-6 col-xl-3"><div class="cardx p-3 h-100"><div class="small text-uppercase muted fw-bold">Conversão</div><div class="display-6 fw-bold">{{ '%.1f'|format(conv_pct) }}%</div><div class="small {{ 'text-danger' if conv_delta < 0 else 'text-success' }}">{{ '▼' if conv_delta < 0 else '▲' }} {{ '%.1f'|format(conv_delta|abs) }} p.p.</div></div></div>
      <div class="col-12 col-md-6 col-xl-3"><div class="cardx p-3 h-100"><div class="small text-uppercase muted fw-bold">Ticket médio</div><div class="display-6 fw-bold" style="font-size:2rem;">{{ format_brl(ticket_ytd) }}</div><div class="small {{ 'text-danger' if ticket_delta < 0 else 'text-success' }}">{{ '▼' if ticket_delta < 0 else '▲' }} {{ '%.1f'|format(ticket_delta|abs) }}% vs ano anterior</div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h5 fw-bold mb-3">Diagnóstico do faturamento</div><p class="mb-3">{{ insight_faturamento }}</p><div class="small text-muted">Mês atual até {{ ref_date.strftime('%d/%m/%Y') }}: <b>{{ format_brl(fat_mes_val) }}</b>. Projeção linear: <b>{{ format_brl(projecao_mes) }}</b>. Mesmo mês fechado em {{ ano_anterior }}: <b>{{ format_brl(fat_mes_ly_val) }}</b>.</div></div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h5 fw-bold mb-3">Funil comercial</div><p class="mb-3">{{ insight_funil }}</p><div class="small text-muted">Se a conversão está saudável e o volume de orçamento cai, o gargalo tende a estar na prospecção e geração de demanda.</div></div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h5 fw-bold mb-3">Churn de clientes</div><p class="mb-0">{{ insight_churn }}</p></div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h5 fw-bold mb-3">Concentração geográfica e de mix</div><p class="mb-2">{{ insight_geo }}</p><p class="mb-0">{{ insight_produto }}</p></div></div>
    </div>

    <div class="row g-3">
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="d-flex justify-content-between align-items-center mb-3"><div class="h6 fw-bold mb-0">Top clientes/redes sem recompra no ano atual</div><span class="chip">{{ churn_total }} clientes/redes</span></div><div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>Cliente</th><th class="text-end">{{ ano_anterior }}</th><th class="text-end">{{ ano_atual }}</th><th class="text-end">Perda</th></tr></thead><tbody>{% for r in churn_rows %}<tr><td>{{ r['cliente'] }}</td><td class="text-end">{{ format_brl(r['valor_ly']) }}</td><td class="text-end">{{ format_brl(r['valor_cy']) }}</td><td class="text-end text-danger">{{ format_brl(r['perda']) }}</td></tr>{% else %}<tr><td colspan="4" class="text-center muted py-4">Sem clientes perdidos neste recorte.</td></tr>{% endfor %}</tbody></table></div></div></div>
      <div class="col-12 col-xl-6">
        <div class="cardx p-4 mb-3"><div class="h6 fw-bold mb-3">Concentração por UF (orçamentos)</div><div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>UF</th><th class="text-end">Qtd</th><th class="text-end">Valor</th><th class="text-end">%</th></tr></thead><tbody>{% for r in uf_rows %}<tr><td>{{ r['uf'] }}</td><td class="text-end">{{ r['qtd'] }}</td><td class="text-end">{{ format_brl(r['valor']) }}</td><td class="text-end">{{ '%.1f'|format(r['pct']) }}%</td></tr>{% else %}<tr><td colspan="4" class="text-center muted py-4">Sem UF suficiente.</td></tr>{% endfor %}</tbody></table></div></div>
        <div class="cardx p-4"><div class="h6 fw-bold mb-3">Concentração por família</div><div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>Família</th><th class="text-end">Qtd</th><th class="text-end">Valor</th><th class="text-end">%</th></tr></thead><tbody>{% for r in produto_rows_final %}<tr><td>{{ r['familia'] }}</td><td class="text-end">{{ r['qtd'] }}</td><td class="text-end">{{ format_brl(r['valor']) }}</td><td class="text-end">{{ '%.1f'|format(r['pct']) }}%</td></tr>{% else %}<tr><td colspan="4" class="text-center muted py-4">Sem dados de produto suficientes.</td></tr>{% endfor %}</tbody></table></div></div>
        <div class="cardx p-4 mt-3"><div class="h6 fw-bold mb-3">Prévia por pedidos no mês / cliente</div><div class="small text-muted mb-2">Esta tabela usa PEDIDOS, não só faturamento. Serve para enxergar clientes como Pompeia logo depois de tirar pedido, antes da nota sair.</div><div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>Cliente</th><th class="text-end">Pedidos</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in ped_cliente_rows %}<tr><td>{{ r['cliente'] }}</td><td class="text-end">{{ r['pedidos'] }}</td><td class="text-end">{{ format_brl(r['valor']) }}</td></tr>{% else %}<tr><td colspan="3" class="text-center muted py-4">Sem pedidos no mês.</td></tr>{% endfor %}</tbody></table></div></div>
      </div>
    </div>
    """, ref_date=ref_date, format_brl=format_brl, fat_ytd_val=fat_ytd_val, fat_delta_ytd=fat_delta_ytd, orc_ytd_qtd=orc_ytd_qtd, orc_delta_qtd=orc_delta_qtd, conv_pct=conv_pct, conv_delta=conv_delta, ticket_ytd=ticket_ytd, ticket_delta=ticket_delta, insight_faturamento=insight_faturamento, insight_funil=insight_funil, insight_churn=insight_churn, insight_geo=insight_geo, insight_produto=insight_produto, ano_anterior=ano_anterior, ano_atual=ano_atual, fat_mes_val=fat_mes_val, projecao_mes=projecao_mes, fat_mes_ly_val=fat_mes_ly_val, churn_rows=churn_rows, churn_total=int(churn_count['qtd'] or 0), uf_rows=uf_rows_fmt, produto_rows_final=produto_rows_final, ped_ytd_qtd=ped_ytd_qtd, ped_ytd_val=ped_ytd_val, ped_mes_qtd=ped_mes_qtd, ped_mes_val=ped_mes_val, ped_cliente_rows=ped_cliente_rows_fmt)
    return render_page('Estudo de Mercado', body, 'estudo_mercado')

@app.route("/login", methods=["GET", "POST"])
def login():
    init_db()
    ensure_admin_seed()
    if request.method == "POST":
        email = norm(request.form.get("email", "")).lower()
        senha = request.form.get("senha", "")
        with conectar() as conn:
            u = conn.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
        if not u or int(u["ativo"]) != 1 or not check_password_hash(u["senha_hash"], senha):
            flash("E-mail não encontrado ou usuário inativo / senha incorreta.", "danger")
            return redirect(url_for("login"))
        session["user_id"] = int(u["id"])
        # Login por perfil: vendedor não passa pelo Dashboard administrativo.
        # Isso evita o flash indevido de "Acesso negado" e elimina um redirecionamento extra.
        role = str(u["role"] or "").strip().lower()
        if role in ("admin", "gestor"):
            return redirect(url_for("dashboard"))
        return redirect(url_for("home"))

    body = r"""
    <div class="row justify-content-center">
      <div class="col-12 col-md-6 col-lg-4">
        <div class="cardx p-4 mt-4">
          <div class="d-flex align-items-center gap-2 mb-3">
            <div class="icon">🔐</div>
            <div>
              <div class="h4 mb-0">Entrar</div>
              <div class="muted">CRM NPD Embalagens</div>
            </div>
          </div>
          <form method="post">
            <div class="mb-3">
              <label class="form-label">E-mail</label>
              <input class="form-control" name="email" required>
            </div>
            <div class="mb-3">
              <label class="form-label">Senha</label>
              <input class="form-control" name="senha" type="password" required>
            </div>
            <button class="btn btn-primary w-100">Entrar</button>
          </form>
        </div>
      </div>
    </div>
    """
    return render_template_string(BASE_HTML, title="Login", body=body, app_title=APP_TITLE, nav_items=[], nav_groups=[], active="", user=None)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@app.route("/")
def home():
    user = get_current_user()
    
    # Se não estiver logado, manda pro login
    if not user:
        return redirect(url_for("login"))

    # CORREÇÃO: Admin E Gestor vão direto pro Dashboard
    if user["role"] in ["admin", "gestor"]:
        return redirect(url_for("dashboard"))
    
    # Se for Vendedor (user), tenta achar a tela dele
    with conectar() as conn:
        vid = get_user_vendor_id(conn, user)
        
        if vid and vid > 0:
            # SUCESSO: Achou o vendedor, redireciona
            return redirect(url_for('vendedor', vendor_id=vid))
        
        else:
            # FALHA: Usuário comum sem vínculo válido
            session.pop("user_id", None)
            flash(f"ERRO DE VÍNCULO: O código '{user.get('vendor_codigo', '')}' não foi encontrado na lista de vendedores ativos. Contate o Admin.", "danger")
            return redirect(url_for("login"))

# -------------------------------------------------------------------------
# FUNÇÃO dashboard
# -------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    # --- TRAVA DE SEGURANÇA: ADMIN E GESTOR PODEM ENTRAR ---
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))
    
    # --- 1. DATAS ---
    data_ref_str = request.args.get("data_ref", "")
    ver_pedidos = request.args.get("ver_pedidos", "0") == "1"
    visao_raw = (request.args.get("visao") or "geral").strip().lower()
    visao = visao_raw if visao_raw in {"geral", "faturamento", "pedidos"} else "geral"

    try:
        if data_ref_str:
            ref_date = datetime.strptime(data_ref_str, "%Y-%m").date()
        else:
            # CORREÇÃO: Força o dia 1 para alinhar com o seletor de mês.
            # Assim, mesmo que seja dia 30, ele pega o ciclo correto do mês atual.
            ref_date = date.today().replace(day=1)
    except:
        ref_date = date.today().replace(day=1)

    hoje = date.today()
    # Ciclo Financeiro (configurável)
    d0_fat, d1_fat = periodo_ciclo_25_24_que_contem_hoje(ref_date)
    # Ciclo Comercial (1-31)
    d0_ped, d1_ped = periodo_mes_calendario(ref_date)

    with conectar() as conn:
        ensure_vendor_type_column(conn)
        site_vendor = get_site_vendor_row(conn)
        site_vendor_id = int(site_vendor["id"]) if site_vendor else 0

        metrics_orc = get_metrics_orcamentos(
        conn, 
        d0_ped.strftime("%Y-%m-%d"), 
        d1_ped.strftime("%Y-%m-%d")
        )

        # --- A. FATURAMENTO (Ciclo configurável) ---
        rows_fat = conn.execute("""
            SELECT COALESCE(upper(v.tipo), 'EXTERNO') as tipo, 
                   COALESCE(SUM(i.valor),0) as val, 
                   COUNT(*) as qtd
            FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id
            WHERE i.data_emissao IS NOT NULL AND date(i.data_emissao) BETWEEN date(?) AND date(?)
              AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
            GROUP BY COALESCE(upper(v.tipo), 'EXTERNO')
        """, (d0_fat.isoformat(), d1_fat.isoformat())).fetchall()
        
        f_map = {r["tipo"]: r["val"] for r in rows_fat}
        q_map = {r["tipo"]: r["qtd"] for r in rows_fat}
        
        fat_int = f_map.get("INTERNO", 0.0)
        fat_ext = f_map.get("EXTERNO", 0.0)
        fat_total = fat_int + fat_ext

        qtd_fat_int = q_map.get("INTERNO", 0)
        qtd_fat_ext = q_map.get("EXTERNO", 0)
        fat_qtd = qtd_fat_int + qtd_fat_ext

        # --- B. PEDIDOS (Mês 1-31) ---
        # Soma VALOR_ITEM por linha. Carteira = RESPONSAVEL. Inclui 05-JIT IMEDIATO.
        rows_ped_excel = conn.execute(f"""
            SELECT
                CASE
                    WHEN COALESCE(UPPER(TRIM(v.nome)), '') = 'SITE' OR COALESCE(UPPER(TRIM(v.tipo)), '') = 'SITE' THEN 'SITE'
                    ELSE COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO')
                END AS tipo,
                COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS val,
                COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd
            FROM orders o
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY tipo
        """, (d0_ped.isoformat(), d1_ped.isoformat())).fetchall()

        p_map = {r["tipo"]: float(r["val"] or 0) for r in rows_ped_excel}
        pq_map = {r["tipo"]: int(r["qtd"] or 0) for r in rows_ped_excel}

        ped_int = p_map.get("INTERNO", 0.0)
        ped_ext = p_map.get("EXTERNO", 0.0)
        ped_site = p_map.get("SITE", 0.0)
        ped_total = ped_int + ped_ext + ped_site
        
        qtd_ped_int = pq_map.get("INTERNO", 0)
        qtd_ped_ext = pq_map.get("EXTERNO", 0)
        ped_qtd = qtd_ped_int + qtd_ped_ext

        # --- C. METAS (Histórico Mês a Mês) ---
        mes_ref = ref_date.strftime("%Y-%m")

        meta_ped_int, meta_ped_ext = get_meta_equipe(conn, mes_ref, "pedidos")
        meta_fat_int, meta_fat_ext = get_meta_equipe(conn, mes_ref, "faturamento")

        # Pedidos: se equipe vazia, tenta soma das metas individuais / meta global legada.
        if (meta_ped_int + meta_ped_ext) == 0:
            rows_m = conn.execute("""
                SELECT COALESCE(upper(v.tipo),'EXTERNO') as tipo, SUM(m.meta_manual) as mt 
                FROM metas_vendedores m JOIN vendors v ON v.id=m.vendor_id 
                WHERE COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
                GROUP BY tipo
            """).fetchall()
            m_map = {r["tipo"]: r["mt"] for r in rows_m}
            if m_map.get("INTERNO") or m_map.get("EXTERNO"):
                meta_ped_int = m_map.get("INTERNO", 0.0)
                meta_ped_ext = m_map.get("EXTERNO", 0.0)

        meta_ped_total = meta_ped_int + meta_ped_ext
        if meta_ped_total == 0:
            meta_total_config = meta_empresa(conn)
            meta_ped_int = meta_total_config * 0.5
            meta_ped_ext = meta_total_config * 0.5
            meta_ped_total = meta_ped_int + meta_ped_ext

        meta_fat_total = meta_fat_int + meta_fat_ext
        # Compat: templates/código antigo ainda usam meta_* como pedidos.
        meta_int, meta_ext, meta_total = meta_ped_int, meta_ped_ext, meta_ped_total

        # Status (Gap Qtd)
        gap_qtd_int = max(0, qtd_ped_int - qtd_fat_int)
        gap_qtd_ext = max(0, qtd_ped_ext - qtd_fat_ext)
        gap_qtd = gap_qtd_int + gap_qtd_ext

        # Tickets e Quebra (Gerais)
        ticket_medio = (fat_total / fat_qtd) if fat_qtd > 0 else 0.0
        diff_val = ped_total - fat_total
        pct_quebra = (100.0 - (fat_total / ped_total * 100)) if ped_total > 0 else 0.0
        if pct_quebra < 0: pct_quebra = 0.0

        def _dias_uteis(d0: date, d1: date) -> tuple[int, int]:
            total = business_days_between(d0, d1)
            if d1 < hoje:
                return total, 0
            if d0 > hoje:
                return total, total
            return total, business_days_between(hoje, d1)

        du_total_ped, du_rest_ped = _dias_uteis(d0_ped, d1_ped)
        du_total_fat, du_rest_fat = _dias_uteis(d0_fat, d1_fat)
        du_total, du_rest = du_total_ped, du_rest_ped

        def calc_eixo(realizado: float, qtd: int, meta: float, du_total: int, du_rest: int) -> dict:
            pct = (realizado / meta * 100) if meta > 0 else 0.0
            falta = max(meta - realizado, 0.0)
            ideal = meta / du_total if du_total > 0 else 0.0
            nec = (falta / du_rest) if du_rest > 0 else falta
            tk = (realizado / qtd) if qtd > 0 else 0.0
            return {
                "pct": pct,
                "falta": falta,
                "ideal": ideal,
                "nec": nec,
                "ticket": tk,
            }

        def calc_grupo(p_val, f_val, p_qtd, f_qtd, meta):
            base = calc_eixo(p_val, p_qtd, meta, du_total_ped, du_rest_ped)
            qb = (100 - (f_val / p_val * 100)) if p_val > 0 else 0.0
            if qb < 0:
                qb = 0.0
            base["quebra"] = qb
            base["gap_qtd"] = max(0, p_qtd - f_qtd)
            return base

        kpi_fat_int = calc_eixo(fat_int, qtd_fat_int, meta_fat_int, du_total_fat, du_rest_fat)
        kpi_fat_ext = calc_eixo(fat_ext, qtd_fat_ext, meta_fat_ext, du_total_fat, du_rest_fat)
        kpi_fat_tot = calc_eixo(fat_total, fat_qtd, meta_fat_total, du_total_fat, du_rest_fat)

        kpi_int = calc_grupo(ped_int, fat_int, qtd_ped_int, qtd_fat_int, meta_ped_int)
        kpi_ext = calc_grupo(ped_ext, fat_ext, qtd_ped_ext, qtd_fat_ext, meta_ped_ext)
        kpi_tot = calc_grupo(ped_total, fat_total, ped_qtd, fat_qtd, meta_ped_total)

        pct_realizado = kpi_tot["pct"]
        falta = kpi_tot["falta"]
        meta_diaria_ideal = kpi_tot["ideal"]
        necessario_hoje = kpi_tot["nec"]

        # --- E. GRÁFICOS ---
        
        # 1. Evolução Diária
        dates = []; c = d0_ped
        while c <= d1_ped: dates.append(c); c += timedelta(days=1)
        lbls = [d.strftime("%d") for d in dates]
        dp_int, dp_ext, dp_tot, df_tot = [], [], [], []
        
        for d in dates:
            rows_day = conn.execute(f"""
                SELECT COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') AS tipo,
                       COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS val
                FROM orders o
                LEFT JOIN vendors v ON v.id = o.vendor_id
                WHERE date(o.data_emissao)=date(?)
                  AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
                  AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO')
            """, (d.isoformat(),)).fetchall()
            dm = {r["tipo"]: r["val"] for r in rows_day}
            vi = dm.get("INTERNO", 0.0); ve = dm.get("EXTERNO", 0.0)
            dp_int.append(vi); dp_ext.append(ve); dp_tot.append(vi+ve)
            
            vf = conn.execute("SELECT COALESCE(SUM(i.valor),0) FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id WHERE date(i.data_emissao)=? AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'", (d.isoformat(),)).fetchone()[0]
            df_tot.append(float(vf))

        line_comp = line_chart(lbls, [("Interno", dp_int), ("Externo", dp_ext)], "Evolução Pedidos: Equipes")
        line_tot = line_chart(lbls, [("Pedidos Total", dp_tot), ("Fat Total", df_tot)], "Geral: Pedidos vs Faturamento")

        # 2. Novo x Repetição - LÓGICA UNIFICADA
        team_entities = summarize_team_entities_in_period(conn, d0_ped, d1_ped, team_tipo=None)
        novo_qtd = int(team_entities["novo_count"] or 0)
        rep_qtd = int(team_entities["rep_count"] or 0)

        pie_novo = donut_chart(
            ["NOVO", "REPETIÇÃO"],
            [novo_qtd, rep_qtd],
            "Perfil (Pedidos)"
        ) if (novo_qtd + rep_qtd) else None

        # 3. Status Geral - LÓGICA UNIFICADA POR PEDIDO
        status_total = summarize_team_status_by_orders(conn, d0_ped, d1_ped, d0_fat, d1_fat, team_tipo=None)
        status_int = summarize_team_status_by_orders(conn, d0_ped, d1_ped, d0_fat, d1_fat, team_tipo="INTERNO")
        status_ext = summarize_team_status_by_orders(conn, d0_ped, d1_ped, d0_fat, d1_fat, team_tipo="EXTERNO")

        # Sobrescreve os contadores visuais para manter tudo coerente
        qtd_ped_int = status_int["total_pedidos"]
        qtd_ped_ext = status_ext["total_pedidos"]
        ped_qtd = status_total["total_pedidos"]

        qtd_fat_int = status_int["faturados"]
        qtd_fat_ext = status_ext["faturados"]
        fat_qtd = status_total["faturados"]

        gap_qtd_int = status_int["pendentes"]
        gap_qtd_ext = status_ext["pendentes"]
        gap_qtd = status_total["pendentes"]

        # Depois de ajustar os contadores visuais pela regra oficial,
        # recalcula os KPIs para ticket médio, gap e status ficarem coerentes.
        kpi_fat_int = calc_eixo(fat_int, qtd_fat_int, meta_fat_int, du_total_fat, du_rest_fat)
        kpi_fat_ext = calc_eixo(fat_ext, qtd_fat_ext, meta_fat_ext, du_total_fat, du_rest_fat)
        kpi_fat_tot = calc_eixo(fat_total, fat_qtd, meta_fat_total, du_total_fat, du_rest_fat)
        kpi_int = calc_grupo(ped_int, fat_int, qtd_ped_int, qtd_fat_int, meta_ped_int)
        kpi_ext = calc_grupo(ped_ext, fat_ext, qtd_ped_ext, qtd_fat_ext, meta_ped_ext)
        kpi_tot = calc_grupo(ped_total, fat_total, ped_qtd, fat_qtd, meta_ped_total)
        pct_realizado = kpi_tot["pct"]
        falta = kpi_tot["falta"]
        meta_diaria_ideal = kpi_tot["ideal"]
        necessario_hoje = kpi_tot["nec"]

        pie_status = donut_chart(
            ["FATURADOS", "PENDENTES"],
            [fat_qtd, gap_qtd],
            "Status da Carteira"
        ) if (fat_qtd + gap_qtd) > 0 else None

        # 4. Top carteiras (RESPONSAVEL do pedido)
        top = conn.execute(f"""
            SELECT
                TRIM(
                    CASE
                        WHEN TRIM(COALESCE(v.codigo,'')) <> ''
                        THEN TRIM(v.codigo) || ' - ' || COALESCE(NULLIF(v.nome,''), '-')
                        ELSE COALESCE(NULLIF(v.nome,''), '-')
                    END
                ) AS nome,
                COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS t
            FROM orders o
            LEFT JOIN vendors v ON v.id=o.vendor_id
            WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY v.id
            ORDER BY t DESC
            LIMIT 8
        """, (d0_ped.isoformat(), d1_ped.isoformat())).fetchall()
        top_bar = rank_chart_horizontal([r["nome"][:18] for r in top], [r["t"] for r in top], money=True) if top else None

        # 5. YoY
        ano = hoje.year
        def gm(y):
            rows = conn.execute("SELECT strftime('%m', data_emissao) as m, SUM(valor) as v FROM invoices WHERE strftime('%Y', data_emissao)=? GROUP BY m", (str(y),)).fetchall()
            d = {x['m']: x['v'] for x in rows}
            return [d.get(f"{k:02d}",0) for k in range(1,13)]
        chart_yoy = line_chart(["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"], [(str(ano-1), gm(ano-1)), (str(ano), gm(ano))], "Comparativo Anual")

        # Feed de últimos pedidos tirados
        ultimos_pedidos = conn.execute(f"""
        SELECT 
            MAX(o.data_emissao) AS data_emissao,
            o.pedido AS numero,
            SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) AS valor,
            COALESCE(NULLIF(MAX(c.fantasia),''), NULLIF(MAX(c.nome),''), '-') AS cliente,
            COALESCE(NULLIF(MAX(v.nome),''), '-') AS vend,
            COALESCE(NULLIF(MAX(o.medida_ref), ''), '-') AS medida
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE date(o.data_emissao) BETWEEN ? AND ?
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY o.pedido
        ORDER BY datetime(MAX(o.data_emissao)) DESC, o.pedido DESC
        LIMIT 8
        """,
        (d0_ped.isoformat(), d1_ped.isoformat())
        ).fetchall()

        # Comercial: ranking de clientes no período de pedidos
        top_clientes_rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(c.rede_nome,''), NULLIF(c.fantasia,''), NULLIF(c.nome,''), '-') AS cliente,
            ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)), 2) AS valor_total
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY COALESCE(NULLIF(c.rede_nome,''), NULLIF(c.fantasia,''), NULLIF(c.nome,''), '-')
        ORDER BY valor_total DESC, cliente ASC
        LIMIT 8
        """,
        (d0_ped.isoformat(), d1_ped.isoformat())
        ).fetchall()
        top_clientes_chart = rank_chart_horizontal([r["cliente"][:20] for r in top_clientes_rows], [r["valor_total"] for r in top_clientes_rows], money=True) if top_clientes_rows else None

        # Comercial: campanha por medida
        total_pedidos_periodo = conn.execute(f"""
        SELECT COUNT(DISTINCT o.pedido) AS total
        FROM orders o
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        """,
        (d0_ped.isoformat(), d1_ped.isoformat())
        ).fetchone()["total"] or 0

        top_medidas_rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA') AS medida,
            COUNT(DISTINCT o.pedido) AS qtd_pedidos,
            ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)), 2) AS valor_total,
            ROUND((COUNT(DISTINCT o.pedido) * 100.0) / NULLIF(?, 0), 2) AS pct_pedidos
        FROM orders o
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND COALESCE(TRIM(o.medida_ref), '') <> ''
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA')
        ORDER BY COUNT(DISTINCT o.pedido) DESC, valor_total DESC, medida ASC
        """,
        (total_pedidos_periodo, d0_ped.isoformat(), d1_ped.isoformat())
        ).fetchall()
        top_medidas_chart = campanha_medida_chart(top_medidas_rows, total_pedidos_periodo) if top_medidas_rows else None
        top_medidas_top10 = top_medidas_rows[:10]

        # SITE / Marketplace (sem meta e sem comissão)
        site_params = site_sql_params(site_vendor_id)
        site_pedidos_row = conn.execute(f"""
            SELECT COUNT(*) AS qtd, COALESCE(SUM(v),0) AS val
            FROM (
                SELECT o.pedido, SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) AS v
                FROM orders o
                LEFT JOIN vendors v ON v.id = o.vendor_id
                WHERE {sql_site_match('o', join_alias='v')}
                  AND date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY o.pedido
            ) x
        """, site_params + (d0_ped.isoformat(), d1_ped.isoformat())).fetchone()
        site_fat_row = conn.execute(f"""
            SELECT COUNT(*) AS qtd, COALESCE(SUM(i.valor),0) AS val
            FROM invoices i
            LEFT JOIN vendors v ON v.id = i.vendor_id
            WHERE {sql_site_match('i', join_alias='v')}
              AND i.data_emissao IS NOT NULL
              AND date(i.data_emissao) BETWEEN date(?) AND date(?)
        """, site_params + (d0_ped.isoformat(), d1_ped.isoformat())).fetchone()
        qtd_ped_site = int((site_pedidos_row["qtd"] if site_pedidos_row else 0) or 0)
        ped_site = float((site_pedidos_row["val"] if site_pedidos_row else 0) or 0)
        qtd_fat_site = int((site_fat_row["qtd"] if site_fat_row else 0) or 0)
        fat_site = float((site_fat_row["val"] if site_fat_row else 0) or 0)
        liq_site = site_liquidez_total_mes(conn, site_vendor_id, d0_ped, d1_ped)
        gap_qtd_site = max(0, qtd_ped_site - qtd_fat_site)
        ticket_site = (ped_site / qtd_ped_site) if qtd_ped_site > 0 else 0.0

    # --- HTML ---
    body = render_template_string(r"""
    <div class="dash-toolbar">
      <div>
        <div class="h3 mb-1 fw-bold">Performance Geral do Time</div>
        <div class="muted small d-flex gap-3 flex-wrap">
            <span>Pedidos: <b>{{ d0_ped.strftime('%d/%m') }}-{{ d1_ped.strftime('%d/%m') }}</b></span>
            <span class="vr d-none d-md-inline"></span>
            <span>Fat: <b>{{ d0_fat.strftime('%d/%m') }}-{{ d1_fat.strftime('%d/%m') }}</b></span>
        </div>
      </div>
      <div class="d-flex align-items-end gap-3 flex-wrap">
        <nav class="dash-tabs" aria-label="Visão do dashboard">
          <a class="{{ 'active' if visao == 'geral' else '' }}" href="{{ url_for('dashboard', data_ref=mes_ref, visao='geral') }}">Visão Geral</a>
          <a class="{{ 'active' if visao == 'faturamento' else '' }}" href="{{ url_for('dashboard', data_ref=mes_ref, visao='faturamento') }}">Faturamento</a>
          <a class="{{ 'active' if visao == 'pedidos' else '' }}" href="{{ url_for('dashboard', data_ref=mes_ref, visao='pedidos') }}">Pedidos</a>
        </nav>
        <form class="d-flex gap-2 align-items-end cardx p-3" method="get">
          <input type="hidden" name="visao" value="{{ visao }}">
          <div><label class="form-label small fw-bold text-muted mb-0">MÊS REF</label><input type="month" name="data_ref" class="form-control fw-bold" value="{{ mes_ref }}" onchange="this.form.submit()"></div>
        </form>
      </div>
    </div>

    {% if visao == 'geral' %}
    <div class="d-flex flex-column gap-4 mb-2">
      <div>
        <div class="dash-block dash-block--fat">
          <div class="dash-block-head">
            <div>
              <span class="dash-pill dash-pill--fat"><i class="bi bi-receipt"></i> Faturamento</span>
              <div class="sub">Ciclo {{ d0_fat.strftime('%d/%m/%Y') }} a {{ d1_fat.strftime('%d/%m/%Y') }}</div>
            </div>
            <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('dashboard', data_ref=mes_ref, visao='faturamento') }}">Detalhar</a>
          </div>
          <div class="row g-3">
            <div class="col-12 col-md-4">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Meta</div><div class="icon meta"><i class="bi bi-bullseye"></i></div></div>
                <div class="kpi-value">{{ format_brl(meta_fat_total) }}</div>
                <div class="progress mt-2" style="height:7px"><div class="progress-bar dash-bar-meta" style="width:{{ 100 if kpi_fat_tot.pct > 100 else kpi_fat_tot.pct }}%"></div></div>
                <div class="kpi-meta">{{ '%.1f'|format(kpi_fat_tot.pct) }}% atingido</div>
              </div>
            </div>
            <div class="col-12 col-md-4">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Total faturado</div><div class="icon done"><i class="bi bi-cash-coin"></i></div></div>
                <div class="kpi-value">{{ format_brl(fat_total) }}</div>
                <div class="kpi-meta">{{ fat_qtd }} notas no ciclo</div>
              </div>
            </div>
            <div class="col-12 col-md-4">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">{{ 'Meta superada' if kpi_fat_tot.falta <= 0 else 'Falta faturar' }}</div><div class="icon {{ 'ok' if kpi_fat_tot.falta <= 0 else 'warn' }}"><i class="bi {{ 'bi-check-lg' if kpi_fat_tot.falta <= 0 else 'bi-exclamation-lg' }}"></i></div></div>
                <div class="kpi-value">{{ format_brl((fat_total - meta_fat_total) if kpi_fat_tot.falta <= 0 else kpi_fat_tot.falta) }}</div>
                <div class="kpi-meta">{{ 'Acima da meta' if kpi_fat_tot.falta <= 0 else 'Para bater a meta' }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div>
        <div class="dash-block dash-block--ped">
          <div class="dash-block-head">
            <div>
              <span class="dash-pill dash-pill--ped"><i class="bi bi-bag-check"></i> Pedidos</span>
              <div class="sub">Mês {{ d0_ped.strftime('%d/%m/%Y') }} a {{ d1_ped.strftime('%d/%m/%Y') }}</div>
            </div>
            <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('dashboard', data_ref=mes_ref, visao='pedidos') }}">Detalhar</a>
          </div>
          <div class="row g-3">
            <div class="col-12 col-md-4">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Meta</div><div class="icon meta"><i class="bi bi-bullseye"></i></div></div>
                <div class="kpi-value">{{ format_brl(meta_ped_total) }}</div>
                <div class="progress mt-2" style="height:7px"><div class="progress-bar dash-bar-meta" style="width:{{ 100 if kpi_tot.pct > 100 else kpi_tot.pct }}%"></div></div>
                <div class="kpi-meta">{{ '%.1f'|format(kpi_tot.pct) }}% atingido</div>
              </div>
            </div>
            <div class="col-12 col-md-4">
              <a href="{{ url_for('dashboard_pedidos_detalhe', data_ref=mes_ref, periodo='mes') }}" class="text-decoration-none text-dark d-block h-100">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Realizado</div><div class="icon done"><i class="bi bi-cart3"></i></div></div>
                <div class="kpi-value">{{ format_brl(ped_total) }}</div>
                <div class="kpi-meta">{{ ped_qtd }} pedidos · clique para detalhar</div>
              </div>
              </a>
            </div>
            <div class="col-12 col-md-4">
              <div class="dash-kpi">
                <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">{{ 'Meta superada' if kpi_tot.falta <= 0 else 'Falta vender' }}</div><div class="icon {{ 'ok' if kpi_tot.falta <= 0 else 'warn' }}"><i class="bi {{ 'bi-check-lg' if kpi_tot.falta <= 0 else 'bi-exclamation-lg' }}"></i></div></div>
                <div class="kpi-value">{{ format_brl((ped_total - meta_ped_total) if kpi_tot.falta <= 0 else kpi_tot.falta) }}</div>
                <div class="kpi-meta">{{ 'Acima da meta' if kpi_tot.falta <= 0 else 'Para bater a meta' }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    {% endif %}

    {% if visao == 'faturamento' %}
    <div class="dash-block dash-block--fat mb-4">
      <div class="dash-block-head">
        <div>
          <span class="dash-pill dash-pill--fat"><i class="bi bi-receipt"></i> Faturamento</span>
          <div class="sub">Ciclo {{ d0_fat.strftime('%d/%m/%Y') }} a {{ d1_fat.strftime('%d/%m/%Y') }} · meta em Settings</div>
        </div>
      </div>
      <div class="row g-3 mb-3">
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Meta Faturamento</div><div class="icon meta"><i class="bi bi-bullseye"></i></div></div>
            <div class="kpi-value">{{ format_brl(meta_fat_total) }}</div>
            <div class="progress mt-2" style="height:8px"><div class="progress-bar dash-bar-meta" style="width:{{ 100 if kpi_fat_tot.pct > 100 else kpi_fat_tot.pct }}%"></div></div>
            <div class="kpi-meta">{{ '%.1f'|format(kpi_fat_tot.pct) }}% atingido</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Total Faturado</div><div class="icon done"><i class="bi bi-cash-coin"></i></div></div>
            <div class="kpi-value">{{ format_brl(fat_total) }}</div>
            <div class="kpi-meta">{{ fat_qtd }} notas no ciclo de faturamento</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">{{ 'Meta superada' if kpi_fat_tot.falta <= 0 else 'Falta faturar' }}</div><div class="icon {{ 'ok' if kpi_fat_tot.falta <= 0 else 'warn' }}"><i class="bi {{ 'bi-check-lg' if kpi_fat_tot.falta <= 0 else 'bi-exclamation-lg' }}"></i></div></div>
            <div class="kpi-value">{{ format_brl((fat_total - meta_fat_total) if kpi_fat_tot.falta <= 0 else kpi_fat_tot.falta) }}</div>
            <div class="kpi-meta">{{ 'Acima da meta de faturamento' if kpi_fat_tot.falta <= 0 else 'Valor necessário para bater a meta' }}</div>
          </div>
        </div>
      </div>
      <div class="section-heading mt-2 mb-3"><div><h2>Performance por canal</h2><p>Interno e Externo no ciclo financeiro.</p></div></div>
      <div class="row g-3">
        <div class="col-12 col-xl-6">
          <div class="channel-card internal">
            <div class="d-flex justify-content-between align-items-center gap-2"><span class="channel-tag internal"><i class="bi bi-people-fill"></i> INTERNO</span><span class="small text-muted">Meta {{ format_brl(meta_fat_int) }}</span></div>
            <div class="d-flex justify-content-between align-items-end gap-3"><div><div class="channel-main">{{ format_brl(fat_int) }}</div><div class="small text-muted">faturado no ciclo</div></div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(kpi_fat_int.pct) }}%</div></div>
            <div class="progress mt-3" style="height:7px"><div class="progress-bar dash-bar-canal-int" style="width:{{ 100 if kpi_fat_int.pct > 100 else kpi_fat_int.pct }}%"></div></div>
            <div class="channel-status"><span>{{ qtd_fat_int }} notas</span></div>
            <div class="channel-meta"><div class="box"><div class="label">Ideal/dia</div><div class="value">{{ format_brl(kpi_fat_int.ideal) }}</div></div><div class="box"><div class="label">Ticket médio</div><div class="value">{{ format_brl(kpi_fat_int.ticket) }}</div></div><div class="box"><div class="label">Nec./dia</div><div class="value {{ 'text-danger' if kpi_fat_int.nec > kpi_fat_int.ideal else 'text-success' }}">{{ format_brl(kpi_fat_int.nec) }}</div></div></div>
          </div>
        </div>
        <div class="col-12 col-xl-6">
          <div class="channel-card external">
            <div class="d-flex justify-content-between align-items-center gap-2"><span class="channel-tag external"><i class="bi bi-person-workspace"></i> EXTERNO</span><span class="small text-muted">Meta {{ format_brl(meta_fat_ext) }}</span></div>
            <div class="d-flex justify-content-between align-items-end gap-3"><div><div class="channel-main">{{ format_brl(fat_ext) }}</div><div class="small text-muted">faturado no ciclo</div></div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(kpi_fat_ext.pct) }}%</div></div>
            <div class="progress mt-3" style="height:7px"><div class="progress-bar dash-bar-canal-ext" style="width:{{ 100 if kpi_fat_ext.pct > 100 else kpi_fat_ext.pct }}%"></div></div>
            <div class="channel-status"><span>{{ qtd_fat_ext }} notas</span></div>
            <div class="channel-meta"><div class="box"><div class="label">Ideal/dia</div><div class="value">{{ format_brl(kpi_fat_ext.ideal) }}</div></div><div class="box"><div class="label">Ticket médio</div><div class="value">{{ format_brl(kpi_fat_ext.ticket) }}</div></div><div class="box"><div class="label">Nec./dia</div><div class="value {{ 'text-danger' if kpi_fat_ext.nec > kpi_fat_ext.ideal else 'text-success' }}">{{ format_brl(kpi_fat_ext.nec) }}</div></div></div>
          </div>
        </div>
      </div>
    </div>
    {% endif %}

    {% if visao == 'pedidos' %}
    <div class="dash-block dash-block--ped mb-4">
      <div class="dash-block-head">
        <div>
          <span class="dash-pill dash-pill--ped"><i class="bi bi-bag-check"></i> Vendas &amp; Pedidos</span>
          <div class="sub">Mês comercial {{ d0_ped.strftime('%d/%m/%Y') }} a {{ d1_ped.strftime('%d/%m/%Y') }} · meta em Settings</div>
        </div>
      </div>
      <div class="row g-3 mb-3 align-items-start">
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Meta Pedidos</div><div class="icon meta"><i class="bi bi-bullseye"></i></div></div>
            <div class="kpi-value">{{ format_brl(meta_ped_total) }}</div>
            <div class="progress mt-2" style="height:8px"><div class="progress-bar dash-bar-meta" style="width:{{ 100 if kpi_tot.pct > 100 else kpi_tot.pct }}%"></div></div>
            <div class="kpi-meta">{{ '%.1f'|format(kpi_tot.pct) }}% atingido</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <a href="{{ url_for('dashboard_pedidos_detalhe', data_ref=mes_ref, periodo='mes') }}" class="text-decoration-none text-dark d-block">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">Total Realizado</div><div class="icon done"><i class="bi bi-cart3"></i></div></div>
            <div class="kpi-value">{{ format_brl(ped_total) }}</div>
            <div class="kpi-meta">Vendas validadas · clique para detalhar</div>
          </div>
          </a>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="d-flex justify-content-between align-items-start"><div class="kpi-label">{{ 'Meta superada' if kpi_tot.falta <= 0 else 'Falta vender' }}</div><div class="icon {{ 'ok' if kpi_tot.falta <= 0 else 'warn' }}"><i class="bi {{ 'bi-check-lg' if kpi_tot.falta <= 0 else 'bi-exclamation-lg' }}"></i></div></div>
            <div class="kpi-value">{{ format_brl((ped_total - meta_ped_total) if kpi_tot.falta <= 0 else kpi_tot.falta) }}</div>
            <div class="kpi-meta">{{ 'Acima da meta de pedidos' if kpi_tot.falta <= 0 else 'Valor necessário para bater a meta' }}</div>
          </div>
        </div>
      </div>

      <div class="section-heading mt-1 mb-3"><div><h2>Performance por canal</h2><p>Interno, Externo e Site no mês comercial.</p></div></div>
      <div class="row g-3 align-items-start">
        <div class="col-12 col-xl-4">
          <div class="channel-card internal">
            <div class="d-flex justify-content-between align-items-center gap-2"><span class="channel-tag internal"><i class="bi bi-people-fill"></i> INTERNO</span><span class="small text-muted">Meta {{ format_brl(meta_ped_int) }}</span></div>
            <div class="d-flex justify-content-between align-items-end gap-3"><div><div class="channel-main">{{ format_brl(ped_int) }}</div><div class="small text-muted">realizado em pedidos</div></div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(kpi_int.pct) }}%</div></div>
            <div class="progress mt-3" style="height:7px"><div class="progress-bar dash-bar-canal-int" style="width:{{ 100 if kpi_int.pct > 100 else kpi_int.pct }}%"></div></div>
            <div class="channel-status"><span>{{ qtd_ped_int }} pedidos</span><span>{{ qtd_fat_int }} faturados</span><span class="pending">{{ gap_qtd_int }} pendentes</span></div>
            <div class="channel-meta"><div class="box"><div class="label">Ideal/dia</div><div class="value">{{ format_brl(kpi_int.ideal) }}</div></div><div class="box"><div class="label">Ticket médio</div><div class="value">{{ format_brl(kpi_int.ticket) }}</div></div><div class="box"><div class="label">Nec./dia</div><div class="value {{ 'text-danger' if kpi_int.nec > kpi_int.ideal else 'text-success' }}">{{ format_brl(kpi_int.nec) }}</div></div></div>
          </div>
        </div>
        <div class="col-12 col-xl-4">
          <div class="channel-card external">
            <div class="d-flex justify-content-between align-items-center gap-2"><span class="channel-tag external"><i class="bi bi-person-workspace"></i> EXTERNO</span><span class="small text-muted">Meta {{ format_brl(meta_ped_ext) }}</span></div>
            <div class="d-flex justify-content-between align-items-end gap-3"><div><div class="channel-main">{{ format_brl(ped_ext) }}</div><div class="small text-muted">realizado em pedidos</div></div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(kpi_ext.pct) }}%</div></div>
            <div class="progress mt-3" style="height:7px"><div class="progress-bar dash-bar-canal-ext" style="width:{{ 100 if kpi_ext.pct > 100 else kpi_ext.pct }}%"></div></div>
            <div class="channel-status"><span>{{ qtd_ped_ext }} pedidos</span><span>{{ qtd_fat_ext }} faturados</span><span class="pending">{{ gap_qtd_ext }} pendentes</span></div>
            <div class="channel-meta"><div class="box"><div class="label">Ideal/dia</div><div class="value">{{ format_brl(kpi_ext.ideal) }}</div></div><div class="box"><div class="label">Ticket médio</div><div class="value">{{ format_brl(kpi_ext.ticket) }}</div></div><div class="box"><div class="label">Nec./dia</div><div class="value {{ 'text-danger' if kpi_ext.nec > kpi_ext.ideal else 'text-success' }}">{{ format_brl(kpi_ext.nec) }}</div></div></div>
          </div>
        </div>
        <div class="col-12 col-xl-4">
          <div class="channel-card site">
            <div class="d-flex justify-content-between align-items-center gap-2"><span class="channel-tag site"><i class="bi bi-globe2"></i> SITE / MARKETPLACE</span><a class="btn btn-sm" style="background:#7c3aed;color:#fff" href="{{ url_for('site_analytics', data_ref=d0_ped.strftime('%Y-%m')) }}">Analisar</a></div>
            <div><div class="channel-main" style="color:#7c3aed">{{ format_brl(ped_site) }}</div><div class="small text-muted">realizado em pedidos do canal</div></div>
            <div class="channel-status"><span>{{ qtd_ped_site }} pedidos</span><span>{{ qtd_fat_site }} faturados</span><span class="pending">{{ gap_qtd_site }} pendentes</span></div>
            <div class="channel-meta"><div class="box"><div class="label">Faturamento</div><div class="value">{{ format_brl(fat_site) }}</div></div><div class="box"><div class="label">Ticket médio</div><div class="value">{{ format_brl(ticket_site) }}</div></div><div class="box"><div class="label">Liquidez</div><div class="value">{{ format_brl(liq_site) }}</div></div></div>
          </div>
        </div>
      </div>
    </div>

    <div class="section-heading"><div><h2>Leitura visual do mês</h2><p>Gráficos compactos, cada um respondendo a uma pergunta diferente.</p></div></div>
    <div class="row g-4 mt-1">
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="d-flex justify-content-between align-items-end mb-3"><div><div class="h6 fw-bold mb-1">Ritmo diário de vendas</div><div class="chart-caption">Pedidos válidos vs faturamento do período comercial</div></div></div>{% if line_tot %}<div class="chart-frame chart-lg"><img class="chart-img" src="data:image/png;base64,{{ line_tot }}"></div>{% endif %}</div></div>
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-1">Status da carteira</div><div class="chart-caption mb-3">Pedidos faturados e pedidos ainda pendentes</div>{% if pie_status %}<div class="chart-frame chart-lg"><img class="chart-img" src="data:image/png;base64,{{ pie_status }}"></div>{% endif %}<div class="row g-2 mt-2"><div class="col-4"><div class="chip chip-success w-100 justify-content-center">{{ fat_qtd }} faturados</div></div><div class="col-4"><div class="chip chip-warning w-100 justify-content-center">{{ gap_qtd }} pendentes</div></div><div class="col-4"><div class="chip w-100 justify-content-center">{{ ped_qtd }} total</div></div></div></div></div>
    </div>
    <div class="row g-4 mt-1">
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-1">Equilíbrio entre equipes</div><div class="chart-caption mb-3">Comparativo diário entre time interno e externo</div>{% if line_comp %}<div class="chart-frame"><img class="chart-img" src="data:image/png;base64,{{ line_comp }}"></div>{% endif %}</div></div>
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-1">Nova carteira x recompra</div><div class="chart-caption mb-3">Mostra se o mês está sendo sustentado por clientes novos ou recorrentes</div>{% if pie_novo %}<div class="chart-frame"><img class="chart-img" src="data:image/png;base64,{{ pie_novo }}"></div>{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div>
    </div>
    <div class="row g-4 mt-1">
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-1">Quem mais vendeu</div><div class="chart-caption mb-3">Ranking por valor realizado em pedidos</div>{% if top_bar %}<div class="chart-frame chart-lg"><img class="chart-img" src="data:image/png;base64,{{ top_bar }}"></div>{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div>
        <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-1">Clientes mais relevantes</div><div class="chart-caption mb-3">Concentração dos maiores clientes no período</div>{% if top_clientes_chart %}<div class="chart-frame chart-lg"><img class="chart-img" src="data:image/png;base64,{{ top_clientes_chart }}"></div>{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div>
    </div>
    <div class="row mt-4">
      <div class="col-12"><div class="cardx p-4 h-100"><div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2"><div><div class="h6 fw-bold mb-1">Campanha por Medida</div><div class="chart-caption">Tabela completa do período • o gráfico resume Top 5 e Bottom 5 • {{ total_pedidos_periodo }} pedidos</div></div></div>{% if top_medidas_chart %}<div class="mb-3 campaign-chart-wrap"><img class="chart-img campaign-chart-img" src="data:image/png;base64,{{ top_medidas_chart }}"></div><div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>Medida</th><th class="text-end">Pedidos</th><th class="text-end">%</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in top_medidas_rows %}<tr><td>{{ r['medida'] }}</td><td class="text-end">{{ r['qtd_pedidos'] }}</td><td class="text-end">{{ '%.2f'|format(r['pct_pedidos'] or 0) }}%</td><td class="text-end">{{ format_brl(r['valor_total'] or 0) }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div>
    </div>
    <div class="row mt-4">
      <div class="col-12">
        <div class="cardx p-4">
          <div class="h6 fw-bold mb-3">Últimos Pedidos Tirados</div>
          <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
              <thead><tr><th>Data</th><th>Pedido</th><th>Cliente</th><th>Vendedor</th><th>Medida</th><th class="text-end">Valor</th></tr></thead>
              <tbody>
                {% for uv in ultimos_pedidos %}
                <tr>
                  <td class="muted">{{ uv["data_emissao"][5:] if uv["data_emissao"] else "-" }}</td>
                  <td class="text-primary fw-bold">{{ uv["numero"] or "-" }}</td>
                  <td>{{ (uv["cliente"] or "-")[:34] }}</td>
                  <td>{{ uv["vend"] or "-" }}</td>
                  <td>{{ uv["medida"] or "-" }}</td>
                  <td class="text-end fw-bold">{{ format_brl(uv["valor"] or 0) }}</td>
                </tr>
                {% else %}
                <tr><td colspan="6" class="text-center py-3">Sem dados.</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    {% endif %}
    """,
    d0_ped=d0_ped, d1_ped=d1_ped, d0_fat=d0_fat, d1_fat=d1_fat,
    meta_total=meta_total, meta_int=meta_int, meta_ext=meta_ext,
    meta_ped_total=meta_ped_total, meta_ped_int=meta_ped_int, meta_ped_ext=meta_ped_ext,
    meta_fat_total=meta_fat_total, meta_fat_int=meta_fat_int, meta_fat_ext=meta_fat_ext,
    ped_total=ped_total, ped_int=ped_int, ped_ext=ped_ext,
    fat_total=fat_total, fat_int=fat_int, fat_ext=fat_ext,
    kpi_tot=kpi_tot, kpi_int=kpi_int, kpi_ext=kpi_ext,
    kpi_fat_tot=kpi_fat_tot, kpi_fat_int=kpi_fat_int, kpi_fat_ext=kpi_fat_ext,
    diff_val=diff_val, pct_quebra=pct_quebra, pct_realizado=pct_realizado, falta=falta,
    ped_qtd=ped_qtd, fat_qtd=fat_qtd, gap_qtd=gap_qtd,
    qtd_ped_int=qtd_ped_int, qtd_fat_int=qtd_fat_int, gap_qtd_int=gap_qtd_int,
    qtd_ped_ext=qtd_ped_ext, qtd_fat_ext=qtd_fat_ext, gap_qtd_ext=gap_qtd_ext,
    qtd_ped_site=qtd_ped_site, qtd_fat_site=qtd_fat_site, gap_qtd_site=gap_qtd_site,
    ped_site=ped_site, fat_site=fat_site, liq_site=liq_site, ticket_site=ticket_site,
    line_comp=line_comp, line_tot=line_tot, top_bar=top_bar, pie_novo=pie_novo, pie_status=pie_status,
    top_clientes_chart=top_clientes_chart, top_medidas_chart=top_medidas_chart,
    chart_yoy=chart_yoy, ultimos_pedidos=ultimos_pedidos,
    metrics_orc=metrics_orc, top_medidas_rows=top_medidas_rows, total_pedidos_periodo=total_pedidos_periodo,
    mes_ref=mes_ref, visao=visao,
    format_brl=format_brl
    )

    return render_page("Dashboard", body, "dashboard")


_MESES_PT_CURTO = (
    (1, "Jan"), (2, "Fev"), (3, "Mar"), (4, "Abr"), (5, "Mai"), (6, "Jun"),
    (7, "Jul"), (8, "Ago"), (9, "Set"), (10, "Out"), (11, "Nov"), (12, "Dez"),
)


def _parse_mes_ano(valor, default: int, minimo: int = 1, maximo: int = 12) -> int:
    try:
        n = int(str(valor or "").strip())
    except Exception:
        return default
    return max(minimo, min(maximo, n))


def _intervalo_meses_calendario(ano: int, mes_ini: int, mes_fim: int) -> tuple[date, date]:
    """Primeiro dia do mês inicial até o último dia do mês final (mesmo ano)."""
    if mes_fim < mes_ini:
        mes_fim = mes_ini
    d0 = date(ano, mes_ini, 1)
    if mes_fim == 12:
        d1 = date(ano, 12, 31)
    else:
        d1 = date(ano, mes_fim + 1, 1) - timedelta(days=1)
    return d0, d1


_DESEMP_JOIN = """
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        LEFT JOIN customers c ON c.id = i.cliente_id
"""
_DESEMP_BASE_WHERE = """
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
"""


def _desempenho_filtros_request() -> dict:
    equipe = (request.args.get("equipe") or "").strip().upper()
    if equipe not in ("INTERNO", "EXTERNO"):
        equipe = ""
    vendor_raw = (request.args.get("vendedor") or "").strip()
    try:
        vendedor_id = int(vendor_raw) if vendor_raw else 0
    except Exception:
        vendedor_id = 0
    return {
        "equipe": equipe,
        "vendedor_id": vendedor_id,
        "grupo": (request.args.get("grupo") or "").strip(),
        "formato": (request.args.get("formato") or "").strip(),
        "papel": (request.args.get("papel") or "").strip(),
        "uf": (request.args.get("uf") or "").strip().upper(),
        "cidade": (request.args.get("cidade") or "").strip(),
    }


def _desempenho_filtro_sql(filtros: dict) -> tuple[str, list]:
    clauses: List[str] = []
    params: list = []
    if filtros.get("equipe") in ("INTERNO", "EXTERNO"):
        clauses.append("COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') = ?")
        params.append(filtros["equipe"])
    if int(filtros.get("vendedor_id") or 0) > 0:
        clauses.append("i.vendor_id = ?")
        params.append(int(filtros["vendedor_id"]))
    if filtros.get("grupo"):
        clauses.append("UPPER(TRIM(COALESCE(i.grupo_ref,''))) = UPPER(?)")
        params.append(filtros["grupo"])
    if filtros.get("formato"):
        clauses.append(
            "UPPER(TRIM(COALESCE(NULLIF(TRIM(i.formato),''), NULLIF(TRIM(i.medida_ref),''), ''))) = UPPER(?)"
        )
        params.append(filtros["formato"])
    if filtros.get("papel"):
        clauses.append("UPPER(TRIM(COALESCE(i.papel_ref,''))) = UPPER(?)")
        params.append(filtros["papel"])
    if filtros.get("uf"):
        # Geo costuma vir do orçamento (Sisplan); customers.uf é opcional/legado.
        clauses.append(
            """(
              UPPER(TRIM(COALESCE(c.uf,''))) = UPPER(?)
              OR EXISTS (
                SELECT 1
                FROM orcamentos ox
                WHERE TRIM(COALESCE(c.codigo,'')) <> ''
                  AND TRIM(c.codigo) = TRIM(COALESCE(ox.cliente_codigo,''))
                  AND UPPER(TRIM(COALESCE(ox.uf,''))) = UPPER(?)
              )
            )"""
        )
        params.extend([filtros["uf"], filtros["uf"]])
    if filtros.get("cidade"):
        clauses.append(
            """(
              UPPER(TRIM(COALESCE(c.cidade,''))) = UPPER(?)
              OR EXISTS (
                SELECT 1
                FROM orcamentos ox
                WHERE TRIM(COALESCE(c.codigo,'')) <> ''
                  AND TRIM(c.codigo) = TRIM(COALESCE(ox.cliente_codigo,''))
                  AND UPPER(TRIM(COALESCE(ox.cidade,''))) = UPPER(?)
              )
            )"""
        )
        params.extend([filtros["cidade"], filtros["cidade"]])
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _desempenho_filtros_ativos(filtros: dict, vendor_nome: str = "") -> List[str]:
    labels = []
    if filtros.get("equipe"):
        labels.append("Equipe " + str(filtros["equipe"]).title())
    if int(filtros.get("vendedor_id") or 0) > 0:
        labels.append(vendor_nome or "Vendedor")
    if filtros.get("grupo"):
        labels.append(filtros["grupo"])
    if filtros.get("formato"):
        labels.append(filtros["formato"])
    if filtros.get("papel"):
        labels.append(filtros["papel"])
    if filtros.get("uf"):
        labels.append(filtros["uf"])
    if filtros.get("cidade"):
        labels.append(filtros["cidade"])
    return labels


def _ensure_customers_geo_cols(conn) -> None:
    """Garante colunas de geo em customers (bases antigas podem não ter)."""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(customers)").fetchall()}
    if "uf" not in cols:
        try:
            conn.execute("ALTER TABLE customers ADD COLUMN uf TEXT")
        except Exception:
            pass
    if "cidade" not in cols:
        try:
            conn.execute("ALTER TABLE customers ADD COLUMN cidade TEXT")
        except Exception:
            pass


def _desempenho_opcoes_filtro(conn, filtros: dict) -> dict:
    _ensure_customers_geo_cols(conn)
    vendors = conn.execute(
        """
        SELECT id, nome, COALESCE(UPPER(TRIM(tipo)),'EXTERNO') AS tipo
        FROM vendors
        WHERE COALESCE(UPPER(TRIM(nome)),'') <> 'SITE'
          AND COALESCE(UPPER(TRIM(tipo)),'') <> 'SITE'
          AND COALESCE(TRIM(nome),'') <> ''
          AND COALESCE(ativo,1)=1
        ORDER BY nome
        """
    ).fetchall()
    grupos = [
        str(r[0]).strip()
        for r in conn.execute(
            "SELECT DISTINCT TRIM(grupo_ref) FROM invoices WHERE TRIM(COALESCE(grupo_ref,''))<>'' ORDER BY 1"
        ).fetchall()
        if r[0]
    ]
    formatos = [
        str(r[0]).strip()
        for r in conn.execute(
            """
            SELECT DISTINCT TRIM(COALESCE(NULLIF(TRIM(formato),''), NULLIF(TRIM(medida_ref),''), ''))
            FROM invoices
            WHERE TRIM(COALESCE(NULLIF(TRIM(formato),''), NULLIF(TRIM(medida_ref),''), ''))<>''
            ORDER BY 1
            """
        ).fetchall()
        if r[0]
    ]
    papeis = [
        str(r[0]).strip()
        for r in conn.execute(
            "SELECT DISTINCT TRIM(papel_ref) FROM invoices WHERE TRIM(COALESCE(papel_ref,''))<>'' ORDER BY 1"
        ).fetchall()
        if r[0]
    ]
    # Estado/cidade: customers (se preenchido) + orçamentos (fonte usual no Sisplan).
    ufs = [
        str(r[0]).strip().upper()
        for r in conn.execute(
            """
            SELECT DISTINCT u FROM (
              SELECT UPPER(TRIM(uf)) AS u FROM customers WHERE TRIM(COALESCE(uf,''))<>''
              UNION
              SELECT UPPER(TRIM(uf)) AS u FROM orcamentos WHERE TRIM(COALESCE(uf,''))<>''
            ) x
            ORDER BY 1
            """
        ).fetchall()
        if r[0]
    ]
    if filtros.get("uf"):
        cidades = [
            str(r[0]).strip()
            for r in conn.execute(
                """
                SELECT DISTINCT c FROM (
                  SELECT TRIM(cidade) AS c FROM customers
                  WHERE TRIM(COALESCE(cidade,''))<>'' AND UPPER(TRIM(COALESCE(uf,''))) = UPPER(?)
                  UNION
                  SELECT TRIM(cidade) AS c FROM orcamentos
                  WHERE TRIM(COALESCE(cidade,''))<>'' AND UPPER(TRIM(COALESCE(uf,''))) = UPPER(?)
                ) x
                ORDER BY 1
                """,
                (filtros["uf"], filtros["uf"]),
            ).fetchall()
            if r[0]
        ]
    else:
        cidades = [
            str(r[0]).strip()
            for r in conn.execute(
                """
                SELECT DISTINCT c FROM (
                  SELECT TRIM(cidade) AS c FROM customers WHERE TRIM(COALESCE(cidade,''))<>''
                  UNION
                  SELECT TRIM(cidade) AS c FROM orcamentos WHERE TRIM(COALESCE(cidade,''))<>''
                ) x
                ORDER BY 1
                LIMIT 800
                """
            ).fetchall()
            if r[0]
        ]
    return {
        "vendors": vendors,
        "grupos": grupos,
        "formatos": formatos,
        "papeis": papeis,
        "ufs": ufs,
        "cidades": cidades,
    }


def _soma_faturamento_periodo(
    conn, d0: date, d1: date, filtro_sql: str = "", filtro_params: Optional[list] = None
) -> tuple[float, int, int, float]:
    """Faturamento do intervalo (sem SITE): valor, qtd notas, clientes e unidades."""
    params = [d0.isoformat(), d1.isoformat()] + list(filtro_params or [])
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(i.valor),0) AS val,
            COUNT(*) AS qtd,
            COUNT(DISTINCT CASE WHEN COALESCE(i.cliente_id,0) > 0 THEN i.cliente_id END) AS clientes,
            COALESCE(SUM(COALESCE(i.quantidade,0)),0) AS unidades
        {_DESEMP_JOIN}
        WHERE i.data_emissao IS NOT NULL
          AND date(i.data_emissao) BETWEEN date(?) AND date(?)
          {_DESEMP_BASE_WHERE}
          {filtro_sql}
        """,
        params,
    ).fetchone()
    return (
        float(row["val"] or 0),
        int(row["qtd"] or 0),
        int(row["clientes"] or 0),
        float(row["unidades"] or 0),
    )


def _fat_mensal_ano(
    conn, ano: int, mes_ini: int, mes_fim: int, filtro_sql: str = "", filtro_params: Optional[list] = None
) -> List[float]:
    """12 valores mensais; fora do intervalo selecionado = NaN (não desenha a linha)."""
    params = [ano, mes_ini, mes_fim] + list(filtro_params or [])
    rows = conn.execute(
        f"""
        SELECT CAST(strftime('%m', i.data_emissao) AS INTEGER) AS m,
               COALESCE(SUM(i.valor),0) AS val
        {_DESEMP_JOIN}
        WHERE i.data_emissao IS NOT NULL
          AND CAST(strftime('%Y', i.data_emissao) AS INTEGER) = ?
          AND CAST(strftime('%m', i.data_emissao) AS INTEGER) BETWEEN ? AND ?
          {_DESEMP_BASE_WHERE}
          {filtro_sql}
        GROUP BY m
        """,
        params,
    ).fetchall()
    by_m = {int(r["m"]): float(r["val"] or 0) for r in rows}
    out: List[float] = []
    for m in range(1, 13):
        if mes_ini <= m <= mes_fim:
            out.append(by_m.get(m, 0.0))
        else:
            out.append(float("nan"))
    return out


def _fat_mensal_por_equipe(
    conn, ano: int, mes_ini: int, mes_fim: int, filtro_sql: str = "", filtro_params: Optional[list] = None
) -> Dict[int, Dict[str, float]]:
    """Faturamento mensal por time (INTERNO/EXTERNO) no intervalo do ano."""
    params = [ano, mes_ini, mes_fim] + list(filtro_params or [])
    rows = conn.execute(
        f"""
        SELECT CAST(strftime('%m', i.data_emissao) AS INTEGER) AS m,
               COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') AS tipo,
               COALESCE(SUM(i.valor),0) AS val
        {_DESEMP_JOIN}
        WHERE i.data_emissao IS NOT NULL
          AND CAST(strftime('%Y', i.data_emissao) AS INTEGER) = ?
          AND CAST(strftime('%m', i.data_emissao) AS INTEGER) BETWEEN ? AND ?
          {_DESEMP_BASE_WHERE}
          {filtro_sql}
        GROUP BY m, COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO')
        """,
        params,
    ).fetchall()
    out: Dict[int, Dict[str, float]] = {
        m: {"INTERNO": 0.0, "EXTERNO": 0.0} for m in range(mes_ini, mes_fim + 1)
    }
    for r in rows:
        m = int(r["m"] or 0)
        if m not in out:
            continue
        tipo = str(r["tipo"] or "EXTERNO").upper()
        if tipo == "INTERNO":
            out[m]["INTERNO"] = float(r["val"] or 0)
        elif tipo == "EXTERNO":
            out[m]["EXTERNO"] = float(r["val"] or 0)
    return out


def _fat_por_equipe(
    conn, d0: date, d1: date, filtro_sql: str = "", filtro_params: Optional[list] = None
) -> dict:
    params = [d0.isoformat(), d1.isoformat()] + list(filtro_params or [])
    rows = conn.execute(
        f"""
        SELECT COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') AS tipo,
               COALESCE(SUM(i.valor),0) AS val
        {_DESEMP_JOIN}
        WHERE i.data_emissao IS NOT NULL
          AND date(i.data_emissao) BETWEEN date(?) AND date(?)
          {_DESEMP_BASE_WHERE}
          {filtro_sql}
        GROUP BY COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO')
        """,
        params,
    ).fetchall()
    out = {"INTERNO": 0.0, "EXTERNO": 0.0}
    for r in rows:
        tipo = str(r["tipo"] or "EXTERNO").upper()
        if tipo == "INTERNO":
            out["INTERNO"] = float(r["val"] or 0)
        elif tipo == "EXTERNO":
            out["EXTERNO"] = float(r["val"] or 0)
    return out


def _rotulo_periodo_meses(ano: int, mes_ini: int, mes_fim: int) -> str:
    nomes = dict(_MESES_PT_CURTO)
    if mes_ini == mes_fim:
        return f"{nomes.get(mes_ini, mes_ini)} {ano}"
    return f"{nomes.get(mes_ini, mes_ini)} - {nomes.get(mes_fim, mes_fim)} {ano}"


def _delta_pct(atual: float, base: float) -> float:
    if base > 0:
        return ((atual / base) - 1.0) * 100.0
    return 100.0 if atual > 0 else 0.0


@app.route("/desempenho")
@login_required
def desempenho():
    """Comparativo de faturamento entre dois intervalos de meses."""
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    hoje = date.today()
    anos = list(range(hoje.year, hoje.year - 8, -1))

    ref_ano = _parse_mes_ano(request.args.get("ref_ano"), hoje.year, 2000, hoje.year + 1)
    ref_mes_ini = _parse_mes_ano(request.args.get("ref_mes_ini"), 1)
    ref_mes_fim = _parse_mes_ano(request.args.get("ref_mes_fim"), hoje.month)
    cmp_ano = _parse_mes_ano(request.args.get("cmp_ano"), hoje.year, 2000, hoje.year + 1)
    cmp_mes_ini = _parse_mes_ano(request.args.get("cmp_mes_ini"), 1)
    cmp_mes_fim = _parse_mes_ano(
        request.args.get("cmp_mes_fim"),
        max(1, hoje.month - 1) if hoje.month > 1 else 1,
    )
    if ref_mes_fim < ref_mes_ini:
        ref_mes_fim = ref_mes_ini
    if cmp_mes_fim < cmp_mes_ini:
        cmp_mes_fim = cmp_mes_ini

    d0_ref, d1_ref = _intervalo_meses_calendario(ref_ano, ref_mes_ini, ref_mes_fim)
    d0_cmp, d1_cmp = _intervalo_meses_calendario(cmp_ano, cmp_mes_ini, cmp_mes_fim)

    filtros = _desempenho_filtros_request()
    filtro_sql, filtro_params = _desempenho_filtro_sql(filtros)

    with conectar() as conn:
        opcoes = _desempenho_opcoes_filtro(conn, filtros)
        vendor_nome = ""
        if filtros.get("vendedor_id"):
            for v in opcoes["vendors"]:
                if int(v["id"]) == int(filtros["vendedor_id"]):
                    vendor_nome = str(v["nome"] or "")
                    break
        filtros_ativos = _desempenho_filtros_ativos(filtros, vendor_nome)
        fat_ref, notas_ref, cli_ref, unid_ref = _soma_faturamento_periodo(
            conn, d0_ref, d1_ref, filtro_sql, filtro_params
        )
        fat_cmp, notas_cmp, cli_cmp, unid_cmp = _soma_faturamento_periodo(
            conn, d0_cmp, d1_cmp, filtro_sql, filtro_params
        )
        mensal_ref = _fat_mensal_ano(conn, ref_ano, ref_mes_ini, ref_mes_fim, filtro_sql, filtro_params)
        mensal_cmp = _fat_mensal_ano(conn, cmp_ano, cmp_mes_ini, cmp_mes_fim, filtro_sql, filtro_params)
        fat_eq_ref = _fat_por_equipe(conn, d0_ref, d1_ref, filtro_sql, filtro_params)
        fat_eq_cmp = _fat_por_equipe(conn, d0_cmp, d1_cmp, filtro_sql, filtro_params)
        fat_mes_eq_ref = _fat_mensal_por_equipe(
            conn, ref_ano, ref_mes_ini, ref_mes_fim, filtro_sql, filtro_params
        )
        fat_mes_eq_cmp = _fat_mensal_por_equipe(
            conn, cmp_ano, cmp_mes_ini, cmp_mes_fim, filtro_sql, filtro_params
        )

    ticket_ref = (fat_ref / notas_ref) if notas_ref > 0 else 0.0
    ticket_cmp = (fat_cmp / notas_cmp) if notas_cmp > 0 else 0.0

    delta_fat = fat_cmp - fat_ref
    delta_fat_pct = _delta_pct(fat_cmp, fat_ref)
    delta_unid = unid_cmp - unid_ref
    delta_unid_pct = _delta_pct(unid_cmp, unid_ref)
    delta_cli = cli_cmp - cli_ref
    delta_cli_pct = _delta_pct(float(cli_cmp), float(cli_ref))
    delta_ticket = ticket_cmp - ticket_ref
    delta_ticket_pct = _delta_pct(ticket_cmp, ticket_ref)

    label_ref = _rotulo_periodo_meses(ref_ano, ref_mes_ini, ref_mes_fim)
    label_cmp = _rotulo_periodo_meses(cmp_ano, cmp_mes_ini, cmp_mes_fim)
    meses_labels = [n for _, n in _MESES_PT_CURTO]
    chart_evol = desempenho_evolucao_chart(
        meses_labels,
        (label_ref, mensal_ref),
        (label_cmp, mensal_cmp),
    )

    max_eq = max(
        fat_eq_ref["INTERNO"], fat_eq_ref["EXTERNO"],
        fat_eq_cmp["INTERNO"], fat_eq_cmp["EXTERNO"],
        1.0,
    )
    equipes = []
    for tipo, nome in (("INTERNO", "Equipe interna"), ("EXTERNO", "Equipe externa")):
        v_ref = float(fat_eq_ref.get(tipo) or 0)
        v_cmp = float(fat_eq_cmp.get(tipo) or 0)
        equipes.append({
            "nome": nome,
            "ref": v_ref,
            "cmp": v_cmp,
            "pct": _delta_pct(v_cmp, v_ref),
            "w_ref": (v_ref / max_eq) * 100.0,
            "w_cmp": (v_cmp / max_eq) * 100.0,
        })

    mes_ini_tab = min(ref_mes_ini, cmp_mes_ini)
    mes_fim_tab = max(ref_mes_fim, cmp_mes_fim)
    nomes_mes = dict(_MESES_PT_CURTO)
    tabela_meses = []
    tot_ref_int = tot_ref_ext = tot_cmp_int = tot_cmp_ext = 0.0
    for m in range(mes_ini_tab, mes_fim_tab + 1):
        in_ref = ref_mes_ini <= m <= ref_mes_fim
        in_cmp = cmp_mes_ini <= m <= cmp_mes_fim
        r_int = float(fat_mes_eq_ref.get(m, {}).get("INTERNO") or 0) if in_ref else None
        r_ext = float(fat_mes_eq_ref.get(m, {}).get("EXTERNO") or 0) if in_ref else None
        c_int = float(fat_mes_eq_cmp.get(m, {}).get("INTERNO") or 0) if in_cmp else None
        c_ext = float(fat_mes_eq_cmp.get(m, {}).get("EXTERNO") or 0) if in_cmp else None
        if r_int is not None:
            tot_ref_int += r_int
            tot_ref_ext += float(r_ext or 0)
        if c_int is not None:
            tot_cmp_int += c_int
            tot_cmp_ext += float(c_ext or 0)
        var_int = _delta_pct(float(c_int or 0), float(r_int or 0)) if (in_ref and in_cmp) else None
        var_ext = _delta_pct(float(c_ext or 0), float(r_ext or 0)) if (in_ref and in_cmp) else None
        diff_ei = (float(c_ext or 0) - float(c_int or 0)) if in_cmp else None
        tabela_meses.append({
            "mes": nomes_mes.get(m, str(m)),
            "ref_int": r_int,
            "ref_ext": r_ext,
            "cmp_int": c_int,
            "cmp_ext": c_ext,
            "var_int": var_int,
            "var_ext": var_ext,
            "diff_ei": diff_ei,
        })
    tabela_total = {
        "ref_int": tot_ref_int,
        "ref_ext": tot_ref_ext,
        "cmp_int": tot_cmp_int,
        "cmp_ext": tot_cmp_ext,
        "var_int": _delta_pct(tot_cmp_int, tot_ref_int),
        "var_ext": _delta_pct(tot_cmp_ext, tot_ref_ext),
        "diff_ei": tot_cmp_ext - tot_cmp_int,
    }

    body = render_template_string(
        r"""
    <div class="mb-3">
      <div class="h3 mb-1 fw-bold">Desempenho comercial</div>
      <div class="muted small">Comparativo de faturamento entre dois intervalos de meses.</div>
    </div>

    <form method="get" class="mb-4" id="desempenhoFiltro">
      <div class="period-compare mb-3">
        <div class="period-compare-side cmp">
          <div class="label-row"><span class="dot"></span> Período de comparação</div>
          <div class="period-compare-fields">
            <select name="cmp_ano" onchange="this.form.submit()">
              {% for a in anos %}<option value="{{ a }}" {% if a == cmp_ano %}selected{% endif %}>{{ a }}</option>{% endfor %}
            </select>
            <select name="cmp_mes_ini" onchange="this.form.submit()">
              {% for m, n in meses %}<option value="{{ m }}" {% if m == cmp_mes_ini %}selected{% endif %}>{{ n }}</option>{% endfor %}
            </select>
            <span class="ate">até</span>
            <select name="cmp_mes_fim" onchange="this.form.submit()">
              {% for m, n in meses %}<option value="{{ m }}" {% if m == cmp_mes_fim %}selected{% endif %}>{{ n }}</option>{% endfor %}
            </select>
          </div>
        </div>
        <div class="period-compare-vs"><span>VS</span></div>
        <div class="period-compare-side ref">
          <div class="label-row"><span class="dot"></span> Período de referência</div>
          <div class="period-compare-fields">
            <select name="ref_ano" onchange="this.form.submit()">
              {% for a in anos %}<option value="{{ a }}" {% if a == ref_ano %}selected{% endif %}>{{ a }}</option>{% endfor %}
            </select>
            <select name="ref_mes_ini" onchange="this.form.submit()">
              {% for m, n in meses %}<option value="{{ m }}" {% if m == ref_mes_ini %}selected{% endif %}>{{ n }}</option>{% endfor %}
            </select>
            <span class="ate">até</span>
            <select name="ref_mes_fim" onchange="this.form.submit()">
              {% for m, n in meses %}<option value="{{ m }}" {% if m == ref_mes_fim %}selected{% endif %}>{{ n }}</option>{% endfor %}
            </select>
          </div>
        </div>
      </div>

      <div class="desemp-extra-filter cardx">
        <div class="desemp-extra-filter-bar">
          <div class="d-flex align-items-center gap-2 flex-wrap min-w-0">
            <i class="bi bi-funnel text-primary"></i>
            <span class="fw-bold">Filtros adicionais</span>
            {% if filtros_ativos %}
              <span class="desemp-filter-count">{{ filtros_ativos|length }} ativo{{ 's' if filtros_ativos|length != 1 else '' }}</span>
              {% for t in filtros_ativos[:4] %}<span class="chip">{{ t }}</span>{% endfor %}
              {% if filtros_ativos|length > 4 %}<span class="chip">+{{ filtros_ativos|length - 4 }}</span>{% endif %}
            {% else %}
              <span class="small text-muted">Equipe, vendedor, produto, formato, papel, UF e cidade</span>
            {% endif %}
          </div>
          <button class="btn btn-sm btn-outline-primary" type="button" data-bs-toggle="collapse" data-bs-target="#desempFiltrosExtra" aria-expanded="{{ 'true' if filtros_ativos else 'false' }}">
            <i class="bi bi-sliders me-1"></i><span class="desemp-filter-toggle-label">{{ 'Ocultar' if filtros_ativos else 'Mostrar' }}</span>
          </button>
        </div>
        <div class="collapse {% if filtros_ativos %}show{% endif %}" id="desempFiltrosExtra">
          <div class="desemp-extra-filter-body">
            <div class="row g-3">
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Equipe</label>
                <select class="form-select" name="equipe">
                  <option value="">Todas</option>
                  <option value="INTERNO" {% if filtros.equipe == 'INTERNO' %}selected{% endif %}>Interna</option>
                  <option value="EXTERNO" {% if filtros.equipe == 'EXTERNO' %}selected{% endif %}>Externa</option>
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Vendedor</label>
                <select class="form-select" name="vendedor">
                  <option value="">Todos</option>
                  {% for v in opcoes.vendors %}
                    <option value="{{ v.id }}" {% if filtros.vendedor_id == v.id %}selected{% endif %}>{{ v.nome }}{% if v.tipo %} ({{ v.tipo|title }}){% endif %}</option>
                  {% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Grupo de produto</label>
                <select class="form-select" name="grupo">
                  <option value="">Todos</option>
                  {% for g in opcoes.grupos %}<option value="{{ g }}" {% if filtros.grupo == g %}selected{% endif %}>{{ g }}</option>{% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Formato</label>
                <select class="form-select" name="formato">
                  <option value="">Todos</option>
                  {% for f in opcoes.formatos %}<option value="{{ f }}" {% if filtros.formato == f %}selected{% endif %}>{{ f }}</option>{% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Tipo de papel</label>
                <select class="form-select" name="papel">
                  <option value="">Todos</option>
                  {% for p in opcoes.papeis %}<option value="{{ p }}" {% if filtros.papel == p %}selected{% endif %}>{{ p }}</option>{% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Estado</label>
                <select class="form-select" name="uf" onchange="this.form.submit()">
                  <option value="">Todos</option>
                  {% for u in opcoes.ufs %}<option value="{{ u }}" {% if filtros.uf == u %}selected{% endif %}>{{ u }}</option>{% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3">
                <label class="form-label fw-bold small mb-1">Cidade</label>
                <select class="form-select" name="cidade">
                  <option value="">Todas</option>
                  {% for c in opcoes.cidades %}<option value="{{ c }}" {% if filtros.cidade == c %}selected{% endif %}>{{ c }}</option>{% endfor %}
                </select>
              </div>
              <div class="col-12 col-md-6 col-xl-3 d-flex align-items-end gap-2">
                <button type="submit" class="btn btn-primary flex-grow-1"><i class="bi bi-check2 me-1"></i>Aplicar</button>
                <a class="btn btn-light border" href="{{ url_for('desempenho', cmp_ano=cmp_ano, cmp_mes_ini=cmp_mes_ini, cmp_mes_fim=cmp_mes_fim, ref_ano=ref_ano, ref_mes_ini=ref_mes_ini, ref_mes_fim=ref_mes_fim) }}" title="Limpar filtros"><i class="bi bi-x-lg"></i></a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </form>
    <script>
      (function(){
        var el = document.getElementById('desempFiltrosExtra');
        var label = document.querySelector('.desemp-filter-toggle-label');
        if(!el || !label) return;
        el.addEventListener('show.bs.collapse', function(){ label.textContent = 'Ocultar'; });
        el.addEventListener('hide.bs.collapse', function(){ label.textContent = 'Mostrar'; });
      })();
    </script>

    <div class="dash-block dash-block--fat mb-3">
      <div class="dash-block-head mb-3">
        <div>
          <div class="h5 fw-bold mb-0">Faturamento</div>
          <div class="sub">Valor faturado no período</div>
        </div>
      </div>
      <div class="row g-3">
        <div class="col-12 col-md-4">
          <div class="dash-kpi dash-kpi--cmp">
            <div class="dash-pill mb-2"><i class="bi bi-clock-history"></i> Comparação</div>
            <div class="small mb-1">{{ d0_cmp.strftime('%d/%m/%Y') }} a {{ d1_cmp.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ format_brl(fat_cmp) }}</div>
            <div class="kpi-meta">{{ notas_cmp }} notas</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill dash-pill--fat mb-2"><i class="bi bi-calendar3"></i> Referência</div>
            <div class="small text-muted mb-1">{{ d0_ref.strftime('%d/%m/%Y') }} a {{ d1_ref.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ format_brl(fat_ref) }}</div>
            <div class="kpi-meta">{{ notas_ref }} notas</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill mb-2" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0"><i class="bi bi-arrow-left-right"></i> Diferença</div>
            <div class="small text-muted mb-1">Comparação − Referência</div>
            <div class="kpi-value {{ 'text-success' if delta_fat >= 0 else 'text-danger' }}">{{ format_brl(delta_fat) }}</div>
            <div class="kpi-meta {{ 'text-success' if delta_fat >= 0 else 'text-danger' }}">{{ '%+.1f'|format(delta_fat_pct) }}% vs referência</div>
          </div>
        </div>
      </div>
    </div>

    <div class="dash-block dash-block--fat mb-3">
      <div class="dash-block-head mb-3">
        <div>
          <div class="h5 fw-bold mb-0">Quantidade faturada</div>
          <div class="sub">Unidades vendidas no período</div>
        </div>
      </div>
      <div class="row g-3">
        <div class="col-12 col-md-4">
          <div class="dash-kpi dash-kpi--cmp">
            <div class="dash-pill mb-2"><i class="bi bi-clock-history"></i> Comparação</div>
            <div class="small mb-1">{{ d0_cmp.strftime('%d/%m/%Y') }} a {{ d1_cmp.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ '%.0f'|format(unid_cmp) }}</div>
            <div class="kpi-meta">unidades</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill dash-pill--fat mb-2"><i class="bi bi-calendar3"></i> Referência</div>
            <div class="small text-muted mb-1">{{ d0_ref.strftime('%d/%m/%Y') }} a {{ d1_ref.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ '%.0f'|format(unid_ref) }}</div>
            <div class="kpi-meta">unidades</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill mb-2" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0"><i class="bi bi-arrow-left-right"></i> Diferença</div>
            <div class="small text-muted mb-1">Comparação − Referência</div>
            <div class="kpi-value {{ 'text-success' if delta_unid >= 0 else 'text-danger' }}">{{ '%+.0f'|format(delta_unid) }}</div>
            <div class="kpi-meta {{ 'text-success' if delta_unid >= 0 else 'text-danger' }}">{{ '%+.1f'|format(delta_unid_pct) }}% vs referência</div>
          </div>
        </div>
      </div>
    </div>

    <div class="dash-block dash-block--fat mb-3">
      <div class="dash-block-head mb-3">
        <div>
          <div class="h5 fw-bold mb-0">Clientes ativos</div>
          <div class="sub">Clientes distintos com faturamento no período</div>
        </div>
      </div>
      <div class="row g-3">
        <div class="col-12 col-md-4">
          <div class="dash-kpi dash-kpi--cmp">
            <div class="dash-pill mb-2"><i class="bi bi-clock-history"></i> Comparação</div>
            <div class="small mb-1">{{ d0_cmp.strftime('%d/%m/%Y') }} a {{ d1_cmp.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ cli_cmp }}</div>
            <div class="kpi-meta">clientes</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill dash-pill--fat mb-2"><i class="bi bi-calendar3"></i> Referência</div>
            <div class="small text-muted mb-1">{{ d0_ref.strftime('%d/%m/%Y') }} a {{ d1_ref.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ cli_ref }}</div>
            <div class="kpi-meta">clientes</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill mb-2" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0"><i class="bi bi-arrow-left-right"></i> Diferença</div>
            <div class="small text-muted mb-1">Comparação − Referência</div>
            <div class="kpi-value {{ 'text-success' if delta_cli >= 0 else 'text-danger' }}">{{ '%+d'|format(delta_cli) }}</div>
            <div class="kpi-meta {{ 'text-success' if delta_cli >= 0 else 'text-danger' }}">{{ '%+.1f'|format(delta_cli_pct) }}% vs referência</div>
          </div>
        </div>
      </div>
    </div>

    <div class="dash-block dash-block--fat mb-3">
      <div class="dash-block-head mb-3">
        <div>
          <div class="h5 fw-bold mb-0">Ticket médio</div>
          <div class="sub">Valor faturado ÷ quantidade de notas</div>
        </div>
      </div>
      <div class="row g-3">
        <div class="col-12 col-md-4">
          <div class="dash-kpi dash-kpi--cmp">
            <div class="dash-pill mb-2"><i class="bi bi-clock-history"></i> Comparação</div>
            <div class="small mb-1">{{ d0_cmp.strftime('%d/%m/%Y') }} a {{ d1_cmp.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ format_brl(ticket_cmp) }}</div>
            <div class="kpi-meta">por nota</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill dash-pill--fat mb-2"><i class="bi bi-calendar3"></i> Referência</div>
            <div class="small text-muted mb-1">{{ d0_ref.strftime('%d/%m/%Y') }} a {{ d1_ref.strftime('%d/%m/%Y') }}</div>
            <div class="kpi-value">{{ format_brl(ticket_ref) }}</div>
            <div class="kpi-meta">por nota</div>
          </div>
        </div>
        <div class="col-12 col-md-4">
          <div class="dash-kpi">
            <div class="dash-pill mb-2" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0"><i class="bi bi-arrow-left-right"></i> Diferença</div>
            <div class="small text-muted mb-1">Comparação − Referência</div>
            <div class="kpi-value {{ 'text-success' if delta_ticket >= 0 else 'text-danger' }}">{{ format_brl(delta_ticket) }}</div>
            <div class="kpi-meta {{ 'text-success' if delta_ticket >= 0 else 'text-danger' }}">{{ '%+.1f'|format(delta_ticket_pct) }}% vs referência</div>
          </div>
        </div>
      </div>
    </div>

    <div class="row g-3 mb-3 desemp-charts-row align-items-stretch">
      <div class="col-12 col-xl-7 d-flex">
        <div class="dash-block desemp-chart-card w-100">
          <div class="dash-block-head mb-2">
            <div>
              <div class="h5 fw-bold mb-0" style="color:#0e7490">Evolução do faturamento</div>
              <div class="sub">Distribuição mensal dos períodos selecionados</div>
            </div>
          </div>
          {% if chart_evol %}
            <div class="text-center desemp-chart-body"><img src="data:image/png;base64,{{ chart_evol }}" alt="Evolução do faturamento" style="max-width:100%;height:auto"></div>
          {% else %}
            <div class="muted small">Sem dados para o gráfico.</div>
          {% endif %}
        </div>
      </div>
      <div class="col-12 col-xl-5 d-flex">
        <div class="dash-block desemp-chart-card desemp-equipe-card w-100">
          <div class="dash-block-head mb-3">
            <div>
              <div class="h5 fw-bold mb-0" style="color:#0e7490">Interno x externo</div>
              <div class="sub">Comparativo por equipe</div>
            </div>
          </div>
          <div class="equipe-cmp-list">
            {% for eq in equipes %}
            <div class="equipe-cmp-row">
              <div class="equipe-cmp-head">
                <div class="equipe-cmp-name">{{ eq.nome }}</div>
                <div class="equipe-cmp-badge {{ 'up' if eq.pct >= 0 else 'down' }}">
                  {{ '↗' if eq.pct >= 0 else '↘' }} {{ '%+.1f'|format(eq.pct) }}%
                </div>
              </div>
              <div class="equipe-cmp-bars">
                <div class="equipe-cmp-bar ref" style="width:{{ '%.1f'|format(eq.w_ref) }}%"></div>
                <div class="equipe-cmp-bar cmp" style="width:{{ '%.1f'|format(eq.w_cmp) }}%"></div>
              </div>
              <div class="equipe-cmp-vals">
                <span>Ref. {{ format_brl(eq.ref) }}</span>
                <span>Comp. {{ format_brl(eq.cmp) }}</span>
              </div>
            </div>
            {% endfor %}
          </div>
        </div>
      </div>
    </div>

    <div class="dash-block mb-3">
      <div class="dash-block-head mb-3">
        <div>
          <div class="h5 fw-bold mb-0" style="color:#0e7490">Faturamento mensal — Interno × Externo</div>
          <div class="sub">Cada linha é um mês. Compare os valores do time interno e externo nas colunas lado a lado.</div>
        </div>
        <div class="dash-pill dash-pill--fat"><i class="bi bi-people"></i> Interno × externo</div>
      </div>
      <div class="table-responsive">
        <table class="table table-sm align-middle mb-0 fat-mes-table">
          <thead>
            <tr>
              <th rowspan="2" class="fat-mes-th-mes">Mês</th>
              <th colspan="2" class="text-center fat-mes-th-cmp">{{ label_cmp }} — Comparação</th>
              <th colspan="2" class="text-center fat-mes-th-ref">{{ label_ref }} — Referência</th>
              <th rowspan="2" class="text-center fat-mes-th-var">Variação<br>time interno</th>
              <th rowspan="2" class="text-center fat-mes-th-var">Variação<br>time externo</th>
              <th rowspan="2" class="text-center fat-mes-th-diff">Externo − Interno<br>na comparação (R$)</th>
            </tr>
            <tr>
              <th class="text-center fat-mes-th-cmp">Interno (R$)</th>
              <th class="text-center fat-mes-th-cmp">Externo (R$)</th>
              <th class="text-center fat-mes-th-ref">Interno (R$)</th>
              <th class="text-center fat-mes-th-ref">Externo (R$)</th>
            </tr>
          </thead>
          <tbody>
            {% for r in tabela_meses %}
            <tr>
              <td class="fw-bold">{{ r.mes }}</td>
              <td class="text-center">{{ format_brl(r.cmp_int) if r.cmp_int is not none else '—' }}</td>
              <td class="text-center">{{ format_brl(r.cmp_ext) if r.cmp_ext is not none else '—' }}</td>
              <td class="text-center">{{ format_brl(r.ref_int) if r.ref_int is not none else '—' }}</td>
              <td class="text-center">{{ format_brl(r.ref_ext) if r.ref_ext is not none else '—' }}</td>
              <td class="text-center">
                {% if r.var_int is not none %}
                  <span class="fat-mes-var {{ 'up' if r.var_int >= 0 else 'down' }}">{{ '↗' if r.var_int >= 0 else '↘' }} {{ '%.0f'|format(r.var_int) }}%</span>
                {% else %}—{% endif %}
              </td>
              <td class="text-center">
                {% if r.var_ext is not none %}
                  <span class="fat-mes-var {{ 'up' if r.var_ext >= 0 else 'down' }}">{{ '↗' if r.var_ext >= 0 else '↘' }} {{ '%.0f'|format(r.var_ext) }}%</span>
                {% else %}—{% endif %}
              </td>
              <td class="text-end {{ 'text-success' if (r.diff_ei or 0) >= 0 else 'text-danger' }}">
                {% if r.diff_ei is not none %}{{ '%s%s'|format('+' if r.diff_ei >= 0 else '', format_brl(r.diff_ei)) }}{% else %}—{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
          <tfoot>
            <tr class="fat-mes-total">
              <td class="fw-bold">TOTAL</td>
              <td class="text-center fw-bold">{{ format_brl(tabela_total.cmp_int) }}</td>
              <td class="text-center fw-bold">{{ format_brl(tabela_total.cmp_ext) }}</td>
              <td class="text-center fw-bold">{{ format_brl(tabela_total.ref_int) }}</td>
              <td class="text-center fw-bold">{{ format_brl(tabela_total.ref_ext) }}</td>
              <td class="text-center">
                <span class="fat-mes-var {{ 'up' if tabela_total.var_int >= 0 else 'down' }}">{{ '↗' if tabela_total.var_int >= 0 else '↘' }} {{ '%.0f'|format(tabela_total.var_int) }}%</span>
              </td>
              <td class="text-center">
                <span class="fat-mes-var {{ 'up' if tabela_total.var_ext >= 0 else 'down' }}">{{ '↗' if tabela_total.var_ext >= 0 else '↘' }} {{ '%.0f'|format(tabela_total.var_ext) }}%</span>
              </td>
              <td class="text-end fw-bold {{ 'text-success' if tabela_total.diff_ei >= 0 else 'text-danger' }}">
                {{ '%s%s'|format('+' if tabela_total.diff_ei >= 0 else '', format_brl(tabela_total.diff_ei)) }}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
        """,
        anos=anos,
        meses=_MESES_PT_CURTO,
        label_ref=label_ref,
        label_cmp=label_cmp,
        tabela_meses=tabela_meses,
        tabela_total=tabela_total,
        ref_ano=ref_ano,
        ref_mes_ini=ref_mes_ini,
        ref_mes_fim=ref_mes_fim,
        cmp_ano=cmp_ano,
        cmp_mes_ini=cmp_mes_ini,
        cmp_mes_fim=cmp_mes_fim,
        d0_ref=d0_ref,
        d1_ref=d1_ref,
        d0_cmp=d0_cmp,
        d1_cmp=d1_cmp,
        fat_ref=fat_ref,
        fat_cmp=fat_cmp,
        notas_ref=notas_ref,
        notas_cmp=notas_cmp,
        unid_ref=unid_ref,
        unid_cmp=unid_cmp,
        cli_ref=cli_ref,
        cli_cmp=cli_cmp,
        ticket_ref=ticket_ref,
        ticket_cmp=ticket_cmp,
        delta_fat=delta_fat,
        delta_fat_pct=delta_fat_pct,
        delta_unid=delta_unid,
        delta_unid_pct=delta_unid_pct,
        delta_cli=delta_cli,
        delta_cli_pct=delta_cli_pct,
        delta_ticket=delta_ticket,
        delta_ticket_pct=delta_ticket_pct,
        chart_evol=chart_evol,
        equipes=equipes,
        filtros=filtros,
        filtros_ativos=filtros_ativos,
        opcoes=opcoes,
        format_brl=format_brl,
    )
    return render_page("Desempenho", body, "desempenho")


@app.route("/orcamentos_detalhe")
@login_required
def orcamentos_detalhe():
    user = get_current_user()
    data_ref = request.args.get("data_ref", date.today().strftime("%Y-%m"))
    status_filtro = request.args.get("status_filtro", "") 
    vendedores_selecionados = [v.strip() for v in request.args.getlist("vendedor_nome") if v and v.strip()]
    if not vendedores_selecionados and (request.args.get("vendedor_nome") or "").strip():
        vendedores_selecionados = [(request.args.get("vendedor_nome") or "").strip()]
    vendedor_filtro = vendedores_selecionados[0] if len(vendedores_selecionados) == 1 else ""
    
    try:
        ref_date = datetime.strptime(data_ref, "%Y-%m").date()
    except:
        ref_date = date.today().replace(day=1)

    d0, d1 = periodo_mes_calendario(ref_date)

    with conectar() as conn:
        vendor_lock = get_user_vendor_id(conn, user)
        params_base = [d0.isoformat(), d1.isoformat()]
        vendor_filter_sql = ""
        
        if vendor_lock and vendor_lock > 0:
            v_info = conn.execute("SELECT codigo FROM vendors WHERE id=?", (vendor_lock,)).fetchone()
            if v_info and v_info["codigo"]:
                vendor_filter_sql = " AND (vendedor_id = ? OR vendedor_id LIKE ?)"
                v_cod = v_info["codigo"]
                params_base.extend([v_cod, f"%{v_cod.lstrip('0')}"])

        # Filtro de vendedor para estatísticas
        sql_vendedor_ativo = vendor_filter_sql
        params_vendedor = list(params_base)
        if vendedores_selecionados:
            placeholders_vendedores = ",".join("?" for _ in vendedores_selecionados)
            sql_vendedor_ativo += f" AND vendedor_nome IN ({placeholders_vendedores})"
            params_vendedor.extend(vendedores_selecionados)

        # --- 1. DADOS DE STATUS (LÓGICA DE RECLASSIFICAÇÃO NOS CARDS) ---
        query_stats = f"""
            SELECT 
                CASE
                    WHEN (pedido_id IS NOT NULL AND pedido_id != '' AND pedido_id != '0')
                        THEN 'PEDIDO APROVADO'
                    ELSE
                        UPPER(TRIM(COALESCE(desc_status, 'SEM STATUS')))
                END AS status_nome,
                COUNT(*) AS qtd,
                COALESCE(SUM(COALESCE(valor_total,0)),0) AS valor_status
            FROM orcamentos
            WHERE data_inicio BETWEEN ? AND ? {sql_vendedor_ativo}
            GROUP BY
                CASE
                    WHEN (pedido_id IS NOT NULL AND pedido_id != '' AND pedido_id != '0')
                        THEN 'PEDIDO APROVADO'
                    ELSE
                        UPPER(TRIM(COALESCE(desc_status, 'SEM STATUS')))
                END
            ORDER BY qtd DESC
        """

        stats_rows = [
            {**dict(r), "status_nome": rotulo_status_os(r["status_nome"])}
            for r in conn.execute(query_stats, params_vendedor).fetchall()
        ]
        total_orc = sum(r["qtd"] for r in stats_rows)

        # Fonte única também para os KPIs de orçamento: usa a mesma classificação
        # de Oportunidades/Funil, em vez de assumir que todo registro sem pedido
        # continua aberto para sempre.
        stage_source_rows = conn.execute(f"""
            SELECT desc_status,status,pedido_id,COALESCE(valor_total,0) AS valor_total
            FROM orcamentos
            WHERE data_inicio BETWEEN ? AND ? {sql_vendedor_ativo}
        """, params_vendedor).fetchall()
        _valor_total_criado = 0.0
        _valor_fechado = 0.0
        _valor_aberto = 0.0
        for _o in stage_source_rows:
            _v = float(_o["valor_total"] or 0)
            _valor_total_criado += _v
            _et = _sisplan_stage(_o["desc_status"], _o["pedido_id"], _o["status"])
            if _et == "GANHO":
                _valor_fechado += _v
            elif _et not in SISPLAN_FINAL_STAGES:
                _valor_aberto += _v
        resumo_valores = {
            "valor_total_criado": _valor_total_criado,
            "valor_fechado": _valor_fechado,
            "valor_aberto": _valor_aberto,
        }
        
        lista_vendedores = conn.execute(f"""
            SELECT DISTINCT vendedor_nome 
            FROM orcamentos 
            WHERE data_inicio BETWEEN ? AND ? {vendor_filter_sql}
            AND vendedor_nome IS NOT NULL ORDER BY vendedor_nome
        """, params_base).fetchall()

        # --- 2. FILTROS DA LISTAGEM (RESPEITANDO A RECLASSIFICAÇÃO) ---
        sql_final = sql_vendedor_ativo
        params_final = list(params_vendedor)
        
        if status_filtro:
            filtro_status = status_filtro.strip().upper()
            if filtro_status == 'PEDIDO APROVADO' or filtro_status.endswith('PEDIDO APROVADO'):
                # Se filtrou por Pedido Aprovado, pega tudo que tem pedido_id
                sql_final += " AND (pedido_id IS NOT NULL AND pedido_id != '' AND pedido_id != '0')"
            else:
                # SQLite pode ter so o codigo (40) ou o rotulo (40 - MANUTENCAO...).
                codigo_filtro = codigo_status_os(status_filtro)
                sql_final += " AND (pedido_id IS NULL OR pedido_id = '' OR pedido_id = '0')"
                if codigo_filtro:
                    sql_final += (
                        " AND ("
                        " TRIM(COALESCE(status_os, '')) = ?"
                        " OR TRIM(COALESCE(status_os, '')) = ?"
                        " OR UPPER(TRIM(COALESCE(desc_status, 'SEM STATUS'))) = ?"
                        " OR UPPER(TRIM(COALESCE(desc_status, ''))) = ?"
                        " OR UPPER(TRIM(COALESCE(desc_status, ''))) LIKE ?"
                        ")"
                    )
                    codigo_curto = codigo_filtro.lstrip("0") or "0"
                    params_final.extend([
                        codigo_filtro,
                        codigo_curto,
                        filtro_status,
                        codigo_filtro,
                        f"{codigo_filtro} -%",
                    ])
                else:
                    sql_final += " AND UPPER(TRIM(COALESCE(desc_status, 'SEM STATUS'))) = ?"
                    params_final.append(filtro_status)

        query_list = f"""
            SELECT
                o.numero,
                o.cliente_nome,
                o.cliente_nome_ref,
                o.status,
                o.desc_status,
                o.valor_total,
                o.motivo,
                o.data_inicio,
                o.pedido_id,
                o.vendedor_nome
            FROM orcamentos o
            WHERE o.data_inicio BETWEEN ? AND ? {sql_final.replace('vendedor_nome','o.vendedor_nome').replace('desc_status','o.desc_status').replace('pedido_id','o.pedido_id').replace('status_os','o.status_os')}
            ORDER BY o.data_inicio DESC
        """
        rows = [dict(r) for r in conn.execute(query_list, params_final).fetchall()]
        for r in rows:
            r["desc_status"] = rotulo_status_os(r.get("desc_status"))

        # Resolve nomes em lote. A versão anterior executava até duas subconsultas por linha
        # de orçamento; em bases grandes isso virava milhares de SELECTs na mesma página.
        pedidos_sem_nome = []
        for r in rows:
            nome_direto = str(r.get("cliente_nome") or "").strip()
            nome_ref = str(r.get("cliente_nome_ref") or "").strip()
            nome_direto_valido = bool(nome_direto) and not nome_direto.isdigit()
            if nome_direto_valido:
                r["cliente_exibir"] = nome_direto
            elif nome_ref:
                r["cliente_exibir"] = nome_ref
            else:
                r["cliente_exibir"] = ""
                pedido = str(r.get("pedido_id") or "").strip()
                if pedido and pedido != "0":
                    pedidos_sem_nome.append(pedido)

        pedidos_sem_nome = list(dict.fromkeys(pedidos_sem_nome))
        cliente_por_pedido = {}
        for i in range(0, len(pedidos_sem_nome), 700):
            lote = pedidos_sem_nome[i:i+700]
            if not lote:
                continue
            marks = ",".join("?" for _ in lote)
            for rr in conn.execute(f"""
                SELECT po.pedido, COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),'')) AS cliente
                FROM orders po LEFT JOIN customers c ON c.id=po.cliente_id
                WHERE po.pedido IN ({marks})
                ORDER BY po.id DESC
            """, lote).fetchall():
                ped = str(rr["pedido"] or "").strip()
                if ped and rr["cliente"] and ped not in cliente_por_pedido:
                    cliente_por_pedido[ped] = rr["cliente"]

            faltam = [x for x in lote if x not in cliente_por_pedido]
            if faltam:
                marks2 = ",".join("?" for _ in faltam)
                for rr in conn.execute(f"""
                    SELECT fi.pedido, COALESCE(NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),'')) AS cliente
                    FROM invoices fi LEFT JOIN customers c ON c.id=fi.cliente_id
                    WHERE fi.pedido IN ({marks2})
                    ORDER BY fi.id DESC
                """, faltam).fetchall():
                    ped = str(rr["pedido"] or "").strip()
                    if ped and rr["cliente"] and ped not in cliente_por_pedido:
                        cliente_por_pedido[ped] = rr["cliente"]

        for r in rows:
            if not r.get("cliente_exibir"):
                r["cliente_exibir"] = cliente_por_pedido.get(str(r.get("pedido_id") or "").strip(), "CLIENTE SEM NOME")


        # --- 3. GRÁFICOS ---
        # Visão executiva: agrupa micro-status do Sisplan em etapas comerciais reais.
        # O status bruto continua preservado na listagem e no resumo lateral para auditoria.
        stage_acc = {k: {"qtd": 0, "valor": 0.0} for k in SISPLAN_STAGE_ORDER}
        for _sr in stage_source_rows:
            _stage = _sisplan_stage(_sr["desc_status"], _sr["pedido_id"], _sr["status"])
            if _stage not in stage_acc:
                _stage = "NOVO"
            stage_acc[_stage]["qtd"] += 1
            stage_acc[_stage]["valor"] += float(_sr["valor_total"] or 0)
        stage_rows = [
            {"stage": k, "label": SISPLAN_STAGE_LABELS.get(k, k), "qtd": stage_acc[k]["qtd"], "valor": stage_acc[k]["valor"]}
            for k in SISPLAN_STAGE_ORDER if stage_acc[k]["qtd"] > 0
        ]
        chart_status = donut_chart([r["label"] for r in stage_rows], [r["qtd"] for r in stage_rows], "Fases comerciais") if stage_rows else None
        top_status_rows = list(stats_rows[:8])
        status_restantes = max(0, len(stats_rows) - len(top_status_rows))

        # Uma única agregação regional alimenta os três gráficos. Além de mais
        # rápido, a conversão passa a ser taxa real (convertidos / total), e não
        # simplesmente quantidade de pedidos convertidos.
        regional_rows_raw = conn.execute(f"""
            SELECT
                UPPER(TRIM(COALESCE(uf, ''))) AS uf,
                COUNT(*) AS total_orcamentos,
                SUM(CASE WHEN pedido_id IS NOT NULL AND TRIM(pedido_id) <> '' AND TRIM(pedido_id) <> '0' THEN 1 ELSE 0 END) AS convertidos,
                SUM(COALESCE(valor_total, 0)) AS valor_total
            FROM orcamentos
            WHERE data_inicio BETWEEN ? AND ? {sql_vendedor_ativo}
              AND COALESCE(TRIM(uf), '') <> ''
            GROUP BY UPPER(TRIM(COALESCE(uf, '')))
        """, params_vendedor).fetchall()

        regional_rows = []
        geo_dict = {}
        for rr in regional_rows_raw:
            uf = (rr["uf"] or "").strip().upper()
            total_uf = int(rr["total_orcamentos"] or 0)
            conv_uf = int(rr["convertidos"] or 0)
            valor_uf = float(rr["valor_total"] or 0)
            taxa = (conv_uf / total_uf * 100.0) if total_uf else 0.0
            regional_rows.append({"uf": uf, "total": total_uf, "convertidos": conv_uf, "taxa": taxa, "valor": valor_uf})
            if uf:
                geo_dict[uf] = valor_uf

        # Evita rankings enganosos do tipo 1/1 = 100%. Prioriza estados com ao
        # menos 5 propostas; se o recorte for pequeno, relaxa gradualmente.
        elegiveis = [r for r in regional_rows if r["total"] >= 5]
        if len(elegiveis) < 5:
            elegiveis = [r for r in regional_rows if r["total"] >= 2]
        if len(elegiveis) < 3:
            elegiveis = list(regional_rows)

        altas = sorted(elegiveis, key=lambda x: (x["taxa"], x["total"]), reverse=True)[:5]
        baixas = sorted(elegiveis, key=lambda x: (x["taxa"], -x["total"]))[:5]

        chart_regioes_altas = regional_conversion_chart(altas, "Melhores taxas de conversão") if altas else None
        chart_regioes_baixas = regional_conversion_chart(baixas, "Menores taxas de conversão") if baixas else None
        chart_distribuicao_uf = regional_value_distribution_chart(geo_dict) if geo_dict else None

    body = render_template_string(r"""
    <style>
      /* O hero global recorta conteúdo com overflow:hidden; aqui isso escondia
         o menu de vendedores. A exceção fica restrita à tela de Orçamentos. */
      .page-hero.orcamentos-hero{overflow:visible;z-index:30}
      .page-hero.orcamentos-hero:after{display:none}
      .orcamentos-hero .dropdown{position:relative;z-index:1080}
      .orcamentos-hero .seller-dropdown-menu{z-index:1090;min-width:290px;max-width:min(92vw,380px);max-height:min(360px,60vh);overflow-y:auto;overflow-x:hidden}
      @media(max-width:700px){.orcamentos-hero .seller-dropdown-menu{left:0!important;right:auto!important;min-width:min(290px,88vw)}}
    </style>
    <div class="page-hero orcamentos-hero mb-4">
      <div class="d-flex justify-content-between align-items-end flex-wrap gap-3 position-relative" style="z-index:1">
        <div>
          <div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Inteligência de propostas</div>
          <div class="hero-title">Análise de Orçamentos</div>
          <div class="hero-sub mt-1">{{ d0.strftime('%d/%m/%Y') }} a {{ d1.strftime('%d/%m/%Y') }} · visão executiva e detalhamento Sisplan</div>
        </div>
        <form class="d-flex gap-2 align-items-end flex-wrap" method="GET" action="{{ url_for('orcamentos_detalhe') }}">
          <input type="hidden" name="status_filtro" value="{{ status_filtro }}">
          <div class="dropdown">
            <button class="btn btn-light dropdown-toggle" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside" style="min-width:210px">
              {% if vendedores_selecionados|length == 0 %}Todos vendedores{% elif vendedores_selecionados|length == 1 %}{{ vendedores_selecionados[0] }}{% else %}{{ vendedores_selecionados|length }} vendedores{% endif %}
            </button>
            <div class="dropdown-menu seller-dropdown-menu p-2 shadow">
              <div class="d-flex gap-2 mb-2"><a class="btn btn-sm btn-outline-secondary flex-fill" href="{{ url_for('orcamentos_detalhe', data_ref=data_ref, status_filtro=status_filtro) }}">Limpar</a><button class="btn btn-sm btn-primary flex-fill" type="submit">Aplicar</button></div>
              {% for v in lista_vendedores %}<label class="dropdown-item d-flex align-items-center gap-2 small"><input class="form-check-input m-0" type="checkbox" name="vendedor_nome" value="{{ v.vendedor_nome }}" {% if v.vendedor_nome in vendedores_selecionados %}checked{% endif %}><span>{{ v.vendedor_nome }}</span></label>{% endfor %}
            </div>
          </div>
          <input type="month" name="data_ref" class="form-control fw-bold" value="{{ data_ref }}" onchange="this.form.submit()" style="width:160px">
          <button class="btn btn-primary">Aplicar</button>
          <a href="{{ url_for('dashboard') }}" class="btn btn-outline-light">Dashboard</a>
          <a class="btn btn-success" href="{{ url_for('relatorios_exportar', relatorio='orcamentos', data_ref=data_ref, periodo='mes') }}"><i class="bi bi-file-earmark-excel me-1"></i>Excel</a>
        </form>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-md-4"><div class="stat-card h-100"><div class="stat-label">Potencial em aberto</div><div class="stat-value text-warning">{{ format_brl(resumo_valores.valor_aberto) }}</div><div class="stat-sub">Propostas ainda sem pedido vinculado</div></div></div>
      <div class="col-12 col-md-4"><div class="stat-card h-100"><div class="stat-label">Valor convertido</div><div class="stat-value text-success">{{ format_brl(resumo_valores.valor_fechado) }}</div><div class="stat-sub">Orçamentos que já viraram pedido</div></div></div>
      <div class="col-12 col-md-4"><div class="stat-card h-100"><div class="stat-label">Valor total criado</div><div class="stat-value text-primary">{{ format_brl(resumo_valores.valor_total_criado) }}</div><div class="stat-sub">Potencial bruto gerado no período</div></div></div>
    </div>

    <div class="row g-4 mb-4 align-items-stretch">
      <div class="col-12 col-xl-7">
        <div class="viz-card">
          <div class="viz-head"><div><div class="viz-title">Distribuição por etapa comercial</div><div class="viz-sub">Micro-status do Sisplan agrupados em etapas que fazem sentido para gestão.</div></div><span class="chip">{{ total_orc }} orçamentos</span></div>
          <div class="viz-body">{% if chart_status %}<img src="data:image/png;base64,{{ chart_status }}">{% else %}<div class="text-muted">Sem dados no período.</div>{% endif %}</div>
          <div class="d-flex flex-wrap gap-2 mt-3">
            {% for st in stage_rows %}<span class="chip">{{ st.label }} · <b>{{ st.qtd }}</b></span>{% endfor %}
          </div>
        </div>
      </div>
      <div class="col-12 col-xl-5">
        <div class="viz-card">
          <div class="viz-head"><div><div class="viz-title">Status Sisplan mais frequentes</div><div class="viz-sub">Dado bruto preservado para auditoria e filtro detalhado.</div></div></div>
          <div class="mini-status-list">
            {% for s in top_status_rows %}
              {% set pct = (s.qtd / total_orc * 100) if total_orc > 0 else 0 %}
              <a href="{{ url_for('orcamentos_detalhe', data_ref=data_ref, status_filtro=s.status_nome) }}{% for nome in vendedores_selecionados %}&vendedor_nome={{ nome|urlencode }}{% endfor %}" class="text-decoration-none">
                <div class="mini-status-row"><div><div class="mini-status-name">{{ s.status_nome }}</div><div class="mini-status-meta">{{ '%.1f'|format(pct) }}% do período · {{ format_brl(s.valor_status or 0) }}</div></div><div class="mini-status-value">{{ s.qtd }}<small>propostas</small></div></div>
              </a>
            {% endfor %}
          </div>
          {% if status_restantes > 0 %}<div class="small text-muted mt-3 text-center">+ {{ status_restantes }} status menos frequentes disponíveis na tabela abaixo.</div>{% endif %}
        </div>
      </div>
    </div>

    <div class="row g-4 mb-4">
      <div class="col-12 col-xl-7">
        <div class="viz-card">
          <div class="viz-head"><div><div class="viz-title">Distribuição financeira por estado</div><div class="viz-sub">Ranking por valor de orçamentos criados no período.</div></div></div>
          <div class="viz-body">{% if chart_distribuicao_uf %}<img src="data:image/png;base64,{{ chart_distribuicao_uf }}">{% else %}<div class="text-muted">Sem dados de UF no período.</div>{% endif %}</div>
        </div>
      </div>
      <div class="col-12 col-xl-5">
        <div class="d-flex flex-column gap-4 h-100">
          <div class="viz-card flex-fill"><div class="viz-head"><div><div class="viz-title">Melhores taxas por UF</div><div class="viz-sub">Conversão real: pedidos ÷ orçamentos. Amostra mínima aplicada.</div></div></div><div class="viz-body" style="min-height:220px">{% if chart_regioes_altas %}<img src="data:image/png;base64,{{ chart_regioes_altas }}">{% endif %}</div></div>
          <div class="viz-card flex-fill"><div class="viz-head"><div><div class="viz-title">UFs com maior oportunidade</div><div class="viz-sub">Menores taxas entre regiões com amostra relevante.</div></div></div><div class="viz-body" style="min-height:220px">{% if chart_regioes_baixas %}<img src="data:image/png;base64,{{ chart_regioes_baixas }}">{% endif %}</div></div>
        </div>
      </div>
    </div>

    <div class="viz-card p-0 overflow-hidden">
        <div class="p-3 border-bottom"><div class="viz-title">Propostas do período {{ '(' ~ status_filtro ~ ')' if status_filtro else '' }}</div><div class="viz-sub">Status original do Sisplan mantido para conferência e rastreabilidade.</div></div>
        <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
                <thead class="bg-light small text-uppercase">
                    <tr><th class="ps-3">Data/Nº</th><th>Cliente</th><th>Situação Atual</th><th class="text-end">Valor</th><th class="text-center">Pedido</th></tr>
                </thead>
                <tbody>
                {% for r in rows %}
                    {% set tem_pedido = r.pedido_id and r.pedido_id|string != '0' and r.pedido_id|string != '' %}
                    <tr style="cursor:pointer" onclick="window.location='{{ url_for('orcamento_detalhe', numero=r.numero, origem='orcamentos', data_ref=data_ref) }}'" title="Abrir orçamento detalhado">
                        <td class="ps-3"><div class="fw-bold">{{ r.numero }}</div><div class="small text-muted">{{ r.data_inicio }}</div></td>
                        <td><div class="fw-semibold">{{ (r.cliente_exibir or '—')[:30] }}</div></td>
                        <td>
                            <div class="fw-bold text-dark mb-1" style="font-size: 0.85rem;">
                                {{ 'PEDIDO CONCRETIZADO' if tem_pedido else ((r.desc_status or 'SEM DESCRIÇÃO')|trim|upper) }}
                            </div>
                            
                            {% if tem_pedido %}
                                <span class="badge bg-success" style="cursor: help;" title="F = FECHADO: Orçamento convertido em venda!">F</span>
                            {% else %}
                                <span class="badge bg-warning text-dark" style="cursor: help;" title="A = ABERTO: Negociação em andamento.">A</span>
                            {% endif %}
                            
                            <span class="ms-1 small text-muted" style="cursor: help;" title="Vendedor responsável">| <b>{{ r.vendedor_nome or '—' }}</b></span>
                            <br>
                            {% if r.motivo and not tem_pedido %}<small class="text-muted" style="font-size:0.75rem;">Motivo: {{ r.motivo }}</small>{% endif %}
                        </td>
                        <td class="text-end fw-bold">{{ format_brl(r.valor_total) }}</td>
                        <td class="text-center">
                            {% if tem_pedido %}
                                <span class="text-success fw-bold" style="cursor: help;" title="Pedido Sisplan gerado com sucesso.">#{{ r.pedido_id }}</span>
                            {% elif r.status == 'F' %}
                                <span class="text-danger fw-bold small" style="cursor: help;" title="Atenção: Marcado como fechado mas sem número de pedido!">FINALIZAR PEDIDO</span>
                            {% else %}
                                <span class="text-danger fw-bold" style="cursor: help;" title="PENDENTE: Orçamento sem pedido vinculado.">✘</span>
                            {% endif %}
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    """, rows=rows, d0=d0, d1=d1, data_ref=data_ref, format_brl=format_brl, 
       chart_distribuicao_uf=chart_distribuicao_uf, chart_regioes_altas=chart_regioes_altas, chart_regioes_baixas=chart_regioes_baixas,
       stats_rows=stats_rows, stage_rows=stage_rows, top_status_rows=top_status_rows, status_restantes=status_restantes, total_orc=total_orc, chart_status=chart_status, 
       status_filtro=status_filtro, vendedor_filtro=vendedor_filtro, vendedores_selecionados=vendedores_selecionados, lista_vendedores=lista_vendedores,
       resumo_valores=resumo_valores)

    return render_page("Análise Detalhada", body, "orcamentos_detalhe")

def analisar_tipo_csv_sisplan(file_bytes: bytes) -> dict:
    """
    Analisa um CSV exportado do Sisplan e identifica qual dos quatro relatórios
    oficiais da NPD ele representa.

    A identificação é baseada nos quatro layouts reais validados em 25/09/2026:
    PEDIDOS, FATURAMENTO, ORCAMENTO e LIQUIDEZ.

    Segurança:
    - nome do arquivo nunca participa da decisão;
    - primeiro valida uma assinatura estrutural formada por colunas exclusivas;
    - depois mede aderência ao layout oficial e checa se os campos-chave têm dados;
    - se houver ambiguidade ou assinatura incompleta, retorna tipo vazio.
    """
    try:
        headers, rows = read_csv_sample_dicts(file_bytes, max_rows=200)
    except Exception as e:
        return {"tipo": "", "confianca": 0, "motivo": f"Falha ao ler CSV: {e}", "scores": {}}

    hs = {normalize_header(h) for h in (headers or []) if normalize_header(h)}
    if not hs:
        return {"tipo": "", "confianca": 0, "motivo": "CSV sem cabeçalho reconhecível.", "scores": {}}

    # Assinaturas exatas dos quatro arquivos reais exportados do Sisplan.
    # O cálculo de aderência abaixo não exige 100% de igualdade, para suportar
    # pequenas inclusões de colunas futuras sem perder a identificação.
    official_headers = {
        "FATURAMENTO": {
            "fantasia", "pedido", "nota", "dt_emissao", "qtde", "preco", "valor",
            "nome_responsavel", "codresp", "representante", "cliente", "produto",
        },
        "ORCAMENTO": {
            "numero", "cliente", "nome", "pedido", "cod_ven", "nomeven", "represen",
            "nome_rep", "motivo", "descmotivo", "dt_inicio", "dt_prev", "dt_retorno",
            "dt_validade", "status", "obs", "observacao", "quantidade", "valor", "valort",
            "prospeccao", "desc_prospeccao", "tipo_contato", "status_os", "desc_status",
            "funcionario", "nome_funcionario", "cep", "cod_uf", "nome_1",
        },
        "LIQUIDEZ": {
            "fantasia", "empresa", "duplicata", "dt_emissao", "dt_pagto", "valor_liq",
            "qtdeparcelas", "nome_responsavel", "nome_representante", "pedido", "fatura",
            "desconto", "valor_pago", "val_nota", "val_frete", "cliche", "codrepre", "codresp",
        },
        "PEDIDOS": {
            "situacao", "tipo_item_pedido", "condicao_entrega", "motivo", "pedido", "fantasia",
            "emissao_pedido", "valor_item", "qtde_total", "representate", "responsavel", "cidade",
            "papel", "medida", "grupo", "entrega_pedido", "cliente", "obs", "uf", "preco",
            "valor_ped", "codigo", "descricao", "ddd", "telefone", "email",
        },
    }

    # Grupos de aliases aceitos caso o Sisplan altere apenas o rótulo da coluna.
    aliases = {
        "pedido": {"pedido", "num_pedido", "numero_pedido"},
        "nota": {"nota", "numero_nota", "num_nota", "numnota", "nf"},
        "dt_emissao": {"dt_emissao", "data_emissao", "emissao"},
        "qtde": {"qtde", "qtd", "quantidade"},
        "produto": {"produto", "descricao_produto", "item"},
        "duplicata": {"duplicata", "titulo", "num_duplicata", "numero_duplicata", "num_titulo", "numero_titulo"},
        "dt_pagto": {"dt_pagto", "data_pagto", "data_pagamento", "pagamento", "baixa"},
        "valor_pago": {"valor_pago", "valor_recebido", "valorrecebido"},
        "valor_liq": {"valor_liq", "valor_liquido"},
        "qtdeparcelas": {"qtdeparcelas", "qtde_parcelas", "qtd_parcelas", "parcelas", "parcela"},
        "fatura": {"fatura", "numero_fatura", "num_fatura"},
        "numero_orc": {"numero", "orcamento", "num_orcamento", "numero_orcamento"},
        "dt_validade": {"dt_validade", "data_validade", "validade"},
        "valort": {"valort", "valor_total", "total_orcamento"},
        "tipo_contato": {"tipo_contato", "contato_tipo"},
        "desc_status": {"desc_status", "descricao_status"},
        "nomeven": {"nomeven", "nome_vendedor", "vendedor"},
        "emissao_pedido": {"emissao_pedido", "data_pedido", "dt_pedido"},
        "valor_item": {"valor_item", "total_item"},
        "qtde_total": {"qtde_total", "qtd_total", "quantidade_total"},
        "valor_ped": {"valor_ped", "valor_pedido", "total_pedido"},
        "tipo_item_pedido": {"tipo_item_pedido", "tipo_item"},
        "condicao_entrega": {"condicao_entrega", "cond_entrega"},
        "entrega_pedido": {"entrega_pedido", "data_entrega", "dt_entrega"},
        "grupo": {"grupo", "grupo_ref", "familia"},
        "codigo": {"codigo", "codigo_produto", "cod_produto"},
        "descricao": {"descricao", "descricao_produto", "produto"},
        "situacao": {"situacao", "situacao_pedido", "status_pedido"},
    }

    def present(key: str) -> bool:
        opts = aliases.get(key, {key})
        return bool(hs.intersection(opts))

    sample_rows = [map_row(r) for r in (rows[:200] if rows else [])]

    def value_for(rr: dict, key: str) -> str:
        for col in aliases.get(key, {key}):
            v = rr.get(col)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    def populated_ratio(key: str) -> float:
        if not sample_rows:
            return 0.0
        ok = sum(1 for rr in sample_rows if value_for(rr, key))
        return ok / len(sample_rows)

    def date_ratio(key: str) -> float:
        vals = [value_for(rr, key) for rr in sample_rows]
        vals = [v for v in vals if v]
        if not vals:
            return 0.0
        return sum(1 for v in vals if try_parse_date(v)) / len(vals)

    def numeric_ratio(key: str) -> float:
        vals = [value_for(rr, key) for rr in sample_rows]
        vals = [v for v in vals if v]
        if not vals:
            return 0.0
        good = 0
        for v in vals:
            txt = str(v).strip().replace("R$", "").replace(" ", "")
            txt = txt.replace(".", "").replace(",", ".") if "," in txt else txt
            try:
                float(txt)
                good += 1
            except Exception:
                pass
        return good / len(vals)

    # Assinaturas estruturais exclusivas, tiradas dos arquivos reais enviados.
    # Cada grupo contém campos que, em conjunto, diferenciam aquele relatório.
    core_checks = {
        "PEDIDOS": [
            present("pedido"), present("emissao_pedido"), present("valor_item"),
            present("qtde_total"), present("valor_ped"), present("grupo"),
            present("codigo"), present("descricao"),
            (present("tipo_item_pedido") or present("condicao_entrega") or present("entrega_pedido")),
        ],
        "FATURAMENTO": [
            present("pedido"), present("nota"), present("dt_emissao"), present("qtde"),
            present("produto"), ("preco" in hs), ("valor" in hs),
        ],
        "LIQUIDEZ": [
            present("pedido"), present("duplicata"), present("dt_pagto"), present("valor_pago"),
            present("valor_liq"), present("qtdeparcelas"), present("fatura"),
            ("val_nota" in hs or "valor_nota" in hs),
        ],
        "ORCAMENTO": [
            present("numero_orc"), present("dt_validade"), present("valort"), present("tipo_contato"),
            present("desc_status"), present("nomeven"), ("dt_inicio" in hs or "data_inicio" in hs),
            ("status" in hs),
        ],
    }

    # Quantidade mínima de sinais centrais exigida. Os layouts reais normalmente
    # passam com todos os sinais; a pequena folga serve apenas para uma coluna opcional renomeada.
    core_min = {"PEDIDOS": 8, "FATURAMENTO": 6, "LIQUIDEZ": 7, "ORCAMENTO": 7}

    # Validações simples sobre o conteúdo, não apenas o texto do cabeçalho.
    content_checks = {
        "PEDIDOS": [
            populated_ratio("pedido") >= 0.80,
            date_ratio("emissao_pedido") >= 0.70,
            numeric_ratio("valor_ped") >= 0.70,
            populated_ratio("descricao") >= 0.70,
        ],
        "FATURAMENTO": [
            populated_ratio("nota") >= 0.80,
            date_ratio("dt_emissao") >= 0.70,
            numeric_ratio("valor") >= 0.70,
            populated_ratio("produto") >= 0.70,
        ],
        "LIQUIDEZ": [
            populated_ratio("duplicata") >= 0.80,
            date_ratio("dt_pagto") >= 0.70,
            numeric_ratio("valor_pago") >= 0.70,
            numeric_ratio("valor_liq") >= 0.70,
        ],
        "ORCAMENTO": [
            populated_ratio("numero_orc") >= 0.80,
            date_ratio("dt_validade") >= 0.70,
            numeric_ratio("valort") >= 0.70,
            populated_ratio("tipo_contato") >= 0.70,
        ],
    }

    results = {}
    for tipo, official in official_headers.items():
        overlap = len(hs.intersection(official))
        official_ratio = overlap / max(1, len(official))
        core_hits = sum(1 for x in core_checks[tipo] if x)
        core_ratio = core_hits / len(core_checks[tipo])
        content_hits = sum(1 for x in content_checks[tipo] if x)
        content_ratio = content_hits / len(content_checks[tipo])

        # Score 0..100: core pesa mais que aderência total, e conteúdo confirma.
        score = round((core_ratio * 55) + (official_ratio * 30) + (content_ratio * 15), 1)
        confirmed = core_hits >= core_min[tipo] and content_hits >= 3
        results[tipo] = {
            "score": score,
            "confirmed": confirmed,
            "core_hits": core_hits,
            "core_total": len(core_checks[tipo]),
            "official_ratio": round(official_ratio, 3),
            "content_hits": content_hits,
        }

    ordered = sorted(results.items(), key=lambda kv: kv[1]["score"], reverse=True)
    tipo, best = ordered[0]
    second_score = ordered[1][1]["score"] if len(ordered) > 1 else 0

    # Nos quatro arquivos reais o score é muito alto. Para qualquer arquivo estranho,
    # preferimos bloquear a importação. A margem evita classificação em layout ambíguo.
    if not best["confirmed"] or best["score"] < 72 or (best["score"] - second_score) < 18:
        return {
            "tipo": "",
            "confianca": int(round(best["score"])),
            "motivo": "Estrutura não corresponde com segurança a um dos quatro relatórios oficiais do Sisplan.",
            "scores": results,
        }

    return {
        "tipo": tipo,
        "confianca": int(round(best["score"])),
        "motivo": "Assinatura estrutural e conteúdo compatíveis com o layout oficial.",
        "scores": results,
    }


def detectar_tipo_csv_sisplan(filename: str, file_bytes: bytes) -> str:
    """
    Compatibilidade com as rotas existentes. O nome do arquivo é propositalmente
    ignorado; a decisão vem exclusivamente de analisar_tipo_csv_sisplan().
    """
    return analisar_tipo_csv_sisplan(file_bytes).get("tipo", "")


def _backup_sqlite_before_rebuild(label: str = "fonte_oficial") -> str:
    """Cria uma cópia consistente do banco antes de sincronizar os 4 fatos Sisplan."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label or "fonte_oficial")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Backup transacional fica no diretório temporário do Windows, fora da pasta
    # do CRM. Em instalações dentro de Google Drive/OneDrive, gravar 100+ MB na
    # pasta sincronizada a cada atualização deixava a importação muito lenta.
    backup_dir = os.path.join(tempfile.gettempdir(), "NPD_CRM_backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"crm_npd_backup_{safe}_{stamp}.db")

    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(backup_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def _restore_sqlite_backup(backup_path: str) -> None:
    """Restaura o snapshot caso uma etapa do pacote completo falhe."""
    if not backup_path or not os.path.exists(backup_path):
        raise FileNotFoundError("Backup da base não encontrado para restauração.")
    src = sqlite3.connect(backup_path, timeout=30)
    dst = sqlite3.connect(DB_PATH, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _purge_sisplan_fact_tables_for_rebuild() -> None:
    """Limpa só os quatro fatos Sisplan, sem alterar ciclos ou cadastros do CRM."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("orders", "invoices", "orcamentos", "liquidez"):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM import_identity_pending WHERE COALESCE(status,'PENDENTE')='PENDENTE'")
        except Exception:
            pass
        conn.commit()


def _monthly_snapshot_stats_csv(tipo: str, file_bytes: bytes) -> Dict[str, Dict[str, float]]:
    """Resumo mensal do próprio CSV para validar a importação sem depender da UI."""
    headers, rows = read_csv_dicts(file_bytes)
    mapper = compile_row_mapper(headers)
    out = {}
    for raw in rows:
        rr = mapper(raw)
        if tipo == "PEDIDOS":
            dt = try_parse_date(first_not_empty(rr.get("emissao_pedido"), rr.get("data_emissao"), rr.get("emissao")))
            val = parse_br_money(first_not_empty(rr.get("valor_item"), "0"))
        elif tipo == "FATURAMENTO":
            dt = try_parse_date(first_not_empty(rr.get("dt_emissao"), rr.get("data_emissao"), rr.get("emissao")))
            val = parse_br_money(first_not_empty(rr.get("valor"), "0"))
        elif tipo == "LIQUIDEZ":
            dt = try_parse_date(first_not_empty(rr.get("dt_pagto"), rr.get("data_pagto"), rr.get("pagamento")))
            val = parse_br_money(first_not_empty(rr.get("valor_pago"), "0"))
        elif tipo == "ORCAMENTO":
            dt = try_parse_date(first_not_empty(rr.get("dt_inicio"), rr.get("data_inicio"), rr.get("data"), rr.get("emissao")))
            val = parse_br_money(first_not_empty(rr.get("valort"), rr.get("valor_total"), rr.get("total"), rr.get("valor"), "0"))
        else:
            continue
        if not dt:
            continue
        ym = str(dt)[:7]
        st = out.setdefault(ym, {"rows": 0, "total": 0.0})
        st["rows"] += 1
        st["total"] += float(val or 0)
    return out


def _monthly_snapshot_stats_db(conn, tipo: str) -> Dict[str, Dict[str, float]]:
    spec = {
        "PEDIDOS": ("orders", "data_emissao", "valor_item_ref"),
        "FATURAMENTO": ("invoices", "data_emissao", "valor"),
        "LIQUIDEZ": ("liquidez", "data_pagto", "valor_pago"),
        "ORCAMENTO": ("orcamentos", "data_inicio", "valor_total"),
    }
    table, dcol, vcol = spec[tipo]
    rows = conn.execute(f"""
        SELECT substr({dcol},1,7) AS ym, COUNT(*) AS rows, COALESCE(SUM({vcol}),0) AS total
        FROM {table}
        WHERE {dcol} IS NOT NULL AND TRIM({dcol})<>''
        GROUP BY substr({dcol},1,7)
    """).fetchall()
    return {str(r["ym"]): {"rows": int(r["rows"] or 0), "total": float(r["total"] or 0)} for r in rows}


def _validar_meses_snapshot_contra_csv(detectados) -> Dict[str, Any]:
    """Compara mês a mês CSV x banco; falha se uma importação mudar data, linha ou valor."""
    result = {"ok": True, "erros": []}
    with conectar() as conn:
        for tipo, filename, data in detectados:
            csv_stats = _monthly_snapshot_stats_csv(tipo, data)
            db_stats = _monthly_snapshot_stats_db(conn, tipo)
            meses = sorted(set(csv_stats) | set(db_stats))
            for ym in meses:
                a = csv_stats.get(ym, {"rows": 0, "total": 0.0})
                b = db_stats.get(ym, {"rows": 0, "total": 0.0})
                if int(a["rows"]) != int(b["rows"]) or abs(float(a["total"]) - float(b["total"])) > 0.01:
                    result["ok"] = False
                    result["erros"].append({
                        "tipo": tipo, "arquivo": filename, "mes": ym,
                        "csv_rows": int(a["rows"]), "db_rows": int(b["rows"]),
                        "csv_total": round(float(a["total"]), 2), "db_total": round(float(b["total"]), 2),
                    })
    return result


def _validar_snapshot_sisplan_pos_importacao() -> Dict[str, Any]:
    """Valida invariantes que não podem quebrar após uma atualização Sisplan."""
    tabelas = ("orders", "invoices", "orcamentos", "liquidez")
    out = {"ok": True, "duplicadas": {}, "vazias": {}, "integrity": "", "foreign_keys": 0}
    with conectar() as conn:
        for table in tabelas:
            dup = int(conn.execute(f"""
                SELECT COUNT(*) FROM (
                  SELECT dedupe_key FROM {table}
                  WHERE COALESCE(TRIM(dedupe_key),'')<>''
                  GROUP BY dedupe_key HAVING COUNT(*)>1
                )
            """).fetchone()[0] or 0)
            blank = int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE COALESCE(TRIM(dedupe_key),'')='' ").fetchone()[0] or 0)
            out["duplicadas"][table] = dup
            out["vazias"][table] = blank
            if dup or blank:
                out["ok"] = False
        try:
            out["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0] or "")
            if out["integrity"].lower() != "ok":
                out["ok"] = False
        except Exception as e:
            out["integrity"] = str(e)
            out["ok"] = False
        try:
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            out["foreign_keys"] = len(fk_rows)
            if fk_rows:
                out["ok"] = False
        except Exception:
            out["foreign_keys"] = -1
            out["ok"] = False
    return out


def _executar_importacao_sisplan(tipo: str, data: bytes, filename: str):
    if tipo == "PEDIDOS":
        return import_pedidos_csv(data, filename)
    if tipo == "FATURAMENTO":
        return import_faturamento_csv(data, filename)
    if tipo == "ORCAMENTO":
        return import_orcamentos_csv(data, filename)
    if tipo == "LIQUIDEZ":
        return import_liquidez_csv(data, filename)
    raise ValueError("Tipo de arquivo Sisplan não reconhecido.")


@app.route("/importar_csv", methods=["GET", "POST"])
@login_required
def importar_csv():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    from crm_npd.repositories.firebird.conexao import descricao_conexao, disponivel
    from crm_npd.settings import aplicar_env
    from crm_npd.sync.fonte import usando_firebird
    aplicar_env()
    somente_sqlite = not usando_firebird()
    fonte = "SQLite local (crm_npd.db)" if somente_sqlite else descricao_conexao()

    if request.method == "POST":
        if somente_sqlite:
            flash("Modo local: esta maquina le so o SQLite. O Sisplan nao e aberto daqui.", "warning")
            return redirect(url_for("importar_csv"))
        if not disponivel():
            flash("Firebird indisponivel. Confira SISPLAN_HOST, SISPLAN_DATABASE e SISPLAN_PASSWORD no .env.", "danger")
            return redirect(url_for("importar_csv"))
        try:
            from crm_npd.sync.etl import sincronizar_crm
            with conectar() as conn:
                lidas = sincronizar_crm(conn)
            flash(
                "Sisplan lido do FDB. Pedidos {pedidos} | Faturamento {faturamento} | Orcamentos {orcamentos} | Liquidez {liquidez}.".format(**lidas),
                "success",
            )
            return redirect(url_for("importacoes"))
        except Exception as e:
            flash(f"Falha ao ler o FDB: {e}", "danger")
            return redirect(url_for("importar_csv"))

    with conectar() as conn:
        origem = get_setting(conn, "sisplan_origem", "")
        synced = get_setting(conn, "sisplan_snapshot_synced_at", "")
        n_ped = int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] or 0)
        n_fat = int(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] or 0)
        n_orc = int(conn.execute("SELECT COUNT(*) FROM orcamentos").fetchone()[0] or 0)
        n_liq = int(conn.execute("SELECT COUNT(*) FROM liquidez").fetchone()[0] or 0)

    body = render_template_string("""
    <div class="d-flex justify-content-between align-items-end flex-wrap gap-2 mb-4">
      <div>
        <div class="h3 mb-1">Atualizar dados do Sisplan</div>
        <div class="muted">Conecta no Sisplan da fabrica em modo somente leitura (MANUAL_ACESSO_BANCO_SISPLAN.md).</div>
      </div>
      <a class="btn btn-outline-secondary" href="{{ url_for('importacoes') }}"><i class="bi bi-clock-history me-1"></i> Historico</a>
    </div>
    <div class="row g-3">
      <div class="col-12 col-xl-8">
        <div class="cardx p-4">
          <div class="h5 mb-2">Fonte atual</div>
          <div class="small text-secondary mb-1">Conexao</div>
          <div class="fw-bold mb-3" style="word-break:break-all">{{ fonte }}</div>
          <div class="small text-secondary mb-1">Ultima leitura</div>
          <div class="mb-3">{{ synced or 'ainda nao sincronizado' }}{% if origem %} &middot; origem {{ origem }}{% endif %}</div>
          <div class="d-flex flex-wrap gap-3 small mb-4">
            <span>Pedidos <b>{{ n_ped }}</b></span>
            <span>Faturamento <b>{{ n_fat }}</b></span>
            <span>Orcamentos <b>{{ n_orc }}</b></span>
            <span>Liquidez <b>{{ n_liq }}</b></span>
          </div>
          <form method="post" id="sisplanFdbForm">
            <button class="btn btn-primary px-4" {% if not ok %}disabled{% endif %}><i class="bi bi-arrow-repeat me-1"></i> Ler FDB agora</button>
          </form>
          {% if somente_sqlite %}<div class="alert alert-info mt-3 mb-0">Modo local: os numeros desta tela vem do SQLite copiado. O Sisplan nao e aberto daqui. No servidor continue usando run.bat.</div>
          {% elif not ok %}<div class="alert alert-warning mt-3 mb-0">Firebird indisponivel. Confira host, porta, caminho do FDB, DLL e senha no .env.</div>{% endif %}
        </div>
      </div>
      <div class="col-12 col-xl-4">
        <div class="cardx p-4 h-100">
          <div class="h6 mb-3">O que acontece</div>
          <p class="small text-secondary">O CRM abre o FDB em modo somente leitura, substitui pedidos, faturamento, orcamentos e liquidez, e recalcula redes e tarefas. CSV nao entra mais.</p>
          <p class="small text-secondary mb-0">Padrao da fabrica: <code>192.168.100.168:3051</code> em <code>C:\\Sisplan\\Base\\14215F.FDB</code>. Nada alem de SELECT.</p>
        </div>
      </div>
    </div>
    <div id="sisplanProcessing" style="display:none;position:fixed;inset:0;z-index:99999;background:rgba(6,18,35,.72);align-items:center;justify-content:center;padding:24px">
      <div style="width:min(520px,94vw);background:#fff;border-radius:20px;padding:28px">
        <div class="d-flex align-items-center gap-3">
          <div class="spinner-border text-primary" role="status"></div>
          <div><div class="h5 mb-1">Lendo o Sisplan</div><div class="text-secondary small">Consultando o FDB. Pode levar alguns minutos na primeira vez.</div></div>
        </div>
      </div>
    </div>
    <script>
      const form = document.getElementById('sisplanFdbForm');
      const overlay = document.getElementById('sisplanProcessing');
      if (form && overlay) form.addEventListener('submit', () => { overlay.style.display = 'flex'; });
    </script>
    """, fonte=fonte, synced=synced, origem=origem, n_ped=n_ped, n_fat=n_fat, n_orc=n_orc, n_liq=n_liq, ok=disponivel() and not somente_sqlite, somente_sqlite=somente_sqlite)
    return render_page("Atualizar Sisplan", body, "importar_csv")


@app.route("/importacoes")
@login_required
def importacoes():
    # --- TRAVA DE SEGURANÇA: APENAS ADMIN E GESTOR ---
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    with conectar() as conn:
        rows = conn.execute("""
            SELECT
                id,
                tipo,
                arquivo,
                imported_at,
                linhas,
                registros,
                COALESCE(novos, 0) AS novos,
                COALESCE(substituidos, 0) AS substituidos,
                COALESCE(duplicados_arquivo, 0) AS duplicados_arquivo
            FROM imports
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-end flex-wrap gap-2 mb-3">
      <div>
        <div class="h3 mb-1">Importações</div>
        <div class="muted">Exclusão remove somente os dados daquele lote.</div>
      </div>
      <div class="d-flex gap-2 flex-wrap">                            
        <a class="btn btn-primary" href="{{ url_for('importar_csv') }}"><i class="bi bi-arrow-repeat me-1"></i>Atualizar Sisplan</a>
        <a class="btn btn-warning text-white" href="{{ url_for('pendencias_importacao') }}">Pendências de Importação</a>
        <form method="post" action="{{ url_for('reprocessar_redes_importacao') }}" onsubmit="return confirm('Deseja reprocessar nomes, redes, matrizes e filiais agora?');">
          <button class="btn btn-outline-success">Reprocessar nomes/redes</button>
        </form>
      </div>
    </div>
    

    <div class="cardx p-3">
      <div class="table-responsive">
        <table class="table align-middle mb-0">
          <thead>
            <tr>
              <th>ID</th>
              <th>Tipo</th>
              <th>Arquivo</th>
              <th>Importado em</th>
              <th class="text-end">Linhas</th>
              <th class="text-end">Registros</th>
              <th class="text-end">Novos</th>
              <th class="text-end">Atualizados</th>
              <th class="text-end">Duplicados</th>
              <th class="text-end">Ações</th>
            </tr>
          </thead>
          <tbody>
            {% for r in rows %}
            <tr>
              <td>{{ r['id'] }}</td>

              <td>
                <span class="badge text-bg-light border">{{ r['tipo'] }}</span>
              </td>

              <td class="fw-semibold">{{ r['arquivo'] }}</td>

              <td class="muted">{{ r['imported_at'] }}</td>

              <td class="text-end">
                <span class="badge bg-secondary-subtle text-dark">{{ r['linhas'] }}</span>
              </td>

              <td class="text-end">
                <span class="badge bg-warning text-dark">{{ r['registros'] }}</span>
              </td>

              <td class="text-end">
                <span class="badge bg-success">{{ r['novos'] }}</span>
              </td>

              <td class="text-end">
                <span class="badge bg-primary">{{ r['substituidos'] }}</span>
              </td>
              
              <td class="text-end">
                <span class="badge bg-dark">{{ r['duplicados_arquivo'] }}</span>
              </td>

              <td class="text-end">
                <form method="post"
                      action="{{ url_for('excluir_importacao', import_id=r['id']) }}"
                      onsubmit="return confirm('Deseja excluir este lote de importação?');">
                  <button class="btn btn-sm btn-outline-danger">Excluir</button>
                </form>
              </td>
            </tr>
            {% else %}
            <tr>
              <td colspan="10" class="text-center py-4 muted">Nenhuma importação encontrada.</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    """, rows=rows)

    return render_page("Importações", body, "importacoes")


@app.route("/importacoes/reprocessar_redes", methods=["POST"])
@login_required
def reprocessar_redes_importacao():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("importacoes"))

    try:
        with conectar() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                pass
            nome_stats = repair_generic_customer_names(conn)
            sync_identity_rules_from_jit_history(conn)
            tarefa_stats = refresh_repurchase_tasks_after_sisplan(conn, actor_name=(user.get("nome") or "Sistema"))
            conn.commit()
        flash(
            f"Nomes e redes reprocessados com sucesso. {int(nome_stats.get('resolved',0) or 0)} nome(s) genérico(s) recuperado(s); "
            f"{int(tarefa_stats.get('cancelled',0) or 0)} tarefa(s) antiga(s) encerrada(s).",
            "success"
        )
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        flash(f"Erro ao reprocessar redes: {e}", "danger")

    return redirect(url_for("importacoes"))


@app.route("/importacoes/pendencias")
@login_required
def pendencias_importacao():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    with conectar() as conn:
        rows = conn.execute("""
            SELECT
                p.id,
                p.customer_id,
                p.customer_nome,
                p.customer_codigo,
                p.condicao_entrega,
                p.candidato_customer_id,
                p.candidato_nome,
                p.status,
                p.created_at
            FROM import_identity_pending p
            WHERE p.status = 'PENDENTE'
            ORDER BY p.id DESC
        """).fetchall()

        matrizes = conn.execute("""
            SELECT
                c.id AS customer_id,
                COALESCE(c.fantasia, c.nome, '') AS nome_base,
                COALESCE(c.codigo, '') AS codigo,
                cir.identity_key
            FROM customer_identity_rules cir
            JOIN customers c ON c.id = cir.customer_id
            WHERE cir.tipo_vinculo = 'MATRIZ'
            ORDER BY COALESCE(c.fantasia, c.nome, '') ASC
        """).fetchall()

    body = render_template_string("""
    <div class="container-fluid py-3">
        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
            <div>
                <h3 class="mb-1">Pendências de Importação</h3>
                <div class="text-muted small">
                    Resolva clientes que ficaram fora da unificação automática.
                </div>
            </div>
            <div>
                <a href="{{ url_for('importacoes') }}" class="btn btn-outline-secondary">
                    Voltar para Importações
                </a>
            </div>
        </div>

        <div class="card shadow-sm border-0">
            <div class="card-body">
                {% if rows %}
                    <div class="table-responsive">
                        <table class="table table-sm align-middle">
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Cliente pendente</th>
                                    <th>Candidato atual</th>
                                    <th>Ação</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for r in rows %}
                                <tr>
                                    <td>{{ r['id'] }}</td>
                                    <td>
                                        <div class="fw-semibold">{{ r['customer_nome'] }}</div>
                                        <div class="small text-muted">ID {{ r['customer_id'] }} · Código {{ r['customer_codigo'] or '-' }}</div>
                                    </td>
                                    <td>
                                        <div class="fw-semibold">{{ r['candidato_nome'] or '-' }}</div>
                                        <div class="small text-muted">ID {{ r['candidato_customer_id'] or '-' }}</div>
                                    </td>
                                    <td style="min-width:360px">
                                        <form method="post" action="{{ url_for('resolver_pendencia_importacao') }}" class="row g-2">
                                            <input type="hidden" name="pendencia_id" value="{{ r['id'] }}">
                                            <div class="col-12">
                                                <select name="acao" class="form-select form-select-sm">
                                                    <option value="FILIAL">Mover para matriz existente</option>
                                                    <option value="MATRIZ">Definir como nova matriz</option>
                                                    <option value="CLIENTE">Manter cliente individual</option>
                                                    <option value="IGNORAR">Ignorar pendência</option>
                                                </select>
                                            </div>
                                            <div class="col-12">
                                                <select name="target_customer_id" class="form-select form-select-sm">
                                                    <option value="">Selecione a matriz de destino</option>
                                                    {% for m in matrizes %}
                                                      <option value="{{ m['customer_id'] }}" {% if r['candidato_customer_id'] == m['customer_id'] %}selected{% endif %}>
                                                        {{ m['nome_base'] }}{% if m['codigo'] %} · {{ m['codigo'] }}{% endif %}
                                                      </option>
                                                    {% endfor %}
                                                </select>
                                            </div>
                                            <div class="col-12">
                                                <input type="text" class="form-control form-control-sm" name="identity_nome_manual"
                                                       value="{{ r['customer_nome'] }}" placeholder="Nome da identidade comercial">
                                            </div>
                                            <div class="col-12 d-grid">
                                                <button class="btn btn-primary btn-sm">Salvar decisão</button>
                                            </div>
                                        </form>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                {% else %}
                    <div class="alert alert-success mb-0">
                        Nenhuma pendência de importação no momento.
                    </div>
                {% endif %}
            </div>
        </div>
    </div>
    """, rows=rows, matrizes=matrizes)

    return render_page("Pendências de Importação", body, "importacoes")


@app.route("/importacoes/pendencias/enviar", methods=["POST"])
@login_required
def enviar_para_pendencias_importacao():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("clientes"))

    customer_id = int(request.form.get("customer_id") or 0)
    candidato_customer_id = int(request.form.get("candidato_customer_id") or 0)

    if customer_id <= 0:
        flash("Cliente inválido.", "danger")
        return redirect(request.referrer or url_for("clientes"))

    try:
        with conectar() as conn:
            c = conn.execute("""
                SELECT id, COALESCE(fantasia, nome, '') AS nome_base, COALESCE(codigo, '') AS codigo
                FROM customers WHERE id = ?
            """, (customer_id,)).fetchone()
            if not c:
                flash("Cliente não encontrado.", "warning")
                return redirect(request.referrer or url_for("clientes"))

            cand_nome = None
            if candidato_customer_id > 0:
                cand = conn.execute("""
                    SELECT COALESCE(fantasia, nome, '') AS nome_base
                    FROM customers WHERE id = ?
                """, (candidato_customer_id,)).fetchone()
                cand_nome = (cand["nome_base"] if cand else "")

            exists = conn.execute("""
                SELECT id FROM import_identity_pending
                WHERE customer_id = ? AND status = 'PENDENTE'
                LIMIT 1
            """, (customer_id,)).fetchone()

            if not exists:
                conn.execute("""
                    INSERT INTO import_identity_pending (
                        import_id, customer_id, customer_nome, customer_codigo,
                        condicao_entrega, candidato_customer_id, candidato_nome,
                        status, created_at
                    ) VALUES (NULL, ?, ?, ?, NULL, ?, ?, 'PENDENTE', CURRENT_TIMESTAMP)
                """, (
                    customer_id,
                    c["nome_base"],
                    c["codigo"],
                    candidato_customer_id if candidato_customer_id > 0 else None,
                    cand_nome,
                ))
            conn.commit()
        flash("Cliente enviado para pendências.", "success")
    except Exception as e:
        flash(f"Erro ao enviar para pendências: {e}", "danger")

    return redirect(request.referrer or url_for("clientes"))


@app.route("/importacoes/pendencias/resolver", methods=["POST"])
@login_required
def resolver_pendencia_importacao():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("pendencias_importacao"))

    pendencia_id = int(request.form.get("pendencia_id") or 0)
    acao = (request.form.get("acao") or "").strip().upper()
    target_customer_id = int(request.form.get("target_customer_id") or 0)
    identity_nome_manual = (request.form.get("identity_nome_manual") or "").strip()

    if pendencia_id <= 0 or acao not in {"FILIAL", "MATRIZ", "CLIENTE", "IGNORAR"}:
        flash("Dados inválidos para resolver a pendência.", "danger")
        return redirect(url_for("pendencias_importacao"))

    try:
        with conectar() as conn:
            p = conn.execute("""
                SELECT id, customer_id, customer_nome, candidato_customer_id, candidato_nome
                FROM import_identity_pending
                WHERE id = ? AND status = 'PENDENTE'
                LIMIT 1
            """, (pendencia_id,)).fetchone()

            if not p:
                flash("Pendência não encontrada ou já resolvida.", "warning")
                return redirect(url_for("pendencias_importacao"))

            customer_id = int(p["customer_id"] or 0)
            customer_nome = identity_nome_manual or (p["customer_nome"] or "").strip() or f"Cliente {customer_id}"

            if acao == "IGNORAR":
                conn.execute("UPDATE import_identity_pending SET status='IGNORADO', resolved_at=CURRENT_TIMESTAMP WHERE id=?", (pendencia_id,))
                conn.commit()
                flash("Pendência ignorada.", "success")
                return redirect(url_for("pendencias_importacao"))

            if acao == "FILIAL":
                destino_id = target_customer_id or int(p["candidato_customer_id"] or 0)
                if destino_id <= 0:
                    flash("Selecione a matriz de destino.", "warning")
                    return redirect(url_for("pendencias_importacao"))

                base = conn.execute("""
                    SELECT customer_id, identity_key, identity_nome, COALESCE(matriz_customer_id, 0) AS matriz_customer_id, tipo_vinculo
                    FROM customer_identity_rules
                    WHERE customer_id = ?
                    LIMIT 1
                """, (destino_id,)).fetchone()

                if base:
                    identity_key = (base["identity_key"] or "").strip()
                    identity_nome = identity_nome_manual or (base["identity_nome"] or "").strip() or customer_nome
                    matriz_customer_id = int(base["matriz_customer_id"] or 0) or int(base["customer_id"])
                else:
                    rowd = conn.execute("SELECT COALESCE(fantasia, nome, '') AS nome_base FROM customers WHERE id=?", (destino_id,)).fetchone()
                    identity_nome = identity_nome_manual or ((rowd["nome_base"] if rowd else "") or customer_nome)
                    identity_key = f"IDENT:{identity_nome}"
                    matriz_customer_id = destino_id
                    conn.execute("""
                        INSERT OR REPLACE INTO customer_identity_rules (
                            customer_id, identity_key, identity_nome, tipo_vinculo,
                            matriz_customer_id, origem, updated_at
                        ) VALUES (?, ?, ?, 'MATRIZ', ?, 'MANUAL', CURRENT_TIMESTAMP)
                    """, (destino_id, identity_key, identity_nome, destino_id))

                conn.execute("""
                    INSERT OR REPLACE INTO customer_identity_rules (
                        customer_id, identity_key, identity_nome, tipo_vinculo,
                        matriz_customer_id, origem, updated_at
                    ) VALUES (?, ?, ?, 'FILIAL', ?, 'MANUAL', CURRENT_TIMESTAMP)
                """, (customer_id, identity_key, identity_nome, matriz_customer_id))

            elif acao == "MATRIZ":
                identity_key = f"IDENT:{customer_nome}"
                conn.execute("""
                    INSERT OR REPLACE INTO customer_identity_rules (
                        customer_id, identity_key, identity_nome, tipo_vinculo,
                        matriz_customer_id, origem, updated_at
                    ) VALUES (?, ?, ?, 'MATRIZ', ?, 'MANUAL', CURRENT_TIMESTAMP)
                """, (customer_id, identity_key, customer_nome, customer_id))

            elif acao == "CLIENTE":
                identity_key = f"IDENT:{customer_nome}"
                conn.execute("""
                    INSERT OR REPLACE INTO customer_identity_rules (
                        customer_id, identity_key, identity_nome, tipo_vinculo,
                        matriz_customer_id, origem, updated_at
                    ) VALUES (?, ?, ?, 'CLIENTE', NULL, 'MANUAL', CURRENT_TIMESTAMP)
                """, (customer_id, identity_key, customer_nome))

            conn.execute("UPDATE import_identity_pending SET status='RESOLVIDO', resolved_at=CURRENT_TIMESTAMP WHERE id=?", (pendencia_id,))
            conn.commit()

        flash(f"Pendência resolvida como {acao}.", "success")
    except Exception as e:
        flash(f"Erro ao resolver pendência: {e}", "danger")

    return redirect(url_for("pendencias_importacao"))

@app.route("/importacoes/<int:import_id>/excluir", methods=["POST"])
@login_required
def excluir_importacao(import_id):
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    with conectar() as conn:
        row = conn.execute(
            "SELECT id, tipo FROM imports WHERE id=?",
            (import_id,)
        ).fetchone()

        if not row:
            flash("Importação não encontrada.", "warning")
            return redirect(url_for("importacoes"))

        tipo = norm(row["tipo"] or "").upper()
        tipo = tipo.replace("Ç", "C").replace("Ã", "A").replace("Á", "A").replace("Â", "A")

        if tipo == "PEDIDOS":
            conn.execute("DELETE FROM orders WHERE import_id=?", (import_id,))

        elif tipo == "FATURAMENTO":
            conn.execute("DELETE FROM invoices WHERE import_id=?", (import_id,))

        elif tipo == "LIQUIDEZ":
            conn.execute("DELETE FROM liquidez WHERE import_id=?", (import_id,))

        elif tipo == "ORCAMENTO":
            conn.execute("DELETE FROM orcamentos WHERE import_id=?", (import_id,))

        else:
            flash(f"Tipo de importação desconhecido: {row['tipo']}", "danger")
            return redirect(url_for("importacoes"))

        conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
        conn.commit()

    flash("Importação excluída com sucesso.", "success")
    return redirect(url_for("importacoes"))



def sync_identity_rules_from_jit_history(conn):
    """
    Reconstrói as identidades comerciais com estas validações, em ordem:
    1) JIT em pedidos cria MATRIZ.
    2) IMEDIATA com 1 matriz compatível vira FILIAL.
    3) Fantasia/nome + código do PRODUTO do faturamento agrupam a REDE.

    Observações práticas desta versão:
    - quando o banco não tem CONDICAO_ENTREGA preenchida, ainda assim a rede é montada
      por PRODUTO + NOME, para evitar que tudo fique solto;
    - clientes sem faturamento, mas com nome claramente compatível com a rede e histórico
      de pedidos, também entram na rede;
    - se não houver matriz JIT, mas houver um candidato muito forte (ex.: MASTER,
      MATRIZ, FRANCHISING e sem faturamento), ele é tratado como matriz heurística.
    - regras MANUAL sempre prevalecem.
    """
    from collections import defaultdict, Counter
    import unicodedata

    _clean_text_cache = {}
    _tokens_cache = {}

    def _strip_accents(s: str) -> str:
        return "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))

    def _clean_text(s: str) -> str:
        key = str(s or "")
        cached = _clean_text_cache.get(key)
        if cached is not None:
            return cached
        out = _strip_accents(key).upper()
        out = out.replace("&", " ")
        out = re.sub(r"[^A-Z0-9 ]+", " ", out)
        out = re.sub(r"\s+", " ", out).strip()
        _clean_text_cache[key] = out
        return out

    LEGAL_TOKENS = {
        "LTDA", "EIRELI", "ME", "MEI", "EPP", "S", "A", "SA",
        "FILIAL", "UNIDADE", "LOJA", "LJ", "SHOPPING", "STORE",
        "RESTAURANTE", "LANCHONETE", "PADARIA", "CAFETERIA"
    }
    BRAND_DROP_TOKENS = {
        "MASTER", "MATRIZ", "FILIAL", "FRANCHISING", "FRANQUIA", "FRANQUIAS"
    }
    MATRIX_HINT_TOKENS = {"MASTER", "MATRIZ", "FRANCHISING", "FRANQUIA", "FRANQUIAS", "SEDE"}
    CONNECTOR_TOKENS = {"DO", "DA", "DE", "DOS", "DAS", "E"}
    STATE_TOKENS = {
        "SC","RS","PR","SP","RJ","GO","MT","MS","MG","BA","DF","ES","PA","MA",
        "CE","PE","PB","RN","AL","SE","PI","AM","RO","RR","AC","AP","TO"
    }

    def _tokens_name(name: str, drop_brand_tokens: bool = False):
        cache_key = (str(name or ""), bool(drop_brand_tokens))
        cached = _tokens_cache.get(cache_key)
        if cached is not None:
            return cached
        raw = [t for t in _clean_text(name).split(" ") if t]
        out = []
        i = 0
        while i < len(raw):
            tok = raw[i]
            if tok in {"LOJA", "LJ"} and i + 1 < len(raw) and re.fullmatch(r"\d+", raw[i + 1] or ""):
                i += 2
                continue
            if tok.startswith("LJ") and len(tok) > 2 and tok[2:].isdigit():
                i += 1
                continue
            if tok in LEGAL_TOKENS:
                i += 1
                continue
            if drop_brand_tokens and tok in BRAND_DROP_TOKENS:
                i += 1
                continue
            out.append(tok)
            i += 1
        result = tuple(out)
        _tokens_cache[cache_key] = result
        return result

    def _canonical_brand_name(name: str) -> str:
        toks = _tokens_name(name, drop_brand_tokens=True)
        return " ".join(toks).strip() if toks else ""

    def _extract_product_code(produto: str) -> str:
        produto = norm(produto or "").strip()
        return produto.split(" - ", 1)[0].strip() if produto else ""

    def _extract_product_brand(produto: str) -> str:
        produto = norm(produto or "").strip()
        if not produto:
            return ""
        parts = [p.strip() for p in produto.split(" - ") if p and str(p).strip()]
        if len(parts) >= 3:
            return _canonical_brand_name(parts[2])
        return ""

    def _name_compatible(customer_name: str, network_name: str) -> bool:
        ct = _tokens_name(customer_name, drop_brand_tokens=False)
        nt = _tokens_name(network_name, drop_brand_tokens=True)
        if not ct or not nt:
            return False
        if len(ct) >= len(nt) and ct[:len(nt)] == nt:
            return True
        if len(nt) >= len(ct) and nt[:len(ct)] == ct:
            return True
        if ct[0] == nt[0]:
            if len(nt) == 1 or len(ct) == 1:
                return True
            if len(ct) >= 2 and len(nt) >= 2 and ct[:2] == nt[:2]:
                return True
        return False

    WEAK_SINGLE_TOKEN_NETWORKS = {
        "ANA", "MARIA", "JOAO", "JOSE", "LUIS", "LUIZ", "CARLOS", "PAULO", "PEDRO",
        "MARCOS", "JULIA", "JULIANA", "FERNANDA", "BEATRIZ", "GABRIELA", "LOJA", "STORE"
    }

    def _is_weak_network_name(name: str) -> bool:
        toks = _tokens_name(name, drop_brand_tokens=True)
        if not toks:
            return True
        if len(toks) == 1:
            tok = toks[0].strip()
            if tok in WEAK_SINGLE_TOKEN_NETWORKS:
                return True
            if len(tok) <= 3:
                return True
        return False

    def _infer_network_from_name(name: str) -> str:
        base = _clean_text(name)
        if not base:
            return ""
        base_norm = base.strip()
        for apelido, rede_principal in REDE_ALIASES_MANUAIS.items():
            apelido_norm = _clean_text(apelido)
            if apelido_norm and (apelido_norm in base_norm or base_norm in apelido_norm):
                return _canonical_brand_name(rede_principal)
        toks = _tokens_name(base, drop_brand_tokens=True)
        if not toks:
            return ""
        if len(toks) >= 2 and toks[1] in {"MAN", "CALCADOS", "BURGER", "FOOD", "DOS"}:
            return " ".join(toks[:2]).strip()
        if len(toks) >= 3 and toks[0] == "MERCADAO" and toks[1] == "DOS":
            return " ".join(toks[:3]).strip()
        if len(toks) == 1 and _is_weak_network_name(toks[0]):
            return ""
        return toks[0].strip()

    def _network_name_from_members(member_names, fallback_name: str = "") -> str:
        token_lists = []
        for n in (member_names or []):
            toks = _tokens_name(n, drop_brand_tokens=True)
            if toks:
                token_lists.append(toks)
        if not token_lists:
            return _canonical_brand_name(fallback_name)
        prefix = []
        for idx in range(min(len(t) for t in token_lists)):
            tok = token_lists[0][idx]
            if all(len(t) > idx and t[idx] == tok for t in token_lists):
                prefix.append(tok)
            else:
                break
        while prefix and (prefix[-1] in CONNECTOR_TOKENS or prefix[-1] in STATE_TOKENS or prefix[-1].isdigit()):
            prefix.pop()
        if prefix:
            return " ".join(prefix[:3]).strip()
        common = Counter(tuple(t[:2]) for t in token_lists if t).most_common(1)
        if common and common[0][0]:
            toks = [t for t in common[0][0] if t]
            while toks and (toks[-1] in CONNECTOR_TOKENS or toks[-1] in STATE_TOKENS or toks[-1].isdigit()):
                toks.pop()
            if toks:
                return " ".join(toks).strip()
        return _canonical_brand_name(fallback_name)

    def _display_name(row) -> str:
        return (row["nome_base"] or "").strip() or f"Cliente {int(row['id'])}"

    manual_rules = conn.execute("""
        SELECT customer_id, identity_key, identity_nome, tipo_vinculo,
               COALESCE(matriz_customer_id, 0) AS matriz_customer_id,
               origem, observacao
        FROM customer_identity_rules
        WHERE UPPER(COALESCE(origem, '')) = 'MANUAL'
    """).fetchall()
    manual_by_customer = {int(r["customer_id"]): r for r in manual_rules}

    conn.execute("DELETE FROM customer_identity_rules WHERE UPPER(COALESCE(origem, '')) <> 'MANUAL'")
    conn.execute("DELETE FROM import_identity_pending WHERE status = 'PENDENTE'")
    conn.execute("DELETE FROM customer_group_members")
    conn.execute("DELETE FROM customer_groups")
    conn.execute("UPDATE customers SET grupo_id = NULL, rede_nome = NULL, rede_key = NULL")

    # IMPORTANTE: invoices e orders são pré-agregados antes do JOIN.
    # A consulta antiga juntava as duas tabelas brutas pelo cliente, produzindo
    # N_notas × N_pedidos linhas para cada cliente antes do GROUP BY. Em bases
    # históricas isso explodia para milhões de combinações e deixava o pós-import
    # travado por minutos. O resultado lógico abaixo é o mesmo para as regras de
    # identidade, sem a multiplicação cartesiana.
    customers = conn.execute("""
        SELECT
            c.id,
            COALESCE(NULLIF(c.fantasia, ''), c.nome, '') AS nome_base,
            COALESCE(c.codigo, '') AS codigo,
            COALESCE(i.invoice_count, 0) AS invoice_count,
            COALESCE(o.order_count, 0) AS order_count,
            COALESCE(o.has_jit, 0) AS has_jit,
            COALESCE(o.has_imediata, 0) AS has_imediata,
            COALESCE(o.any_condicao, '') AS any_condicao
        FROM customers c
        LEFT JOIN (
            SELECT cliente_id, COUNT(*) AS invoice_count
            FROM invoices
            WHERE cliente_id IS NOT NULL
            GROUP BY cliente_id
        ) i ON i.cliente_id = c.id
        LEFT JOIN (
            SELECT
                cliente_id,
                COUNT(*) AS order_count,
                MAX(CASE WHEN UPPER(REPLACE(TRIM(COALESCE(condicao_entrega, '')),' ','')) LIKE '98%JIT%' THEN 1 ELSE 0 END) AS has_jit,
                MAX(CASE WHEN UPPER(COALESCE(condicao_entrega, '')) LIKE '%IMEDIATA%' THEN 1 ELSE 0 END) AS has_imediata,
                MAX(COALESCE(condicao_entrega, '')) AS any_condicao
            FROM orders
            WHERE cliente_id IS NOT NULL
            GROUP BY cliente_id
        ) o ON o.cliente_id = c.id
        ORDER BY c.id
    """).fetchall()
    customer_meta = {int(c["id"]): c for c in customers}

    invoice_rows = conn.execute("""
        SELECT i.cliente_id, COALESCE(c.fantasia, c.nome, '') AS nome_base, COALESCE(i.produto, '') AS produto
        FROM invoices i
        JOIN customers c ON c.id = i.cliente_id
        WHERE COALESCE(i.produto, '') <> ''
    """).fetchall()

    raw_networks = {}
    customer_raw_scores = defaultdict(lambda: defaultdict(lambda: {"rows": 0, "codes": set(), "name": ""}))
    for row in invoice_rows:
        cid = int(row["cliente_id"] or 0)
        customer_name = (row["nome_base"] or "").strip()
        produto = (row["produto"] or "").strip()
        code = _extract_product_code(produto)
        brand = _extract_product_brand(produto)
        if cid <= 0 or not customer_name or not code or not brand:
            continue
        if not _name_compatible(customer_name, brand):
            continue
        raw_key = normalize_key_text(brand)
        if not raw_key:
            continue
        entry = raw_networks.setdefault(raw_key, {"brand": brand, "customer_ids": set(), "codes": set(), "rows": 0})
        entry["customer_ids"].add(cid)
        entry["codes"].add(code)
        entry["rows"] += 1
        sc = customer_raw_scores[cid][raw_key]
        sc["rows"] += 1
        sc["codes"].add(code)
        sc["name"] = brand

    raw_to_final = {}
    final_networks = {}
    for raw_key, info in raw_networks.items():
        member_names = [_display_name(customer_meta[cid]) for cid in info["customer_ids"] if cid in customer_meta]
        final_name = _network_name_from_members(member_names, info["brand"]) if len(info["customer_ids"]) >= 2 else _canonical_brand_name(info["brand"])
        final_key = normalize_key_text(final_name or info["brand"])
        if not final_key:
            continue
        raw_to_final[raw_key] = (final_key, final_name)
        entry = final_networks.setdefault(final_key, {"name": final_name, "customer_ids": set(), "codes": set(), "rows": 0})
        entry["customer_ids"].update(info["customer_ids"])
        entry["codes"].update(info["codes"])
        entry["rows"] += info["rows"]

    customer_network_scores = defaultdict(lambda: defaultdict(lambda: {"rows": 0, "codes": set(), "name": ""}))
    for cid, score_map in customer_raw_scores.items():
        for raw_key, sc in score_map.items():
            if raw_key not in raw_to_final:
                continue
            final_key, final_name = raw_to_final[raw_key]
            out = customer_network_scores[cid][final_key]
            out["rows"] += int(sc["rows"] or 0)
            out["codes"].update(sc["codes"] or set())
            out["name"] = final_name

    # Índices por primeiro token evitam comparar cada cliente com todas as redes.
    # Toda compatibilidade aceita por _name_compatible compartilha o primeiro token,
    # então o resultado lógico é preservado com uma fração das comparações.
    network_first_index = defaultdict(list)
    network_exact_index = defaultdict(list)
    for nk, info in final_networks.items():
        ntoks = _tokens_name(info.get("name", ""), drop_brand_tokens=True)
        if ntoks:
            network_first_index[ntoks[0]].append(nk)
        nkey = normalize_key_text(info.get("name", ""))
        if nkey:
            network_exact_index[nkey].append(nk)

    # estende a rede para clientes sem faturamento, mas com nome/fantasia compatível com uma única rede
    # regra reforçada: não auto-agrupar nomes genéricos de 1 palavra (ex.: ANA)
    for cid, meta in customer_meta.items():
        if cid in customer_network_scores:
            continue
        nome_base = _display_name(meta)
        compat = []
        ctoks = _tokens_name(nome_base, drop_brand_tokens=False)
        candidate_keys = network_first_index.get(ctoks[0], []) if ctoks else []
        for nk in candidate_keys:
            info = final_networks[nk]
            if _is_weak_network_name(info["name"]):
                continue
            if _name_compatible(nome_base, info["name"]):
                compat.append(nk)
        if len(compat) == 1:
            nk = compat[0]
            customer_network_scores[cid][nk]["rows"] = 1
            customer_network_scores[cid][nk]["name"] = final_networks[nk]["name"]

    customer_best_network = {}
    customer_best_ambiguous = {}
    for cid, score_map in customer_network_scores.items():
        ranked = []
        for key, sc in (score_map or {}).items():
            ranked.append((len(sc.get("codes", set())), int(sc.get("rows", 0)), key))
        ranked.sort(reverse=True)
        if not ranked:
            continue
        customer_best_network[cid] = ranked[0][2]
        customer_best_ambiguous[cid] = len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]
        final_networks.setdefault(ranked[0][2], {"name": score_map[ranked[0][2]].get("name") or _infer_network_from_name(_display_name(customer_meta[cid])), "customer_ids": set(), "codes": set(), "rows": 0})["customer_ids"].add(cid)

    matrices_by_network = defaultdict(list)
    final_assignments = {}  # cid -> dict(tipo, key, nome, matriz_id, network_key, network_name)

    # 1) MANUAL sempre prevalece
    for mr in manual_rules:
        cid = int(mr["customer_id"] or 0)
        if cid <= 0:
            continue
        identity_key = (mr["identity_key"] or "").strip() or f"CLIENTE:{cid}"
        identity_nome = (mr["identity_nome"] or "").strip() or _display_name(customer_meta.get(cid, {"id": cid, "nome_base": ""}))
        tipo = (mr["tipo_vinculo"] or "CLIENTE").strip().upper()
        matriz_id = int(mr["matriz_customer_id"] or 0)
        network_key = customer_best_network.get(cid, normalize_key_text(_infer_network_from_name(identity_nome)))
        network_name = final_networks.get(network_key, {}).get("name") or _infer_network_from_name(identity_nome) or identity_nome
        final_assignments[cid] = {"tipo": tipo, "identity_key": identity_key, "identity_nome": identity_nome, "matriz_customer_id": matriz_id, "network_key": network_key, "network_name": network_name, "origem": "MANUAL", "observacao": mr["observacao"] or ""}
        if tipo == "MATRIZ":
            matrices_by_network[network_key].append({"customer_id": cid, "identity_key": identity_key, "identity_nome": identity_nome, "network_key": network_key, "network_name": network_name, "origem": "MANUAL"})

    # 2) MATRIZ por JIT
    for cid, meta in customer_meta.items():
        if cid in final_assignments:
            continue
        if int(meta["has_jit"] or 0) != 1:
            continue
        nome_base = _display_name(meta)
        network_key = customer_best_network.get(cid, "")
        network_name = final_networks.get(network_key, {}).get("name", "") if network_key else ""
        if not network_name:
            network_name = _infer_network_from_name(nome_base) or nome_base
            network_key = normalize_key_text(network_name)
        identity_key = f"MATRIZ:{cid}"
        final_assignments[cid] = {"tipo": "MATRIZ", "identity_key": identity_key, "identity_nome": nome_base, "matriz_customer_id": cid, "network_key": network_key, "network_name": network_name, "origem": "AUTO_JIT", "observacao": f"Matriz automática por JIT ({network_name})"}
        matrices_by_network[network_key].append({"customer_id": cid, "identity_key": identity_key, "identity_nome": nome_base, "network_key": network_key, "network_name": network_name, "origem": "AUTO_JIT"})
        final_networks.setdefault(network_key, {"name": network_name, "customer_ids": set(), "codes": set(), "rows": 0})["customer_ids"].add(cid)

    # 3) fallback de matriz heurística, quando não veio JIT mas existe um candidato forte
    for nk, info in list(final_networks.items()):
        if matrices_by_network.get(nk):
            continue
        members = sorted(info.get("customer_ids", set()))
        if not members:
            continue
        cand = []
        for cid in members:
            if cid in final_assignments:
                continue
            meta = customer_meta.get(cid)
            if not meta:
                continue
            nome = _display_name(meta)
            clean = _clean_text(nome)
            tok_score = 1 if any(tok in clean.split() for tok in MATRIX_HINT_TOKENS) else 0
            no_invoice = 1 if int(meta["invoice_count"] or 0) == 0 and int(meta["order_count"] or 0) > 0 else 0
            exact = 1 if _canonical_brand_name(nome) == info["name"] else 0
            if tok_score or no_invoice or exact:
                cand.append((tok_score, no_invoice, exact, -len(nome), cid))
        cand.sort(reverse=True)
        if cand:
            top = cand[0]
            unique = len(cand) == 1 or cand[0][:3] != cand[1][:3]
            if unique:
                cid = top[4]
                meta = customer_meta[cid]
                nome = _display_name(meta)
                identity_key = f"MATRIZ:{cid}"
                final_assignments[cid] = {"tipo": "MATRIZ", "identity_key": identity_key, "identity_nome": nome, "matriz_customer_id": cid, "network_key": nk, "network_name": info["name"], "origem": "AUTO_HEURISTICA", "observacao": f"Matriz heurística por pedidos/nome ({info['name']})"}
                matrices_by_network[nk].append({"customer_id": cid, "identity_key": identity_key, "identity_nome": nome, "network_key": nk, "network_name": info["name"], "origem": "AUTO_HEURISTICA"})

    # Recria os índices porque JIT/heurística acima podem ter criado novas redes.
    network_first_index = defaultdict(list)
    network_exact_index = defaultdict(list)
    for nk, info in final_networks.items():
        ntoks = _tokens_name(info.get("name", ""), drop_brand_tokens=True)
        if ntoks:
            network_first_index[ntoks[0]].append(nk)
        nkey = normalize_key_text(info.get("name", ""))
        if nkey:
            network_exact_index[nkey].append(nk)

    # 4) atribui REDE/FILIAL/CLIENTE para todos os demais
    for cid, meta in customer_meta.items():
        if cid in final_assignments:
            continue
        nome_base = _display_name(meta)
        has_imediata = int(meta["has_imediata"] or 0) == 1
        condicao_entrega = (meta["any_condicao"] or "").strip()
        network_key = customer_best_network.get(cid, "")
        network_name = final_networks.get(network_key, {}).get("name", "") if network_key else ""
        if not network_name:
            inferred = _infer_network_from_name(nome_base)
            ctoks = _tokens_name(nome_base, drop_brand_tokens=False)
            candidate_keys = network_first_index.get(ctoks[0], []) if ctoks else []
            compat = [nk for nk in candidate_keys if _name_compatible(nome_base, final_networks[nk]["name"])]
            if len(compat) == 1:
                network_key = compat[0]
                network_name = final_networks[network_key]["name"]
            elif inferred:
                matches = network_exact_index.get(normalize_key_text(inferred), [])
                if len(matches) == 1:
                    network_key = matches[0]
                    network_name = final_networks[network_key]["name"]

        candidate_matrices = matrices_by_network.get(network_key, []) if network_key else []
        if has_imediata and len(candidate_matrices) == 1:
            m = candidate_matrices[0]
            final_assignments[cid] = {"tipo": "FILIAL", "identity_key": m["identity_key"], "identity_nome": m["identity_nome"], "matriz_customer_id": int(m["customer_id"]), "network_key": network_key, "network_name": network_name or m["network_name"], "origem": "AUTO_IMEDIATA", "observacao": f"Filial automática por IMEDIATA vinculada à matriz {m['identity_nome']}"}
            continue
        if network_key and len(candidate_matrices) == 1:
            m = candidate_matrices[0]
            final_assignments[cid] = {"tipo": "FILIAL", "identity_key": m["identity_key"], "identity_nome": m["identity_nome"], "matriz_customer_id": int(m["customer_id"]), "network_key": network_key, "network_name": network_name or m["network_name"], "origem": "AUTO_PRODUTO", "observacao": f"Filial automática por produto/fantasia vinculada à matriz {m['identity_nome']}"}
            continue
        network_members = len(final_networks.get(network_key, {}).get("customer_ids", set())) if network_key else 0
        if network_key and network_members >= 2 and not _is_weak_network_name(network_name or nome_base):
            final_assignments[cid] = {"tipo": "REDE", "identity_key": f"REDE:{network_key}", "identity_nome": network_name or nome_base, "matriz_customer_id": 0, "network_key": network_key, "network_name": network_name or nome_base, "origem": "AUTO_PRODUTO", "observacao": f"Rede automática por produto/fantasia ({network_name or nome_base})"}
            continue
        if has_imediata and len(candidate_matrices) > 1:
            create_import_identity_pending(conn, customer_id=cid, customer_nome=nome_base, customer_codigo=(meta["codigo"] or "").strip(), condicao_entrega=condicao_entrega, candidato_customer_id=0, candidato_nome=network_name, import_id=None)
        final_assignments[cid] = {"tipo": "CLIENTE", "identity_key": f"CLIENTE:{cid}", "identity_nome": nome_base, "matriz_customer_id": 0, "network_key": "", "network_name": "", "origem": "AUTO_CLIENTE", "observacao": "Cliente sem rede definida"}

    # 5) persiste regras
    for cid, info in final_assignments.items():
        if info["origem"] == "MANUAL":
            # já existe na tabela; regrava só para consistência do banco atual
            pass
        conn.execute("""
            INSERT OR REPLACE INTO customer_identity_rules (
                customer_id, identity_key, identity_nome, tipo_vinculo,
                matriz_customer_id, origem, observacao, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (cid, info["identity_key"], info["identity_nome"], info["tipo"], int(info.get("matriz_customer_id") or 0), info["origem"], info.get("observacao") or ""))

    # 6) reconstrói customer_groups/customer_group_members e campos derivados dos clientes
    groups = defaultdict(list)
    for cid, info in final_assignments.items():
        groups[info["identity_key"]].append(cid)
        conn.execute("UPDATE customers SET rede_key=?, rede_nome=? WHERE id=?", (info.get("network_key") or "", info.get("network_name") or "", cid))

    for identity_key, members in groups.items():
        root = final_assignments[members[0]]
        group_name = (root.get("network_name") or root.get("identity_nome") or "Grupo").strip()
        matriz_id = int(root.get("matriz_customer_id") or 0)
        gid = get_or_create_customer_group(conn, group_name, matriz_customer_id=matriz_id if matriz_id else None)
        if gid:
            for cid in members:
                vincular_cliente_ao_grupo(conn, cid, gid)
            if matriz_id:
                conn.execute("UPDATE customer_groups SET matriz_customer_id=? WHERE id=?", (matriz_id, gid))

    conn.commit()


def get_customer_identity_row(conn, customer_id: Optional[int]):


    if not customer_id:
        return {
            "identity_key": "SEM_IDENTIDADE",
            "identity_nome": "",
            "tipo": "CLIENTE",
            "customer_id": 0,
            "matriz_customer_id": 0,

            # compatibilidade leve para telas antigas
            "grupo_id": 0,
            "rede_key": "",
            "rede_nome": "",
        }

    # 1) regra já resolvida/salva
    rule = conn.execute("""
        SELECT
            customer_id,
            identity_key,
            identity_nome,
            tipo_vinculo,
            COALESCE(matriz_customer_id, 0) AS matriz_customer_id
        FROM customer_identity_rules
        WHERE customer_id = ?
        LIMIT 1
    """, (customer_id,)).fetchone()

    if rule:
        identity_key = (rule["identity_key"] or "").strip() or f"CLIENTE:{int(customer_id)}"
        identity_nome = (rule["identity_nome"] or "").strip() or f"Cliente {int(customer_id)}"
        tipo = (rule["tipo_vinculo"] or "CLIENTE").strip().upper()
        matriz_customer_id = int(rule["matriz_customer_id"] or 0)

        return {
            "identity_key": identity_key,
            "identity_nome": identity_nome,
            "tipo": tipo,
            "customer_id": int(rule["customer_id"] or 0),
            "matriz_customer_id": matriz_customer_id,

            # compatibilidade leve
            "grupo_id": 0,
            "rede_key": identity_key if tipo in ("MATRIZ", "FILIAL", "REDE") else "",
            "rede_nome": identity_nome,
        }

    # 2) se não tiver regra, mas já teve JIT histórico, trata como MATRIZ
    row_jit = conn.execute("""
        SELECT
            c.id,
            COALESCE(c.fantasia, c.nome, '') AS nome_base
        FROM customers c
        WHERE c.id = ?
          AND EXISTS (
              SELECT 1
              FROM orders o
              WHERE o.cliente_id = c.id
                AND UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega, '')),' ','')) LIKE '98%JIT%'
          )
        LIMIT 1
    """, (customer_id,)).fetchone()

    if row_jit:
        identity_key = f"MATRIZ:{int(row_jit['id'])}"
        identity_nome = (row_jit["nome_base"] or "").strip() or f"Cliente {int(row_jit['id'])}"

        return {
            "identity_key": identity_key,
            "identity_nome": identity_nome,
            "tipo": "MATRIZ",
            "customer_id": int(row_jit["id"]),
            "matriz_customer_id": int(row_jit["id"]),

            # compatibilidade leve
            "grupo_id": 0,
            "rede_key": identity_key,
            "rede_nome": identity_nome,
        }

    # 3) fallback simples
    row = conn.execute("""
        SELECT
            c.id,
            COALESCE(c.fantasia, c.nome, '') AS nome_base
        FROM customers c
        WHERE c.id = ?
        LIMIT 1
    """, (customer_id,)).fetchone()

    if not row:
        return {
            "identity_key": f"CLIENTE:{int(customer_id)}",
            "identity_nome": f"Cliente {int(customer_id)}",
            "tipo": "CLIENTE",
            "customer_id": int(customer_id),
            "matriz_customer_id": 0,

            "grupo_id": 0,
            "rede_key": "",
            "rede_nome": "",
        }

    nome_base = (row["nome_base"] or "").strip() or f"Cliente {int(row['id'])}"

    return {
        "identity_key": f"CLIENTE:{int(row['id'])}",
        "identity_nome": nome_base,
        "tipo": "CLIENTE",
        "customer_id": int(row["id"]),
        "matriz_customer_id": 0,

        "grupo_id": 0,
        "rede_key": "",
        "rede_nome": nome_base,
    }

def upsert_identity_rule_as_matriz(conn, customer_id: int, identity_nome: str, origem: str = "AUTO_JIT"):
    customer_id = int(customer_id or 0)
    identity_nome = (identity_nome or "").strip() or f"Cliente {customer_id}"
    identity_key = f"MATRIZ:{customer_id}"

    conn.execute("""
        INSERT INTO customer_identity_rules (
            customer_id, identity_key, identity_nome, tipo_vinculo,
            matriz_customer_id, origem, updated_at
        )
        VALUES (?, ?, ?, 'MATRIZ', ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(customer_id) DO UPDATE SET
            identity_key = excluded.identity_key,
            identity_nome = excluded.identity_nome,
            tipo_vinculo = excluded.tipo_vinculo,
            matriz_customer_id = excluded.matriz_customer_id,
            origem = excluded.origem,
            updated_at = CURRENT_TIMESTAMP
    """, (
        customer_id,
        identity_key,
        identity_nome,
        customer_id,
        origem,
    ))

def create_import_identity_pending(
    conn,
    customer_id: int,
    customer_nome: str,
    customer_codigo: str = "",
    condicao_entrega: str = "",
    candidato_customer_id: int = 0,
    candidato_nome: str = "",
    import_id: Optional[int] = None
):
    customer_id = int(customer_id or 0)
    candidato_customer_id = int(candidato_customer_id or 0)
    customer_nome = (customer_nome or "").strip()
    customer_codigo = (customer_codigo or "").strip()
    condicao_entrega = (condicao_entrega or "").strip()
    candidato_nome = (candidato_nome or "").strip()

    if customer_id <= 0 or not customer_nome or candidato_customer_id <= 0:
        return

    ja_existe = conn.execute("""
        SELECT id
        FROM import_identity_pending
        WHERE customer_id = ?
          AND candidato_customer_id = ?
          AND status = 'PENDENTE'
        LIMIT 1
    """, (customer_id, candidato_customer_id)).fetchone()

    if ja_existe:
        return

    conn.execute("""
        INSERT INTO import_identity_pending (
            import_id,
            customer_id,
            customer_nome,
            customer_codigo,
            condicao_entrega,
            candidato_customer_id,
            candidato_nome,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDENTE')
    """, (
        import_id,
        customer_id,
        customer_nome,
        customer_codigo,
        condicao_entrega,
        candidato_customer_id,
        candidato_nome,
    ))

def build_identity_key(grupo_id: int = 0, rede_key: str = "", customer_id: int = 0) -> str:
    if int(grupo_id or 0) > 0:
        return f"GRUPO:{int(grupo_id)}"
    if (rede_key or "").strip():
        return f"REDE:{(rede_key or '').strip()}"
    if int(customer_id or 0) > 0:
        return f"CLIENTE:{int(customer_id)}"
    return "SEM_IDENTIDADE"


def infer_identity_matrix_candidate(conn, identity_key: str, fallback_nome: str = ""):
    identity_key = (identity_key or "").strip()
    if not identity_key or identity_key.startswith("CLIENTE:"):
        return 0, ""

    rows = conn.execute("""
        SELECT
            cir.customer_id,
            COALESCE(c.fantasia, c.nome, '') AS nome_base,
            COUNT(DISTINCT i.id) AS invoice_count,
            COUNT(DISTINCT o.id) AS order_count,
            MAX(CASE WHEN UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega, '')),' ','')) LIKE '98%JIT%' THEN 1 ELSE 0 END) AS has_jit
        FROM customer_identity_rules cir
        JOIN customers c ON c.id = cir.customer_id
        LEFT JOIN invoices i ON i.cliente_id = c.id
        LEFT JOIN orders o ON o.cliente_id = c.id
        WHERE cir.identity_key = ?
        GROUP BY cir.customer_id, COALESCE(c.fantasia, c.nome, '')
    """, (identity_key,)).fetchall()

    if not rows:
        return 0, ""

    def _clean(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.upper()
        s = re.sub(r"[^A-Z0-9 ]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _canon_name(s: str) -> str:
        toks = []
        raw = [t for t in _clean(s).split(" ") if t]
        drop = {"MASTER", "MATRIZ", "FILIAL", "FRANCHISING", "FRANQUIA", "FRANQUIAS"}
        legal = {"LTDA", "EIRELI", "ME", "MEI", "EPP", "S", "A", "SA", "FILIAL", "UNIDADE", "LOJA", "LJ", "SHOPPING", "STORE"}
        i = 0
        while i < len(raw):
            tok = raw[i]
            if tok in {"LOJA", "LJ"} and i + 1 < len(raw) and (raw[i + 1] or "").isdigit():
                i += 2
                continue
            if tok.startswith("LJ") and len(tok) > 2 and tok[2:].isdigit():
                i += 1
                continue
            if tok in legal or tok in drop:
                i += 1
                continue
            toks.append(tok)
            i += 1
        return " ".join(toks).strip()

    net = _canon_name(fallback_nome or rows[0]["nome_base"] or "")
    ranked = []
    for r in rows:
        nome = (r["nome_base"] or "").strip()
        clean = _clean(nome)
        tok_score = 1 if any(t in clean.split() for t in {"MASTER", "MATRIZ", "FRANCHISING", "FRANQUIA", "FRANQUIAS", "SEDE"}) else 0
        no_invoice = 1 if int(r["invoice_count"] or 0) == 0 and int(r["order_count"] or 0) > 0 else 0
        exact = 1 if net and _canon_name(nome) == net else 0
        has_jit = int(r["has_jit"] or 0)
        ranked.append((has_jit, tok_score, no_invoice, exact, -len(nome), int(r["customer_id"]), nome))

    ranked.sort(reverse=True)
    top = ranked[0]
    if len(ranked) > 1 and top[:4] == ranked[1][:4]:
        return 0, ""
    return int(top[5]), (top[6] or "").strip()


def get_customer_identity_key(conn, customer_id: Optional[int]) -> str:
    info = get_customer_identity_row(conn, customer_id)
    return (info.get("identity_key") or "SEM_IDENTIDADE").strip() or "SEM_IDENTIDADE"


def get_identity_display_name(conn, identity_key: str, fallback_customer_id: Optional[int] = None) -> str:
    """
    Nome amigável da identidade comercial.
    Prioridade:
    GRUPO > REDE > CLIENTE
    """
    if not identity_key or identity_key == "SEM_IDENTIDADE":
        if fallback_customer_id:
            row = conn.execute("""
                SELECT id, codigo, nome, fantasia
                FROM customers
                WHERE id = ?
                LIMIT 1
            """, (fallback_customer_id,)).fetchone()
            if row:
                return safe_customer_display_name(row["nome"], row["fantasia"], row["codigo"], row["id"])
        return "Cliente não identificado"

    if identity_key.startswith("GRUPO:"):
        grupo_id = int(identity_key.split(":", 1)[1])
        row = conn.execute("""
            SELECT
                g.nome,
                COALESCE(cm.fantasia, cm.nome, '') AS matriz_nome
            FROM customer_groups g
            LEFT JOIN customers cm ON cm.id = g.matriz_customer_id
            WHERE g.id = ?
            LIMIT 1
        """, (grupo_id,)).fetchone()

        if row:
            return (row["nome"] or row["matriz_nome"] or "Grupo").strip()

    elif identity_key.startswith("REDE:"):
        rede_key = identity_key.split(":", 1)[1].strip()

        row = conn.execute("""
            SELECT
                COALESCE(NULLIF(rede_nome, ''), NULLIF(fantasia, ''), nome, ?) AS nome
            FROM customers
            WHERE COALESCE(rede_key, '') = ?
            ORDER BY
                CASE WHEN COALESCE(rede_nome, '') <> '' THEN 0 ELSE 1 END,
                nome
            LIMIT 1
        """, (rede_key, rede_key)).fetchone()

        if row:
            return (row["nome"] or rede_key or "Rede").strip()

        return rede_key or "Rede"

    elif identity_key.startswith("CLIENTE:"):
        customer_id = int(identity_key.split(":", 1)[1])
        row = conn.execute("""
            SELECT id, codigo, nome, fantasia
            FROM customers
            WHERE id = ?
            LIMIT 1
        """, (customer_id,)).fetchone()

        if row:
            return safe_customer_display_name(row["nome"], row["fantasia"], row["codigo"], row["id"])

    return "Cliente não identificado"


def normalize_task_status(status: str) -> str:
    status = (status or "").strip().upper()
    mapa = {
        "ABERTA": "ABERTA",
        "EM CONTATO": "EM CONTATO",
        "AGUARDANDO RESPOSTA": "AGUARDANDO RESPOSTA",
        "NEGOCIANDO": "NEGOCIANDO",
        "CONCLUIDA": "CONCLUÍDA",
        "CONCLUÍDA": "CONCLUÍDA",
        "CANCELADA": "CANCELADA",
        "PENDENTE": "ABERTA",
        "AGUARDANDO": "AGUARDANDO RESPOSTA",
    }
    return mapa.get(status, "ABERTA")


def is_open_task_status(status: str) -> bool:
    return normalize_task_status(status) in {
        "ABERTA",
        "EM CONTATO",
        "AGUARDANDO RESPOSTA",
        "NEGOCIANDO",
    }


def create_task_unificada(
    conn,
    vendor_id: int,
    titulo: str,
    descricao: str = "",
    cliente_id: Optional[int] = None,
    identity_key: str = "",
    criada_por: str = "Sistema",
    status: str = "ABERTA",
):
    """
    Cria tarefa usando o schema REAL da sua tabela tasks:
    titulo / descricao / criada_por / identity_key
    """
    status = normalize_task_status(status)
    titulo = (titulo or "").strip()
    descricao = (descricao or "").strip()
    criada_por = (criada_por or "Sistema").strip()

    if not titulo:
        return None

    conn.execute("""
        INSERT INTO tasks (
            vendor_id,
            cliente_id,
            titulo,
            descricao,
            status,
            criada_por,
            created_at,
            updated_at,
            identity_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        int(vendor_id),
        int(cliente_id) if cliente_id else None,
        titulo,
        descricao,
        status,
        criada_por,
        now_iso(),
        now_iso(),
        (identity_key or "").strip() or None
    ))

    task_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute("""
        INSERT INTO task_history (
            task_id,
            status_anterior,
            status_novo,
            observacao,
            alterado_por,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        task_id,
        "",
        status,
        "Tarefa criada.",
        criada_por,
        now_iso()
    ))

    return int(task_id)


def expand_task_ids_by_identity(conn, task_ids: List[str]) -> List[int]:
    """
    Se uma tarefa pertence a uma identidade comercial, expande para as demais
    tarefas abertas da MESMA identidade.
    Isso evita repartir rede/matriz entre vendedores.
    """
    ids = []
    for tid in task_ids:
        try:
            ids.append(int(tid))
        except:
            pass

    if not ids:
        return []

    placeholders = ",".join("?" for _ in ids)
    base_rows = conn.execute(f"""
        SELECT id, COALESCE(identity_key, '') AS identity_key
        FROM tasks
        WHERE id IN ({placeholders})
    """, ids).fetchall()

    final_ids = set(ids)

    for row in base_rows:
        identity_key = (row["identity_key"] or "").strip()
        if not identity_key:
            continue

        same_identity = conn.execute("""
            SELECT id
            FROM tasks
            WHERE COALESCE(identity_key, '') = ?
              AND status IN ('ABERTA', 'EM CONTATO', 'AGUARDANDO RESPOSTA', 'NEGOCIANDO')
        """, (identity_key,)).fetchall()

        for r in same_identity:
            final_ids.add(int(r["id"]))

    return sorted(final_ids)

def get_customer_ids_by_identity(conn, identity_key: str) -> List[int]:
    """
    Retorna todos os customer_id que pertencem à mesma identidade comercial
    """
    if not identity_key:
        return []

    # CLIENTE
    if identity_key.startswith("CLIENTE:"):
        try:
            return [int(identity_key.split(":")[1])]
        except:
            return []

    # GRUPO
    if identity_key.startswith("GRUPO:"):
        grupo_id = int(identity_key.split(":")[1])

        rows = conn.execute("""
            SELECT customer_id
            FROM customer_group_members
            WHERE group_id = ?
        """, (grupo_id,)).fetchall()

        return [int(r["customer_id"]) for r in rows]

    # REDE
    if identity_key.startswith("REDE:"):
        rede_key = identity_key.split(":", 1)[1]

        rows = conn.execute("""
            SELECT id
            FROM customers
            WHERE COALESCE(rede_key, '') = ?
        """, (rede_key,)).fetchall()

        return [int(r["id"]) for r in rows]

    return []



def normalize_open_tasks_to_last_vendor(conn, actor_name: str = "Sistema") -> int:
    moved = 0
    rows = conn.execute("""
        SELECT id, vendor_id, COALESCE(identity_key, '') AS identity_key, status
        FROM tasks
        WHERE status IN ('ABERTA', 'EM CONTATO', 'AGUARDANDO RESPOSTA', 'NEGOCIANDO')
          AND COALESCE(identity_key, '') <> ''
    """).fetchall()

    vendor_map = build_last_vendor_map(conn)
    ts = now_iso()

    for r in rows:
        identity_key = (r['identity_key'] or '').strip()
        if not identity_key:
            continue
        correct_vendor_id = int(vendor_map.get(identity_key) or 0)
        current_vendor_id = int(r['vendor_id'] or 0)
        if correct_vendor_id <= 0 or current_vendor_id == correct_vendor_id:
            continue

        conn.execute("UPDATE tasks SET vendor_id=?, updated_at=? WHERE id=?", (correct_vendor_id, ts, int(r['id'])))
        conn.execute("""
            INSERT INTO task_history (
                task_id, status_anterior, status_novo, observacao, alterado_por, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(r['id']),
            normalize_task_status(r['status'] or 'ABERTA'),
            normalize_task_status(r['status'] or 'ABERTA'),
            'Tarefa realocada automaticamente para o último responsável da carteira.',
            actor_name,
            ts,
        ))
        moved += 1

    return moved

def transfer_tasks_by_identity(conn, selected_task_ids: List[str], target_vendor_id: int, actor_name: str) -> Tuple[int, str]:
    """
    Transfere as tarefas selecionadas e todas as tarefas abertas da mesma identidade.
    """
    expanded_ids = expand_task_ids_by_identity(conn, selected_task_ids)
    if not expanded_ids:
        return 0, ""

    row_new = conn.execute("SELECT nome FROM vendors WHERE id = ?", (target_vendor_id,)).fetchone()
    new_vendor_name = (row_new["nome"] if row_new else "Novo vendedor")

    placeholders = ",".join("?" for _ in expanded_ids)
    rows = conn.execute(f"""
        SELECT
            t.id,
            t.status,
            t.vendor_id,
            COALESCE(t.identity_key, '') AS identity_key,
            v.nome AS old_vendor_name
        FROM tasks t
        LEFT JOIN vendors v ON v.id = t.vendor_id
        WHERE t.id IN ({placeholders})
    """, expanded_ids).fetchall()

    moved = 0

    for r in rows:
        old_vendor_id = int(r["vendor_id"] or 0)
        if old_vendor_id == int(target_vendor_id):
            continue

        conn.execute("""
            UPDATE tasks
            SET vendor_id = ?, updated_at = ?
            WHERE id = ?
        """, (int(target_vendor_id), now_iso(), int(r["id"])))

        identidade = (r["identity_key"] or "").strip()
        obs = (
            f"Tarefa transferida de {r['old_vendor_name'] or 'Sem dono'} para {new_vendor_name}."
            + (f" Identidade comercial: {identidade}." if identidade else "")
        )

        conn.execute("""
            INSERT INTO task_history (
                task_id,
                status_anterior,
                status_novo,
                observacao,
                alterado_por,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            int(r["id"]),
            r["status"] or "",
            r["status"] or "",
            obs,
            actor_name or "Sistema",
            now_iso()
        ))

        moved += 1

    return moved, new_vendor_name

def list_customer_identities_for_user(conn, user, q: str = "", limit: int = 300):
    """
    Lista carteira comercial consolidada pelo modelo novo:
    MATRIZ / FILIAL / CLIENTE
    """
    role = ((user["role"] if user and "role" in user else "") or "").strip().lower()
    vendor_lock = get_user_vendor_id(conn, user)

    params = []
    vendor_sql = ""

    if role not in ("admin", "gestor") and vendor_lock:
        vendor_sql = """
            AND EXISTS (
                SELECT 1
                FROM invoices i
                WHERE i.cliente_id = c.id
                  AND i.vendor_id = ?
            )
        """
        params.append(vendor_lock)

    rows = conn.execute(f"""
        SELECT
            c.id,
            COALESCE(c.codigo, '') AS codigo,
            COALESCE(c.nome, '') AS nome,
            COALESCE(c.fantasia, '') AS fantasia
        FROM customers c
        WHERE 1=1
        {vendor_sql}
        ORDER BY COALESCE(c.fantasia, c.nome, '') ASC
    """, params).fetchall()

    entidades = {}
    prioridade_tipo = {"MATRIZ": 0, "REDE": 1, "FILIAL": 2, "CLIENTE": 3}

    for r in rows:
        customer_id = int(r["id"])
        info = get_customer_identity_row(conn, customer_id)

        identity_key = info.get("identity_key") or f"CLIENTE:{customer_id}"
        identity_nome = (info.get("identity_nome") or COALESCE_STR(r["fantasia"], r["nome"], "Cliente")).strip()
        identity_tipo = (info.get("tipo") or "CLIENTE").strip().upper()
        matriz_id = int(info.get("matriz_customer_id") or 0)

        matriz_nome = ""
        if matriz_id:
            mr = conn.execute("""
                SELECT COALESCE(fantasia, nome, '') AS nome_base
                FROM customers
                WHERE id = ?
                LIMIT 1
            """, (matriz_id,)).fetchone()
            if mr:
                matriz_nome = (mr["nome_base"] or "").strip()
        elif identity_tipo in ("REDE", "FILIAL"):
            matriz_id, matriz_nome = infer_identity_matrix_candidate(conn, identity_key, identity_nome)

        if identity_key not in entidades:
            entidades[identity_key] = {
                "identity_key": identity_key,
                "identity_nome": identity_nome,
                "identity_tipo": identity_tipo,
                "cliente_id": matriz_id or customer_id,
                "codigo": r["codigo"] or "",
                "matriz_nome": matriz_nome,
                "total_clientes": 0,
                "filiais_qtd": 0,
            }
        else:
            atual = entidades[identity_key]
            if prioridade_tipo.get(identity_tipo, 9) < prioridade_tipo.get(atual["identity_tipo"], 9):
                atual["identity_tipo"] = identity_tipo
                atual["cliente_id"] = matriz_id or customer_id
                atual["matriz_nome"] = matriz_nome
                if identity_tipo == "MATRIZ" and matriz_nome:
                    atual["identity_nome"] = matriz_nome

        entidades[identity_key]["total_clientes"] += 1

    items = list(entidades.values())

    for item in items:
        total = int(item.get("total_clientes") or 0)
        if (item.get("identity_tipo") or "").upper() == "REDE":
            item["filiais_qtd"] = total
        elif (item.get("identity_tipo") or "").upper() == "CLIENTE":
            item["filiais_qtd"] = 0
        else:
            item["filiais_qtd"] = max(total - 1, 0)

    if q:
        qq = q.strip().upper()
        items = [
            x for x in items
            if qq in (x["identity_nome"] or "").upper()
            or qq in (x["identity_tipo"] or "").upper()
            or qq in (x["codigo"] or "").upper()
            or qq in (x["matriz_nome"] or "").upper()
            or qq in (x["identity_key"] or "").upper()
        ]

    ordem_tipo = {"MATRIZ": 0, "REDE": 1, "FILIAL": 2, "CLIENTE": 3}
    items.sort(key=lambda x: (ordem_tipo.get(x["identity_tipo"], 9), (x["identity_nome"] or "").upper()))

    return items[:limit]


def COALESCE_STR(*vals):
    for v in vals:
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""

def enrich_top_entities_for_ui(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Padroniza os itens de top clientes/redes para exibição visual.
    Não recalcula nada; só organiza os campos para o template.
    """
    out = []

    for r in items or []:
        item = dict(r)

        identity_key = str(item.get("identity_key") or "").strip()
        nome = str(item.get("nome") or item.get("cliente") or "—").strip()

        tipo = "CLIENTE"
        if identity_key.startswith("GRUPO:"):
            tipo = "GRUPO"
        elif identity_key.startswith("REDE:"):
            tipo = "REDE"

        pedidos = item.get("pedidos")
        if isinstance(pedidos, set):
            pedidos_qtd = len(pedidos)
        else:
            try:
                pedidos_qtd = int(item.get("pedidos_qtd") or item.get("qtd_pedidos") or 0)
            except:
                pedidos_qtd = 0

        valor = float(item.get("valor") or 0)
        is_jit = bool(item.get("is_jit") or False)

        item["identity_tipo"] = tipo
        item["nome_exibicao"] = nome
        item["pedidos_qtd"] = pedidos_qtd
        item["valor"] = valor
        item["is_jit"] = is_jit
        item["badge"] = "JIT" if is_jit else "CARTEIRA"
        item["badge_class"] = "text-bg-warning" if is_jit else "text-bg-light border text-dark"

        out.append(item)

    return out


def get_first_invoice_for_identity(conn, vendor_id: Optional[int], identity_key: str):
    if not identity_key or identity_key == "SEM_IDENTIDADE":
        return None

    if vendor_id is None:
        return conn.execute("""
            SELECT
                i.id,
                i.numero,
                i.pedido,
                i.data_emissao,
                i.vendor_id
            FROM invoices i
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
            WHERE COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) = ?
              AND i.data_emissao IS NOT NULL
            ORDER BY date(i.data_emissao) ASC, i.id ASC
            LIMIT 1
        """, (identity_key,)).fetchone()

    return conn.execute("""
        SELECT
            i.id,
            i.numero,
            i.pedido,
            i.data_emissao,
            i.vendor_id
        FROM invoices i
        LEFT JOIN customer_identity_rules cir ON cir.customer_id = i.cliente_id
        WHERE i.vendor_id = ?
          AND COALESCE(cir.identity_key, 'CLIENTE:' || i.cliente_id) = ?
          AND i.data_emissao IS NOT NULL
        ORDER BY date(i.data_emissao) ASC, i.id ASC
        LIMIT 1
    """, (vendor_id, identity_key)).fetchone()


def get_manual_customer_status(conn, identity_key: str) -> Optional[str]:
    if not identity_key:
        return None
    row = conn.execute("SELECT status_manual FROM customer_status_overrides WHERE identity_key=?", (identity_key,)).fetchone()
    if not row:
        return None
    status = (row["status_manual"] or "").strip().upper()
    return status or None


def apply_manual_customer_status(conn, identity_key: str, identity_nome: str, is_novo: bool) -> bool:
    manual = get_manual_customer_status(conn, identity_key)
    if manual == "NOVO":
        return True
    if manual == "RECOMPRA":
        return False
    return is_novo


def set_manual_customer_status(conn, identity_key: str, identity_nome: str, status_novo: str, user):
    status_novo = (status_novo or "").strip().upper()
    if status_novo not in ["NOVO", "RECOMPRA"]:
        raise ValueError("Status inválido.")

    role = (user or {}).get("role") if isinstance(user, dict) else None
    if status_novo == "NOVO" and role not in ["admin", "gestor"]:
        raise PermissionError("Apenas gestor/admin podem marcar como NOVO.")

    atual = get_manual_customer_status(conn, identity_key)
    if atual == status_novo:
        return

    if atual:
        conn.execute("UPDATE customer_status_overrides SET identity_nome=?, status_manual=?, updated_by=?, updated_at=? WHERE identity_key=?",
                     (identity_nome, status_novo, (user or {}).get("nome", "Sistema"), now_iso(), identity_key))
    else:
        conn.execute("INSERT INTO customer_status_overrides(identity_key, identity_nome, status_manual, updated_by, updated_at) VALUES (?,?,?,?,?)",
                     (identity_key, identity_nome, status_novo, (user or {}).get("nome", "Sistema"), now_iso()))

    conn.execute("INSERT INTO customer_status_history(identity_key, identity_nome, status_anterior, status_novo, changed_by, changed_role, created_at) VALUES (?,?,?,?,?,?,?)",
                 (identity_key, identity_nome, atual, status_novo, (user or {}).get("nome", "Sistema"), role, now_iso()))


def summarize_team_entities_in_period(conn, d0: date, d1: date, team_tipo: Optional[str] = None):
    """
    Perfil NOVO x REPETIÇÃO pela MESMA base oficial do Dashboard.

    Correção 15/05/2026:
    O gráfico estava mostrando 64 porque contava CLIENTES/identidades únicas,
    enquanto o Status Geral e os cards trabalham com PEDIDOS únicos.
    Agora o donut também conta PEDIDOS únicos válidos, mantendo o total coerente
    com Status Interno + Status Externo.
    """
    sql_tipo = ""
    params = [d0.isoformat(), d1.isoformat()]

    if team_tipo:
        sql_tipo = " AND COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') = ? "
        params.append(team_tipo.upper())

    rows = conn.execute(f"""
        SELECT
            o.id,
            TRIM(COALESCE(o.pedido,'')) AS pedido,
            o.data_emissao,
            o.cliente_id,
            COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0) AS valor,
            COALESCE(o.motivo_pedido,'') AS motivo_pedido,
            COALESCE(o.desc_motivo_pedido,'') AS desc_motivo_pedido,
            COALESCE(o.condicao_entrega,'') AS condicao_entrega,
            COALESCE(c.fantasia, c.nome, o.cliente_nome_ref, '—') AS cliente
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE o.data_emissao IS NOT NULL
          AND date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND TRIM(COALESCE(o.pedido,'')) <> ''
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o','v')}
          {sql_tipo}
        ORDER BY date(o.data_emissao) ASC, o.id ASC
    """, params).fetchall()

    entidades = {}
    pedidos_unicos = {}

    for r in rows:
        pedido_ref = (r["pedido"] or "").strip()
        if not pedido_ref:
            continue

        customer_id = r["cliente_id"]
        info = get_customer_identity_row(conn, customer_id)
        identity_key = info.get("identity_key") or f"CLIENTE:{customer_id}"
        identity_nome = info.get("identity_nome") or (r["cliente"] or "—")
        is_jit_linha = f_comissao.condicao_jit_imediato(r["condicao_entrega"])
        sisplan_novo_linha = f_comissao.pedido_sisplan_cliente_novo(
            r["motivo_pedido"], r["desc_motivo_pedido"]
        )

        if identity_key not in entidades:
            entidades[identity_key] = {
                "identity_key": identity_key,
                "cliente_id": customer_id,
                "nome": identity_nome,
                "identity_nome": identity_nome,
                "identity_tipo": info.get("tipo") or "CLIENTE",
                "matriz_customer_id": int(info.get("matriz_customer_id") or 0),
                "is_jit": is_jit_linha,
                "pedidos": set(),
                "valor": 0.0,
            }

        entidades[identity_key]["pedidos"].add(pedido_ref)
        entidades[identity_key]["valor"] += float(r["valor"] or 0)
        if not is_jit_linha:
            entidades[identity_key]["is_jit"] = False

        # Guarda 1 registro por PEDIDO, não por linha e não por cliente.
        if pedido_ref not in pedidos_unicos:
            pedidos_unicos[pedido_ref] = {
                "pedido": pedido_ref,
                "identity_key": identity_key,
                "identity_nome": identity_nome,
                "is_jit": is_jit_linha,
                "sisplan_novo": sisplan_novo_linha,
            }
        else:
            if not is_jit_linha:
                pedidos_unicos[pedido_ref]["is_jit"] = False
            if sisplan_novo_linha:
                pedidos_unicos[pedido_ref]["sisplan_novo"] = True

    novo_count = 0
    rep_count = 0

    for ident, item in entidades.items():
        item["is_novo"] = False

    for pedido_ref, ped in pedidos_unicos.items():
        ident = ped.get("identity_key")
        status_manual = f_comissao.status_manual_de_texto(
            get_manual_customer_status(conn, ident)
        )
        is_novo = f_comissao.classificar_cliente_novo(
            pedido_sisplan_novo=bool(ped.get("sisplan_novo")),
            is_jit_imediato=bool(ped.get("is_jit")),
            status_manual=status_manual,
        )
        if is_novo:
            novo_count += 1
            if ident in entidades:
                entidades[ident]["is_novo"] = True
        else:
            rep_count += 1

    return {
        "novo_count": novo_count,
        "rep_count": rep_count,
        "entities": list(entidades.values()),
        "total_pedidos": len(pedidos_unicos),
    }

def summarize_team_status_by_orders(conn, d0_ped: date, d1_ped: date, d0_fat: date, d1_fat: date, team_tipo: Optional[str] = None):
    """
    Status por pedido:
    - total_pedidos: pedidos únicos do mês comercial
    - faturados: pedidos únicos que têm faturamento no ciclo
    - pendentes: pedidos do mês ainda sem faturamento no ciclo

    team_tipo:
      None       -> time todo
      "INTERNO"  -> só internos
      "EXTERNO"  -> só externos
    """
    sql_tipo_orders = ""
    sql_tipo_inv = ""
    params_orders = [d0_ped.isoformat(), d1_ped.isoformat()]
    params_inv = [d0_fat.isoformat(), d1_fat.isoformat()]

    if team_tipo:
        sql_tipo_orders = " AND COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') = ? "
        sql_tipo_inv = " AND COALESCE(UPPER(TRIM(v.tipo)), 'EXTERNO') = ? "
        params_orders.append(team_tipo.upper())
        params_inv.append(team_tipo.upper())

    pedidos_rows = conn.execute(f"""
        SELECT DISTINCT TRIM(COALESCE(o.pedido, '')) AS pedido
        FROM orders o
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE o.data_emissao IS NOT NULL
          AND date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND TRIM(COALESCE(o.pedido, '')) <> ''
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          AND {sql_pedidos_realizados_excel_cond('o', 'v')}
          {sql_tipo_orders}
    """, params_orders).fetchall()

    faturados_rows = conn.execute(f"""
        SELECT DISTINCT TRIM(COALESCE(i.pedido, '')) AS pedido
        FROM invoices i
        LEFT JOIN vendors v ON v.id = i.vendor_id
        WHERE i.data_emissao IS NOT NULL
          AND date(i.data_emissao) BETWEEN date(?) AND date(?)
          AND TRIM(COALESCE(i.pedido, '')) <> ''
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
          {sql_tipo_inv}
    """, params_inv).fetchall()

    pedidos_set = { (r["pedido"] or "").strip() for r in pedidos_rows if (r["pedido"] or "").strip() }
    faturados_set = { (r["pedido"] or "").strip() for r in faturados_rows if (r["pedido"] or "").strip() }

    faturados_no_mes = pedidos_set.intersection(faturados_set)

    total_pedidos = len(pedidos_set)
    qtd_faturados = len(faturados_no_mes)
    qtd_pendentes = max(0, total_pedidos - qtd_faturados)

    return {
        "total_pedidos": total_pedidos,
        "faturados": qtd_faturados,
        "pendentes": qtd_pendentes
    }



def summarize_vendor_entities_in_period(conn, vendor_id: int, d0: date, d1: date):
    """
    Unifica a leitura comercial em cima da mesma identidade usada na comissão:
    GRUPO > REDE > CLIENTE.
    Pedido novo = MOTIVO 10 no Sisplan (PEDIDO PRIMEIRA COMPRA), salvo
    override manual. JIT IMEDIATO anula o bônus/classificação de novo.
    """
    rows = conn.execute(f"""
        SELECT
            o.id,
            o.pedido,
            o.data_emissao,
            o.cliente_id,
            COALESCE(o.motivo_pedido,'') AS motivo_pedido,
            COALESCE(o.desc_motivo_pedido,'') AS desc_motivo_pedido,
            COALESCE(o.condicao_entrega,'') AS condicao_entrega,
            COALESCE(c.fantasia, c.nome, '—') AS cliente
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE o.vendor_id = ?
          AND o.data_emissao IS NOT NULL
          AND date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        ORDER BY date(o.data_emissao) ASC, o.id ASC
    """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall()

    entities = {}
    for r in rows:
        customer_id = r["cliente_id"]
        identity_key = get_customer_identity_key(conn, customer_id)
        is_jit_imediato = f_comissao.condicao_jit_imediato(r["condicao_entrega"])
        sisplan_novo = f_comissao.pedido_sisplan_cliente_novo(
            r["motivo_pedido"], r["desc_motivo_pedido"]
        )

        if identity_key not in entities:
            entities[identity_key] = {
                "identity_key": identity_key,
                "cliente_id": customer_id,
                "cliente": (get_customer_identity_row(conn, customer_id).get("identity_nome") or r["cliente"] or "-"),
                "is_jit": is_jit_imediato,
                "sisplan_novo": bool(sisplan_novo and not is_jit_imediato),
                "pedido_ref": r["pedido"] or "",
                "data_ref": r["data_emissao"] or "",
            }
        else:
            if not is_jit_imediato:
                entities[identity_key]["is_jit"] = False
            if sisplan_novo and not is_jit_imediato:
                entities[identity_key]["sisplan_novo"] = True

    novo_count = 0
    rep_count = 0
    recuperado_count = 0

    for ident, item in entities.items():
        primeiro = get_first_invoice_for_identity(conn, None, ident)
        is_novo = f_comissao.classificar_cliente_novo(
            pedido_sisplan_novo=bool(item.get("sisplan_novo")),
            is_jit_imediato=bool(item.get("is_jit")),
            status_manual=f_comissao.status_manual_de_texto(
                get_manual_customer_status(conn, ident)
            ),
        )

        # RECUPERADO é uma subdivisão da recorrência, usando a MESMA base histórica
        # que o CRM já usa para atraso/recompra: voltou a comprar depois de ultrapassar
        # o próprio intervalo mediano de recompra. Não cria uma segunda fonte.
        is_recuperado = False
        if not is_novo and not item.get("is_jit"):
            try:
                data_mov = datetime.strptime((item.get("data_ref") or "").strip(), "%Y-%m-%d").date()
                hist_dates = []
                for inv in get_identity_invoices(conn, ident):
                    ds = (inv["data_emissao"] or "").strip()
                    if not ds:
                        continue
                    try:
                        di = datetime.strptime(ds, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if di < data_mov:
                        hist_dates.append(di)
                hist_dates = sorted(set(hist_dates))
                if len(hist_dates) >= 2:
                    intervalos = [(hist_dates[i] - hist_dates[i-1]).days for i in range(1, len(hist_dates))]
                    intervalo_mediano = float(statistics.median(intervalos)) if intervalos else 0.0
                    gap_retorno = (data_mov - hist_dates[-1]).days
                    if intervalo_mediano > 0 and gap_retorno > intervalo_mediano:
                        is_recuperado = True
            except Exception:
                is_recuperado = False

        if is_novo:
            novo_count += 1
        else:
            rep_count += 1
            if is_recuperado:
                recuperado_count += 1

        item["is_novo"] = is_novo
        item["is_recuperado"] = is_recuperado
        item["primeiro_faturamento"] = (primeiro["data_emissao"] if primeiro else "")

    return {
        "novo_count": novo_count,
        "rep_count": rep_count,
        "recuperado_count": recuperado_count,
        "rep_regular_count": max(0, rep_count - recuperado_count),
        "entities": list(entities.values())
    }

def summarize_vendor_sales_identities_in_period(conn, vendor_id: int, d0: date, d1: date):
    """
    Consolida os pedidos do período por identidade comercial
    no modelo novo: MATRIZ / FILIAL / CLIENTE.
    """
    rows = conn.execute(f"""
        SELECT
            o.id,
            o.pedido,
            o.data_emissao,
            o.cliente_id,
            COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0) AS valor,
            CASE WHEN UPPER(REPLACE(TRIM(COALESCE(o.condicao_entrega,'')),' ','')) LIKE '98%JIT%' THEN 1 ELSE 0 END AS is_jit,
            COALESCE(c.fantasia, c.nome, '—') AS cliente
        FROM orders o
        LEFT JOIN customers c ON c.id = o.cliente_id
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE o.vendor_id = ?
          AND o.data_emissao IS NOT NULL
          AND date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        ORDER BY date(o.data_emissao) ASC, o.id ASC
    """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall()

    acc = {}

    for r in rows:
        customer_id = r["cliente_id"]
        info = get_customer_identity_row(conn, customer_id)
        identity_key = info.get("identity_key") or f"CLIENTE:{customer_id}"
        identity_tipo = info.get("tipo") or "CLIENTE"
        nome_exibicao = info.get("identity_nome") or (r["cliente"] or "—")

        if identity_key not in acc:
            acc[identity_key] = {
                "identity_key": identity_key,
                "identity_tipo": identity_tipo,
                "nome": nome_exibicao,
                "cliente_id": customer_id,
                "matriz_customer_id": int(info.get("matriz_customer_id") or 0),
                "identity_nome": nome_exibicao,
                "valor": 0.0,
                "pedidos": set(),
                "is_jit_only": True,
            }

        acc[identity_key]["valor"] += float(r["valor"] or 0)

        pedido_ref = (r["pedido"] or "").strip()
        if pedido_ref:
            acc[identity_key]["pedidos"].add(pedido_ref)

        if int(r["is_jit"] or 0) == 0:
            acc[identity_key]["is_jit_only"] = False

    result = []
    for item in acc.values():
        result.append({
            "identity_key": item["identity_key"],
            "identity_tipo": item["identity_tipo"],
            "nome": item["nome"] or "—",
            "cliente_id": item["cliente_id"],
            "matriz_customer_id": item["matriz_customer_id"],
            "identity_nome": item["identity_nome"],
            "valor": float(item["valor"] or 0),
            "pedidos_qtd": len(item["pedidos"]),
            "is_jit_only": bool(item["is_jit_only"]),
        })

    result.sort(key=lambda x: x["valor"], reverse=True)
    return result


# -----------------------------
# Commission compute (robusto)
# -----------------------------
def _extrato_comissao_vazio(faturamento: float = 0.0) -> dict:
    return {
        "faturamento": float(faturamento or 0.0),
        "comissao": 0.0,
        "lines": [],
        "extra_bonus": 0.0,
        "novo_count": 0,
        "rep_count": 0,
        "bateu_meta": False,
        "devolucoes_valor": 0.0,
    }


def compute_vendor_commission(conn, vendor_id: int, d0: date, d1: date):
    v = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    if not v:
        return _extrato_comissao_vazio()

    tipo_vendor = (v["tipo"] or "Externo").strip().lower()

    if is_site_vendor_name(v["nome"] or v["codigo"] or ""):
        rows_site = conn.execute("""
            SELECT COALESCE(SUM(valor), 0) AS total
            FROM invoices
            WHERE vendor_id = ?
              AND data_emissao IS NOT NULL
              AND date(data_emissao) BETWEEN date(?) AND date(?)
        """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchone()
        return _extrato_comissao_vazio(float((rows_site["total"] if rows_site else 0) or 0))

    # =========================
    # EXTERNO = 7% sobre LIQUIDEZ
    # =========================
    if tipo_vendor == "externo":
        return linhas_comissao_externo_liquidez(conn, vendor_id, d0, d1)

    # =========================
    # INTERNO = comissão por FATURAMENTO no ciclo 26→25
    # Base: NF de saída + DEMO, sem frete e sem clichê.
    # Devoluções não abatem; aparecem no extrato.
    # Pedido novo = MOTIVO 10 no Sisplan; JIT IMEDIATO anula o bônus.
    # ANFARMA (05975) entra nesta carteira; a origem fica no vendor_nome_ref.
    # =========================
    from crm_npd.sync.etl import eh_responsavel_anfarma
    rows = conn.execute("""
        SELECT
            i.id,
            i.numero,
            i.pedido,
            i.data_emissao,
            i.cliente_id,
            i.vendor_id,
            COALESCE(i.tipo_item_ref, '') AS tipo_item_ref,
            COALESCE(c.fantasia, c.nome, '—') AS cliente,
            COALESCE(i.valor, 0) AS valor,
            COALESCE(i.vendor_nome_ref, '') AS vendor_nome_ref
        FROM invoices i
        LEFT JOIN customers c ON c.id = i.cliente_id
        WHERE i.vendor_id = ?
          AND i.data_emissao IS NOT NULL
          AND date(i.data_emissao) BETWEEN date(?) AND date(?)
        ORDER BY date(i.data_emissao) ASC, i.id ASC
    """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall()

    total_faturamento = 0.0
    for r in rows:
        valor = float(r["valor"] or 0)
        if not f_comissao.invoice_eh_devolucao(
            tipo_item_ref=r["tipo_item_ref"], valor=valor
        ):
            total_faturamento += valor

    mes_str = d1.strftime("%Y-%m")
    meta = float(get_goal_history(conn, mes_str, "VENDEDOR", vendor_id) or 0.0)
    if meta <= 0:
        meta = float(meta_vendedor_get_manual(conn, vendor_id) or 0.0)
    bateu_meta = f_comissao.bateu_meta_faturamento(total_faturamento, meta)

    lines = []
    total_comissao = 0.0
    total_devolucoes = 0.0
    novo_count = 0
    rep_count = 0
    cache_pedido = {}

    for r in rows:
        valor = float(r["valor"] or 0)
        invoice_id = int(r["id"])
        cliente_id = r["cliente_id"]
        pedido = (r["pedido"] or "").strip()
        doc_atual = (r["numero"] or pedido or f"ID:{invoice_id}").strip()
        is_devolucao = f_comissao.invoice_eh_devolucao(
            tipo_item_ref=r["tipo_item_ref"], valor=valor
        )

        identity_key = get_customer_identity_key(conn, cliente_id)
        info = get_customer_identity_row(conn, cliente_id)

        if pedido not in cache_pedido:
            cache_pedido[pedido] = _info_pedido_comissao(
                conn, pedido, int(cliente_id or 0), int(vendor_id or 0)
            )
        ped_info = cache_pedido[pedido]
        is_jit_imediato = f_comissao.condicao_jit_imediato(ped_info["condicao_entrega"])
        sisplan_novo = f_comissao.pedido_sisplan_cliente_novo(
            ped_info["motivo_pedido"], ped_info["desc_motivo_pedido"]
        )
        is_novo = False
        if not is_devolucao:
            is_novo = f_comissao.classificar_cliente_novo(
                pedido_sisplan_novo=sisplan_novo,
                is_jit_imediato=is_jit_imediato,
                status_manual=f_comissao.status_manual_de_texto(
                    get_manual_customer_status(conn, identity_key)
                ),
            )
            base_pct, bonus_pct, comissao_linha = f_comissao.comissao_linha_interno(
                valor, is_novo=is_novo, bateu_meta=bateu_meta
            )
            total_comissao += comissao_linha
            if is_novo:
                novo_count += 1
            else:
                rep_count += 1
        else:
            base_pct, bonus_pct, comissao_linha = 0.0, 0.0, 0.0
            total_devolucoes += valor

        lines.append({
            "data": r["data_emissao"] or "",
            "cliente": r["cliente"] or "—",
            "cliente_id": r["cliente_id"],
            "pedido": pedido,
            "valor": valor,
            "base_pct": base_pct,
            "bonus_pct": bonus_pct,
            "comissao": comissao_linha,
            "is_novo": is_novo,
            "is_jit": is_jit_imediato,
            "is_devolucao": is_devolucao,
            "is_anfarma": eh_responsavel_anfarma(r["vendor_nome_ref"]),
            "identity_nome": info.get("identity_nome") or "",
            "identity_key": identity_key,
            "doc_ref": doc_atual,
        })

    extra_bonus = f_comissao.bonus_volume(total_faturamento, bateu_meta=bateu_meta)
    total_comissao += extra_bonus

    return {
        "faturamento": total_faturamento,
        "comissao": total_comissao,
        "lines": lines,
        "extra_bonus": extra_bonus,
        "novo_count": novo_count,
        "rep_count": rep_count,
        "bateu_meta": bateu_meta,
        "devolucoes_valor": total_devolucoes,
    }
    
def realizado_externo_liquidez(conn, vendor_id: int, d0: date, d1: date) -> float:
    row = conn.execute("""
        SELECT COALESCE(SUM(COALESCE(valor_pago, valor_liq, 0)), 0) AS total
        FROM liquidez
        WHERE vendor_id = ?
          AND data_pagto IS NOT NULL
          AND date(data_pagto) BETWEEN date(?) AND date(?)
    """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchone()
    return float(row["total"] or 0)


def linhas_comissao_externo_liquidez(conn, vendor_id: int, d0: date, d1: date):
    rows = conn.execute("""
        SELECT
            l.data_pagto,
            COALESCE(COALESCE(c.fantasia, c.nome), l.responsavel, '—') AS cliente,
            l.pedido,
            COALESCE(l.valor_pago, l.valor_liq, 0) AS valor
        FROM liquidez l
        LEFT JOIN customers c ON c.id = l.customer_id
        WHERE l.vendor_id = ?
          AND l.data_pagto IS NOT NULL
          AND date(l.data_pagto) BETWEEN date(?) AND date(?)
        ORDER BY date(l.data_pagto) ASC, l.id ASC
    """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall()

    lines = []
    total = 0.0

    for r in rows:
        valor = float(r["valor"] or 0)
        comissao = f_comissao.comissao_linha_externo(valor)
        total += comissao

        lines.append({
            "data": r["data_pagto"] or "",
            "cliente": r["cliente"] or "—",
            "pedido": r["pedido"] or "",
            "valor": valor,
            "base_pct": f_comissao.PCT_EXTERNO_LIQUIDEZ,
            "bonus_pct": 0.0,
            "comissao": comissao,
            "is_novo": False,
        })

    return {
        "faturamento": realizado_externo_liquidez(conn, vendor_id, d0, d1),
        "comissao": total,
        "lines": lines,
        "extra_bonus": 0.0,
        "novo_count": 0,
        "rep_count": 0,
        "bateu_meta": False,
        "devolucoes_valor": 0.0,
    }


def _clear_access_denied_flash_for_sales_pages():
    """Remove somente o flash genérico herdado de um redirecionamento anterior."""
    try:
        flashes = session.get("_flashes") or []
        kept = []
        for item in flashes:
            try:
                cat, msg = item
            except Exception:
                kept.append(item)
                continue
            if str(msg or "").strip().lower().rstrip(".") == "acesso negado":
                continue
            kept.append(item)
        if kept:
            session["_flashes"] = kept
        else:
            session.pop("_flashes", None)
    except Exception:
        pass


def vendor_repurchase_metrics_fast(conn, vendor_id: int, ref=None):
    """Versão em lote das métricas de recompra da tela do vendedor.

    Mantém a mesma identidade comercial, mas elimina uma consulta de histórico
    para cada cliente/identidade da carteira.
    """
    ref = ref or date.today()
    base_rows = conn.execute("""
        SELECT DISTINCT cliente_id
        FROM invoices
        WHERE vendor_id=? AND cliente_id IS NOT NULL AND COALESCE(data_emissao,'')<>''
    """, (vendor_id,)).fetchall()
    if not base_rows:
        return {"atraso_mediano": 0.0, "intervalo_mediano": 0.0, "atrasados_qtd": 0}

    rep_by_identity = {}
    for r in base_rows:
        cid = int(r["cliente_id"] or 0)
        if not cid:
            continue
        ident = get_customer_identity_key(conn, cid)
        if ident and ident != "SEM_IDENTIDADE":
            rep_by_identity.setdefault(ident, cid)

    identities = list(rep_by_identity.keys())
    if not identities:
        return {"atraso_mediano": 0.0, "intervalo_mediano": 0.0, "atrasados_qtd": 0}

    member_to_identity = {}
    # Regras resolvidas: traz todos os membros das identidades da carteira.
    for i in range(0, len(identities), 700):
        batch = identities[i:i+700]
        marks = ",".join("?" for _ in batch)
        for rr in conn.execute(f"""
            SELECT customer_id, identity_key
            FROM customer_identity_rules
            WHERE identity_key IN ({marks})
        """, batch).fetchall():
            member_to_identity[int(rr["customer_id"])] = str(rr["identity_key"] or "")

    # Fallback CLIENTE/MATRIZ sem regra explícita.
    for ident, cid in rep_by_identity.items():
        member_to_identity.setdefault(int(cid), ident)

    member_ids = sorted(member_to_identity.keys())
    dates_by_identity = {ident: set() for ident in identities}
    for i in range(0, len(member_ids), 850):
        batch = member_ids[i:i+850]
        marks = ",".join("?" for _ in batch)
        for rr in conn.execute(f"""
            SELECT cliente_id, data_emissao
            FROM invoices
            WHERE cliente_id IN ({marks})
              AND COALESCE(data_emissao,'')<>''
            ORDER BY date(data_emissao), id
        """, batch).fetchall():
            ident = member_to_identity.get(int(rr["cliente_id"] or 0))
            if not ident:
                continue
            raw = str(rr["data_emissao"] or "")[:10]
            try:
                dates_by_identity.setdefault(ident, set()).add(datetime.strptime(raw, "%Y-%m-%d").date())
            except Exception:
                pass

    atrasos, intervalos = [], []
    atrasados_qtd = 0
    for ident in identities:
        datas = sorted(dates_by_identity.get(ident) or [])
        if len(datas) < 2:
            continue
        deltas = [(datas[j] - datas[j-1]).days for j in range(1, len(datas))]
        intervalo = float(statistics.median(deltas)) if deltas else 0.0
        if intervalo <= 0:
            continue
        intervalos.append(intervalo)
        atraso_atual = float(max(0, (ref - datas[-1]).days))
        if atraso_atual > intervalo:
            atrasados_qtd += 1
            atrasos.append(atraso_atual - intervalo)

    return {
        "atraso_mediano": float(statistics.median(atrasos)) if atrasos else 0.0,
        "intervalo_mediano": float(statistics.median(intervalos)) if intervalos else 0.0,
        "atrasados_qtd": int(atrasados_qtd),
    }


# -------------------------------------------------------------------------
# SUBSTITUIR A FUNÇÃO vendedores() POR ESTA:
# -------------------------------------------------------------------------
@app.route("/vendedores")
@login_required
def vendedores():
    user = get_current_user()
    _clear_access_denied_flash_for_sales_pages()
    
    # --- DATAS E CICLOS ---
    data_ref = request.args.get("data_ref", "")
    try:
        ref_date = datetime.strptime(data_ref, "%Y-%m").date() if data_ref else date.today()
    except:
        ref_date = date.today()

    # 1. Ciclo Financeiro (configurável) -> Para Faturamento
    d0_fat, d1_fat = periodo_ciclo_25_24_que_contem_hoje(ref_date)
    
    # 2. Ciclo Comercial (1-31) -> Para Pedidos/Meta
    d0_ped, d1_ped = periodo_mes_calendario(ref_date)

    with conectar() as conn:
        ensure_vendor_type_column(conn)
        ensure_vendors_schema(conn)
        force_site_vendor_types(conn)
        from crm_npd.services.vendedores_ciclo import _sync_vendedores_inativos
        _sync_vendedores_inativos(conn)
        
        # Filtra permissão
        vendor_lock = get_user_vendor_id(conn, user)
        vrows = conn.execute(
            """SELECT * FROM vendors
               WHERE COALESCE(ativo,1)=1
                 AND (? IS NULL OR id=?)
                 AND COALESCE(UPPER(TRIM(nome)), '') <> 'SITE'
                 AND COALESCE(UPPER(TRIM(tipo)), '') <> 'SITE'
               ORDER BY nome ASC""",
            (vendor_lock, vendor_lock)
        ).fetchall()
        
        # Totais em lote: evita duas consultas para cada vendedor da lista.
        fat_rows = conn.execute("""
            SELECT vendor_id, COALESCE(SUM(valor),0) AS total
            FROM invoices
            WHERE data_emissao IS NOT NULL
              AND date(data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY vendor_id
        """, (d0_fat.isoformat(), d1_fat.isoformat())).fetchall()
        totals_fat = {int(r["vendor_id"]): float(r["total"] or 0) for r in fat_rows if r["vendor_id"] is not None}

        ped_rows = conn.execute(f"""
            SELECT o.vendor_id, COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS total
            FROM orders o
            LEFT JOIN vendors v2 ON v2.id=o.vendor_id
            WHERE o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v2')}
            GROUP BY o.vendor_id
        """, (d0_ped.isoformat(), d1_ped.isoformat())).fetchall()
        totals_ped = {int(r["vendor_id"]): float(r["total"] or 0) for r in ped_rows if r["vendor_id"] is not None}
        for vv in vrows:
            totals_fat.setdefault(int(vv["id"]), 0.0)
            totals_ped.setdefault(int(vv["id"]), 0.0)

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-end mb-3">
        <div>
            <h3 class="mb-1">Equipe de Vendas</h3>
            <div class="muted small">
                Ref: <b>{{ d0_ped.strftime('%b/%Y') }}</b> | 
                Fat: {{ d0_fat.strftime('%d/%m') }} a {{ d1_fat.strftime('%d/%m') }}
            </div>
        </div>
        <div class="d-flex gap-2">
            {% if user.role in ['admin','gestor'] %}
            <a class="btn btn-light border" href="{{ url_for('vendedores_inativos') }}"><i class="bi bi-person-x me-1"></i>Inativos</a>
            {% endif %}
            <form class="d-flex align-items-center cardx p-1">
                <input type="month" name="data_ref" class="form-control form-control-sm border-0 bg-light fw-bold" 
                       value="{{ d1_ped.strftime('%Y-%m') }}" onchange="this.form.submit()">
            </form>
        </div>
    </div>

    <div class="cardx p-0 overflow-hidden">
        <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
                <thead class="bg-light">
                    <tr>
                        <th class="ps-3">Nome / Código</th>
                        <th>Tipo</th>
                        <th class="text-end text-primary">Pedidos (Mês)</th>
                        <th class="text-end text-success">Fat. (Ciclo)</th>
                        <th class="text-end pe-3">Ação</th>
                    </tr>
                </thead>
                <tbody>
                {% for v in vrows %}
                    <tr>
                        <td class="ps-3">
                            <div class="fw-bold text-dark">{{ v['nome'] }}</div>
                            <div class="small muted">{{ v['codigo'] or '-' }}</div>
                        </td>
                        <td>
                            {% if user.role in ['admin','gestor'] %}
                                <form action="{{ url_for('alterar_tipo_vendedor', vendor_id=v['id']) }}" method="post">
                                    <select name="novo_tipo" class="form-select form-select-sm py-0" 
                                            style="width: 100px; font-size: 0.8rem; border-color: #e5e7eb;" 
                                            onchange="this.form.submit()">
                                        <option value="Externo" {{ 'selected' if v['tipo'] == 'Externo' else '' }}>Externo</option>
                                        <option value="Interno" {{ 'selected' if v['tipo'] == 'Interno' else '' }}>Interno</option>
                                        <option value="Site" {{ 'selected' if v['tipo'] == 'Site' else '' }}>Site</option>
                                    </select>
                                </form>
                            {% else %}
                                <span class="badge bg-secondary">{{ v['tipo'] or 'Externo' }}</span>
                            {% endif %}
                        </td>
                        <td class="text-end fw-bold text-primary">
                            {{ format_brl(totals_ped[v['id']]) }}
                        </td>
                        <td class="text-end fw-bold text-success">
                            {{ format_brl(totals_fat[v['id']]) }}
                        </td>
                        <td class="text-end pe-3">
                            <a class="btn btn-sm btn-outline-primary" href="{{ url_for('vendedor', vendor_id=v['id']) }}">
                                Abrir
                            </a>
                            {% if user.role in ['admin','gestor'] %}
                            <form class="d-inline" method="post" action="{{ url_for('vendedor_acao', vendor_id=v['id']) }}" onsubmit="return confirm('Inativar {{ v['nome']|e }}?');">
                                <input type="hidden" name="acao" value="inativar">
                                <button class="btn btn-sm btn-outline-danger" title="Inativar">Inativar</button>
                            </form>
                            {% endif %}
                        </td>
                    </tr>
                {% else %}
                    <tr><td colspan="5" class="text-center py-4 muted">Nenhum vendedor encontrado.</td></tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    """, vrows=vrows, totals_fat=totals_fat, totals_ped=totals_ped, format_brl=format_brl, d0_fat=d0_fat, d1_fat=d1_fat, d0_ped=d0_ped, d1_ped=d1_ped, user=user)
    
    return render_page("Vendedores", body, "vendedores")


@app.route("/site")
@login_required
def site_analytics():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        return redirect(url_for("home"))

    data_ref_str = request.args.get("data_ref", "")
    with conectar() as conn:
        site_vendor_id = get_site_vendor_id(conn)
        site_params = site_sql_params(site_vendor_id)
        if data_ref_str:
            try:
                ref_date = datetime.strptime(data_ref_str, "%Y-%m").date()
            except Exception:
                ref_date = date.today().replace(day=1)
        else:
            # Sempre abre no mês atual, igual ao Dashboard.
            # O usuário ainda pode consultar meses anteriores pelo seletor.
            ref_date = date.today().replace(day=1)

        d0_ped, d1_ped = periodo_mes_calendario(ref_date)
        d0_fat, d1_fat = d0_ped, d1_ped

        ped_rows = conn.execute(f"""
            SELECT
                o.pedido,
                MAX(date(o.data_emissao)) AS data_emissao,
                SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) AS valor,
                COALESCE(NULLIF(MAX(c.rede_nome),''), NULLIF(MAX(c.fantasia),''), NULLIF(MAX(c.nome),''), '-') AS cliente,
                MAX(COALESCE(NULLIF(TRIM(o.medida_ref), ''), '-')) AS medida
            FROM orders o
            LEFT JOIN customers c ON c.id = o.cliente_id
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE {sql_site_match('o', join_alias='v')}
              AND o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY o.pedido
            ORDER BY date(data_emissao) DESC, o.pedido DESC
        """, site_params + (d0_ped.isoformat(), d1_ped.isoformat())).fetchall()

        fat_rows = conn.execute(f"""
            SELECT
                i.numero,
                i.pedido,
                date(i.data_emissao) AS data_emissao,
                COALESCE(i.valor,0) AS valor,
                COALESCE(NULLIF(c.rede_nome,''), NULLIF(c.fantasia,''), NULLIF(c.nome,''), '-') AS cliente
            FROM invoices i
            LEFT JOIN customers c ON c.id = i.cliente_id
            LEFT JOIN vendors v ON v.id = i.vendor_id
            WHERE {sql_site_match('i', join_alias='v')}
              AND i.data_emissao IS NOT NULL
              AND date(i.data_emissao) BETWEEN date(?) AND date(?)
            ORDER BY date(i.data_emissao) DESC, i.id DESC
        """, site_params + (d0_fat.isoformat(), d1_fat.isoformat())).fetchall()

        liq_total = site_liquidez_total_mes(conn, site_vendor_id, d0_fat, d1_fat)

        pedidos_val = sum(float(r["valor"] or 0) for r in ped_rows)
        pedidos_qtd = len(ped_rows)
        fat_val = sum(float(r["valor"] or 0) for r in fat_rows)
        fat_qtd = len(fat_rows)
        ticket_ped = (pedidos_val / pedidos_qtd) if pedidos_qtd else 0.0
        ticket_fat = (fat_val / fat_qtd) if fat_qtd else 0.0
        conv_qtd = f_funil.taxa_faturamento_pedidos(fat_qtd, pedidos_qtd)
        conv_val = (fat_val / pedidos_val * 100.0) if pedidos_val else 0.0
        receb_ratio = (liq_total / fat_val * 100.0) if fat_val else 0.0
        gap_qtd = max(0, pedidos_qtd - fat_qtd)
        gap_val = max(0.0, pedidos_val - fat_val)

        if conv_val >= 70 and receb_ratio >= 75:
            parecer = ("Vale a pena manter", "success", "Boa conversão de pedido em faturamento e recebimento saudável.")
        elif conv_val >= 45:
            parecer = ("Operação em atenção", "warning", "O canal vende, mas precisa melhorar conversão/recebimento.")
        else:
            parecer = ("Revisar estratégia", "danger", "Baixa conversão do marketplace no mês analisado.")

        top_clientes = conn.execute(f"""
            SELECT COALESCE(NULLIF(c.rede_nome,''), NULLIF(c.fantasia,''), NULLIF(c.nome,''), '-') AS cliente,
                   ROUND(SUM(COALESCE(i.valor,0)),2) AS valor_total
            FROM invoices i
            LEFT JOIN customers c ON c.id = i.cliente_id
            LEFT JOIN vendors v ON v.id = i.vendor_id
            WHERE {sql_site_match('i', join_alias='v')}
              AND i.data_emissao IS NOT NULL
              AND date(i.data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY 1
            ORDER BY valor_total DESC, cliente ASC
            LIMIT 10
        """, site_params + (d0_fat.isoformat(), d1_fat.isoformat())).fetchall()
        chart_clientes = bar_chart([r["cliente"][:14] for r in top_clientes], [r["valor_total"] for r in top_clientes], "Top clientes do site") if top_clientes else None

        medidas_rows = conn.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA') AS medida,
                   COUNT(DISTINCT o.pedido) AS qtd_pedidos,
                   ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),2) AS valor_total
            FROM orders o
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE {sql_site_match('o', join_alias='v')}
              AND o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY 1
            ORDER BY qtd_pedidos DESC, valor_total DESC, medida ASC
        """, site_params + (d0_ped.isoformat(), d1_ped.isoformat())).fetchall()
        chart_medidas = campanha_medida_chart(medidas_rows, pedidos_qtd) if medidas_rows else None

        meses = [f"{m:02d}" for m in range(1, 13)]
        ped_mes = []
        fat_mes = []
        for m in meses:
            ym = f"{ref_date.year}-{m}"
            pval = conn.execute(f"""
                SELECT COALESCE(SUM(v),0) AS total
                FROM (
                    SELECT o.pedido, SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) AS v
                    FROM orders o
                    LEFT JOIN vendors v ON v.id = o.vendor_id
                    WHERE {sql_site_match('o', join_alias='v')}
                      AND strftime('%Y-%m', date(o.data_emissao)) = ?
                      AND {sql_pedidos_realizados_excel_cond('o','v')}
                    GROUP BY o.pedido
                ) x
            """, site_params + (ym,)).fetchone()["total"] or 0
            fval = conn.execute(f"""
                SELECT COALESCE(SUM(i.valor),0) AS total
                FROM invoices i
                LEFT JOIN vendors v ON v.id = i.vendor_id
                WHERE {sql_site_match('i', join_alias='v')}
                  AND strftime('%Y-%m', date(i.data_emissao)) = ?
            """, site_params + (ym,)).fetchone()["total"] or 0
            ped_mes.append(float(pval or 0))
            fat_mes.append(float(fval or 0))
        chart_evol = line_chart(["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"], [("Pedidos Site", ped_mes), ("Fat Site", fat_mes)], "Evolução anual do site")

    body = render_template_string(r"""
    <div class="page-hero mb-4">
      <div class="d-flex align-items-end justify-content-between flex-wrap gap-3 position-relative" style="z-index:1">
        <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Canal digital</div><div class="hero-title">Marketplace / Site</div><div class="hero-sub mt-1">Janela mensal {{ d0_ped.strftime('%d/%m') }}–{{ d1_ped.strftime('%d/%m') }} · vendas, faturamento e liquidez em uma leitura única</div></div>
        <form class="d-flex gap-2 align-items-end" method="get"><div><label class="form-label small fw-bold mb-1" style="color:#b9cbe0">MÊS REF</label><input type="month" name="data_ref" class="form-control fw-bold" value="{{ d1_ped.strftime('%Y-%m') }}" onchange="this.form.submit()"></div></form>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-md-3"><div class="stat-card h-100"><div class="stat-label">Pedidos no mês</div><div class="stat-value">{{ pedidos_qtd }}</div><div class="stat-sub">{{ format_brl(pedidos_val) }}</div></div></div>
      <div class="col-12 col-md-3"><div class="stat-card h-100"><div class="stat-label">Faturamento do mês</div><div class="stat-value text-primary">{{ format_brl(fat_val) }}</div><div class="stat-sub">{{ fat_qtd }} faturamentos</div></div></div>
      <div class="col-12 col-md-3"><div class="stat-card h-100"><div class="stat-label">Liquidez do mês</div><div class="stat-value text-success">{{ format_brl(liq_total) }}</div><div class="stat-sub">Recebido / faturado: {{ '%.1f'|format(receb_ratio) }}%</div></div></div>
      <div class="col-12 col-md-3"><div class="stat-card h-100"><div class="stat-label">Gap operacional</div><div class="stat-value text-warning">{{ gap_qtd }}</div><div class="stat-sub">{{ format_brl(gap_val) }}</div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-md-3"><div class="cardx p-3 h-100"><div class="small text-muted fw-bold">TICKET PEDIDO</div><div class="h4 fw-bold mb-0">{{ format_brl(ticket_ped) }}</div></div></div>
      <div class="col-12 col-md-3"><div class="cardx p-3 h-100"><div class="small text-muted fw-bold">TICKET FATURADO</div><div class="h4 fw-bold mb-0">{{ format_brl(ticket_fat) }}</div></div></div>
      <div class="col-12 col-md-3"><div class="cardx p-3 h-100"><div class="small text-muted fw-bold">TAXA DE FATURAMENTO</div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(conv_qtd) }}%</div><div class="small text-muted">notas / pedidos do ciclo</div></div></div>
      <div class="col-12 col-md-3"><div class="cardx p-3 h-100"><div class="small text-muted fw-bold">CONVERSÃO VALOR</div><div class="h4 fw-bold mb-0">{{ '%.1f'|format(conv_val) }}%</div></div></div>
    </div>

    <div class="cardx p-3 mb-4 border-start border-4 border-{{ parecer[1] }}">
      <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
        <div>
          <div class="small fw-bold text-muted">PARECER AUTOMÁTICO</div>
          <div class="h5 mb-0 text-{{ parecer[1] }}">{{ parecer[0] }}</div>
          <div class="small text-muted mt-1">{{ parecer[2] }}</div>
        </div>
        <div class="text-end small text-muted">Mês civil puro · sem meta · sem comissão</div>
      </div>
    </div>

    <div class="row g-4">
      <div class="col-12 col-xl-7"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Evolução anual do canal</div><div class="viz-sub">Pedidos e faturamento mês a mês.</div></div></div><div class="viz-body">{% if chart_evol %}<img src="data:image/png;base64,{{ chart_evol }}">{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div></div>
      <div class="col-12 col-xl-5"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Clientes que mais faturaram</div><div class="viz-sub">Ranking por valor faturado no canal.</div></div></div><div class="viz-body">{% if chart_clientes %}<img src="data:image/png;base64,{{ chart_clientes }}">{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div></div>
    </div>

    <div class="row g-4 mt-1">
      <div class="col-12 col-xl-6"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Medidas com maior recorrência</div><div class="viz-sub">Pedidos por medida no mês, em ordem de relevância.</div></div></div><div class="viz-body">{% if chart_medidas %}<img src="data:image/png;base64,{{ chart_medidas }}">{% else %}<div class="text-muted">Sem dados.</div>{% endif %}</div></div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="h6 fw-bold mb-3">Últimos pedidos do site</div>
        <div class="table-responsive"><table class="table table-sm align-middle"><thead><tr><th>Data</th><th>Pedido</th><th>Cliente</th><th>Medida</th><th class="text-end">Valor</th></tr></thead><tbody>
        {% for r in ped_rows[:12] %}<tr><td>{{ r['data_emissao'] }}</td><td class="fw-bold">{{ r['pedido'] }}</td><td>{{ r['cliente'] }}</td><td>{{ r['medida'] }}</td><td class="text-end">{{ format_brl(r['valor']) }}</td></tr>{% endfor %}
        {% if not ped_rows %}<tr><td colspan="5" class="text-muted">Sem pedidos no mês.</td></tr>{% endif %}
        </tbody></table></div>
      </div></div>
    </div>

    <div class="row g-4 mt-1">
      <div class="col-12"><div class="cardx p-4"><div class="h6 fw-bold mb-3">Faturamentos do site</div>
        <div class="table-responsive"><table class="table table-sm align-middle"><thead><tr><th>Data</th><th>NF</th><th>Pedido</th><th>Cliente</th><th class="text-end">Valor</th></tr></thead><tbody>
        {% for r in fat_rows[:15] %}<tr><td>{{ r['data_emissao'] }}</td><td class="fw-bold">{{ r['numero'] }}</td><td>{{ r['pedido'] or '-' }}</td><td>{{ r['cliente'] }}</td><td class="text-end">{{ format_brl(r['valor']) }}</td></tr>{% endfor %}
        {% if not fat_rows %}<tr><td colspan="5" class="text-muted">Sem faturamentos no mês.</td></tr>{% endif %}
        </tbody></table></div>
      </div></div>
    </div>
    """, d0_ped=d0_ped, d1_ped=d1_ped, pedidos_qtd=pedidos_qtd, pedidos_val=pedidos_val, fat_qtd=fat_qtd, fat_val=fat_val, liq_total=liq_total, gap_qtd=gap_qtd, gap_val=gap_val, ticket_ped=ticket_ped, ticket_fat=ticket_fat, conv_qtd=conv_qtd, conv_val=conv_val, receb_ratio=receb_ratio, parecer=parecer, chart_evol=chart_evol, chart_clientes=chart_clientes, chart_medidas=chart_medidas, ped_rows=ped_rows, fat_rows=fat_rows, format_brl=format_brl)
    return render_page("Site", body, "site_analytics")


@app.route("/vendedor/<int:vendor_id>")
@login_required
def vendedor(vendor_id: int):
    user = get_current_user()
    _clear_access_denied_flash_for_sales_pages()
    if user and user.get("role") not in ["admin", "gestor"]:
        with conectar() as _pc:
            _own_vendor = get_user_vendor_id(_pc, user)
        if _own_vendor and _own_vendor > 0 and int(vendor_id) != int(_own_vendor):
            return redirect(url_for("vendedor", vendor_id=int(_own_vendor)))
    
    # --- 1. DATAS E CICLOS ---
    data_ref_str = request.args.get("data_ref", "")
    try:
        if data_ref_str:
            ref_date = datetime.strptime(data_ref_str, "%Y-%m").date()
        else:
            # CORREÇÃO: Força o dia 1 para alinhar com o seletor de mês.
            # Assim, "Janeiro" sempre será o ciclo 25/Dez-24/Jan, mesmo se hoje for dia 30.
            ref_date = date.today().replace(day=1)
    except:
        ref_date = date.today().replace(day=1)
    
    mes_str = ref_date.strftime("%Y-%m")
    hoje = date.today()
    
    # DEFINIÇÃO DOS CICLOS
    # A) Financeiro (26-25) -> Faturamento, Comissão, Meta e Projeção
    d0_fat, d1_fat = periodo_ciclo_25_24_que_contem_hoje(ref_date)
    
    # B) Comercial (1-31) -> Pedidos do mês-calendário
    d0_ped = date(ref_date.year, ref_date.month, 1)
    if ref_date.month == 12:
        d1_ped = date(ref_date.year, 12, 31)
    else:
        d1_ped = date(ref_date.year, ref_date.month + 1, 1) - timedelta(days=1)

    with conectar() as conn:
        ensure_goals_table(conn)
        force_site_vendor_types(conn)
        
        #try: check_repurchase_tasks(conn); conn.commit()
        #except: pass

        v = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not v:
            flash("Vendedor não encontrado.", "warning"); return redirect(url_for("vendedores"))
        if is_site_vendor_name(v["nome"] or v["codigo"] or ""):
            return redirect(url_for("site_analytics", data_ref=mes_str))

        # --- A. FINANCEIRO (Ciclo configurável) ---
        d1_calc_fat = min(hoje, d1_fat)
        # Calcula comissão usando as datas financeiras
        data_fin = compute_vendor_commission(conn, vendor_id, d0_fat, d1_calc_fat)

        faturamento = float(data_fin.get("faturamento", 0) or 0)
        comissao = float(data_fin.get("comissao", 0) or 0)
        lines = data_fin.get("lines", [])
        extra_bonus = float(data_fin.get("extra_bonus", 0) or 0)
        bateu_meta = bool(data_fin.get("bateu_meta"))
        devolucoes_valor = float(data_fin.get("devolucoes_valor", 0) or 0)
        
        # Variáveis legadas de contagem (baseadas em notas fiscais)
 # --- B. COMERCIAL (Ciclo 1-31) ---
        
        # 1. Total Pedidos - mesma regra validada do Dashboard/Excel.
        # Soma VALOR_ITEM por linha válida, não MAX(valor) agrupado por pedido.
        row_ped = conn.execute(f"""
            SELECT
                COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS val,
                COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd
            FROM orders o
            LEFT JOIN vendors v2 ON v2.id = o.vendor_id
            WHERE o.vendor_id=?
              AND o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o', 'v2')}
        """, (vendor_id, d0_ped.isoformat(), d1_ped.isoformat())).fetchone()
        
        pedidos_val = float(row_ped["val"])
        pedidos_qtd = int(row_ped["qtd"])
        ver_pedidos = request.args.get("ver_pedidos", "0") == "1"

        pedidos_lista = []
        if ver_pedidos:
            pedidos_lista = conn.execute(f"""
                SELECT
                    o.pedido,
                    MAX(date(o.data_emissao)) AS data_emissao,
                    COALESCE(MAX(NULLIF(TRIM(o.cliente_nome_ref),'')), COALESCE(c.fantasia, c.nome, '—')) AS cliente,
                    SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) AS valor,
                    MAX(COALESCE(o.condicao_entrega,'')) AS condicao_entrega,
                    GROUP_CONCAT(DISTINCT COALESCE(o.grupo_ref,'')) AS grupo_ref,
                    GROUP_CONCAT(DISTINCT COALESCE(o.tipo_item_ref,'')) AS tipo_item_ref,
                    GROUP_CONCAT(DISTINCT COALESCE(o.papel_ref,'')) AS papel_ref,
                    GROUP_CONCAT(DISTINCT COALESCE(o.medida_ref,'')) AS medida_ref
                FROM orders o
                LEFT JOIN customers c ON c.id = o.cliente_id
                LEFT JOIN vendors v2 ON v2.id = o.vendor_id
                WHERE o.vendor_id = ?
                  AND o.data_emissao IS NOT NULL
                  AND strftime('%Y-%m', date(o.data_emissao)) = ?
                  AND TRIM(COALESCE(o.pedido,'')) <> ''
                  AND {sql_pedidos_realizados_excel_cond('o','v2')}
                GROUP BY TRIM(COALESCE(o.pedido,''))
                ORDER BY date(data_emissao) DESC, CAST(o.pedido AS INTEGER) DESC, o.pedido DESC
            """, (vendor_id, mes_str)).fetchall()
            pedidos_lista = [dict(p) for p in pedidos_lista]
            for p in pedidos_lista:
                p["familia_produto"] = infer_order_product_family(
                    p.get("grupo_ref", ""),
                    p.get("tipo_item_ref", ""),
                    p.get("papel_ref", ""),
                    p.get("medida_ref", ""),
                )


        # 2. Classificação Novo/Repetição/Recuperado — FONTE ÚNICA
        # Usa o MESMO ciclo do Extrato de Comissão e o serviço canônico em lote.
        comercial_summary = summarize_vendor_entities_in_period(conn, vendor_id, d0_fat, d1_calc_fat)
        novo_count = int(comercial_summary["novo_count"] or 0)
        rep_count = int(comercial_summary["rep_count"] or 0)
        recuperado_count = int(comercial_summary.get("recuperado_count", 0) or 0)

        # 2. Meta Histórica (Do mês selecionado)
        meta_mensal = get_goal_history(conn, mes_str, "VENDEDOR", vendor_id)
        if meta_mensal == 0:
             # Fallback para meta manual antiga
             old_meta = conn.execute("SELECT meta_manual FROM metas_vendedores WHERE vendor_id=?", (vendor_id,)).fetchone()
             if old_meta: meta_mensal = float(old_meta["meta_manual"] or 0)

        meta_realizado_pct = (faturamento / meta_mensal * 100.0) if meta_mensal > 0 else 0.0
        meta_variacao_pct = ((faturamento - meta_mensal) / meta_mensal * 100.0) if meta_mensal > 0 else None

        # 3. Metas Derivadas (Diária e Semanal) — ciclo 26→25
        dias_uteis_mes = business_days_between(d0_fat, d1_fat)
        meta_diaria = meta_mensal / dias_uteis_mes if dias_uteis_mes > 0 else 0.0
        meta_semanal = meta_diaria * 5
        
        # Cálculo do "Necessário Hoje" (Catch-up) sobre faturamento do ciclo
        falta_para_meta = max(0.0, meta_mensal - faturamento)
        
        if d1_fat < hoje: du_rest = 0
        elif d0_fat > hoje: du_rest = dias_uteis_mes
        else: du_rest = business_days_between(hoje, d1_fat)
             
        nec_dia = falta_para_meta / du_rest if du_rest > 0 else 0.0

        # 4. Projeção no ciclo 26→25 (mesmo número da comissão)
        total_dias_ciclo = (d1_fat - d0_fat).days + 1
        if d1_fat < hoje:
            dias_passados = total_dias_ciclo
            projecao = faturamento
        elif d0_fat > hoje:
            dias_passados = 0
            projecao = 0.0
        else:
            dias_passados = (hoje - d0_fat).days
            if dias_passados > 0:
                media_dia = faturamento / dias_passados
                projecao = media_dia * total_dias_ciclo
            else:
                projecao = 0.0

        # --- D. MÉTRICAS (Recompra) ---
        rep_stats = vendor_repurchase_metrics_fast(conn, vendor_id, ref=hoje)
        atraso_med = rep_stats["atraso_mediano"]
        intervalo_med = rep_stats["intervalo_mediano"] # <--- Faltava essa
        atrasados_qtd = rep_stats["atrasados_qtd"]

        # --- E. GRÁFICOS ---
        v_info = conn.execute("SELECT codigo, nome FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        v_codigo_sisplan = v_info["codigo"] if v_info else None
        v_nome_sisplan = v_info["nome"] if v_info else None
        
        metrics_orc_final = get_metrics_orcamentos(
            conn,
            d0_ped.strftime("%Y-%m-%d"),
            d1_ped.strftime("%Y-%m-%d"),
            vendor_id=v_codigo_sisplan,
            vendor_nome=v_nome_sisplan
        )
        
        # Evolução diária em duas agregações, em vez de 2 SELECTs por dia do mês.
        dates = []; c = d0_ped
        while c <= d1_ped:
            dates.append(c); c += timedelta(days=1)
        lbls = [d.strftime("%d") for d in dates]
        ped_day_rows = conn.execute(f"""
            SELECT date(o.data_emissao) AS dia,
                   COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) AS total
            FROM orders o
            LEFT JOIN vendors v2 ON v2.id=o.vendor_id
            WHERE o.vendor_id=?
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v2')}
            GROUP BY date(o.data_emissao)
        """, (vendor_id, d0_ped.isoformat(), d1_ped.isoformat())).fetchall()
        fat_day_rows = conn.execute("""
            SELECT date(data_emissao) AS dia, COALESCE(SUM(valor),0) AS total
            FROM invoices
            WHERE vendor_id=? AND date(data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY date(data_emissao)
        """, (vendor_id, d0_ped.isoformat(), d1_ped.isoformat())).fetchall()
        ped_by_day = {str(r["dia"]): float(r["total"] or 0) for r in ped_day_rows}
        fat_by_day = {str(r["dia"]): float(r["total"] or 0) for r in fat_day_rows}
        dp = [ped_by_day.get(d.isoformat(), 0.0) for d in dates]
        df = [fat_by_day.get(d.isoformat(), 0.0) for d in dates]
        evo = line_chart(lbls, [("Pedidos", dp), ("Faturamento", df)], "Ritmo diário")

        # Histórico Anual
        ano_atual = date.today().year
        def get_hist(y, vid):
            r = conn.execute("SELECT strftime('%m', data_emissao) as m, SUM(valor) as v FROM invoices WHERE vendor_id=? AND strftime('%Y', data_emissao)=? GROUP BY m", (vid, str(y))).fetchall()
            d = {x['m']: x['v'] for x in r}
            return [d.get(f"{k:02d}",0) for k in range(1,13)]
        chart_yoy = line_chart(["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"], [(str(ano_atual-1), get_hist(ano_atual-1, vendor_id)), (str(ano_atual), get_hist(ano_atual, vendor_id))], "Comparativo Anual")

        # Top Clientes / Redes (Pedidos Mês) - UNIFICADO
        top_entities = summarize_vendor_sales_identities_in_period(conn, vendor_id, d0_ped, d1_ped)[:8]
        top_entities = enrich_top_entities_for_ui(top_entities)

        # Score somente para as identidades que realmente aparecem no Top 8.
        identity_keys = [str(r.get("identity_key") or "") for r in top_entities if r.get("identity_key")]
        score_scope_ids = {int(r.get("cliente_id") or 0) for r in top_entities if int(r.get("cliente_id") or 0) > 0}
        score_scope_ids.update(int(r.get("matriz_customer_id") or 0) for r in top_entities if int(r.get("matriz_customer_id") or 0) > 0)
        if identity_keys:
            for _i in range(0, len(identity_keys), 700):
                _batch = identity_keys[_i:_i+700]
                _marks = ",".join("?" for _ in _batch)
                for _rr in conn.execute(f"SELECT customer_id FROM customer_identity_rules WHERE identity_key IN ({_marks})", _batch).fetchall():
                    if int(_rr["customer_id"] or 0) > 0:
                        score_scope_ids.add(int(_rr["customer_id"]))
        score_map_vendor = build_identity_score_map(conn, user=user, customer_ids_scope=sorted(score_scope_ids)) if score_scope_ids else {}
        
        for r in top_entities:
            sc = enrich_identity_score_for_ui(score_map_vendor.get(r["identity_key"], {}))
            r["score"] = sc.get("score", 0)
            r["score_faixa"] = sc.get("faixa", "D")
            r["score_label"] = sc.get("label", "Baixa prioridade")
            r["score_cor"] = sc.get("cor", "secondary")
            r["prioridade"] = sc.get("prioridade", "BAIXA")
            r["prioridade_cor"] = sc.get("prioridade_cor", "secondary")
        
        topc_bar = rank_chart_horizontal(
            [r["nome_exibicao"][:24] for r in top_entities],
            [float(r["valor"] or 0) for r in top_entities],
            money=True
        ) if top_entities else None

        # O donut de perfil não era exibido nesta página; não o renderizamos mais em background.
        pie = None

    # HTML
    body = render_template_string(r"""
    <div class="page-hero mb-4">
      <div class="d-flex align-items-end justify-content-between flex-wrap gap-3 position-relative" style="z-index:1">
        <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Performance individual</div><div class="hero-title">{{ (v['codigo'] ~ ' · ') if v['codigo'] else '' }}{{ v['nome'] }}</div><div class="hero-sub mt-1">Meta de faturamento {{ d0_fat.strftime('%d/%m') }}–{{ d1_fat.strftime('%d/%m') }}</div></div>
        <div class="d-flex gap-2 align-items-end"><form method="get"><input type="month" name="data_ref" class="form-control fw-bold" value="{{ mes_str }}" onchange="this.form.submit()"></form><a class="btn btn-outline-light" href="{{ url_for('vendedores') }}">Voltar</a></div>
      </div>
    </div>

    <div class="row g-3 mb-3">
      <div class="col-12 col-md-3">
        <div class="kpi p-3 h-100">
          <div class="muted">Meta de faturamento</div>
          <div class="h4 mb-0 fw-bold">{{ format_brl(meta_mensal) }}</div>
          <div class="small muted">{{ d0_fat.strftime('%d/%m') }} a {{ d1_fat.strftime('%d/%m') }}</div>
          {% if meta_mensal > 0 %}<div class="small fw-bold {{ 'text-success' if meta_variacao_pct>=0 else 'text-danger' }}">{{ '%.1f'|format(meta_variacao_pct|abs) }}% {{ 'acima' if meta_variacao_pct>=0 else 'abaixo' }} da meta · {{ '%.1f'|format(meta_realizado_pct) }}% realizado</div>{% endif %}
          {% if user.role in ['admin','gestor'] %}
          <form method="post" action="{{ url_for('vendedor_meta_hist', vendor_id=v['id']) }}" class="mt-2 d-flex gap-1">
             <input type="hidden" name="mes_ref" value="{{ mes_str }}">
             <input type="text" name="meta_valor" class="form-control form-control-sm" placeholder="Definir..." style="width:100px;">
             <button class="btn btn-sm btn-light border">OK</button>
          </form>
          {% endif %}
        </div>
      </div>
    <div class="col-12 col-md-3">
        <div class="kpi p-3 h-100">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <div class="muted">Pedidos (mês civil)</div>
              <div class="h4 mb-0 text-primary fw-bold">{{ format_brl(pedidos_v) }}</div>
              <div class="small muted">{{ pedidos_n }} pedidos · {{ d0_ped.strftime('%d/%m') }} a {{ d1_ped.strftime('%d/%m') }}</div>
            </div>
            <div>
              {% if ver_pedidos %}
                <a class="btn btn-sm btn-outline-secondary"
                   href="{{ url_for('vendedor', vendor_id=v['id'], data_ref=mes_str) }}">
                  Ocultar
                </a>
              {% else %}
                <a class="btn btn-sm btn-outline-primary"
                   href="{{ url_for('vendedor', vendor_id=v['id'], data_ref=mes_str, ver_pedidos=1) }}">
                  Ver pedidos
                </a>
              {% endif %}
            </div>
          </div>
        </div>
      </div>
      <div class="col-12 col-md-3">
        <div class="kpi p-3 h-100">
          <div class="muted">Projeção Fechamento</div>
          <div class="h4 mb-0 text-info fw-bold">{{ format_brl(projecao) }}</div>
          {% if meta_mensal > 0 %}
            <div class="small {{ 'text-success' if projecao >= meta_mensal else 'text-danger' }}">
                {{ "%.1f"|format(projecao / meta_mensal * 100) }}% da meta
            </div>
          {% endif %}
        </div>
      </div>
      <div class="col-12 col-md-3">
        <div class="kpi p-3 h-100" style="border-left: 4px solid #20c997;">
            <div class="d-flex justify-content-between">
                <div class="muted">Comissão do próximo mês</div>
                <div class="icon" style="height:30px; width:30px; font-size:0.8rem;">$</div>
            </div>
            <div class="h4 mb-0 text-success fw-bold">{{ format_brl(comissao) }}</div>
            <div class="small muted">Base Fat: {{ format_brl(faturamento) }}{% if meta_mensal > 0 %} · {{ '%.1f'|format(meta_realizado_pct) }}% da meta{% endif %}</div>
            {% if bateu_meta %}<div class="small text-success fw-bold">Meta batida · 1,75%</div>{% endif %}
        </div>
      </div>
    </div>
<div class="row g-3 mb-4">
    <div class="col-12 col-md-4">
        {% if user.role == 'admin' %}<a href="{{ url_for('performance_vendedor_metrica', vendor_id=v['id'], metrica='orcamentos', data_ref=mes_str) }}" class="text-decoration-none text-dark d-block h-100">{% endif %}
        <div class="cardx p-3 bg-white shadow-sm border rounded h-100" {% if user.role == 'admin' %}style="cursor:pointer;"{% endif %}>
            <div class="small text-muted fw-bold">ORÇAMENTOS CRIADOS</div>
            <div class="h4 mb-0">{{ metrics_orc['total'] }}</div>
            <div class="small text-muted">Total no período{% if user.role == 'admin' %} · clique para detalhar{% endif %}</div>
        </div>
        {% if user.role == 'admin' %}</a>{% endif %}
    </div>
    <div class="col-12 col-md-4">
        {% if user.role == 'admin' %}<a href="{{ url_for('performance_vendedor_metrica', vendor_id=v['id'], metrica='convertidos', data_ref=mes_str) }}" class="text-decoration-none text-dark d-block h-100">{% endif %}
         <div class="cardx p-3 bg-white shadow-sm border rounded h-100" {% if user.role == 'admin' %}style="cursor:pointer;"{% endif %}>
            <div class="small text-muted fw-bold">CONVERSÃO (PEDIDOS)</div>
            <div class="h4 mb-0">{{ metrics_orc['convertidos'] }}</div>
            <div class="small text-success">{{ "%.1f"|format(metrics_orc['taxa_conversao']) }}% de aproveitamento{% if user.role == 'admin' %} · clique para detalhar{% endif %}</div>
        </div>
        {% if user.role == 'admin' %}</a>{% endif %}
    </div>
    <div class="col-12 col-md-4">
        <a href="{{ url_for('performance_vendedor_metrica', vendor_id=v['id'], metrica='orcamentos', data_ref=mes_str) if user.role == 'admin' else url_for('orcamentos_detalhe', data_ref=mes_ref) }}" class="text-decoration-none text-dark d-block h-100">
        <div class="cardx p-3 bg-white shadow-sm border rounded h-100" style="cursor:pointer;">
            <div class="small text-muted fw-bold">VALOR EM ORÇAMENTOS</div>
            <div class="h4 mb-0">{{ format_brl(metrics_orc['valor_total']) }}</div>
            <div class="small text-muted">Potencial de vendas · clique para detalhar</div>
        </div>
        </a>
    </div>
</div>
        
    <div class="row g-2 mb-3">
        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate">Meta Diária</div>
                <div class="h5 mb-0 fw-bold">{{ format_brl(meta_diaria) }}</div>
            </div>
        </div>

        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate">Meta Semanal</div>
                <div class="h5 mb-0 fw-bold">{{ format_brl(meta_semanal) }}</div>
            </div>
        </div>

        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate" title="Quanto vender por dia p/ bater a meta">Necessário/Dia</div>
                <div class="h5 mb-0 fw-bold {{ 'text-danger' if nec_dia > meta_diaria else 'text-success' }}">
                    {{ format_brl(nec_dia) }}
                </div>
            </div>
        </div>

        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate">Carteira</div>
                <div class="d-flex gap-2 mt-1" style="font-size:0.9rem">
                    <div><span class="text-success fw-bold">{{ novo }}</span> <small>Novos</small></div>
                    <div><span class="text-warning fw-bold">{{ rep }}</span> <small>Rec.</small></div>
                    {% if recuperados %}<div title="Subconjunto dos recorrentes que voltou após passar do intervalo mediano de recompra"><span class="text-primary fw-bold">{{ recuperados }}</span> <small>Recup.</small></div>{% endif %}
                </div>
            </div>
        </div>

        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate">Clientes Atrasados</div>
                <div class="h5 mb-0 fw-bold text-warning">{{ atrasados_qtd }}</div>
            </div>
        </div>

        <div class="col-6 col-lg-2">
            <div class="kpi p-2 h-100">
                <div class="muted small text-truncate">Média Atraso</div>
                <div class="h5 mb-0 fw-bold">{{ atraso_med|int }} dias</div>
            </div>
        </div>
    </div>

    <div class="vendor-dashboard">
    <div class="row g-4">
      <div class="col-12 col-xl-6">
        <div class="viz-card h-100"><div class="viz-head"><div><div class="viz-title">Ritmo diário</div><div class="viz-sub">Pedidos e faturamento dia a dia no mês selecionado.</div></div></div><div class="viz-body vendor-chart-wide">{% if evo %}<img src="data:image/png;base64,{{ evo }}">{% else %}<div class="muted py-4 text-center">Sem dados.</div>{% endif %}</div></div>
      </div>
      <div class="col-12 col-xl-6">
        <div class="viz-card h-100"><div class="viz-head"><div><div class="viz-title">Histórico anual</div><div class="viz-sub">Comparativo mensal do faturamento contra o ano anterior.</div></div></div><div class="viz-body vendor-chart-wide">{% if chart_yoy %}<img src="data:image/png;base64,{{ chart_yoy }}">{% else %}<div class="muted py-4 text-center">Sem dados.</div>{% endif %}</div></div>
      </div>
    </div>
    <div class="row g-4 mt-1">
      <div class="col-12 col-xl-6">
        <div class="viz-card h-100"><div class="viz-head"><div><div class="viz-title">Top clientes / redes</div><div class="viz-sub">Ranking do mês por valor realizado, sem comprimir os nomes.</div></div></div><div class="viz-body vendor-chart-rank">{% if topc_bar %}<img src="data:image/png;base64,{{ topc_bar }}">{% else %}<div class="muted py-4 text-center">Sem dados.</div>{% endif %}</div></div>
      </div>
      <div class="col-12 col-xl-6">
        <div class="cardx p-3 h-100">
          <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
              <div class="h6 mb-0">Top Clientes / Redes</div>
              <div class="muted small">Pedidos do mês consolidados por identidade comercial</div>
            </div>
          </div>
      
          {% if top_entities %}
            <div class="d-flex flex-column gap-2">
              {% for r in top_entities %}
                <div class="border rounded-3 p-2 px-3 bg-white">
                  <div class="d-flex justify-content-between align-items-start gap-3">
                    <div>
                      <div class="fw-semibold">{{ r['nome_exibicao'] }}</div>
                      <div class="small text-muted d-flex flex-wrap gap-2 mt-1">
                        <span class="badge rounded-pill
                          {% if r['identity_tipo'] == 'GRUPO' %}text-bg-primary
                          {% elif r['identity_tipo'] == 'REDE' %}text-bg-info
                          {% else %}text-bg-light border text-dark
                          {% endif %}">
                          {{ r['identity_tipo'] }}
                        </span>
                        <span class="badge rounded-pill text-bg-{{ r['score_cor'] }}">
                          {{ r['score_faixa'] }} · {{ r['score_label'] }}
                        </span>
                        
                        <span class="badge rounded-pill text-bg-{{ r['prioridade_cor'] }}">
                          {{ r['prioridade'] }}
                        </span>
      
                        <span class="badge rounded-pill {{ r['badge_class'] }} customer-status-badge"
                              style="cursor:context-menu"
                              data-identity-key="{{ r['identity_key'] }}"
                              data-identity-nome="{{ r['nome_exibicao'] }}"
                              data-is-novo="{{ 1 if r['badge'] == 'NOVO' else 0 }}"
                              data-can-set-novo="{{ 1 if user and user.get('role') in ['admin','gestor'] else 0 }}"
                              title="Botão direito para ações">
                          {{ r['badge'] }}
                        </span>
                        <span>{{ r['pedidos_qtd'] }} pedido(s)</span>
                      </div>
                    </div>
      
                    <div class="text-end">
                      <div class="fw-bold text-primary">{{ format_brl(r['valor']) }}</div>
                      {% if r['cliente_id'] %}
                        <a href="{{ url_for('cliente_360', cliente_id=r['cliente_id']) }}"
                           class="btn btn-sm btn-light border mt-1">
                          Perfil
                        </a>
                      {% endif %}
                    </div>
                  </div>
                </div>
              {% endfor %}
            </div>
          {% else %}
            <div class="muted text-center py-4">Nenhum movimento no período.</div>
          {% endif %}
        </div>
      </div>
    </div>
    </div>

    <div class="cardx p-3 mt-4">
      <div class="d-flex justify-content-between align-items-start flex-wrap gap-2 mb-1">
        <div class="h6 mb-0">Extrato de Comissão (Ciclo 26→25)</div>
        <a class="btn btn-sm btn-success" href="{{ url_for('vendedor_comissao_excel', vendor_id=v['id'], data_ref=mes_str) }}">
          <i class="bi bi-file-earmark-excel me-1"></i>Excel
        </a>
      </div>
      <div class="small muted mb-2">{{ d0_fat.strftime('%d/%m/%Y') }} a {{ d1_fat.strftime('%d/%m/%Y') }} · NF de saída + DEMO, sem frete e sem clichê. Devolução não abate.{% if bateu_meta %} Meta batida: 1,75% (novos 2%).{% endif %}</div>
      <div class="small mb-3">
        <span class="badge text-bg-light border">Carteira 1%</span>
        <span class="badge text-bg-success">Pedido novo (Sisplan 10) 2%</span>
        <span class="badge text-bg-secondary">JIT IMEDIATO sem bônus</span>
        <span class="badge text-bg-warning">Devolução visível, não desconta</span>
        <span class="badge text-bg-dark">Associação Nacional (05975)</span>
        {% if bateu_meta %}<span class="badge text-bg-info">Meta batida 1,75%</span>{% endif %}
      </div>
      <div class="table-responsive">
        <table class="table table-sm table-hover align-middle">
          <thead><tr><th>Data</th><th>NF / Pedido</th><th>Cliente</th><th class="text-end">Base</th><th class="text-end">%</th><th class="text-end">Comissão</th></tr></thead>
          <tbody>
            {% for l in lines %}
              <tr class="linha-comissao {{ 'table-light' if l.is_devolucao else '' }}"
                  data-cliente-id="{{ l.cliente_id or '' }}"
                  data-cliente="{{ l.cliente }}">
                <td class="muted">{{ l.data[5:] }}</td>
                <td class="small font-monospace">{{ l.doc_ref or l.pedido or '—' }}</td>
                <td class="fw-semibold">
                  {{ l.cliente[:25] }}
                  {% if l.identity_nome and l.identity_nome != l.cliente %}
                    <span class="badge rounded-pill text-bg-light border" style="font-size:0.6rem">{{ l.identity_nome }}</span>
                  {% endif %}
                  {% if l.is_devolucao %}
                    <span class="badge rounded-pill text-bg-warning" style="font-size:0.6rem">DEVOLUÇÃO</span>
                  {% else %}
                  <span class="badge rounded-pill {{ 'text-bg-success' if l.is_novo else 'text-bg-light border text-dark' }} customer-status-badge"
                        style="font-size:0.6rem; cursor:context-menu"
                        data-identity-key="{{ l.identity_key }}"
                        data-identity-nome="{{ l.identity_nome or l.cliente }}"
                        data-is-novo="{{ 1 if l.is_novo else 0 }}"
                        data-can-set-novo="{{ 1 if user and user.get('role') in ['admin','gestor'] else 0 }}"
                        title="Botão direito para ações">
                    {{ 'NOVO' if l.is_novo else 'CARTEIRA' }}
                  </span>
                  {% endif %}
                  {% if l.is_jit %}
                    <span class="badge rounded-pill text-bg-secondary" style="font-size:0.6rem">JIT IMEDIATO</span>
                  {% endif %}
                  {% if l.is_anfarma %}
                    <span class="badge rounded-pill text-bg-dark" style="font-size:0.6rem" title="Responsável original 05975 — Associação Nacional">ANFARMA</span>
                  {% endif %}
                </td>
                <td class="text-end">{{ format_brl(l.valor) }}</td>
                <td class="text-end text-muted">{% if l.is_devolucao %}—{% else %}{{ "%.2f"|format(l.base_pct + l.bonus_pct) }}%{% endif %}</td>
                <td class="text-end fw-bold {{ 'text-muted' if l.is_devolucao else 'text-success' }}">{% if l.is_devolucao %}—{% else %}{{ format_brl(l.comissao) }}{% endif %}</td>
              </tr>
            {% else %}
              <tr><td colspan="7" class="text-center muted py-3">Nenhum faturamento neste ciclo.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
                                  
      <div id="menuContextoCliente"
           style="display:none; position:absolute; z-index:99999; min-width:240px; background:#fff; border:1px solid #ddd; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.15); overflow:hidden;">
        <a href="#" id="ctxCriarFilial" class="d-block px-3 py-2 text-decoration-none text-dark border-bottom">
          Criar filial para este cliente
        </a>
        <a href="#" id="ctxAdicionarFilial" class="d-block px-3 py-2 text-decoration-none text-dark">
          Adicionar a uma filial existente
        </a>
      </div>                              
    
      <div id="menuContextoStatusCliente"
           style="display:none; position:absolute; z-index:100000; min-width:240px; background:#fff; border:1px solid #ddd; border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.15); overflow:hidden;">
        <button type="button" id="ctxMoverRecompra" class="w-100 text-start px-3 py-2 border-0 bg-white border-bottom">
          Mudar para carteira
        </button>
        <button type="button" id="ctxMoverNovo" class="w-100 text-start px-3 py-2 border-0 bg-white">
          Mudar para novo
        </button>
      </div>

      <form id="ctxStatusForm" method="POST" style="display:none;">
        <input type="hidden" name="status_novo" id="ctxStatusNovo">
        <input type="hidden" name="identity_nome" id="ctxIdentityNome">
        <input type="hidden" name="next" id="ctxNext">
      </form>

      {% if devolucoes_valor %}
        <div class="alert alert-warning mt-3 mb-0 py-2">Devoluções no ciclo: <b>{{ format_brl(devolucoes_valor) }}</b>. Esse valor <b>não reduz</b> a base da comissão; fica visível para o gestor decidir.</div>
      {% endif %}
      {% if bateu_meta %}
        <div class="alert alert-info mt-3 mb-0 py-2">Meta batida: todas as notas a <b>1,75%</b>. Pedidos novos continuam a <b>2%</b>. O bônus de 1% sobre o excedente de R$ 100 mil não se aplica.</div>
      {% elif extra_bonus > 0 %}
        <div class="alert alert-success mt-3 mb-0 py-2">Bônus de 1% sobre o que passou de R$ 100 mil no ciclo: <b>{{ format_brl(extra_bonus) }}</b></div>
      {% endif %}
    </div>
    {% if ver_pedidos %}
    <div class="cardx p-3 mt-4">
      <div class="d-flex justify-content-between align-items-center mb-3">
        <div>
          <div class="h5 mb-0">Pedidos do mês</div>
          <div class="small muted">Lista dos pedidos que compõem o realizado comercial</div>
        </div>
      </div>

      <div class="table-responsive">
        <table class="table table-sm align-middle">
          <thead>
            <tr>
              <th>Data</th>
              <th>Pedido</th>
              <th>Cliente</th>
              <th>Produto</th>
              <th class="text-end">Valor</th>
            </tr>
          </thead>
          <tbody>
            {% for p in pedidos_lista %}
            <tr>
              <td>{{ p['data_emissao'] or '-' }}</td>
              <td>{{ p['pedido'] or '-' }}</td>
              <td>{{ p['cliente'] or '-' }}</td>
              <td><span class="badge rounded-pill text-bg-light border">{{ p['familia_produto'] or 'Sem produto' }}</span></td>
              <td class="text-end fw-bold">{{ format_brl(p['valor'] or 0) }}</td>
            </tr>
            {% else %}
            <tr>
              <td colspan="5" class="text-center text-muted py-3">Nenhum pedido encontrado no período.</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    {% endif %}
<script>
(function(){
    const menuFilial = document.getElementById("menuContextoCliente");
    const btnCriar = document.getElementById("ctxCriarFilial");
    const btnAdd = document.getElementById("ctxAdicionarFilial");
    const menuStatus = document.getElementById("menuContextoStatusCliente");
    const btnRecompra = document.getElementById("ctxMoverRecompra");
    const btnNovo = document.getElementById("ctxMoverNovo");
    const formStatus = document.getElementById("ctxStatusForm");
    const inputStatus = document.getElementById("ctxStatusNovo");
    const inputNome = document.getElementById("ctxIdentityNome");
    const inputNext = document.getElementById("ctxNext");

    function hideMenus(){
        if (menuFilial) menuFilial.style.display = "none";
        if (menuStatus) menuStatus.style.display = "none";
    }

    if (menuFilial && btnCriar && btnAdd) {
        document.querySelectorAll(".linha-comissao").forEach(row => {
            row.addEventListener("contextmenu", function(e){
                if (e.target.closest(".customer-status-badge")) return;
                const clienteId = this.getAttribute("data-cliente-id");
                const clienteNome = this.getAttribute("data-cliente");
                if (!clienteId || clienteId === "None" || clienteId === "") return;

                e.preventDefault();
                hideMenus();

                btnCriar.href = "{{ url_for('clientes_filiais') }}?cliente_id=" + clienteId + "&modo=criar&nome=" + encodeURIComponent(clienteNome || "");
                btnAdd.href = "{{ url_for('clientes_filiais') }}?cliente_id=" + clienteId + "&modo=adicionar&nome=" + encodeURIComponent(clienteNome || "");
                menuFilial.style.display = "block";
                menuFilial.style.left = e.pageX + "px";
                menuFilial.style.top = e.pageY + "px";
            });
        });
    }

    if (menuStatus && btnRecompra && btnNovo && formStatus) {
        document.querySelectorAll(".customer-status-badge").forEach(badge => {
            badge.addEventListener("contextmenu", function(e){
                const identityKey = this.getAttribute("data-identity-key") || "";
                if (!identityKey) return;

                e.preventDefault();
                e.stopPropagation();
                hideMenus();

                formStatus.action = "{{ request.script_root }}/cliente-status/" + encodeURIComponent(identityKey);
                inputNome.value = this.getAttribute("data-identity-nome") || this.textContent.trim();
                inputNext.value = window.location.pathname + window.location.search;
                btnNovo.style.display = ((this.getAttribute("data-can-set-novo") || "0") === "1") ? "block" : "none";

                menuStatus.style.display = "block";
                menuStatus.style.left = e.pageX + "px";
                menuStatus.style.top = e.pageY + "px";
            });
        });

        btnRecompra.addEventListener("click", function(){
            inputStatus.value = "RECOMPRA";
            formStatus.submit();
        });

        btnNovo.addEventListener("click", function(){
            inputStatus.value = "NOVO";
            formStatus.submit();
        });
    }

    document.addEventListener("click", hideMenus);
    window.addEventListener("scroll", hideMenus);
})();
</script>
    """, 
   v=v, mes_str=mes_str, user=user, format_brl=format_brl,
    d0_ped=d0_ped, d1_ped=d1_ped, d0_fat=d0_fat, d1_fat=d1_fat,
    meta_mensal=meta_mensal, meta_diaria=meta_diaria, meta_semanal=meta_semanal, meta_realizado_pct=meta_realizado_pct, meta_variacao_pct=meta_variacao_pct,
    pedidos_v=pedidos_val, pedidos_n=pedidos_qtd,
    projecao=projecao, comissao=comissao, faturamento=faturamento,
    novo=novo_count, rep=rep_count, recuperados=recuperado_count,
    evo=evo, topc_bar=topc_bar, lines=lines, extra_bonus=extra_bonus,
    bateu_meta=bateu_meta, devolucoes_valor=devolucoes_valor,
    atrasados_qtd=atrasados_qtd, atraso_med=atraso_med, intervalo_med=intervalo_med,
    chart_yoy=chart_yoy, pie=pie, nec_dia=nec_dia,
    metrics_orc=metrics_orc_final, ver_pedidos=ver_pedidos,
    pedidos_lista=pedidos_lista, top_entities=top_entities,
    )
    
    return render_page("Visão do Vendedor", body, "vendedores")


@app.route("/vendedor/<int:vendor_id>/comissao.xlsx")
@login_required
def vendedor_comissao_excel(vendor_id: int):
    """Exporta o extrato de comissão do vendedor, no mesmo ciclo da tela."""
    from crm_npd.services.relatorios import _xlsx_response, linhas_excel_extrato_comissao

    user = get_current_user()
    if user and user.get("role") not in ["admin", "gestor"]:
        with conectar() as _pc:
            _own_vendor = get_user_vendor_id(_pc, user)
        if _own_vendor and _own_vendor > 0 and int(vendor_id) != int(_own_vendor):
            flash("Acesso negado.", "warning")
            return redirect(url_for("vendedor", vendor_id=int(_own_vendor)))

    data_ref_str = request.args.get("data_ref", "")
    try:
        if data_ref_str:
            ref_date = datetime.strptime(data_ref_str, "%Y-%m").date()
        else:
            ref_date = date.today().replace(day=1)
    except Exception:
        ref_date = date.today().replace(day=1)

    mes_str = ref_date.strftime("%Y-%m")
    d0_fat, d1_fat = periodo_ciclo_25_24_que_contem_hoje(ref_date)
    d1_calc_fat = min(date.today(), d1_fat)

    with conectar() as conn:
        v = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not v:
            flash("Vendedor não encontrado.", "warning")
            return redirect(url_for("vendedores"))
        data_fin = compute_vendor_commission(conn, vendor_id, d0_fat, d1_calc_fat)

    nome = (v["nome"] or v["codigo"] or f"vendedor_{vendor_id}").strip()
    tipo = (v["tipo"] or "").strip() or "—"
    rows = linhas_excel_extrato_comissao(data_fin)
    metadata = [
        ("Vendedor", nome),
        ("Canal", tipo),
        ("Mês", mes_str),
        ("Ciclo", f"{d0_fat.strftime('%d/%m/%Y')} a {d1_fat.strftime('%d/%m/%Y')}"),
        ("Valor faturamento", float(data_fin.get("faturamento") or 0)),
        ("Valor comissão", float(data_fin.get("comissao") or 0)),
        ("Valor bônus 100 mil", float(data_fin.get("extra_bonus") or 0)),
        ("Valor devoluções", float(data_fin.get("devolucoes_valor") or 0)),
        ("Meta batida", "Sim" if data_fin.get("bateu_meta") else "Não"),
        ("Linhas novo", int(data_fin.get("novo_count") or 0)),
    ]
    filename = f"comissao_{v['codigo'] or vendor_id}_{mes_str}"
    return _xlsx_response(
        filename,
        f"CRM NPD · Extrato de comissão · {nome}",
        metadata,
        [{
            "name": "Extrato",
            "title": f"Extrato de comissão · {nome} · {d0_fat.strftime('%d/%m/%Y')} a {d1_fat.strftime('%d/%m/%Y')}",
            "headers": [
                "Data", "NF", "Pedido", "Cliente", "Identidade",
                "Status", "JIT IMEDIATO", "Valor base", "%", "Valor comissão",
                "Associação Nacional",
            ],
            "rows": rows,
        }],
    )


# -----------------------------
# Placeholders (sem quebrar)
# -----------------------------
@app.route("/clientes")
@login_required
def clientes():
    user = get_current_user()
    q = (request.args.get("q") or "").strip()
    page = max(1, safe_int(request.args.get("page"), 1))
    per_page = 120

    with conectar() as conn:
        # Consolida a carteira uma vez e pagina antes de montar o HTML.
        # Mantém identidade/score intactos, mas reduz bastante o peso da tela.
        all_rows = list_customer_identities_for_user(conn, user, q=q, limit=10000)
        total_rows = len(all_rows)
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        rows = all_rows[start:start + per_page]
        score_map = build_identity_score_map(conn, user=user)

        for r in rows:
            sc = score_map.get(r["identity_key"], {})
            r["score"] = sc.get("score", 0)
            r["score_faixa"] = sc.get("faixa", "D")
            r["score_label"] = sc.get("label", "Baixa prioridade")
            r["score_cor"] = sc.get("cor", "secondary")
            r["score_recompra"] = sc.get("score_recompra", 0)
            r["score_valor"] = sc.get("score_valor", 0)
            r["score_frequencia"] = sc.get("score_frequencia", 0)

            prio = get_task_priority_from_score(sc)
            r["prioridade"] = prio.get("prioridade", "BAIXA")
            r["prioridade_cor"] = prio.get("cor", "secondary")

            r["row_bg"] = soft_color_from_bootstrap(r["score_cor"])
            r["row_border"] = soft_border_from_bootstrap(r["score_cor"])

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <div class="h3 mb-1">Clientes</div>
        <div class="muted">
          Base comercial consolidada por identidade.
          {% if user["role"] in ["admin", "gestor"] %}
            Visão geral do CRM.
          {% else %}
            Sua carteira.
          {% endif %}
        </div>
      </div>
      <div class="d-flex gap-2">
        <a href="{{ url_for('medidas') }}" class="btn btn-outline-secondary">Medidas</a>
        <a href="{{ url_for('clientes_filiais') }}" class="btn btn-outline-primary">Filiais</a>
      </div>
    </div>

    <div class="cardx p-3 mb-3">
      <form method="get" class="row g-2 align-items-end">
        <div class="col-md-9">
          <label class="form-label small text-uppercase muted fw-bold">Buscar</label>
          <input type="text" name="q" class="form-control"
                 value="{{ q }}"
                 placeholder="Cliente, matriz, código...">
        </div>
        <div class="col-md-3 d-flex gap-2">
          <button class="btn btn-primary w-100">Aplicar</button>
          <a href="{{ url_for('clientes') }}" class="btn btn-light border w-100">Limpar</a>
        </div>
      </form>
    </div>

    <div class="cardx p-3">
      <div class="d-flex justify-content-between align-items-center mb-2 small muted">
        <span>{{ total_rows }} identidade(s) encontrada(s)</span>
        <span>Página {{ page }} de {{ total_pages }}</span>
      </div>
      <div class="table-responsive">
        <table class="table align-middle mb-0">
          <thead>
            <tr>
                <th>Tipo</th>
                <th>Identidade Comercial</th>
                <th class="text-center">Matriz</th>
                <th class="text-center">Filiais</th>
                <th class="text-center">Score</th>
                <th class="text-end">Ações</th>
            </tr>
          </thead>
          <tbody>
          {% for r in rows %}
            <tr style="background: {{ r['row_bg'] }}; border-left: 4px solid {{ r['row_border'] }};">
                <td>
                  <span class="badge rounded-pill
                    {% if r['identity_tipo'] == 'MATRIZ' %}text-bg-primary
                    {% elif r['identity_tipo'] == 'REDE' %}text-bg-secondary
                    {% elif r['identity_tipo'] == 'FILIAL' %}text-bg-info
                    {% else %}text-bg-light border text-dark
                    {% endif %}">
                    {{ r['identity_tipo'] }}
                  </span>
                </td>

                <td>
                  <div class="fw-semibold">{{ r['identity_nome'] }}</div>
                  {% if r['codigo'] %}
                    <div class="small text-muted">Código base: {{ r['codigo'] }}</div>
                  {% endif %}
                </td>

                <td class="text-center">
                  {% if r['matriz_nome'] %}
                    {{ r['matriz_nome'] }}
                  {% elif r['identity_tipo'] == 'MATRIZ' %}
                    {{ r['identity_nome'] }}
                  {% else %}
                    —
                  {% endif %}
                </td>

                <td class="text-center">{{ r['filiais_qtd'] or 0 }}</td>

                <td class="text-center">
                  <div class="fw-bold">{{ r['score'] }}</div>

                  <div class="d-flex flex-column align-items-center gap-1">
                    <span class="badge rounded-pill text-bg-{{ r['score_cor'] }}">
                      {{ r['score_faixa'] }} · {{ r['score_label'] }}
                    </span>

                    <span class="badge rounded-pill text-bg-{{ r['prioridade_cor'] }}">
                      {{ r['prioridade'] }}
                    </span>
                  </div>

                  <div class="small text-muted mt-1">
                    F{{ r['score_frequencia'] }} / R{{ r['score_recompra'] }} / V{{ r['score_valor'] }}
                  </div>
                </td>

                <td class="text-end">
                    <div class="d-flex justify-content-end gap-2">
                        <a href="{{ url_for('cliente_360', cliente_id=r['cliente_id']) }}" class="btn btn-sm btn-primary">
                            Perfil
                        </a>
                        <form method="post" action="{{ url_for('enviar_para_pendencias_importacao') }}" style="display:inline;">
                            <input type="hidden" name="customer_id" value="{{ r['cliente_id'] }}">
                            <input type="hidden" name="candidato_customer_id" value="{{ r['cliente_id'] if r['identity_tipo']=='MATRIZ' else '' }}">
                            <button class="btn btn-sm btn-outline-warning">Pendência</button>
                        </form>
                    </div>
                </td>
            </tr>
          {% else %}
            <tr><td colspan="6" class="muted text-center py-4">Nenhum cliente encontrado para esta carteira.</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>

      {% if total_pages > 1 %}
      <nav class="d-flex justify-content-center align-items-center gap-2 mt-3" aria-label="Paginação de clientes">
        <a class="btn btn-sm btn-outline-secondary {% if page <= 1 %}disabled{% endif %}"
           href="{{ url_for('clientes', q=q, page=page-1) }}">← Anterior</a>
        <span class="small muted">{{ total_rows }} resultados · {{ page }}/{{ total_pages }}</span>
        <a class="btn btn-sm btn-outline-secondary {% if page >= total_pages %}disabled{% endif %}"
           href="{{ url_for('clientes', q=q, page=page+1) }}">Próxima →</a>
      </nav>
      {% endif %}
    </div>
    """, rows=rows, q=q, user=user, page=page, total_pages=total_pages, total_rows=total_rows)

    return render_page("Clientes", body, "clientes")

@app.route("/clientes/filiais", methods=["GET"])
@login_required
def clientes_filiais():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("clientes"))

    with conectar() as conn:
        roots = conn.execute("""
            SELECT
                cir.identity_key,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN 1 ELSE 0 END) AS has_matriz,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN cir.customer_id ELSE 0 END) AS matriz_customer_id,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN COALESCE(c.fantasia, c.nome, '') ELSE '' END) AS matriz_nome,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN COALESCE(c.codigo, '') ELSE '' END) AS codigo,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN cir.identity_nome ELSE cir.identity_nome END) AS identity_nome,
                MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN 'MATRIZ' ELSE cir.tipo_vinculo END) AS root_tipo
            FROM customer_identity_rules cir
            LEFT JOIN customers c ON c.id = cir.customer_id
            WHERE cir.tipo_vinculo IN ('MATRIZ', 'REDE')
            GROUP BY cir.identity_key
            ORDER BY identity_nome ASC
        """).fetchall()

        rows = []
        for root in roots:
            identity_key = (root["identity_key"] or "").strip()
            has_matriz = int(root["has_matriz"] or 0) == 1
            matriz_customer_id = int(root["matriz_customer_id"] or 0)
            filiais = conn.execute("""
                SELECT
                    cir.customer_id,
                    COALESCE(c.codigo, '') AS codigo,
                    COALESCE(c.fantasia, c.nome, '') AS nome_base,
                    cir.tipo_vinculo
                FROM customer_identity_rules cir
                LEFT JOIN customers c ON c.id = cir.customer_id
                WHERE cir.identity_key = ?
                  AND (
                        cir.tipo_vinculo = 'FILIAL'
                        OR (cir.tipo_vinculo = 'REDE')
                        OR (cir.tipo_vinculo = 'MATRIZ' AND cir.customer_id <> ?)
                  )
                ORDER BY COALESCE(c.fantasia, c.nome, '') ASC
            """, (identity_key, matriz_customer_id)).fetchall()

            matriz_nome = (root["matriz_nome"] or '').strip()
            if not matriz_customer_id:
                matriz_customer_id, inferred_nome = infer_identity_matrix_candidate(conn, identity_key, (root["identity_nome"] or '').strip())
                if inferred_nome:
                    matriz_nome = inferred_nome

            if has_matriz:
                root_tipo = 'MATRIZ'
                nome_exibicao = (matriz_nome or root["identity_nome"] or '').strip()
            else:
                root_tipo = 'REDE'
                nome_exibicao = (root["identity_nome"] or '').strip()

            rows.append({
                "identity_key": identity_key,
                "identity_nome": nome_exibicao,
                "codigo": (root["codigo"] or '').strip(),
                "matriz_nome": matriz_nome,
                "filiais": filiais,
                "filiais_qtd": len(filiais),
                "matriz_customer_id": matriz_customer_id,
                "root_tipo": root_tipo,
            })

        pendentes = conn.execute("""
            SELECT COUNT(*) AS qtd
            FROM import_identity_pending
            WHERE status = 'PENDENTE'
        """).fetchone()["qtd"]

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <div class="h3 mb-1">Filiais</div>
        <div class="muted">Validação por JIT, IMEDIATA e produto/fantasia. Redes sem JIT também aparecem aqui para não ficar tudo solto.</div>
      </div>
      <div class="d-flex gap-2">
        <a href="{{ url_for('mesclar_matriz_cliente') }}" class="btn btn-outline-primary">Mesclar grupo/matriz</a>
        <a href="{{ url_for('clientes') }}" class="btn btn-light border">Voltar</a>
      </div>
    </div>

    <div class="cardx p-3">
      {% if rows %}
        <div class="table-responsive">
          <table class="table align-middle mb-0">
            <thead>
              <tr>
                <th>Grupo / Matriz</th>
                <th class="text-center">Código</th>
                <th class="text-center">Filiais</th>
                <th>Lista</th>
              </tr>
            </thead>
            <tbody>
              {% for r in rows %}
              <tr>
                <td>
                  <div class="fw-semibold d-flex align-items-center gap-2 flex-wrap">
                    <span>{{ r['identity_nome'] }}</span>
                    {% if r['root_tipo'] == 'MATRIZ' %}
                      <span class="badge text-bg-primary">Matriz</span>
                    {% else %}
                      <span class="badge text-bg-secondary">Rede</span>
                    {% endif %}
                    {% if r['matriz_customer_id'] %}
                    <form method="post" action="{{ url_for('enviar_para_pendencias_importacao') }}" style="display:inline;">
                      <input type="hidden" name="customer_id" value="{{ r['matriz_customer_id'] }}">
                      <button class="btn btn-sm btn-outline-warning">Editar</button>
                    </form>
                    {% endif %}
                    <a href="{{ url_for('mesclar_matriz_cliente', source_identity_key=r['identity_key']) }}" class="btn btn-sm btn-outline-primary">Mesclar</a>
                  </div>
                  {% if r['root_tipo'] == 'REDE' %}
                    <div class="small text-muted mt-1">Sem matriz JIT definida ainda.</div>
                  {% endif %}
                </td>
                <td class="text-center">{{ r['codigo'] or '—' }}</td>
                <td class="text-center">{{ r['filiais_qtd'] }}</td>
                <td>
                  {% if r['filiais'] %}
                    {% for f in r['filiais'] %}
                      <div class="d-inline-flex align-items-center gap-1 me-1 mb-1">
                        <span class="badge text-bg-light border text-dark">
                          {{ f['nome_base'] }}{% if f['codigo'] %} · {{ f['codigo'] }}{% endif %}
                        </span>
                        <form method="post" action="{{ url_for('desvincular_filial_cliente') }}" style="display:inline;" onsubmit="return confirm('Remover este cliente desta rede/matriz? Ele voltará como cliente individual e você poderá classificar depois.');">
                          <input type="hidden" name="customer_id" value="{{ f['customer_id'] }}">
                          <input type="hidden" name="identity_key" value="{{ r['identity_key'] }}">
                          <button class="btn btn-sm btn-outline-danger py-0 px-2">Remover</button>
                        </form>
                      </div>
                    {% endfor %}
                  {% else %}
                    <span class="text-muted">Nenhuma filial vinculada.</span>
                  {% endif %}
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      {% else %}
        <div class="alert alert-light border mb-0">
          Nenhuma rede identificada ainda.
        </div>
      {% endif %}
    </div>
    """, rows=rows, pendentes=pendentes)

    return render_page("Filiais", body, "clientes")


@app.route("/clientes/filiais/desvincular", methods=["POST"])
@login_required
def desvincular_filial_cliente():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("clientes_filiais"))

    customer_id = int(request.form.get("customer_id") or 0)
    identity_key = (request.form.get("identity_key") or "").strip()
    if customer_id <= 0:
        flash("Cliente inválido.", "warning")
        return redirect(url_for("clientes_filiais"))

    try:
        with conectar() as conn:
            row = conn.execute("""
                SELECT COALESCE(c.fantasia, c.nome, '') AS nome_base, COALESCE(c.codigo, '') AS codigo
                FROM customers c
                WHERE c.id = ?
                LIMIT 1
            """, (customer_id,)).fetchone()
            nome_base = (row["nome_base"] or f"Cliente {customer_id}").strip() if row else f"Cliente {customer_id}"
            codigo = (row["codigo"] or '').strip() if row else ''

            conn.execute("""
                INSERT OR REPLACE INTO customer_identity_rules (
                    customer_id, identity_key, identity_nome, tipo_vinculo,
                    matriz_customer_id, origem, observacao, updated_at
                ) VALUES (?, ?, ?, 'CLIENTE', 0, 'MANUAL', ?, CURRENT_TIMESTAMP)
            """, (
                customer_id,
                f"CLIENTE:{customer_id}",
                nome_base,
                f"Desvinculado manualmente de {identity_key or 'grupo'}"
            ))
            conn.execute("UPDATE customers SET rede_key = '', rede_nome = '' WHERE id = ?", (customer_id,))
            conn.commit()

        flash(f"Cliente {nome_base}{(' · ' + codigo) if codigo else ''} removido da rede/matriz.", "success")
    except Exception as e:
        flash(f"Erro ao remover vínculo: {e}", "danger")

    return redirect(url_for("clientes_filiais"))

@app.route("/clientes/filiais/mesclar", methods=["GET", "POST"])
@login_required
def mesclar_matriz_cliente():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("clientes_filiais"))

    source_identity_key = (request.values.get("source_identity_key") or "").strip()
    source_customer_id = int(request.values.get("source_customer_id") or 0)

    try:
        with conectar() as conn:
            if source_customer_id > 0 and not source_identity_key:
                row_src = conn.execute("SELECT identity_key FROM customer_identity_rules WHERE customer_id=? LIMIT 1", (source_customer_id,)).fetchone()
                if row_src:
                    source_identity_key = (row_src["identity_key"] or "").strip()

            if request.method == "POST":
                source_identity_key = (request.form.get("source_identity_key") or "").strip()
                target_identity_key = (request.form.get("target_identity_key") or "").strip()

                if not source_identity_key or not target_identity_key or source_identity_key == target_identity_key:
                    flash("Selecione dois grupos diferentes para mesclar.", "warning")
                    return redirect(url_for("mesclar_matriz_cliente", source_identity_key=source_identity_key))

                source_root = conn.execute("""
                    SELECT customer_id, identity_key, identity_nome, tipo_vinculo, COALESCE(matriz_customer_id, 0) AS matriz_customer_id
                    FROM customer_identity_rules
                    WHERE identity_key = ?
                    ORDER BY CASE WHEN tipo_vinculo = 'MATRIZ' THEN 0 WHEN tipo_vinculo = 'REDE' THEN 1 ELSE 2 END, customer_id
                    LIMIT 1
                """, (source_identity_key,)).fetchone()
                target_root = conn.execute("""
                    SELECT customer_id, identity_key, identity_nome, tipo_vinculo, COALESCE(matriz_customer_id, 0) AS matriz_customer_id
                    FROM customer_identity_rules
                    WHERE identity_key = ?
                    ORDER BY CASE WHEN tipo_vinculo = 'MATRIZ' THEN 0 WHEN tipo_vinculo = 'REDE' THEN 1 ELSE 2 END, customer_id
                    LIMIT 1
                """, (target_identity_key,)).fetchone()

                if not source_root or not target_root:
                    flash("Origem ou destino não encontrados.", "danger")
                    return redirect(url_for("clientes_filiais"))

                target_type = (target_root["tipo_vinculo"] or "").strip().upper()
                target_name = (target_root["identity_nome"] or "").strip()
                target_matrix_id = int(target_root["customer_id"] if target_type == 'MATRIZ' else (target_root["matriz_customer_id"] or 0))

                source_members = conn.execute("""
                    SELECT customer_id
                    FROM customer_identity_rules
                    WHERE identity_key = ?
                """, (source_identity_key,)).fetchall()

                for member in source_members:
                    cid = int(member["customer_id"] or 0)
                    if cid <= 0:
                        continue
                    if target_type == 'MATRIZ':
                        new_tipo = 'FILIAL'
                        new_matriz_id = target_matrix_id
                    else:
                        new_tipo = 'REDE'
                        new_matriz_id = 0

                    conn.execute("""
                        UPDATE customer_identity_rules
                        SET identity_key = ?,
                            identity_nome = ?,
                            tipo_vinculo = ?,
                            matriz_customer_id = ?,
                            origem = 'MANUAL',
                            observacao = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE customer_id = ?
                    """, (
                        target_identity_key,
                        target_name,
                        new_tipo,
                        new_matriz_id,
                        f"Mesclado manualmente em {target_name}",
                        cid,
                    ))

                if target_matrix_id > 0:
                    conn.execute("""
                        UPDATE import_identity_pending
                        SET candidato_customer_id = ?,
                            candidato_nome = ?
                        WHERE status = 'PENDENTE'
                          AND (candidato_customer_id = 0 OR candidato_customer_id IN (
                              SELECT customer_id FROM customer_identity_rules WHERE identity_key = ?
                          ))
                    """, (target_matrix_id, target_name, source_identity_key))

                conn.commit()
                flash("Mesclagem concluída com sucesso.", "success")
                return redirect(url_for("clientes_filiais"))

            identities = conn.execute("""
                SELECT
                    cir.identity_key,
                    MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN 1 ELSE 0 END) AS has_matriz,
                    MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN COALESCE(c.fantasia, c.nome, '') ELSE '' END) AS matriz_nome,
                    MAX(CASE WHEN cir.tipo_vinculo = 'MATRIZ' THEN COALESCE(c.codigo, '') ELSE '' END) AS codigo,
                    MAX(cir.identity_nome) AS identity_nome,
                    COUNT(*) AS membros
                FROM customer_identity_rules cir
                LEFT JOIN customers c ON c.id = cir.customer_id
                WHERE cir.tipo_vinculo IN ('MATRIZ', 'REDE', 'FILIAL')
                GROUP BY cir.identity_key
                HAVING COUNT(*) > 0
                ORDER BY MAX(cir.identity_nome) ASC
            """).fetchall()

            source_nome = ""
            if source_identity_key:
                row = conn.execute("""
                    SELECT MAX(identity_nome) AS identity_nome
                    FROM customer_identity_rules
                    WHERE identity_key = ?
                """, (source_identity_key,)).fetchone()
                if row:
                    source_nome = (row["identity_nome"] or "").strip()

        body = render_template_string(r"""
        <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
          <div>
            <div class="h3 mb-1">Mesclar grupo / matriz</div>
            <div class="muted">Use quando a mesma rede tiver sido dividida em dois grupos ou duas matrizes separadas por engano.</div>
          </div>
          <div class="d-flex gap-2">
            <a href="{{ url_for('clientes_filiais') }}" class="btn btn-light border">Voltar</a>
          </div>
        </div>

        <div class="cardx p-3">
          <form method="post" class="row g-3">
            <div class="col-12 col-lg-6">
              <label class="form-label fw-bold">Origem</label>
              <select name="source_identity_key" class="form-select" required>
                <option value="">Selecione</option>
                {% for m in identities %}
                  <option value="{{ m['identity_key'] }}" {{ 'selected' if m['identity_key'] == source_identity_key else '' }}>
                    {{ m['matriz_nome'] or m['identity_nome'] }}{% if m['has_matriz'] %} · Matriz{% else %} · Rede{% endif %} · {{ m['membros'] }} membros
                  </option>
                {% endfor %}
              </select>
              {% if source_nome %}
                <div class="small text-muted mt-2">Origem selecionada: <b>{{ source_nome }}</b></div>
              {% endif %}
            </div>

            <div class="col-12 col-lg-6">
              <label class="form-label fw-bold">Destino</label>
              <select name="target_identity_key" class="form-select" required>
                <option value="">Selecione</option>
                {% for m in identities %}
                  {% if m['identity_key'] != source_identity_key %}
                  <option value="{{ m['identity_key'] }}">
                    {{ m['matriz_nome'] or m['identity_nome'] }}{% if m['has_matriz'] %} · Matriz{% else %} · Rede{% endif %} · {{ m['membros'] }} membros
                  </option>
                  {% endif %}
                {% endfor %}
              </select>
            </div>

            <div class="col-12">
              <div class="alert alert-warning mb-0">
                Todos os clientes do grupo de origem serão movidos para o grupo de destino. Se o destino tiver matriz, a origem passa a ser tratada como filial manual dessa matriz.
              </div>
            </div>

            <div class="col-12 d-flex gap-2">
              <button class="btn btn-primary">Mesclar agora</button>
              <a href="{{ url_for('clientes_filiais') }}" class="btn btn-light border">Cancelar</a>
            </div>
          </form>
        </div>
        """, identities=identities, source_identity_key=source_identity_key, source_nome=source_nome)

        return render_page("Mesclar grupo / matriz", body, "clientes")
    except Exception as e:
        flash(f"Erro ao mesclar grupos/matrizes: {e}", "danger")
        return redirect(url_for("clientes_filiais"))


def get_identity_measure_stats(conn, customer_ids: List[int], limit: int = 12):
    customer_ids = [int(x) for x in (customer_ids or []) if int(x or 0) > 0]
    if not customer_ids:
        return {
            "rows": [],
            "top_measure": "-",
            "top_share": 0.0,
            "total_orders": 0,
            "total_value": 0.0,
            "chart": None,
        }

    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA') AS medida,
            COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd_pedidos,
            ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)), 2) AS valor_total
        FROM orders o
        LEFT JOIN vendors v ON v.id = o.vendor_id
        WHERE o.cliente_id IN ({placeholders})
          AND COALESCE(TRIM(o.medida_ref), '') <> ''
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA')
        ORDER BY COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) DESC, SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) DESC, medida ASC
    """, customer_ids).fetchall()

    total_orders = int(sum(int(r["qtd_pedidos"] or 0) for r in rows))
    total_value = float(sum(float(r["valor_total"] or 0) for r in rows))
    out = []
    for r in rows:
        qtd = int(r["qtd_pedidos"] or 0)
        val = float(r["valor_total"] or 0)
        share = round((qtd / total_orders) * 100, 1) if total_orders else 0.0
        out.append({
            "medida": (r["medida"] or "-").strip(),
            "qtd_pedidos": qtd,
            "valor_total": val,
            "share": share,
        })

    chart = None
    top_chart_rows = out[: min(limit, 8)]
    if top_chart_rows:
        chart = bar_chart(
            [x["medida"][:18] for x in top_chart_rows],
            [x["qtd_pedidos"] for x in top_chart_rows],
            "Pedidos por Medida"
        )

    top_measure = out[0]["medida"] if out else "-"
    top_share = out[0]["share"] if out else 0.0
    return {
        "rows": out[:limit],
        "top_measure": top_measure,
        "top_share": top_share,
        "total_orders": total_orders,
        "total_value": total_value,
        "chart": chart,
    }


@app.post("/cliente-status/<path:identity_key>")
@login_required
def cliente_status_update(identity_key):
    user = get_current_user()
    destino = (request.form.get("next") or request.referrer or url_for("dashboard")).strip()
    status_novo = (request.form.get("status_novo") or "").strip().upper()
    identity_nome = (request.form.get("identity_nome") or identity_key).strip()

    try:
        with conectar() as conn:
            set_manual_customer_status(conn, identity_key, identity_nome, status_novo, user)
            conn.commit()
        flash(f"Status do cliente atualizado para {status_novo}.", "success")
    except PermissionError as e:
        flash(str(e), "warning")
    except Exception as e:
        flash(f"Erro ao atualizar status do cliente: {e}", "danger")
    return redirect(destino)


@app.route("/admin/historico-status-clientes")
@login_required
def admin_historico_status_clientes():
    user = get_current_user()
    if not user or user.get("role") != "admin":
        flash("Acesso restrito ao administrador.", "warning")
        return redirect(url_for("dashboard"))

    with conectar() as conn:
        rows = conn.execute("""
            SELECT identity_nome, identity_key, status_anterior, status_novo, changed_by, changed_role, created_at
            FROM customer_status_history
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 300
        """).fetchall()

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <div class="h3 mb-1">Auditoria Novo / Carteira</div>
        <div class="muted">Registra somente alterações manuais da classificação automática de clientes. Não é histórico de pedidos nem de funil.</div>
      </div>
      <a href="{{ url_for('dashboard') }}" class="btn btn-outline-secondary">Voltar</a>
    </div>
    <div class="alert alert-light border small">
      <b>Como ler:</b> o CRM calcula <b>NOVO</b> ou <b>CARTEIRA</b> automaticamente. Quando alguém força essa classificação pelo menu de contexto, a mudança aparece aqui.
      <b>Automático</b> em “Classificação anterior” significa que ainda não existia nenhuma substituição manual para aquele cliente/rede.
    </div>
    <div class="cardx p-3">
      <div class="table-responsive">
        <table class="table table-sm align-middle">
          <thead><tr><th>Data</th><th>Cliente / Rede</th><th>Classificação anterior</th><th>Nova classificação</th><th>Alterado por</th><th>Perfil</th></tr></thead>
          <tbody>
            {% for r in rows %}
            <tr>
              <td class="text-nowrap">{{ r['created_at'] or '-' }}</td>
              <td><div class="fw-semibold">{{ r['identity_nome'] or '-' }}</div><div class="small text-muted">Identidade interna: {{ r['identity_key'] or '-' }}</div></td>
              <td>
                {% if not r['status_anterior'] %}<span class="badge text-bg-light border text-dark">Automático</span>
                {% elif r['status_anterior'] == 'RECOMPRA' %}<span class="badge text-bg-secondary">CARTEIRA</span>
                {% else %}<span class="badge text-bg-primary">{{ r['status_anterior'] }}</span>{% endif %}
              </td>
              <td>
                {% if r['status_novo'] == 'RECOMPRA' %}<span class="badge text-bg-secondary">CARTEIRA</span>
                {% else %}<span class="badge text-bg-primary">{{ r['status_novo'] }}</span>{% endif %}
              </td>
              <td>{{ r['changed_by'] or '-' }}</td>
              <td>{{ r['changed_role'] or '-' }}</td>
            </tr>
            {% else %}<tr><td colspan="6" class="text-center muted py-3">Nenhuma classificação foi alterada manualmente.</td></tr>{% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    """, rows=rows)
    return render_page("Auditoria Novo / Carteira", body, "dashboard")


@app.route("/medidas")
@login_required
def medidas():
    user = get_current_user()
    q = (request.args.get("q") or "").strip()
    q_norm = normalize_measure_text(q)
    cliente_id = safe_int(request.args.get("cliente_id"))
    identity_key_filter = (request.args.get("identity_key") or "").strip()
    origem_nome = (request.args.get("origem_nome") or "").strip()

    with conectar() as conn:
        params = []
        where = ["COALESCE(TRIM(o.medida_ref), '') <> ''"]

        customer_ids_scope = []
        if identity_key_filter:
            customer_ids_scope = get_customer_ids_by_identity(conn, identity_key_filter)
        elif cliente_id:
            identity_key_scope = get_customer_identity_key(conn, cliente_id)
            customer_ids_scope = get_customer_ids_by_identity(conn, identity_key_scope) if identity_key_scope else [cliente_id]
            if not customer_ids_scope:
                customer_ids_scope = [cliente_id]
        if customer_ids_scope:
            placeholders_scope = ",".join("?" for _ in customer_ids_scope)
            where.append(f"o.cliente_id IN ({placeholders_scope})")
            params.extend(customer_ids_scope)

        if q:
            where.append("(LOWER(COALESCE(o.medida_ref, '')) LIKE ? OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(o.medida_ref,'')), ' ', ''), 'x', ''), '.', '') LIKE ?)")
            params.extend([f"%{q.lower()}%", f"%{q_norm.replace('x','').replace('.','')}%"])
        vendor_id = None
        if user and user.get("role") not in ["admin", "gestor"]:
            vendor_id = get_user_vendor_id(conn, user)
            if vendor_id:
                where.append("o.vendor_id = ?")
                params.append(vendor_id)

        where_sql = " AND ".join(where)
        rows = conn.execute(f"""
            SELECT
                c.id AS cliente_id,
                COALESCE(c.fantasia, c.nome, '') AS cliente_nome,
                COALESCE(c.codigo, '') AS codigo,
                COALESCE(MAX(c.rede_nome), '') AS rede_nome,
                COALESCE(MAX(c.rede_key), '') AS rede_key,
                COALESCE(MAX(o.medida_ref), '') AS medida_exemplo,
                COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd_pedidos,
                ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)), 2) AS valor_total
            FROM orders o
            JOIN customers c ON c.id = o.cliente_id
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE {where_sql}
            GROUP BY c.id, COALESCE(c.fantasia, c.nome, ''), COALESCE(c.codigo, '')
            ORDER BY COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) DESC, SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) DESC, cliente_nome ASC
            LIMIT 400
        """, params).fetchall()

        total_pedidos = int(sum(int(r["qtd_pedidos"] or 0) for r in rows))
        out = []
        for r in rows:
            qtd = int(r["qtd_pedidos"] or 0)
            pct = round((qtd / total_pedidos) * 100, 1) if total_pedidos else 0.0
            top = conn.execute("""
                SELECT COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA') AS medida, COUNT(*) AS qtd
                FROM orders o
                WHERE o.cliente_id = ? AND COALESCE(TRIM(o.medida_ref), '') <> ''
                GROUP BY COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA')
                ORDER BY COUNT(*) DESC, medida ASC
                LIMIT 1
            """, (int(r["cliente_id"]),)).fetchone()
            out.append({
                "cliente_id": int(r["cliente_id"]),
                "cliente_nome": r["cliente_nome"],
                "codigo": r["codigo"],
                "rede_nome": r["rede_nome"],
                "medida_exemplo": r["medida_exemplo"],
                "qtd_pedidos": qtd,
                "valor_total": float(r["valor_total"] or 0),
                "pct": pct,
                "medida_top": (top["medida"] if top else "-"),
            })

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <div class="h3 mb-1">Medidas</div>
        <div class="muted">Pesquise uma medida e veja quais clientes compram esse formato.</div>
        {% if origem_nome %}<div class="small text-primary mt-1">Análise rápida de: <strong>{{ origem_nome }}</strong></div>{% endif %}
      </div>
      {% if cliente_id or identity_key_filter %}
        <a href="{{ url_for('clientes') }}" class="btn btn-outline-secondary">Voltar aos clientes</a>
      {% endif %}
    </div>

    <div class="cardx p-3 mb-3">
      <form method="get" class="row g-2 align-items-end">
        <input type="hidden" name="cliente_id" value="{{ cliente_id or '' }}">
        <input type="hidden" name="identity_key" value="{{ identity_key_filter or '' }}">
        <input type="hidden" name="origem_nome" value="{{ origem_nome or '' }}">
        <div class="col-md-9">
          <label class="form-label small text-uppercase muted fw-bold">Buscar medida</label>
          <input type="text" name="q" class="form-control" value="{{ q }}" placeholder="Ex.: 31x31x18">
        </div>
        <div class="col-md-3 d-flex gap-2">
          <button class="btn btn-primary w-100">Aplicar</button>
          <a href="{{ url_for('medidas_exportar', q=q, cliente_id=cliente_id or '', identity_key=identity_key_filter or '', origem_nome=origem_nome or '') }}" class="btn btn-success w-100">Exportar Excel</a>
          <a href="{{ url_for('medidas', cliente_id=cliente_id, identity_key=identity_key_filter, origem_nome=origem_nome) if (cliente_id or identity_key_filter) else url_for('medidas') }}" class="btn btn-light border w-100">Limpar</a>
        </div>
      </form>
    </div>

    <div class="cardx p-3">
      <div class="table-responsive">
        <table class="table align-middle mb-0">
          <thead>
            <tr>
              <th>Cliente</th>
              <th>Rede</th>
              <th>Medida buscada</th>
              <th class="text-center">Pedidos</th>
              <th class="text-center">% no resultado</th>
              <th>Medida mais comprada</th>
              <th class="text-end">Ações</th>
            </tr>
          </thead>
          <tbody>
          {% for r in rows %}
            <tr>
              <td>
                <div class="fw-semibold">{{ r.cliente_nome }}</div>
                {% if r.codigo %}<div class="small text-muted">Código {{ r.codigo }}</div>{% endif %}
              </td>
              <td>{{ r.rede_nome or '—' }}</td>
              <td>{{ r.medida_exemplo }}</td>
              <td class="text-center">{{ r.qtd_pedidos }}</td>
              <td class="text-center">{{ r.pct }}%</td>
              <td>{{ r.medida_top }}</td>
              <td class="text-end"><a href="{{ url_for('cliente_360', cliente_id=r.cliente_id) }}" class="btn btn-sm btn-primary">Perfil</a></td>
            </tr>
          {% else %}
            <tr><td colspan="7" class="muted text-center py-4">Nenhum cliente encontrado para esta medida.</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    """, q=q, rows=out, cliente_id=cliente_id, identity_key_filter=identity_key_filter, origem_nome=origem_nome)

    return render_page("Medidas", body, "medidas")


@app.route("/medidas/exportar")
@login_required
def medidas_exportar():
    user = get_current_user()
    q = (request.args.get("q") or "").strip()
    q_norm = normalize_measure_text(q)
    cliente_id = safe_int(request.args.get("cliente_id"))
    identity_key_filter = (request.args.get("identity_key") or "").strip()

    with conectar() as conn:
        params = []
        where = ["COALESCE(TRIM(o.medida_ref), '') <> ''", sql_pedidos_realizados_excel_cond('o','v')]
        customer_ids_scope = []
        if identity_key_filter:
            customer_ids_scope = get_customer_ids_by_identity(conn, identity_key_filter)
        elif cliente_id:
            identity_key_scope = get_customer_identity_key(conn, cliente_id)
            customer_ids_scope = get_customer_ids_by_identity(conn, identity_key_scope) if identity_key_scope else [cliente_id]
            if not customer_ids_scope:
                customer_ids_scope = [cliente_id]
        if customer_ids_scope:
            placeholders_scope = ",".join("?" for _ in customer_ids_scope)
            where.append(f"o.cliente_id IN ({placeholders_scope})")
            params.extend(customer_ids_scope)
        if q:
            where.append("(LOWER(COALESCE(o.medida_ref, '')) LIKE ? OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(o.medida_ref,'')), ' ', ''), 'x', ''), '.', '') LIKE ?)")
            params.extend([f"%{q.lower()}%", f"%{q_norm.replace('x','').replace('.','')}%"] )
        if user and user.get("role") not in ["admin", "gestor"]:
            vendor_id = get_user_vendor_id(conn, user)
            if vendor_id:
                where.append("o.vendor_id = ?")
                params.append(vendor_id)
        where_sql = " AND ".join(where)
        rows = conn.execute(f"""
            SELECT
                c.id AS cliente_id,
                COALESCE(c.fantasia, c.nome, '') AS cliente_nome,
                COALESCE(c.codigo, '') AS codigo,
                COALESCE(MAX(c.rede_nome), '') AS rede_nome,
                COALESCE(MAX(o.medida_ref), '') AS medida_exemplo,
                COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS qtd_pedidos,
                ROUND(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)), 2) AS valor_total
            FROM orders o
            JOIN customers c ON c.id = o.cliente_id
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE {where_sql}
            GROUP BY c.id, COALESCE(c.fantasia, c.nome, ''), COALESCE(c.codigo, '')
            ORDER BY COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) DESC, SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)) DESC, cliente_nome ASC
            LIMIT 5000
        """, params).fetchall()

        total_pedidos = int(sum(int(r["qtd_pedidos"] or 0) for r in rows))
        export_rows = []
        for r in rows:
            top = conn.execute("""
                SELECT COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA') AS medida, COUNT(*) AS qtd
                FROM orders o
                WHERE o.cliente_id = ? AND COALESCE(TRIM(o.medida_ref), '') <> ''
                GROUP BY COALESCE(NULLIF(TRIM(o.medida_ref), ''), 'SEM MEDIDA')
                ORDER BY COUNT(*) DESC, medida ASC
                LIMIT 1
            """, (int(r["cliente_id"]),)).fetchone()
            qtd = int(r["qtd_pedidos"] or 0)
            export_rows.append([
                r["cliente_nome"], r["codigo"], r["rede_nome"], r["medida_exemplo"],
                qtd, round((qtd / total_pedidos) * 100, 1) if total_pedidos else 0,
                top["medida"] if top else "-", float(r["valor_total"] or 0)
            ])

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except Exception:
        flash("Para exportar Excel, instale a biblioteca openpyxl: pip install openpyxl", "danger")
        return redirect(url_for("medidas", q=q, cliente_id=cliente_id or "", identity_key=identity_key_filter or ""))

    wb = Workbook()
    ws = wb.active
    ws.title = "Medidas"
    headers = ["Cliente", "Código", "Rede", "Medida filtrada", "Pedidos", "% no resultado", "Medida mais comprada", "Valor total"]
    ws.append(headers)
    for row in export_rows:
        ws.append(row)
    header_fill = PatternFill("solid", fgColor="0D6EFD")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    widths = [34, 14, 24, 22, 12, 16, 24, 16]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=8):
        row[0].number_format = '0'
        row[1].number_format = '0.0'
        row[3].number_format = 'R$ #,##0.00'
    ws.freeze_panes = "A2"
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    nome = f"medidas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(bio, as_attachment=True, download_name=nome, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/cliente/<int:cliente_id>")
@login_required
def cliente_360(cliente_id):
        with conectar() as conn:
            cliente = conn.execute("""
                SELECT id, nome, codigo
                FROM customers
                WHERE id=?
            """, (cliente_id,)).fetchone()
    
            if not cliente:
                flash("Cliente não encontrado.", "warning")
                return redirect(url_for('clientes'))
    
            # -----------------------------
            # Identidade comercial consolidada
            # -----------------------------
            identity_key = get_customer_identity_key(conn, cliente_id)
            identity_nome = get_identity_display_name(conn, identity_key, cliente_id)
    
            customer_ids = get_customer_ids_by_identity(conn, identity_key)
            if not customer_ids:
                customer_ids = [cliente_id]
    
            placeholders = ",".join("?" for _ in customer_ids)
    
            # -----------------------------
            # Métricas consolidadas
            # -----------------------------
            metrics = cliente_metrics(conn, cliente_id)
    
            # Complementos comerciais do score
            score_map = build_identity_score_map(conn, customer_ids_scope=customer_ids)
            score_info = enrich_identity_score_for_ui(score_map.get(identity_key, {}))
    
            # -----------------------------
            # Tarefas consolidadas da identidade
            # -----------------------------
            tarefas = conn.execute(f"""
                SELECT
                    t.status,
                    t.descricao,
                    v.nome AS vendedor,
                    t.criada_por AS origem,
                    t.titulo,
                    COALESCE(t.identity_key, '') AS identity_key
                FROM tasks t
                LEFT JOIN vendors v ON v.id=t.vendor_id
                WHERE
                    t.cliente_id IN ({placeholders})
                    OR COALESCE(t.identity_key, '') = ?
                ORDER BY t.created_at DESC
            """, [*customer_ids, identity_key]).fetchall()
    
            # -----------------------------
            # Histórico consolidado (últimas 20 NFs)
            # -----------------------------
            historico = conn.execute(f"""
                SELECT
                    i.data_emissao,
                    i.numero AS pedido,
                    i.valor,
                    v.nome AS vendedor
                FROM invoices i
                LEFT JOIN vendors v ON v.id=i.vendor_id
                WHERE i.cliente_id IN ({placeholders})
                ORDER BY date(i.data_emissao) DESC
                LIMIT 20
            """, customer_ids).fetchall()
    
            # -----------------------------
            # Medidas do cliente / rede
            # -----------------------------
            measure_stats = get_identity_measure_stats(conn, customer_ids, limit=12)

            # -----------------------------
            # Gráfico de recompra
            # -----------------------------
            hist_asc = sorted(historico, key=lambda x: x['data_emissao'])
            labels = [h['data_emissao'][5:] for h in hist_asc if h['data_emissao']]
            values = [float(h['valor'] or 0) for h in hist_asc if h['data_emissao']]
    
            grafico_recompra = None
            if values:
                grafico_recompra = line_chart(labels, [("Compras", values)], "Padrão de Recompra (Últimas 20)")
    
            # --- HTML ---
            body_html = render_template_string(r"""
    <div class="page-hero mb-4">
      <div class="d-flex align-items-center justify-content-between flex-wrap gap-3 position-relative" style="z-index:1">
        <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Cliente 360</div><div class="hero-title">{{ identity_nome }}</div><div class="hero-sub mt-1">Visão consolidada da identidade comercial · matriz + filiais</div>
          <div class="d-flex flex-wrap gap-2 mt-3">
            <span class="badge rounded-pill text-bg-{{ score_info['cor'] }}">
              Score {{ score_info['score'] }} · {{ score_info['faixa'] }} · {{ score_info['label'] }}
            </span>
    
            <span class="badge rounded-pill text-bg-{{ score_info['prioridade_cor'] }}">
              Prioridade {{ score_info['prioridade'] }}
            </span>
    
            {% if metrics.transferido %}
              <span class="badge bg-info">Transferido</span>
            {% endif %}
            {% if metrics.novo_empresa %}
              <span class="badge bg-success">Novo da Empresa</span>
            {% endif %}
          </div>
    
          <div class="hero-sub mt-2">{{ score_info['prioridade_motivo'] }}</div>
        </div>
        <div class="d-flex flex-wrap gap-2"><a href="{{ url_for('medidas', cliente_id=cliente_id, identity_key=identity_key, origem_nome=identity_nome) }}" class="btn btn-light">Ver medidas</a><a href="{{ url_for('clientes') }}" class="btn btn-outline-light">Voltar</a></div>
      </div>
    </div>

    <div class="row mb-4 g-3">
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Status</small><br>
          <div class="mt-1 fw-bold">
            {% if metrics.status == "ATIVO" %}<span class="text-success">🟢 Ativo</span>
            {% elif metrics.status == "EM RISCO" %}<span class="text-warning">🟡 Em risco</span>
            {% elif metrics.status == "INATIVO" %}<span class="text-danger">🔴 Inativo</span>
            {% else %}{{ metrics.status }}{% endif %}
          </div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Score Cliente</small><br>
          <div class="mt-1 h5 mb-0">{{ score_info['score'] }}</div>
          <div class="small text-muted">{{ score_info['faixa'] }} · {{ score_info['label'] }}</div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Prioridade</small><br>
          <div class="mt-1">
            <span class="badge rounded-pill text-bg-{{ score_info['prioridade_cor'] }}">
              {{ score_info['prioridade'] }}
            </span>
          </div>
          <div class="small text-muted mt-1">{{ score_info['prioridade_motivo'] }}</div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Fat. (Ciclo Atual)</small><br>
          <div class="mt-1 h5 mb-0">{{ format_brl(metrics.total_ciclo) }}</div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Notas (Ciclo)</small><br>
          <div class="mt-1 h5 mb-0">{{ metrics.pedidos_ciclo }}</div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Dias s/ compra</small><br>
          <div class="mt-1 h5 mb-0">{{ metrics.dias_ultima if metrics.dias_ultima is not none else "-" }}</div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Recompra média</small><br>
          <div class="mt-1 h5 mb-0">
            {% if metrics.intervalo %}{{ metrics.intervalo }} dias{% else %}-{% endif %}
          </div>
        </div>
      </div>
    
      <div class="col-12 col-md-6 col-xl-3">
        <div class="cardx p-3 h-100">
          <small class="muted text-uppercase">Projeção (Mês)</small><br>
          <div class="mt-1 h5 mb-0 text-primary">{{ format_brl(metrics.projecao) }}</div>
        </div>
      </div>
    </div>

    <div class="row g-3 mb-4">
        <div class="col-12 col-lg-6">
            <div class="cardx p-4 h-100" style="border-left: 5px solid #0d6efd;">
                <div class="h5 mb-3 text-primary fw-bold">Análise da Última Compra</div>
                
                {% if metrics.ultima_compra %}
                    <div class="d-flex justify-content-between align-items-end mb-3">
                        <div>
                            <div class="muted text-uppercase small">Valor da NF</div>
                            <div class="display-6 fw-bold">{{ format_brl(metrics.ultima_compra.valor) }}</div>
                        </div>
                        <div class="text-end">
                            <div class="muted text-uppercase small">Data</div>
                            <div class="h5 mb-0">{{ metrics.ultima_compra.data }}</div>
                            <small class="text-muted">Há {{ metrics.dias_ultima }} dias</small>
                        </div>
                    </div>
                    
                    <div class="p-3 bg-light rounded border">
                        <div class="d-flex align-items-center gap-2 mb-1">
                            <span class="fw-bold text-dark">Impacto na Meta:</span>
                            <div class="progress flex-grow-1" style="height: 8px;">
                                <div class="progress-bar bg-success" style="width: {{ metrics.ultima_compra.pct_meta }}%"></div>
                            </div>
                            <span class="fw-bold text-success">{{ "%.2f"|format(metrics.ultima_compra.pct_meta) }}%</span>
                        </div>
                        <div class="small text-muted">
                            Vendedor responsável: <b>{{ metrics.ultima_compra.vendedor or "—" }}</b>
                        </div>
                    </div>
                {% else %}
                    <div class="text-muted py-4">Sem dados de última compra.</div>
                {% endif %}
            </div>
        </div>

        <div class="col-12 col-lg-6">
            <div class="viz-card h-100"><div class="viz-head"><div><div class="viz-title">Ritmo de recompra</div><div class="viz-sub">Últimas compras em ordem cronológica.</div></div></div><div class="viz-body">{% if grafico_recompra %}<img src="data:image/png;base64,{{ grafico_recompra }}">{% else %}<div class="text-muted">Sem histórico suficiente para gráfico.</div>{% endif %}</div></div>
          <div class="d-flex flex-wrap gap-2 mt-2">
            <span class="badge rounded-pill text-bg-light border text-dark">Medida mais comprada: {{ measure_stats['top_measure'] }}</span>
            <span class="badge rounded-pill text-bg-light border text-dark">Participação: {{ measure_stats['top_share'] }}%</span>
            <span class="badge rounded-pill text-bg-light border text-dark">Pedidos com medida: {{ measure_stats['total_orders'] }}</span>
          </div>
        </div>
    </div>

    <div class="row g-4 mb-1">
        <div class="col-12 col-lg-6">
            <div class="cardx p-3 h-100">
                <h5 class="mb-3">Mix de Medidas</h5>
                <div class="table-responsive">
                    <table class="table table-sm align-middle mb-0">
                      <thead><tr><th>Medida</th><th class="text-center">Pedidos</th><th class="text-center">%</th><th class="text-end">Valor</th></tr></thead>
                      <tbody>
                      {% for m in measure_stats['rows'] %}
                      <tr>
                        <td class="fw-semibold">{{ m.medida }}</td>
                        <td class="text-center">{{ m.qtd_pedidos }}</td>
                        <td class="text-center">{{ m.share }}%</td>
                        <td class="text-end">{{ format_brl(m.valor_total) }}</td>
                      </tr>
                      {% else %}
                        <tr><td colspan="4" class="text-center muted p-3">Nenhuma medida encontrada nos pedidos.</td></tr>
                      {% endfor %}
                      </tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-12 col-lg-6">
            <div class="cardx p-3 h-100">
                <h5 class="mb-3">Concentração por Medida</h5>
                <div style="min-height:280px;">
                    {% if measure_stats['chart'] %}
                        <img class="chart-img" style="height: 100%; object-fit: contain;" src="data:image/png;base64,{{ measure_stats['chart'] }}">
                    {% else %}
                        <div class="d-flex align-items-center justify-content-center h-100 text-muted">Sem medidas suficientes para gráfico.</div>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <div class="row g-4">
        <div class="col-12 col-lg-6">
            <div class="cardx p-3">
                <h5 class="mb-3">Histórico de Faturamento (NF)</h5>
                <div class="table-responsive">
                    <table class="table table-sm align-middle mb-0">
                      <thead><tr><th>Data</th><th>NF</th><th class="text-end">Valor</th><th>Vendedor</th></tr></thead>
                      <tbody>
                      {% for h in historico %}
                      <tr>
                        <td class="muted">{{ h.data_emissao }}</td>
                        <td>{{ h.pedido }}</td>
                        <td class="text-end fw-semibold">{{ format_brl(h.valor) }}</td>
                        <td class="small">{{ h.vendedor or "-" }}</td>
                      </tr>
                      {% else %}
                        <tr><td colspan="4" class="text-center muted p-3">Nenhum faturamento encontrado.</td></tr>
                      {% endfor %}
                      </tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-12 col-lg-6">
            <div class="cardx p-3">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="mb-0">Tarefas</h5>
                    <a href="{{ url_for('tarefas') }}" class="btn btn-sm btn-light border">Gerenciar</a>
                </div>
                <div class="table-responsive">
                    <table class="table table-sm align-middle mb-0">
                      <thead><tr><th>Status</th><th>Tarefa</th><th>Resp.</th></tr></thead>
                      <tbody>
                      {% for t in tarefas %}
                      <tr>
                        <td>{{ t.status or "-" }}</td>
                        <td>
                          <div class="fw-semibold">{{ t.titulo or "-" }}</div>
                          <div class="small text-muted">{{ t.descricao or "-" }}</div>
                        </td>
                        <td>{{ t.vendedor or t.origem or "-" }}</td>
                      </tr>
                      {% else %}
                        <tr><td colspan="3" class="text-center muted p-3">Nenhuma tarefa aberta.</td></tr>
                      {% endfor %}
                      </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    """,
    cliente=cliente,
    identity_nome=identity_nome,
    identity_key=identity_key,
    cliente_id=cliente_id,
    metrics=metrics,
    tarefas=tarefas,
    historico=historico,
    format_brl=format_brl,
    grafico_recompra=grafico_recompra,
    score_info=score_info,
    measure_stats=measure_stats
    )

            return render_page("Perfil do Cliente", body_html, "clientes")

CRM_STAGE_ORDER = ["NOVO", "QUALIFICACAO", "CONTATO", "PROPOSTA", "NEGOCIACAO", "GANHO", "PERDIDO"]
CRM_STAGE_LABELS = {
    "NOVO": "Novo",
    "QUALIFICACAO": "Qualificação",
    "CONTATO": "Contato",
    "PROPOSTA": "Proposta",
    "NEGOCIACAO": "Negociação",
    "GANHO": "Ganho",
    "PERDIDO": "Perdido",
}
CRM_STAGE_PROB = {
    "NOVO": 10,
    "QUALIFICACAO": 20,
    "CONTATO": 35,
    "PROPOSTA": 55,
    "NEGOCIACAO": 75,
    "GANHO": 100,
    "PERDIDO": 0,
}
CRM_STAGE_ICONS = {
    "NOVO": "bi-inbox",
    "QUALIFICACAO": "bi-funnel",
    "CONTATO": "bi-chat-dots",
    "PROPOSTA": "bi-file-earmark-text",
    "NEGOCIACAO": "bi-arrow-left-right",
    "GANHO": "bi-trophy",
    "PERDIDO": "bi-x-circle",
}
CRM_SOURCES = ["Indicação", "Site", "WhatsApp", "Instagram", "Prospecção", "Carteira", "Evento", "Outro"]
CRM_ACTIVITY_TYPES = ["LIGAÇÃO", "WHATSAPP", "E-MAIL", "REUNIÃO", "PROPOSTA", "NOTA"]


def _crm_scope_vendor_id(conn, user):
    if not user:
        return -1
    if user.get("role") in ["admin", "gestor"]:
        return None
    return get_user_vendor_id(conn, user)


def _crm_stage(stage):
    value = str(stage or "NOVO").strip().upper()
    return value if value in CRM_STAGE_ORDER else "NOVO"


def _crm_temp(temp):
    value = str(temp or "MORNO").strip().upper()
    return value if value in ["FRIO", "MORNO", "QUENTE"] else "MORNO"


def _crm_date_state(value):
    if not value:
        return ""
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        if d < date.today():
            return "overdue"
        if d == date.today():
            return "today"
    except Exception:
        pass
    return ""


def _crm_days_stale(updated_at):
    if not updated_at:
        return 0
    try:
        dt = datetime.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
        return max(0, (datetime.now() - dt).days)
    except Exception:
        return 0


def _crm_log_activity(conn, lead_id, tipo, descricao, user_name, agendado_em=None, concluido=1):
    conn.execute(
        """INSERT INTO crm_lead_activities(lead_id,tipo,descricao,agendado_em,concluido,created_by,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (lead_id, tipo, descricao, agendado_em or None, int(concluido), user_name or "Sistema", now_iso()),
    )


def _crm_can_access_lead(conn, lead_id, user):
    lead = conn.execute("SELECT * FROM crm_leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        return None
    scope = _crm_scope_vendor_id(conn, user)
    if scope is not None and int(lead["vendor_id"] or -1) != int(scope or -1):
        return None
    return lead


SISPLAN_STAGE_ORDER = ["NOVO", "CONTATO", "NEGOCIACAO", "APROVACAO", "GANHO", "PERDIDO", "ENCERRADO"]
# Derivados do enum para que etapa, probabilidade e "o que sai do pipeline"
# tenham uma unica fonte. Antes eram listas literais mantidas a mao.
SISPLAN_FINAL_STAGES = tuple(str(e) for e in EtapaFunil if e.e_final)
SISPLAN_STAGE_LABELS = {
    "NOVO": "Novo orçamento",
    "CONTATO": "Contato / Retorno",
    "NEGOCIACAO": "Em negociação",
    "APROVACAO": "Aguardando aprovação",
    "GANHO": "Convertido em pedido",
    "PERDIDO": "Perdido / Cancelado",
    "ENCERRADO": "Encerrado / Finalizado",
}
SISPLAN_STAGE_ICONS = {
    "NOVO": "bi-inbox",
    "CONTATO": "bi-telephone",
    "NEGOCIACAO": "bi-chat-square-text",
    "APROVACAO": "bi-hourglass-split",
    "GANHO": "bi-check2-circle",
    "PERDIDO": "bi-x-circle",
    "ENCERRADO": "bi-archive",
}
SISPLAN_STAGE_PROB = {str(e): e.probabilidade for e in EtapaFunil}


def _sisplan_stage(desc_status, pedido_id=None, status_code=None):
    """Traduz a fotografia atual do 104 em uma etapa visual única.

    Ordem de verdade: vínculo com pedido > descrição final do Sisplan > código
    de status final (F) > etapas ainda abertas. Assim, um orçamento que recebeu
    desfecho na atualização seguinte deixa imediatamente Oportunidades/Funil.
    """
    return str(f_funil.determinar_etapa(desc_status, pedido_id, status_code))


def _sisplan_scope_sql(conn, scope_vendor_id, alias="o"):
    """Retorna filtro compatível com os códigos de vendedor gravados no relatório de orçamentos."""
    if not scope_vendor_id:
        return "", []
    row = conn.execute("SELECT codigo,nome FROM vendors WHERE id=?", (int(scope_vendor_id),)).fetchone()
    if not row:
        return " AND 1=0", []
    codigo = str(row["codigo"] or "").strip()
    nome = str(row["nome"] or "").strip()
    clauses, params = [], []
    if codigo:
        clauses.append(f"{alias}.vendedor_id = ?")
        params.append(codigo)
        sem_zero = codigo.lstrip("0")
        if sem_zero and sem_zero != codigo:
            clauses.append(f"{alias}.vendedor_id LIKE ?")
            params.append(f"%{sem_zero}")
    if nome:
        clauses.append(f"UPPER(TRIM(COALESCE({alias}.vendedor_nome,''))) = UPPER(TRIM(?))")
        params.append(nome.lstrip('.').strip())
        if nome.startswith('.'):
            clauses.append(f"UPPER(TRIM(COALESCE({alias}.vendedor_nome,''))) = UPPER(TRIM(?))")
            params.append(nome)
    return (" AND (" + " OR ".join(clauses) + ")", params) if clauses else (" AND 1=0", [])


def _sisplan_period_rows(conn, d0, d1, scope_vendor_id=None, vendor_name="", q=""):
    scope_sql, params_scope = _sisplan_scope_sql(conn, scope_vendor_id, "o")
    extra = scope_sql
    params = [str(d0), str(d1)] + params_scope
    if vendor_name and not scope_vendor_id:
        extra += " AND o.vendedor_nome=?"
        params.append(vendor_name)
    if q:
        like = f"%{q}%"
        extra += " AND (COALESCE(o.numero,'') LIKE ? OR COALESCE(o.cliente_nome_ref,'') LIKE ? OR COALESCE(o.cliente_nome,'') LIKE ? OR COALESCE(o.pedido_id,'') LIKE ?)"
        params.extend([like, like, like, like])
    rows = conn.execute(f"""
        SELECT o.id,o.numero,o.cliente_nome,o.cliente_nome_ref,o.pedido_id,o.vendedor_id,o.vendedor_nome,
               o.data_inicio,o.data_validade,o.status,o.desc_status,o.tipo_contato,o.valor_total,o.uf,o.motivo
        FROM orcamentos o
        WHERE o.data_inicio BETWEEN ? AND ? {extra}
        ORDER BY o.data_inicio DESC,o.valor_total DESC
    """, params).fetchall()
    out = []
    hoje = date.today()
    for row in rows:
        item = dict(row)
        item["cliente_exibir"] = (str(item.get("cliente_nome_ref") or "").strip() or str(item.get("cliente_nome") or "").strip() or "CLIENTE SEM NOME")
        item["etapa_crm"] = _sisplan_stage(item.get("desc_status"), item.get("pedido_id"), item.get("status"))
        item["probabilidade"] = SISPLAN_STAGE_PROB[item["etapa_crm"]]
        try:
            dt = datetime.strptime(str(item.get("data_inicio") or "")[:10], "%Y-%m-%d").date()
            item["dias_aberto"] = max(0, (hoje - dt).days)
        except Exception:
            item["dias_aberto"] = 0
        out.append(item)
    return out


def _sisplan_metrics_from_rows(rows):
    """Adaptador: ver crm_npd.metrics.funil.calcular_metricas."""
    m = f_funil.calcular_metricas(
        (
            EtapaFunil(r["etapa_crm"]),
            float(r.get("valor_total") or 0),
            int(r.get("dias_aberto") or 0),
        )
        for r in rows
    )
    return {
        "active": m.ativos,
        "pipeline": m.pipeline,
        "weighted": m.ponderado,
        "overdue": m.parados,
        "stale": m.parados,
        "won": m.ganhos,
        "closed": m.fechados,
        "conversion": m.conversao,
        "actionable": m.acionaveis,
    }


def _goal_achievement_ticker(conn, limit=6):
    """Mensagens motivacionais para o topo do CRM quando uma vendedora bate a meta diária."""
    hoje = date.today()
    d0, d1 = periodo_mes_calendario(hoje.replace(day=1))
    dias_uteis = max(1, business_days_between(d0, d1))
    rows = conn.execute(f"""
        SELECT
            v.id,
            v.nome,
            v.tipo,
            COALESCE(m.meta_manual, 0) AS meta_mensal,
            COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS vendido_hoje
        FROM vendors v
        LEFT JOIN metas_vendedores m ON m.vendor_id = v.id
        LEFT JOIN orders o
               ON o.vendor_id = v.id
              AND date(o.data_emissao) = date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
        WHERE v.ativo = 1
          AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
          AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
        GROUP BY v.id, v.nome, v.tipo, m.meta_manual
        HAVING COALESCE(m.meta_manual,0) > 0
           AND vendido_hoje >= (COALESCE(m.meta_manual,0) / ?)
        ORDER BY vendido_hoje DESC, v.nome ASC
        LIMIT ?
    """, (hoje.isoformat(), dias_uteis, int(limit))).fetchall()

    msgs = []
    for r in rows:
        meta_mensal = float(r["meta_mensal"] or 0)
        vendido = float(r["vendido_hoje"] or 0)
        meta_diaria = meta_mensal / dias_uteis if dias_uteis else 0.0
        excedente = max(0.0, vendido - meta_diaria)
        if vendido <= 0 or meta_diaria <= 0:
            continue
        nome = str(r["nome"] or "Vendedora").strip().title()
        verbo = "ultrapassou" if excedente > 1 else "bateu"
        msgs.append(f"🚀 {nome} {verbo} a meta do dia e vendeu {format_brl(vendido)} hoje. Bora buscar a próxima!")
    return msgs


def _crm_pipeline_metrics(conn, scope_vendor_id=None, d0=None, d1=None):
    """Compatibilidade do dashboard: agora o pipeline é derivado do Sisplan, não de leads manuais."""
    if d0 is None or d1 is None:
        ref = date.today().replace(day=1)
        d0, d1 = periodo_mes_calendario(ref)
    rows = _sisplan_period_rows(conn, d0, d1, scope_vendor_id=scope_vendor_id)
    return _sisplan_metrics_from_rows(rows)


@app.route("/buscar")
@login_required
def global_search():
    q = (request.args.get("q") or "").strip()
    user = get_current_user()
    results = {"leads": [], "clientes": [], "vendedores": [], "pedidos": []}
    if q:
        like = f"%{q}%"
        with conectar() as conn:
            scope = _crm_scope_vendor_id(conn, user)
            opp_scope_sql, opp_scope_params = _sisplan_scope_sql(conn, scope, "o")
            opp_params = [like, like, like, like] + opp_scope_params
            opp_rows = conn.execute(f"""
                SELECT o.id,o.numero,
                       COALESCE(NULLIF(TRIM(o.cliente_nome_ref),''),NULLIF(TRIM(o.cliente_nome),''),'CLIENTE SEM NOME') AS nome,
                       o.desc_status,o.status,o.pedido_id,o.valor_total,o.vendedor_nome AS vendedor,o.data_inicio
                FROM orcamentos o
                WHERE (COALESCE(o.numero,'') LIKE ? OR COALESCE(o.cliente_nome_ref,'') LIKE ? OR COALESCE(o.cliente_nome,'') LIKE ? OR COALESCE(o.pedido_id,'') LIKE ?)
                {opp_scope_sql}
                ORDER BY o.data_inicio DESC,o.id DESC LIMIT 8
            """, opp_params).fetchall()
            results["leads"] = []
            for _r in opp_rows:
                _d = dict(_r)
                _d["etapa"] = _sisplan_stage(_d.get("desc_status"), _d.get("pedido_id"), _d.get("status"))
                _d["data_ref"] = str(_d.get("data_inicio") or "")[:7]
                results["leads"].append(_d)

            if scope is None:
                results["clientes"] = conn.execute("""
                    SELECT c.id,COALESCE(c.fantasia,c.nome) AS nome,c.codigo
                    FROM customers c WHERE COALESCE(c.fantasia,'') LIKE ? OR c.nome LIKE ? OR COALESCE(c.codigo,'') LIKE ?
                    ORDER BY nome LIMIT 8
                """, (like, like, like)).fetchall()
                results["pedidos"] = conn.execute("""
                    SELECT o.pedido,o.data_emissao,COALESCE(c.fantasia,c.nome,o.cliente_nome_ref) AS cliente,MAX(o.valor) AS valor
                    FROM orders o LEFT JOIN customers c ON c.id=o.cliente_id
                    WHERE COALESCE(o.pedido,'') LIKE ? OR COALESCE(o.cliente_nome_ref,'') LIKE ?
                    GROUP BY o.pedido,o.data_emissao,cliente ORDER BY o.data_emissao DESC LIMIT 8
                """, (like, like)).fetchall()
                results["vendedores"] = conn.execute("SELECT id,nome,codigo FROM vendors WHERE ativo=1 AND (nome LIKE ? OR COALESCE(codigo,'') LIKE ?) ORDER BY nome LIMIT 8", (like, like)).fetchall()
            else:
                results["clientes"] = conn.execute("""
                    SELECT DISTINCT c.id,COALESCE(c.fantasia,c.nome) AS nome,c.codigo
                    FROM customers c JOIN orders o ON o.cliente_id=c.id
                    WHERE o.vendor_id=? AND (COALESCE(c.fantasia,'') LIKE ? OR c.nome LIKE ? OR COALESCE(c.codigo,'') LIKE ?)
                    ORDER BY nome LIMIT 8
                """, (scope, like, like, like)).fetchall()
                results["pedidos"] = conn.execute("""
                    SELECT o.pedido,o.data_emissao,COALESCE(c.fantasia,c.nome,o.cliente_nome_ref) AS cliente,MAX(o.valor) AS valor
                    FROM orders o LEFT JOIN customers c ON c.id=o.cliente_id
                    WHERE o.vendor_id=? AND (COALESCE(o.pedido,'') LIKE ? OR COALESCE(o.cliente_nome_ref,'') LIKE ?)
                    GROUP BY o.pedido,o.data_emissao,cliente ORDER BY o.data_emissao DESC LIMIT 8
                """, (scope, like, like)).fetchall()

    body = render_template_string(r"""
    <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-4">
      <div><div class="h4 fw-bold mb-1">Busca global</div><div class="muted">Resultados em todo o workspace comercial.</div></div>
      <form class="d-flex gap-2" method="get"><input class="form-control" style="min-width:320px" name="q" value="{{ q }}" placeholder="Busque por nome, código, pedido…"><button class="btn btn-primary"><i class="bi bi-search"></i></button></form>
    </div>
    {% if not q %}<div class="cardx p-5 text-center"><i class="bi bi-search fs-1 text-muted"></i><div class="h5 mt-3">Digite algo para buscar</div></div>{% else %}
    <div class="row g-3">
      <div class="col-12 col-xl-6"><div class="cardx p-3 h-100"><div class="section-title mb-3"><i class="bi bi-lightning-charge me-2 text-primary"></i>Oportunidades Sisplan</div>{% for r in results.leads %}<a class="d-flex justify-content-between align-items-center text-decoration-none text-dark border-top py-2" href="{{ url_for('orcamento_detalhe', numero=r['numero'], origem='busca', data_ref=r['data_ref']) }}"><div><div class="fw-bold">#{{ r['numero'] or '—' }} · {{ r['nome'] }}</div><div class="small text-muted">{{ r['vendedor'] or 'Sem vendedor' }} · {{ format_brl(r['valor_total'] or 0) }}</div></div><span class="chip">{{ stage_labels[r['etapa']] }}</span></a>{% else %}<div class="muted">Nenhuma oportunidade encontrada.</div>{% endfor %}</div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-3 h-100"><div class="section-title mb-3"><i class="bi bi-buildings me-2 text-primary"></i>Clientes</div>{% for r in results.clientes %}<a class="d-flex justify-content-between align-items-center text-decoration-none text-dark border-top py-2" href="{{ url_for('cliente_360', cliente_id=r['id']) }}"><div><div class="fw-bold">{{ r['nome'] }}</div><div class="small text-muted">Código {{ r['codigo'] or '—' }}</div></div><i class="bi bi-chevron-right text-muted"></i></a>{% else %}<div class="muted">Nenhum cliente encontrado.</div>{% endfor %}</div></div>
      <div class="col-12 col-xl-6"><div class="cardx p-3 h-100"><div class="section-title mb-3"><i class="bi bi-receipt me-2 text-primary"></i>Pedidos</div>{% for r in results.pedidos %}<div class="d-flex justify-content-between border-top py-2"><div><div class="fw-bold">#{{ r['pedido'] }}</div><div class="small text-muted">{{ r['cliente'] or '—' }} · {{ r['data_emissao'] or '—' }}</div></div><div class="fw-bold">{{ format_brl(r['valor'] or 0) }}</div></div>{% else %}<div class="muted">Nenhum pedido encontrado.</div>{% endfor %}</div></div>
      {% if results.vendedores %}<div class="col-12 col-xl-6"><div class="cardx p-3 h-100"><div class="section-title mb-3"><i class="bi bi-people me-2 text-primary"></i>Vendedores</div>{% for r in results.vendedores %}<a class="d-flex justify-content-between align-items-center text-decoration-none text-dark border-top py-2" href="{{ url_for('vendedor', vendor_id=r['id']) }}"><div><div class="fw-bold">{{ r['nome'] }}</div><div class="small text-muted">Código {{ r['codigo'] or '—' }}</div></div><i class="bi bi-chevron-right text-muted"></i></a>{% endfor %}</div></div>{% endif %}
    </div>{% endif %}
    """, q=q, results=results, stage_labels=SISPLAN_STAGE_LABELS, format_brl=format_brl)
    return render_page("Busca", body, "")


@app.route("/orcamento/<numero>")
@login_required
def orcamento_detalhe(numero):
    """Visão 360 do orçamento 104, com itens do 721 quando já existe pedido."""
    user = get_current_user()
    numero = str(numero or "").strip()
    origem = (request.args.get("origem") or "oportunidades").strip().lower()
    data_ref_retorno = (request.args.get("data_ref") or "").strip()
    with conectar() as conn:
        scope = _crm_scope_vendor_id(conn, user)
        scope_sql, scope_params = _sisplan_scope_sql(conn, scope, "o")
        orc = conn.execute(f"SELECT o.* FROM orcamentos o WHERE o.numero=? {scope_sql} LIMIT 1", [numero] + scope_params).fetchone()
        if not orc:
            flash("Orçamento não encontrado ou fora do seu acesso.", "warning")
            return redirect(url_for("leads"))
        orc = dict(orc)
        pedido = str(orc.get("pedido_id") or "").strip()
        itens = []
        notas = []
        if pedido and pedido != "0":
            itens = [dict(r) for r in conn.execute("""
                SELECT medida_ref,grupo_ref,tipo_item_ref,papel_ref,quantidade,preco_ref,valor_item_ref,
                       cliente_nome_ref,vendor_nome_ref
                FROM orders
                WHERE TRIM(COALESCE(pedido,''))=TRIM(?)
                  AND COALESCE(pedido_cancelado,0)=0
                ORDER BY id
            """, (pedido,)).fetchall()]
            notas = [dict(r) for r in conn.execute("""
                SELECT numero,data_emissao,produto,formato,quantidade,valor
                FROM invoices WHERE TRIM(COALESCE(pedido,''))=TRIM(?) ORDER BY data_emissao,numero,id
            """, (pedido,)).fetchall()]

    qtd = float(orc.get("quantidade") or 0)
    valor_base = float(orc.get("valor_base") or 0)
    valor_total = float(orc.get("valor_total") or 0)
    acrescimos = max(0.0, valor_total - valor_base)
    unitario_medio = (valor_base / qtd) if qtd else 0.0
    texto_obs = " ".join([str(orc.get("obs") or ""), str(orc.get("observacao") or "")]).strip()
    desconto_mencionado = "DESCONTO" in _norm_jit_text_py(texto_obs)
    desconto_percentual = None
    _m_desc = re.search(r"DESCONTO\s*(?:DE\s*)?([0-9]+(?:[,.][0-9]+)?)\s*%", texto_obs, flags=re.I)
    if _m_desc:
        try:
            desconto_percentual = float(_m_desc.group(1).replace(",", "."))
        except Exception:
            desconto_percentual = None
    etapa = _sisplan_stage(orc.get("desc_status"), orc.get("pedido_id"), orc.get("status"))

    mes_orc = str(orc.get("data_inicio") or "")[:7]
    ref_volta = data_ref_retorno or mes_orc
    if origem == "orcamentos":
        back_url = url_for("orcamentos_detalhe", data_ref=ref_volta)
        back_label = "Voltar aos orçamentos"
    elif origem == "funil":
        back_url = url_for("funil", data_ref=ref_volta)
        back_label = "Voltar ao funil"
    elif origem == "busca":
        back_url = url_for("global_search")
        back_label = "Voltar à busca"
    elif origem == "performance":
        back_url = url_for("performance_time", data_ref=ref_volta)
        back_label = "Voltar à performance"
    else:
        back_url = url_for("leads", data_ref=ref_volta)
        back_label = "Voltar às oportunidades"

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-start flex-wrap gap-3 mb-4">
      <div><div class="small text-uppercase text-muted fw-bold">Orçamento Sisplan 104</div><div class="h3 fw-bold mb-1">#{{ orc.numero }} · {{ orc.cliente_nome_ref or orc.cliente_nome or 'Cliente sem nome' }}</div><div class="muted">{{ orc.vendedor_nome or 'Sem vendedor' }} · {{ orc.data_inicio or '—' }}{% if orc.cidade or orc.uf %} · {{ orc.cidade or '' }}{{ '/' if orc.cidade and orc.uf else '' }}{{ orc.uf or '' }}{% endif %}</div></div>
      <div class="d-flex gap-2"><a class="btn btn-outline-secondary" href="{{ back_url }}"><i class="bi bi-arrow-left me-1"></i>{{ back_label }}</a>{% if orc.pedido_id and orc.pedido_id!='0' %}<span class="btn btn-success disabled">Pedido {{ orc.pedido_id }}</span>{% endif %}</div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Valor total</div><div class="metric-value">{{ format_brl(valor_total) }}</div><div class="small text-muted">VALORT do 104</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Quantidade</div><div class="metric-value">{{ '%.0f'|format(qtd) }}</div><div class="small text-muted">Quantidade informada no orçamento</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Valor base</div><div class="metric-value">{{ format_brl(valor_base) }}</div><div class="small text-muted">VALOR do 104</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Preço médio base</div><div class="metric-value">{{ format_brl(unitario_medio) }}</div><div class="small text-muted">Valor base ÷ quantidade</div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-xl-7"><div class="cardx p-4 h-100"><div class="section-title mb-3">Situação comercial</div>
        <div class="row g-3"><div class="col-md-6"><div class="small text-muted">Etapa</div><div class="fw-bold">{{ stage_labels[etapa] }}</div></div><div class="col-md-6"><div class="small text-muted">Status Sisplan</div><div class="fw-bold">{{ orc.desc_status or 'SEM STATUS' }}</div></div>
        <div class="col-md-6"><div class="small text-muted">Contato</div><div>{{ orc.tipo_contato or '—' }}</div></div><div class="col-md-6"><div class="small text-muted">Motivo</div><div>{{ orc.motivo or '—' }}</div></div>
        <div class="col-md-4"><div class="small text-muted">Previsão</div><div>{{ orc.data_prev or '—' }}</div></div><div class="col-md-4"><div class="small text-muted">Retorno</div><div>{{ orc.data_retorno or '—' }}</div></div><div class="col-md-4"><div class="small text-muted">Validade</div><div>{{ orc.data_validade or '—' }}</div></div>
        <div class="col-md-6"><div class="small text-muted">Status OS</div><div>{{ orc.status_os or '—' }}</div></div><div class="col-md-6"><div class="small text-muted">Funcionário</div><div>{{ orc.nome_funcionario or orc.funcionario or '—' }}</div></div></div>
      </div></div>
      <div class="col-12 col-xl-5"><div class="cardx p-4 h-100"><div class="section-title mb-3">Composição financeira</div>
        <div class="d-flex justify-content-between border-bottom py-2"><span>Base</span><b>{{ format_brl(valor_base) }}</b></div><div class="d-flex justify-content-between border-bottom py-2"><span>Diferença até o total</span><b>{{ format_brl(acrescimos) }}</b></div><div class="d-flex justify-content-between py-2"><span>Total</span><b>{{ format_brl(valor_total) }}</b></div>
        <div class="mt-3 small"><b>Desconto:</b> {% if desconto_percentual is not none %}<span class="text-warning">{{ '%.2f'|format(desconto_percentual)|replace('.', ',') }}% mencionado nas observações</span>{% elif desconto_mencionado %}<span class="text-warning">há menção de desconto nas observações</span>{% else %}<span class="text-muted">sem informação estruturada de desconto no relatório 104</span>{% endif %}</div>
      </div></div>
    </div>

    <div class="cardx p-4 mb-4"><div class="section-title mb-2">Observações do orçamento</div>{% if orc.obs or orc.observacao %}{% if orc.obs %}<div class="mb-2" style="white-space:pre-wrap">{{ orc.obs }}</div>{% endif %}{% if orc.observacao %}<div style="white-space:pre-wrap">{{ orc.observacao }}</div>{% endif %}{% else %}<div class="text-muted">Sem observações no 104.</div>{% endif %}</div>

    <div class="cardx p-0 overflow-hidden mb-4"><div class="p-3 border-bottom"><div class="section-title">Itens / produto</div><div class="section-sub">{% if itens %}Itens reais encontrados no pedido {{ orc.pedido_id }} pelo relatório 721.{% else %}Este 104 não contém coluna de produto/item. Enquanto o orçamento ainda não virou pedido, o CRM mostra tudo o que o arquivo realmente fornece: quantidade, preço médio calculado, valores, status e observações — sem inventar o produto.{% endif %}</div></div>
      {% if itens %}<div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Medida</th><th>Grupo / tipo</th><th>Papel</th><th class="text-end">Qtd.</th><th class="text-end">Unitário</th><th class="text-end">Valor item</th></tr></thead><tbody>{% for i in itens %}<tr><td>{{ i.medida_ref or '—' }}</td><td>{{ i.grupo_ref or '—' }}{% if i.tipo_item_ref %}<div class="small text-muted">{{ i.tipo_item_ref }}</div>{% endif %}</td><td>{{ i.papel_ref or '—' }}</td><td class="text-end">{{ '%.0f'|format(i.quantidade or 0) }}</td><td class="text-end">{{ format_brl(i.preco_ref or 0) }}</td><td class="text-end fw-bold">{{ format_brl(i.valor_item_ref or 0) }}</td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="p-3"><div class="alert alert-light border mb-0"><div class="fw-bold mb-1"><i class="bi bi-info-circle me-1 text-primary"></i>Produto não vem no relatório 104</div><div class="small text-muted">Para enxergar o item exato antes de gerar o pedido, será necessário acrescentar ao CRM um relatório do Sisplan que exporte os itens do orçamento. Nesta versão eu mantive somente dados comprováveis dos arquivos atuais.</div></div></div>{% endif %}
    </div>
    {% if notas %}<div class="cardx p-0 overflow-hidden"><div class="p-3 border-bottom"><div class="section-title">Faturamento vinculado</div></div><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Nota</th><th>Data</th><th>Produto</th><th class="text-end">Qtd.</th><th class="text-end">Valor</th></tr></thead><tbody>{% for n in notas %}<tr><td>{{ n.numero or '—' }}</td><td>{{ n.data_emissao or '—' }}</td><td>{{ n.produto or n.formato or '—' }}</td><td class="text-end">{{ n.quantidade or 0 }}</td><td class="text-end">{{ format_brl(n.valor or 0) }}</td></tr>{% endfor %}</tbody></table></div></div>{% endif %}
    """, orc=orc, itens=itens, notas=notas, qtd=qtd, valor_base=valor_base, valor_total=valor_total, acrescimos=acrescimos,
       unitario_medio=unitario_medio, desconto_mencionado=desconto_mencionado, desconto_percentual=desconto_percentual, etapa=etapa,
       back_url=back_url, back_label=back_label, stage_labels=SISPLAN_STAGE_LABELS, format_brl=format_brl)
    return render_page(f"Orçamento {numero}", body, "orcamentos_detalhe")

def _performance_bounds(data_ref: str):
    data_ref = (data_ref or date.today().strftime("%Y-%m")).strip()
    try:
        ref = datetime.strptime(data_ref, "%Y-%m").date().replace(day=1)
    except Exception:
        ref = date.today().replace(day=1)
        data_ref = ref.strftime("%Y-%m")
    d0, mes_fim = periodo_mes_calendario(ref)
    hoje = date.today()
    d1 = min(mes_fim, hoje) if (ref.year, ref.month) == (hoje.year, hoje.month) else mes_fim
    if ref.month == 1:
        prev0 = date(ref.year - 1, 12, 1)
    else:
        prev0 = date(ref.year, ref.month - 1, 1)
    prev_mes_fim = end_of_month(prev0)
    if (ref.year, ref.month) == (hoje.year, hoje.month):
        dias = max(1, (d1 - d0).days + 1)
        prev1 = min(prev_mes_fim, prev0 + timedelta(days=dias - 1))
    else:
        prev1 = prev_mes_fim
    return data_ref, ref, d0, d1, prev0, prev1


def _performance_pct_delta(atual, anterior):
    """Adaptador: ver crm_npd.metrics.percentuais.variacao_percentual_ou_nulo."""
    return f_pct.variacao_percentual_ou_nulo(atual, anterior)


def _performance_vendor_catalog(conn):
    vendors = [dict(r) for r in conn.execute("SELECT id,codigo,nome,tipo,ativo FROM vendors WHERE ativo=1").fetchall()]
    by_id = {int(v["id"]): v for v in vendors}
    by_code = {}
    for v in vendors:
        code = str(v.get("codigo") or "").strip()
        if code:
            by_code.setdefault(code, []).append(v)
    return vendors, by_id, by_code


def _performance_norm_name(x):
    return _norm_jit_text_py(str(x or "").lstrip('.').strip())


def _performance_resolve_orc_vendor(vendors, by_code, code, name):
    code = str(code or "").strip()
    nn = _performance_norm_name(name)
    cands = by_code.get(code, []) if code else []
    exact = [v for v in cands if _performance_norm_name(v.get("nome")) == nn]
    if len(exact) == 1:
        return ("V", int(exact[0]["id"])), exact[0]
    if len(cands) == 1:
        return ("V", int(cands[0]["id"])), cands[0]
    exact_global = [v for v in vendors if _performance_norm_name(v.get("nome")) == nn]
    if len(exact_global) == 1:
        return ("V", int(exact_global[0]["id"])), exact_global[0]
    return ("O", code or nn or "SEM"), {"id": None, "codigo": code, "nome": name or "Sem vendedor", "tipo": "Não classificado"}


def _performance_collect(conn, a, b, catalog=None):
    vendors, by_id, by_code = catalog or _performance_vendor_catalog(conn)
    data = {}

    def item(key, meta):
        if key not in data:
            data[key] = {
                "key": key, "id": meta.get("id"), "codigo": meta.get("codigo") or "",
                "nome": str(meta.get("nome") or "Sem vendedor").lstrip('.').strip(), "tipo": meta.get("tipo") or "",
                "orc_qtd": 0, "orc_valor": 0.0, "conv_qtd": 0, "conv_valor": 0.0,
                "aberto_qtd": 0, "aberto_valor": 0.0, "perdido_qtd": 0, "perdido_valor": 0.0,
                "ped_qtd": 0, "ped_valor": 0.0, "fat_qtd": 0, "fat_valor": 0.0,
            }
        return data[key]

    for r in conn.execute("""
        SELECT vendedor_id,vendedor_nome,valor_total,pedido_id,desc_status,status
        FROM orcamentos
        WHERE date(data_inicio) BETWEEN date(?) AND date(?)
    """, (a.isoformat(), b.isoformat())).fetchall():
        key, meta = _performance_resolve_orc_vendor(vendors, by_code, r["vendedor_id"], r["vendedor_nome"])
        x = item(key, meta)
        valor = float(r["valor_total"] or 0)
        x["orc_qtd"] += 1
        x["orc_valor"] += valor
        etapa = _sisplan_stage(r["desc_status"], r["pedido_id"], r["status"])
        if etapa == "GANHO":
            x["conv_qtd"] += 1
            x["conv_valor"] += valor
        elif etapa == "PERDIDO":
            x["perdido_qtd"] += 1
            x["perdido_valor"] += valor
        elif etapa not in SISPLAN_FINAL_STAGES:
            x["aberto_qtd"] += 1
            x["aberto_valor"] += valor

    for r in conn.execute("""
        SELECT i.vendor_id, COUNT(DISTINCT COALESCE(i.numero,'') || '|' || COALESCE(i.data_emissao,'')) qtd,
               COALESCE(SUM(i.valor),0) valor
        FROM invoices i
        WHERE date(i.data_emissao) BETWEEN date(?) AND date(?)
        GROUP BY i.vendor_id
    """, (a.isoformat(), b.isoformat())).fetchall():
        vid = int(r["vendor_id"] or 0)
        meta = by_id.get(vid, {"id": vid, "codigo": "", "nome": "Sem vendedor", "tipo": ""})
        x = item(("V", vid), meta)
        x["fat_qtd"] += int(r["qtd"] or 0)
        x["fat_valor"] += float(r["valor"] or 0)

    for r in conn.execute(f"""
        SELECT o.vendor_id, COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) qtd,
               COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) valor
        FROM orders o LEFT JOIN vendors v ON v.id=o.vendor_id
        WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
          AND {sql_pedidos_realizados_excel_cond('o','v')}
        GROUP BY o.vendor_id
    """, (a.isoformat(), b.isoformat())).fetchall():
        vid = int(r["vendor_id"] or 0)
        meta = by_id.get(vid, {"id": vid, "codigo": "", "nome": "Sem vendedor", "tipo": ""})
        x = item(("V", vid), meta)
        x["ped_qtd"] += int(r["qtd"] or 0)
        x["ped_valor"] += float(r["valor"] or 0)
    return data


def _performance_assign_scores(rows):
    """
    Score comercial mensal 0-100.
    Mantém atividade como maior peso e acrescenta qualidade de carteira usando
    a mesma fonte Novo/Recompra da Visão do Vendedor. Recuperado é um subconjunto
    da recorrência e recebe mais peso que uma recompra normal.
    """
    teams = {}
    for r in rows:
        t = (r.get("tipo") or "").strip().upper()
        if t in ("INTERNO", "EXTERNO"):
            teams.setdefault(t, []).append(r)

    for team_rows in teams.values():
        max_orc = max([float(r.get("orc_qtd") or 0) for r in team_rows] + [1.0])
        max_orc_val = max([float(r.get("orc_valor") or 0) for r in team_rows] + [1.0])
        max_fat = max([float(r.get("fat_valor") or 0) for r in team_rows] + [1.0])

        # Pontuação de carteira: NOVO vale 1,0; RECUPERADO 0,8; recorrência normal 0,30.
        # Recuperado já está dentro de rep_count, então não é contado duas vezes.
        for r in team_rows:
            r["carteira_pontos_brutos"] = f_score_vendedor.pontos_carteira(
                r.get("clientes_novos") or 0,
                r.get("clientes_repeticao") or 0,
                r.get("clientes_recuperados") or 0,
            )
        max_carteira = max([float(r.get("carteira_pontos_brutos") or 0) for r in team_rows] + [1.0])

        for r in team_rows:
            dims = f_score_vendedor.calcular_dimensoes(
                orcamentos_qtd=int(r.get("orc_qtd") or 0),
                orcamentos_valor=float(r.get("orc_valor") or 0),
                faturamento_valor=float(r.get("fat_valor") or 0),
                conversao_pct=float(r.get("conv_pct") or 0),
                carteira_pontos=float(r.get("carteira_pontos_brutos") or 0),
                variacao_orcamentos_pct=r.get("orc_delta"),
                variacao_conversao_pp=float(r.get("conv_delta_pp") or 0),
                max_orcamentos_qtd=max_orc,
                max_orcamentos_valor=max_orc_val,
                max_faturamento=max_fat,
                max_carteira_pontos=max_carteira,
            )
            r["score"] = f_score_vendedor.calcular_score(dims)
            r["score_dims"] = {
                "atividade": round(dims.atividade, 1),
                "pipeline": round(dims.pipeline, 1),
                "conversao": round(dims.conversao, 1),
                "resultado": round(dims.resultado, 1),
                "carteira": round(dims.carteira, 1),
                "evolucao": round(dims.evolucao, 1),
            }
            r["score_label"], r["score_tone"] = f_score_vendedor.rotular_score(r["score"])

def _performance_build_context(conn, data_ref, include_customer_mix: bool = True, catalog=None):
    data_ref, ref, d0, d1, prev0, prev1 = _performance_bounds(data_ref)
    catalog = catalog or _performance_vendor_catalog(conn)
    cur = _performance_collect(conn, d0, d1, catalog)
    prev = _performance_collect(conn, prev0, prev1, catalog)
    all_keys = set(cur) | set(prev)
    rows = []
    zero_int = {k: 0 for k in ("orc_qtd", "conv_qtd", "aberto_qtd", "perdido_qtd", "ped_qtd", "fat_qtd")}
    zero_float = {k: 0.0 for k in ("orc_valor", "conv_valor", "aberto_valor", "perdido_valor", "ped_valor", "fat_valor")}
    for key in all_keys:
        if key in cur:
            c = dict(cur[key])
        else:
            c = dict(prev[key])
            c.update(zero_int)
            c.update(zero_float)
        p = prev.get(key) or {}
        nome = (c.get("nome") or "Sem vendedor").strip()
        tipo = (c.get("tipo") or "").strip()
        if tipo.upper() == "SITE" or nome.upper() == "SITE":
            continue
        if not any(float(c.get(k) or 0) for k in ("orc_qtd", "ped_qtd", "fat_qtd", "orc_valor", "fat_valor")) and not any(float(p.get(k) or 0) for k in ("orc_qtd", "ped_qtd", "fat_qtd")):
            continue
        c["conv_pct"] = (c["conv_qtd"] / c["orc_qtd"] * 100.0) if c["orc_qtd"] else 0.0
        c["perdido_pct"] = (c["perdido_qtd"] / c["orc_qtd"] * 100.0) if c["orc_qtd"] else 0.0
        c["ticket_fat"] = (c["fat_valor"] / c["fat_qtd"]) if c["fat_qtd"] else 0.0
        c["orc_delta"] = _performance_pct_delta(c["orc_qtd"], p.get("orc_qtd", 0))
        c["fat_delta"] = _performance_pct_delta(c["fat_valor"], p.get("fat_valor", 0))
        prev_conv = (float(p.get("conv_qtd", 0)) / float(p.get("orc_qtd", 1)) * 100.0) if p.get("orc_qtd", 0) else 0.0
        c["conv_delta_pp"] = c["conv_pct"] - prev_conv
        c["prev_orc_qtd"] = int(p.get("orc_qtd", 0) or 0)
        c["prev_conv_qtd"] = int(p.get("conv_qtd", 0) or 0)
        c["prev_fat_valor"] = float(p.get("fat_valor", 0) or 0)
        c["clientes_novos"] = 0
        c["clientes_repeticao"] = 0
        c["clientes_recuperados"] = 0
        c["meta_mensal"] = 0.0
        c["fat_ciclo"] = 0.0
        c["meta_pct"] = 0.0
        c["meta_delta_pct"] = None
        rows.append(c)
    if include_customer_mix:
        periodo = ref.strftime("%Y-%m")
        # A referência de clientes é o mesmo ciclo da comissão/faturamento da
        # Visão do Vendedor. summarize_vendor_entities_in_period é substituída
        # em runtime pelo serviço canônico em lote/cacheado, então todos os cards
        # usam a MESMA fonte sem N+1 queries.
        mix_d0, mix_d1 = periodo_ciclo_25_24_que_contem_hoje(ref)
        if mix_d0 <= date.today() <= mix_d1:
            mix_d1 = date.today()
        fat_ciclo_map = _faturamento_comissao_por_vendedor(conn, mix_d0, mix_d1)
        for c in rows:
            vid = int(c.get("id") or 0)
            if vid <= 0:
                continue
            mix = summarize_vendor_entities_in_period(conn, vid, mix_d0, mix_d1)
            c["clientes_novos"] = int(mix.get("novo_count", 0) or 0)
            c["clientes_repeticao"] = int(mix.get("rep_count", 0) or 0)
            c["clientes_recuperados"] = int(mix.get("recuperado_count", 0) or 0)
            c["clientes_periodo_inicio"] = mix_d0.isoformat()
            c["clientes_periodo_fim"] = mix_d1.isoformat()
            try:
                meta = get_goal_history(conn, periodo, "VENDEDOR", vid)
                if not meta:
                    old_meta = conn.execute("SELECT meta_manual FROM metas_vendedores WHERE vendor_id=?", (vid,)).fetchone()
                    meta = float(old_meta["meta_manual"] or 0) if old_meta else 0.0
                fat_ciclo = float(fat_ciclo_map.get(vid, 0.0) or 0.0)
                c["fat_ciclo"] = fat_ciclo
                c["meta_mensal"] = float(meta or 0)
                c["meta_pct"] = (fat_ciclo / meta * 100.0) if meta else 0.0
                c["meta_delta_pct"] = ((fat_ciclo - meta) / meta * 100.0) if meta else None
            except Exception:
                pass

    # O score só é calculado DEPOIS de carregar a carteira oficial do vendedor.
    _performance_assign_scores(rows)

    return {
        "data_ref": data_ref, "ref": ref, "d0": d0, "d1": d1, "prev0": prev0, "prev1": prev1,
        "rows": rows, "catalog": catalog,
    }


def _performance_annual_score_map(conn, year: int, through_month: int, catalog=None):
    """Média dos scores mensais independentes de jan até o mês selecionado."""
    catalog = catalog or _performance_vendor_catalog(conn)
    vendors, _, _ = catalog
    active_ids = [int(v["id"]) for v in vendors if int(v.get("id") or 0) > 0 and (v.get("tipo") or "").upper() in ("INTERNO", "EXTERNO")]
    monthly = {vid: [] for vid in active_ids}
    through_month = max(1, min(12, int(through_month or 1)))
    for m in range(1, through_month + 1):
        ctx = _performance_build_context(conn, f"{year:04d}-{m:02d}", include_customer_mix=True, catalog=catalog)
        by_id = {int(r.get("id") or 0): float(r.get("score") or 0) for r in ctx["rows"] if int(r.get("id") or 0) > 0}
        for vid in active_ids:
            monthly[vid].append(by_id.get(vid, 0.0))
    return {vid: round(sum(vals) / len(vals), 1) if vals else 0.0 for vid, vals in monthly.items()}


def _performance_metric_rows(conn, vendor_id: int, data_ref: str, metric: str):
    data_ref, ref, d0, d1, _p0, _p1 = _performance_bounds(data_ref)
    metric = (metric or "orcamentos").strip().lower()
    catalog = _performance_vendor_catalog(conn)
    vendors, _by_id, by_code = catalog
    rows = []
    title = "Detalhamento"
    value_label = "Valor"

    if metric in {"orcamentos", "convertidos"}:
        title = "Orçamentos do mês" if metric == "orcamentos" else "Orçamentos convertidos em pedido"
        for o in conn.execute("""
            SELECT numero,cliente_nome_ref,cliente_nome,data_inicio,valor_total,pedido_id,desc_status,status,vendedor_id,vendedor_nome
            FROM orcamentos
            WHERE date(data_inicio) BETWEEN date(?) AND date(?)
            ORDER BY date(data_inicio) DESC, id DESC
        """, (d0.isoformat(), d1.isoformat())).fetchall():
            key, _meta = _performance_resolve_orc_vendor(vendors, by_code, o["vendedor_id"], o["vendedor_nome"])
            if key != ("V", vendor_id):
                continue
            etapa = _sisplan_stage(o["desc_status"], o["pedido_id"], o["status"])
            if metric == "convertidos" and etapa != "GANHO":
                continue
            rows.append({
                "ref": o["numero"] or "—", "data": o["data_inicio"] or "—",
                "cliente": o["cliente_nome_ref"] or o["cliente_nome"] or "—",
                "status": o["desc_status"] or etapa, "valor": float(o["valor_total"] or 0),
                "pedido": o["pedido_id"] or "", "orcamento": o["numero"] or "",
            })
    elif metric == "pedidos":
        title = "Pedidos realizados no mês"
        for r in conn.execute(f"""
            SELECT TRIM(COALESCE(o.pedido,'')) AS ref,
                   MAX(date(o.data_emissao)) AS data,
                   COALESCE(MAX(NULLIF(TRIM(o.cliente_nome_ref),'')), MAX(c.fantasia), MAX(c.nome), '—') AS cliente,
                   SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)) AS valor
            FROM orders o
            LEFT JOIN customers c ON c.id=o.cliente_id
            LEFT JOIN vendors v ON v.id=o.vendor_id
            WHERE o.vendor_id=? AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o','v')}
            GROUP BY TRIM(COALESCE(o.pedido,''))
            HAVING TRIM(COALESCE(o.pedido,''))<>''
            ORDER BY valor DESC, data DESC
        """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall():
            rows.append({"ref": r["ref"], "data": r["data"] or "—", "cliente": r["cliente"] or "—", "status": "PEDIDO", "valor": float(r["valor"] or 0), "pedido": r["ref"], "orcamento": ""})
    elif metric == "faturamento":
        title = "Faturamento do mês"
        for r in conn.execute("""
            SELECT COALESCE(NULLIF(TRIM(i.numero),''),'—') AS ref,
                   MAX(date(i.data_emissao)) AS data,
                   COALESCE(MAX(NULLIF(TRIM(i.cliente_nome_ref),'')), MAX(c.fantasia), MAX(c.nome), '—') AS cliente,
                   COALESCE(MAX(NULLIF(TRIM(i.pedido),'')),'') AS pedido,
                   SUM(COALESCE(i.valor,0)) AS valor
            FROM invoices i
            LEFT JOIN customers c ON c.id=i.cliente_id
            WHERE i.vendor_id=? AND date(i.data_emissao) BETWEEN date(?) AND date(?)
            GROUP BY COALESCE(NULLIF(TRIM(i.numero),''),'—'), date(i.data_emissao)
            ORDER BY valor DESC, data DESC
        """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchall():
            rows.append({"ref": r["ref"], "data": r["data"] or "—", "cliente": r["cliente"] or "—", "status": "FATURADO", "valor": float(r["valor"] or 0), "pedido": r["pedido"] or "", "orcamento": ""})
    else:
        metric = "orcamentos"
        return _performance_metric_rows(conn, vendor_id, data_ref, metric)

    total = sum(float(r.get("valor") or 0) for r in rows)
    maior = max([float(r.get("valor") or 0) for r in rows] + [0.0])
    maior_pct = (maior / total * 100.0) if total else 0.0
    return {"metric": metric, "title": title, "value_label": value_label, "rows": rows, "total": total, "maior": maior, "maior_pct": maior_pct, "data_ref": data_ref, "d0": d0, "d1": d1}


def _performance_manager_reading(r, team_rows):
    team_orc = sum(x["orc_qtd"] for x in team_rows)
    team_conv = sum(x["conv_qtd"] for x in team_rows)
    avg_conv = (team_conv / team_orc * 100.0) if team_orc else 0.0
    median_orc = statistics.median([x["orc_qtd"] for x in team_rows]) if team_rows else 0
    if r["orc_qtd"] < 5 and r["fat_valor"] > 0:
        return "Carteira gerando resultado com pouca geração nova", "O faturamento existe, mas há pouca atividade de propostas novas. Observe se a carteira está sendo apenas colhida sem reposição de pipeline."
    if median_orc > 0 and r["orc_qtd"] < median_orc * 0.55:
        return "Atividade abaixo do padrão do time", "A prioridade é elevar a geração de propostas e a cadência comercial antes de interpretar uma conversão alta como desempenho forte."
    if r["orc_qtd"] >= max(10, median_orc * 0.8) and avg_conv > 0 and r["conv_pct"] < avg_conv * 0.75:
        return "Esforço forte, fechamento pode melhorar", "Há volume e tentativa comercial. Vale revisar qualificação, preço, follow-up e motivos que travam o fechamento."
    if r["orc_qtd"] >= median_orc and r["conv_pct"] >= avg_conv and avg_conv > 0:
        return "Ritmo e aproveitamento consistentes", "Combina geração de oportunidades com capacidade de fechamento. É um padrão útil para replicar no time."
    if r.get("orc_delta") is not None and r.get("orc_delta", 0) < -20:
        return "Queda relevante de atividade", "O volume de propostas caiu bastante contra o período anterior. Investigue carteira, prospecção e rotina comercial."
    return "Desempenho intermediário", "Leia o conjunto: atividade, geração de pipeline, conversão, faturamento e evolução. Nenhum indicador isolado define a performance."


@app.route("/admin/performance-time")
@login_required
def performance_time():
    """Painel executivo de performance comercial. Exclusivo para ADMIN."""
    user = get_current_user()
    if not user or user["role"] != "admin":
        flash("Acesso restrito ao administrador.", "warning")
        return redirect(url_for("dashboard"))

    data_ref = (request.args.get("data_ref") or date.today().strftime("%Y-%m")).strip()
    tipo_filtro = (request.args.get("tipo") or "TODOS").strip().upper()
    if tipo_filtro not in ("TODOS", "INTERNO", "EXTERNO"):
        tipo_filtro = "TODOS"

    with conectar() as conn:
        ctx = _performance_build_context(conn, data_ref)
        annual_scores = _performance_annual_score_map(conn, ctx["ref"].year, ctx["ref"].month, ctx["catalog"])
        for _r in ctx["rows"]:
            _r["score_anual"] = float(annual_scores.get(int(_r.get("id") or 0), 0.0))
    data_ref = ctx["data_ref"]
    rows_all = ctx["rows"]
    d0, d1, prev0, prev1 = ctx["d0"], ctx["d1"], ctx["prev0"], ctx["prev1"]
    _meses_perf = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    score_mes_label = f"{_meses_perf[ctx['ref'].month-1]}/{str(ctx['ref'].year)[2:]}"

    internos = [r for r in rows_all if (r.get("tipo") or "").upper() == "INTERNO"]
    externos = [r for r in rows_all if (r.get("tipo") or "").upper() == "EXTERNO"]
    internos_ativos = [r for r in internos if any(float(r.get(k) or 0) for k in ("orc_qtd", "ped_qtd", "fat_qtd", "orc_valor", "fat_valor"))]
    externos_ativos = [r for r in externos if any(float(r.get(k) or 0) for k in ("orc_qtd", "ped_qtd", "fat_qtd", "orc_valor", "fat_valor"))]
    destaque_interno = max(internos_ativos, key=lambda x: x.get("score", 0), default=None)
    destaque_externo = max(externos_ativos, key=lambda x: x.get("score", 0), default=None)

    if tipo_filtro == "INTERNO":
        rows = internos
    elif tipo_filtro == "EXTERNO":
        rows = externos
    else:
        rows = [r for r in rows_all if (r.get("tipo") or "").upper() in ("INTERNO", "EXTERNO")]
    rows.sort(key=lambda x: (x.get("score", 0), x.get("orc_qtd", 0), x.get("fat_valor", 0)), reverse=True)

    team_orc = sum(r["orc_qtd"] for r in rows)
    team_conv = sum(r["conv_qtd"] for r in rows)
    team_fat = sum(r["fat_valor"] for r in rows)
    team_ped = sum(r["ped_qtd"] for r in rows)
    prev_orc = sum(r.get("prev_orc_qtd", 0) for r in rows)
    prev_conv = sum(r.get("prev_conv_qtd", 0) for r in rows)
    prev_fat = sum(r.get("prev_fat_valor", 0) for r in rows)
    team_conv_pct = (team_conv / team_orc * 100.0) if team_orc else 0.0
    prev_team_conv_pct = (prev_conv / prev_orc * 100.0) if prev_orc else 0.0
    team_conv_delta_pp = team_conv_pct - prev_team_conv_pct
    team_orc_delta = _performance_pct_delta(team_orc, prev_orc)
    team_fat_delta = _performance_pct_delta(team_fat, prev_fat)

    sellers_activity_up = sum(1 for r in rows if r.get("orc_delta") is not None and r.get("orc_delta", 0) > 0)
    sellers_conv_up = sum(1 for r in rows if r.get("conv_delta_pp", 0) > 0)
    sellers_both_down = sum(1 for r in rows if r.get("orc_delta") is not None and r.get("orc_delta", 0) < 0 and r.get("conv_delta_pp", 0) < 0)
    if team_orc_delta is not None and team_orc_delta > 0 and team_conv_delta_pp > 0:
        team_reading = "Atividade e aproveitamento cresceram no comparativo."
    elif team_orc_delta is not None and team_orc_delta > 0 and team_conv_delta_pp < 0:
        team_reading = "O time aumentou a atividade, mas converteu uma parcela menor das propostas."
    elif team_orc_delta is not None and team_orc_delta < 0 and team_conv_delta_pp > 0:
        team_reading = "A conversão melhorou, porém com menor volume de propostas."
    elif team_orc_delta is not None and team_orc_delta < 0 and team_conv_delta_pp < 0:
        team_reading = "Atividade e conversão recuaram; vale abrir os vendedores com queda para entender a causa."
    else:
        team_reading = "Os indicadores gerais estão próximos do período anterior; abra os vendedores para enxergar a evolução individual."

    for r in rows:
        own_team = internos if (r.get("tipo") or "").upper() == "INTERNO" else externos
        r["diagnostico"], r["acao"] = _performance_manager_reading(r, own_team)

    body = render_template_string(r"""
    <style>
      .perf-hero{background:linear-gradient(135deg,#0b1d36 0%,#123c69 58%,#0c7b8f 100%);color:#fff;border-radius:22px;padding:22px 24px;box-shadow:0 18px 45px rgba(14,45,80,.18)}
      .perf-switch .btn{border-radius:12px!important;font-weight:800;padding:.55rem 1rem}.perf-switch .btn.active{box-shadow:0 8px 18px rgba(37,99,235,.2)}
      .winner-card{border:0;border-radius:18px;padding:18px;background:linear-gradient(145deg,#fff,#f7fbff);box-shadow:0 10px 28px rgba(28,64,110,.10);height:100%}
      .seller-card{border:1px solid #e5edf6;border-radius:20px;background:#fff;box-shadow:0 10px 26px rgba(30,61,97,.07);transition:.18s ease;overflow:hidden}.seller-card:hover{transform:translateY(-2px);box-shadow:0 14px 34px rgba(30,61,97,.12)}
      .score-ring{width:84px;height:84px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(#2563eb calc(var(--score)*1%),#e9eff7 0);position:relative;flex:0 0 auto}.score-ring:after{content:"";position:absolute;width:64px;height:64px;border-radius:50%;background:#fff}.score-ring .score-num{position:relative;z-index:2;font-size:22px;font-weight:900;line-height:1}.score-ring .score-sub{position:relative;z-index:2;font-size:9px;color:#64748b;font-weight:800}
      .dimbar{height:7px;border-radius:99px;background:#edf2f8;overflow:hidden}.dimbar>span{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#2563eb,#06b6d4)}
      .perf-kpi{border-radius:14px;background:#f8fbff;border:1px solid #edf2f7;padding:10px 12px}.perf-kpi .v{font-size:18px;font-weight:900;color:#0f2745}.perf-kpi .l{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#64748b;font-weight:800}.perf-kpi-link{display:block;text-decoration:none;color:inherit;border-radius:14px}.perf-kpi-link:hover .perf-kpi{border-color:#8eb5ff;background:#f2f7ff;transform:translateY(-1px)}
      .score-help{font-size:11px;color:#64748b}
    </style>

    <div class="perf-hero mb-4">
      <div class="d-flex justify-content-between align-items-start flex-wrap gap-3">
        <div><div class="small text-uppercase fw-bold opacity-75">Gestão comercial</div><div class="h3 fw-bold mb-1">Performance do Time</div><div class="opacity-75">Esforço + qualidade + resultado. O score dá peso maior à geração real de propostas para não premiar uma conversão alta em amostra pequena.</div></div>
        <div class="d-flex gap-2 align-items-end flex-wrap"><form method="get" class="d-flex gap-2 align-items-end"><input type="hidden" name="tipo" value="{{ tipo_filtro }}"><div><label class="form-label mb-1 small text-white-50">MÊS</label><input type="month" class="form-control" name="data_ref" value="{{ data_ref }}"></div><button class="btn btn-light fw-bold">Aplicar</button></form><a class="btn btn-success" href="{{ url_for('relatorios_exportar',relatorio='performance',data_ref=data_ref,escopo=('TOTAL' if tipo_filtro=='TODOS' else tipo_filtro)) }}"><i class="bi bi-file-earmark-excel me-1"></i>Excel</a></div>
      </div>
      <div class="perf-switch d-flex gap-2 flex-wrap mt-3">
        <a class="btn {{ 'btn-light active' if tipo_filtro=='TODOS' else 'btn-outline-light' }}" href="{{ url_for('performance_time',data_ref=data_ref,tipo='TODOS') }}">Todos</a>
        <a class="btn {{ 'btn-light active' if tipo_filtro=='INTERNO' else 'btn-outline-light' }}" href="{{ url_for('performance_time',data_ref=data_ref,tipo='INTERNO') }}"><i class="bi bi-building me-1"></i>Time interno</a>
        <a class="btn {{ 'btn-light active' if tipo_filtro=='EXTERNO' else 'btn-outline-light' }}" href="{{ url_for('performance_time',data_ref=data_ref,tipo='EXTERNO') }}"><i class="bi bi-car-front me-1"></i>Time externo</a>
      </div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-12 col-xl-6"><div class="winner-card"><div class="d-flex justify-content-between align-items-center"><div><div class="small text-uppercase text-muted fw-bold">Destaque do mês · Interno</div>{% if destaque_interno %}<a href="{{ url_for('performance_vendedor_detalhe',vendor_id=destaque_interno.id,data_ref=data_ref) }}" class="h5 fw-bold text-decoration-none d-block mt-1">{{ destaque_interno.nome }}</a><div class="small text-muted">{{ destaque_interno.orc_qtd }} orçamentos · {{ '%.1f'|format(destaque_interno.conv_pct) }}% conversão · {{ format_brl(destaque_interno.fat_valor) }}</div>{% else %}<div class="text-muted mt-2">Sem dados.</div>{% endif %}</div>{% if destaque_interno %}<div class="score-ring" style="--score:{{ destaque_interno.score }}"><div class="text-center"><div class="score-num">{{ '%.0f'|format(destaque_interno.score) }}</div><div class="score-sub">{{ score_mes_label }}</div></div></div><div class="small text-muted mt-1 text-center">Anual {{ '%.0f'|format(destaque_interno.score_anual) }}</div>{% endif %}</div></div></div>
      <div class="col-12 col-xl-6"><div class="winner-card"><div class="d-flex justify-content-between align-items-center"><div><div class="small text-uppercase text-muted fw-bold">Destaque do mês · Externo</div>{% if destaque_externo %}<a href="{{ url_for('performance_vendedor_detalhe',vendor_id=destaque_externo.id,data_ref=data_ref) }}" class="h5 fw-bold text-decoration-none d-block mt-1">{{ destaque_externo.nome }}</a><div class="small text-muted">{{ destaque_externo.orc_qtd }} orçamentos · {{ '%.1f'|format(destaque_externo.conv_pct) }}% conversão · {{ format_brl(destaque_externo.fat_valor) }}</div>{% else %}<div class="text-muted mt-2">Sem dados.</div>{% endif %}</div>{% if destaque_externo %}<div class="score-ring" style="--score:{{ destaque_externo.score }}"><div class="text-center"><div class="score-num">{{ '%.0f'|format(destaque_externo.score) }}</div><div class="score-sub">{{ score_mes_label }}</div></div></div><div class="small text-muted mt-1 text-center">Anual {{ '%.0f'|format(destaque_externo.score_anual) }}</div>{% endif %}</div></div></div>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Orçamentos criados</div><div class="metric-value">{{ team_orc }}</div><div class="small text-muted">{{ fmt_delta(team_orc_delta) }} vs período anterior</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Conversão</div><div class="metric-value">{{ '%.1f'|format(team_conv_pct) }}%</div><div class="small text-muted">{{ team_conv }} de {{ team_orc }} propostas</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Faturado no período</div><div class="metric-value">{{ format_brl(team_fat) }}</div><div class="small text-muted">{{ fmt_delta(team_fat_delta) }} vs período anterior</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3 h-100"><div class="metric-label">Pedidos</div><div class="metric-value">{{ team_ped }}</div><div class="small text-muted">{{ rows|length }} vendedores com atividade</div></div></div>
    </div>

    <div class="cardx p-3 mb-4"><div class="d-flex justify-content-between align-items-start flex-wrap gap-3"><div><div class="section-title">Sinal geral do time</div><div class="mt-1">{{ team_reading }}</div></div><div class="d-flex gap-3 flex-wrap small"><div><span class="text-muted">Volume:</span> <b>{{ fmt_delta(team_orc_delta) }}</b></div><div><span class="text-muted">Conversão:</span> <b>{{ '%+.1f pp'|format(team_conv_delta_pp) }}</b></div><div><span class="text-muted">Faturamento:</span> <b>{{ fmt_delta(team_fat_delta) }}</b></div></div></div><div class="small text-muted mt-2">{{ sellers_activity_up }} aumentaram propostas · {{ sellers_conv_up }} melhoraram conversão · {{ sellers_both_down }} recuaram em volume e conversão.</div></div>

    <div class="d-flex justify-content-between align-items-end mb-3"><div><div class="h5 fw-bold mb-1">Vendedores</div><div class="muted">Clique no nome para abrir evolução, gráficos e composição do score.</div></div><div class="score-help">Score: 50% atividade · 15% pipeline · 10% conversão ajustada · 10% resultado · 10% carteira · 5% evolução</div></div>
    <div class="row g-3">
      {% for r in rows %}
      <div class="col-12 col-xxl-6"><div class="seller-card p-3 h-100">
        <div class="d-flex gap-3 align-items-start">
          <div><div class="score-ring" style="--score:{{ r.score }}"><div class="text-center"><div class="score-num">{{ '%.0f'|format(r.score) }}</div><div class="score-sub">{{ score_mes_label }}</div></div></div><div class="small text-muted text-center mt-1">Anual <b>{{ '%.0f'|format(r.score_anual) }}</b></div></div>
          <div class="flex-grow-1 min-width-0"><div class="d-flex justify-content-between gap-2 align-items-start"><div><a class="h5 fw-bold text-decoration-none mb-0 d-block" href="{{ url_for('performance_vendedor_detalhe',vendor_id=r.id,data_ref=data_ref) if r.id else '#' }}">{{ r.nome }}</a><div class="small text-muted">{{ r.tipo }}{% if r.codigo %} · {{ r.codigo }}{% endif %}</div></div><span class="badge text-bg-{{ r.score_tone }}">{{ r.score_label }}</span></div>
            <div class="row g-2 mt-2"><div class="col-4"><a class="perf-kpi-link" href="{{ url_for('performance_vendedor_metrica',vendor_id=r.id,metrica='orcamentos',data_ref=data_ref) if r.id else '#' }}"><div class="perf-kpi"><div class="l">Orçamentos</div><div class="v">{{ r.orc_qtd }}</div></div></a></div><div class="col-4"><a class="perf-kpi-link" href="{{ url_for('performance_vendedor_metrica',vendor_id=r.id,metrica='convertidos',data_ref=data_ref) if r.id else '#' }}"><div class="perf-kpi"><div class="l">Conversão</div><div class="v">{{ '%.1f'|format(r.conv_pct) }}%</div></div></a></div><div class="col-4"><a class="perf-kpi-link" href="{{ url_for('performance_vendedor_metrica',vendor_id=r.id,metrica='faturamento',data_ref=data_ref) if r.id else '#' }}"><div class="perf-kpi"><div class="l">Faturado</div><div class="v" style="font-size:15px">{{ format_brl(r.fat_valor) }}</div></div></a></div></div>
          </div>
        </div>
        <div class="row g-2 mt-3 small"><div class="col-6"><span class="text-muted">Valor orçado:</span> <b>{{ format_brl(r.orc_valor) }}</b></div><div class="col-6"><a class="text-decoration-none" href="{{ url_for('performance_vendedor_metrica',vendor_id=r.id,metrica='pedidos',data_ref=data_ref) if r.id else '#' }}"><span class="text-muted">Pedidos:</span> <b>{{ r.ped_qtd }}</b> <i class="bi bi-box-arrow-up-right ms-1"></i></a></div><div class="col-6"><span class="text-muted">Clientes no mês:</span> <b class="text-success">{{ r.clientes_novos }} novos</b> · <b>{{ r.clientes_repeticao }} recorrentes</b>{% if r.clientes_recuperados %} <span class="text-primary">({{ r.clientes_recuperados }} recuperados)</span>{% endif %}</div><div class="col-6"><span class="text-muted">Abertos:</span> <b>{{ r.aberto_qtd }}</b> · <span class="text-muted">Δ volume:</span> <b>{{ fmt_delta(r.orc_delta) }}</b></div></div>
        <div class="mt-3 pt-3 border-top"><div class="fw-bold small">{{ r.diagnostico }}</div><div class="small text-muted mt-1">{{ r.acao }}</div></div>
      </div></div>
      {% else %}<div class="col-12"><div class="cardx p-4 text-center text-muted">Sem atividade para esta equipe no período.</div></div>{% endfor %}
    </div>
    """, rows=rows, data_ref=data_ref, tipo_filtro=tipo_filtro, d0=d0, d1=d1, prev0=prev0, prev1=prev1,
       destaque_interno=destaque_interno, destaque_externo=destaque_externo,
       team_orc=team_orc, team_conv=team_conv, team_conv_pct=team_conv_pct, team_fat=team_fat, team_ped=team_ped,
       team_orc_delta=team_orc_delta, team_conv_delta_pp=team_conv_delta_pp, team_fat_delta=team_fat_delta,
       sellers_activity_up=sellers_activity_up, sellers_conv_up=sellers_conv_up, sellers_both_down=sellers_both_down,
       team_reading=team_reading, format_brl=format_brl, score_mes_label=score_mes_label,
       fmt_delta=lambda v: "—" if v is None else f"{v:+.1f}%")
    return render_page("Performance do Time", body, "performance_time")


@app.route("/admin/performance-time/vendedor/<int:vendor_id>")
@login_required
def performance_vendedor_detalhe(vendor_id):
    user = get_current_user()
    if not user or user["role"] != "admin":
        flash("Acesso restrito ao administrador.", "warning")
        return redirect(url_for("dashboard"))
    data_ref = (request.args.get("data_ref") or date.today().strftime("%Y-%m")).strip()
    with conectar() as conn:
        ctx = _performance_build_context(conn, data_ref)
        annual_scores = _performance_annual_score_map(conn, ctx["ref"].year, ctx["ref"].month, ctx["catalog"])
        row = next((r for r in ctx["rows"] if r.get("id") == vendor_id), None)
        if not row:
            flash("Vendedor sem dados no período selecionado.", "warning")
            return redirect(url_for("performance_time", data_ref=ctx["data_ref"]))
        vendor = conn.execute("SELECT id,codigo,nome,tipo FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not vendor:
            abort(404)
        catalog = ctx["catalog"]
        row["score_anual"] = float(annual_scores.get(vendor_id, 0.0))
        score_mes_label = f"{['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'][ctx['ref'].month-1]}/{str(ctx['ref'].year)[2:]}"
        team_rows = [r for r in ctx["rows"] if (r.get("tipo") or "").upper() == (row.get("tipo") or "").upper()]
        diagnostico, acao = _performance_manager_reading(row, team_rows)

        labels, hist_orc, hist_conv_qtd, hist_conv_pct, hist_fat, hist_score = [], [], [], [], [], []
        ref = ctx["ref"]
        month_cursor = ref
        months = []
        for _ in range(6):
            months.append(month_cursor)
            if month_cursor.month == 1:
                month_cursor = date(month_cursor.year - 1, 12, 1)
            else:
                month_cursor = date(month_cursor.year, month_cursor.month - 1, 1)
        months.reverse()
        meses_pt = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        for m in months:
            mctx = _performance_build_context(conn, f"{m.year:04d}-{m.month:02d}", include_customer_mix=True, catalog=catalog)
            x = next((rr for rr in mctx["rows"] if int(rr.get("id") or 0) == vendor_id), {})
            oq = int(x.get("orc_qtd", 0) or 0)
            cq = int(x.get("conv_qtd", 0) or 0)
            labels.append(f"{meses_pt[m.month-1]}/{str(m.year)[2:]}")
            hist_orc.append(oq)
            hist_conv_qtd.append(cq)
            hist_conv_pct.append(float(x.get("conv_pct", 0) or 0))
            hist_fat.append(float(x.get("fat_valor", 0) or 0))
            hist_score.append(float(x.get("score", 0) or 0))

        chart_atividade = line_chart(labels, [("Orçamentos", hist_orc), ("Convertidos", hist_conv_qtd)], "Evolução de atividade")
        chart_conversao = line_chart(labels, [("Conversão %", hist_conv_pct)], "Evolução da conversão")
        chart_faturamento = line_chart(labels, [("Faturamento", hist_fat)], "Evolução do faturamento")
        chart_score = line_chart(labels, [("Score mensal", hist_score)], "Score mensal")

        vendors, by_id, by_code = catalog
        orc_recent = []
        for o in conn.execute("""
            SELECT numero,cliente_nome_ref,cliente_nome,data_inicio,valor_total,pedido_id,desc_status,status,vendedor_id,vendedor_nome
            FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?) ORDER BY date(data_inicio) DESC, id DESC
        """, (ctx["d0"].isoformat(), ctx["d1"].isoformat())).fetchall():
            key, _meta = _performance_resolve_orc_vendor(vendors, by_code, o["vendedor_id"], o["vendedor_nome"])
            if key == ("V", vendor_id):
                od = dict(o)
                od["etapa"] = _sisplan_stage(o["desc_status"], o["pedido_id"], o["status"])
                orc_recent.append(od)
                if len(orc_recent) >= 12:
                    break

    body = render_template_string(r"""
    <style>
      .pd-hero{background:linear-gradient(135deg,#0b1d36,#174c82 60%,#0c7b8f);color:white;border-radius:22px;padding:22px 24px;box-shadow:0 18px 45px rgba(14,45,80,.18)}
      .pd-score{font-size:52px;font-weight:950;line-height:1}.pd-dim{margin-bottom:12px}.pd-dim .track{height:8px;border-radius:99px;background:#e9eff6;overflow:hidden}.pd-dim .track span{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#2563eb,#06b6d4)}
    </style>
    <div class="pd-hero mb-4"><div class="d-flex justify-content-between gap-3 align-items-start flex-wrap"><div><div class="small text-uppercase fw-bold opacity-75">Análise individual · {{ row.tipo }}</div><div class="h3 fw-bold mb-1">{{ row.nome }}</div><div class="opacity-75">Período {{ d0 }} a {{ d1 }} · comparação com {{ prev0 }} a {{ prev1 }}</div></div><div class="text-end"><div class="pd-score">{{ '%.0f'|format(row.score) }}</div><div class="small fw-bold">SCORE {{ score_mes_label }} / 100</div><span class="badge text-bg-{{ row.score_tone }} mt-1">{{ row.score_label }}</span><div class="small mt-2 opacity-75">Score anual: <b>{{ '%.0f'|format(row.score_anual) }}</b></div></div></div></div>

    <div class="row g-3 mb-4"><div class="col-6 col-xl-3"><a class="text-decoration-none text-reset" href="{{ url_for('performance_vendedor_metrica',vendor_id=row.id,metrica='orcamentos',data_ref=data_ref) }}"><div class="cardx p-3 h-100"><div class="metric-label">Orçamentos</div><div class="metric-value">{{ row.orc_qtd }}</div><div class="small text-muted">{{ fmt_delta(row.orc_delta) }} vs anterior · clique para abrir</div></div></a></div><div class="col-6 col-xl-3"><a class="text-decoration-none text-reset" href="{{ url_for('performance_vendedor_metrica',vendor_id=row.id,metrica='convertidos',data_ref=data_ref) }}"><div class="cardx p-3 h-100"><div class="metric-label">Conversão</div><div class="metric-value">{{ '%.1f'|format(row.conv_pct) }}%</div><div class="small text-muted">{{ '%+.1f pp'|format(row.conv_delta_pp) }} vs anterior · clique para abrir</div></div></a></div><div class="col-6 col-xl-3"><a class="text-decoration-none text-reset" href="{{ url_for('performance_vendedor_metrica',vendor_id=row.id,metrica='orcamentos',data_ref=data_ref) }}"><div class="cardx p-3 h-100"><div class="metric-label">Valor orçado</div><div class="metric-value" style="font-size:24px">{{ format_brl(row.orc_valor) }}</div><div class="small text-muted">{{ row.aberto_qtd }} propostas ainda abertas · clique para abrir</div></div></a></div><div class="col-6 col-xl-3"><a class="text-decoration-none text-reset" href="{{ url_for('performance_vendedor_metrica',vendor_id=row.id,metrica='faturamento',data_ref=data_ref) }}"><div class="cardx p-3 h-100"><div class="metric-label">Faturado</div><div class="metric-value" style="font-size:24px">{{ format_brl(row.fat_valor) }}</div><div class="small text-muted">{{ row.ped_qtd }} pedidos no período · clique para abrir</div></div></a></div></div>

    <div class="row g-3 mb-4"><div class="col-12 col-xl-5"><div class="cardx p-4 h-100"><div class="section-title mb-1">Composição do score</div><div class="small text-muted mb-3">O peso maior continua sendo atividade. A carteira usa a mesma fonte Novo/Recompra da Visão do Vendedor; cliente novo e recuperado pesam mais que recorrência normal.</div>
      {% for label,key,weight in [('Atividade / propostas','atividade','50%'),('Geração de pipeline','pipeline','15%'),('Conversão ajustada','conversao','10%'),('Resultado faturado','resultado','10%'),('Carteira: novos / recuperados / recorrentes','carteira','10%'),('Evolução','evolucao','5%')] %}<div class="pd-dim"><div class="d-flex justify-content-between small mb-1"><b>{{ label }}</b><span>{{ '%.0f'|format(row.score_dims[key]) }} / 100 · peso {{ weight }}</span></div><div class="track"><span style="width:{{ row.score_dims[key] }}%"></span></div></div>{% endfor %}
      <div class="alert alert-light border mt-3 mb-0"><div class="fw-bold">{{ diagnostico }}</div><div class="small text-muted mt-1">{{ acao }}</div></div>
    </div></div><div class="col-12 col-xl-7"><div class="cardx p-4 h-100"><div class="section-title mb-1">Leitura gerencial</div><div class="small text-muted mb-3">O score ajuda a localizar padrões; o gestor deve abrir propostas e causas antes de decidir qualquer ação.</div><div class="row g-2"><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Convertidos</div><div class="h4 fw-bold mb-0">{{ row.conv_qtd }}</div></div></div><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Perdidos</div><div class="h4 fw-bold mb-0">{{ row.perdido_qtd }}</div></div></div><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Clientes no mês</div><div class="h6 fw-bold mb-0"><span class="text-success">{{ row.clientes_novos }} novos</span> · {{ row.clientes_repeticao }} recorrentes{% if row.clientes_recuperados %} · <span class="text-primary">{{ row.clientes_recuperados }} recuperados</span>{% endif %}</div></div></div><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Meta de faturamento</div>{% if row.meta_mensal>0 %}<div class="h6 fw-bold mb-0">{{ format_brl(row.fat_ciclo) }} / {{ format_brl(row.meta_mensal) }}</div><div class="small {{ 'text-success' if row.meta_delta_pct>=0 else 'text-danger' }}">{{ '%.1f'|format(row.meta_delta_pct|abs) }}% {{ 'acima' if row.meta_delta_pct>=0 else 'abaixo' }} da meta</div>{% else %}<div class="text-muted">Sem meta definida</div>{% endif %}</div></div><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Ticket faturado</div><div class="h5 fw-bold mb-0">{{ format_brl(row.ticket_fat) }}</div></div></div><div class="col-6"><div class="p-3 rounded-3 bg-light"><div class="small text-muted">Variação faturamento</div><div class="h5 fw-bold mb-0">{{ fmt_delta(row.fat_delta) }}</div></div></div></div></div></div></div>

    <div class="row g-3 mb-4"><div class="col-12"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Score mensal</div><div class="viz-sub">Cada mês recebe um score novo e independente. O score anual é a média dos meses do ano até o período selecionado.</div></div></div><div class="viz-body">{% if chart_score %}<img src="data:image/png;base64,{{ chart_score }}">{% endif %}</div></div></div><div class="col-12"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Atividade comercial</div><div class="viz-sub">Orçamentos emitidos e quantos viraram pedido nos últimos 6 meses.</div></div></div><div class="viz-body">{% if chart_atividade %}<img src="data:image/png;base64,{{ chart_atividade }}">{% endif %}</div></div></div><div class="col-12 col-xl-6"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Conversão</div><div class="viz-sub">Taxa mensal de aproveitamento das propostas.</div></div></div><div class="viz-body">{% if chart_conversao %}<img src="data:image/png;base64,{{ chart_conversao }}">{% endif %}</div></div></div><div class="col-12 col-xl-6"><div class="viz-card"><div class="viz-head"><div><div class="viz-title">Faturamento</div><div class="viz-sub">Resultado realizado nos últimos 6 meses.</div></div></div><div class="viz-body">{% if chart_faturamento %}<img src="data:image/png;base64,{{ chart_faturamento }}">{% endif %}</div></div></div></div>

    <div class="cardx p-0 overflow-hidden mb-4"><div class="p-3 border-bottom d-flex justify-content-between align-items-center"><div><div class="section-title">Propostas do período</div><div class="section-sub">Abra uma proposta para entender o que está em negociação.</div></div><a class="btn btn-outline-secondary btn-sm" href="{{ url_for('performance_time',data_ref=data_ref,tipo=row.tipo.upper()) }}">Voltar ao time</a></div><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Nº</th><th>Cliente</th><th>Data</th><th>Status</th><th class="text-end">Valor</th><th></th></tr></thead><tbody>{% for o in orc_recent %}<tr><td class="fw-bold">{{ o.numero }}</td><td>{{ o.cliente_nome_ref or o.cliente_nome or '—' }}</td><td>{{ o.data_inicio or '—' }}</td><td><span class="badge {{ 'text-bg-success' if o.etapa=='GANHO' else ('text-bg-danger' if o.etapa=='PERDIDO' else 'text-bg-primary') }}">{{ o.desc_status or o.etapa }}</span></td><td class="text-end fw-bold">{{ format_brl(o.valor_total or 0) }}</td><td class="text-end"><a class="btn btn-sm btn-outline-primary" href="{{ url_for('orcamento_detalhe',numero=o.numero,origem='performance',data_ref=data_ref) }}">Abrir</a></td></tr>{% else %}<tr><td colspan="6" class="text-center py-4 text-muted">Sem propostas no período.</td></tr>{% endfor %}</tbody></table></div></div>
    """, row=row, diagnostico=diagnostico, acao=acao, data_ref=ctx["data_ref"], d0=ctx["d0"], d1=ctx["d1"], prev0=ctx["prev0"], prev1=ctx["prev1"],
       chart_atividade=chart_atividade, chart_conversao=chart_conversao, chart_faturamento=chart_faturamento, chart_score=chart_score, orc_recent=orc_recent, score_mes_label=score_mes_label,
       format_brl=format_brl, fmt_delta=lambda v: "—" if v is None else f"{v:+.1f}%")
    return render_page(f"Performance · {row['nome']}", body, "performance_time")


@app.route("/admin/performance-time/vendedor/<int:vendor_id>/detalhe/<metrica>")
@login_required
def performance_vendedor_metrica(vendor_id, metrica):
    user = get_current_user()
    if not user or user.get("role") != "admin":
        flash("Acesso restrito ao administrador.", "warning")
        return redirect(url_for("dashboard"))
    data_ref = (request.args.get("data_ref") or date.today().strftime("%Y-%m")).strip()
    with conectar() as conn:
        vendor = conn.execute("SELECT id,codigo,nome,tipo FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not vendor:
            abort(404)
        det = _performance_metric_rows(conn, vendor_id, data_ref, metrica)

    body = render_template_string(r"""
    <style>
      .dist-hero{background:linear-gradient(135deg,#0b1d36,#174c82 60%,#0c7b8f);color:white;border-radius:20px;padding:20px 22px;box-shadow:0 16px 38px rgba(14,45,80,.16)}
      .dist-stat{background:#fff;border:1px solid #e6edf6;border-radius:16px;padding:14px 16px;height:100%}.dist-stat .n{font-size:22px;font-weight:900;color:#102944}.dist-stat .l{font-size:11px;color:#64748b;font-weight:800;text-transform:uppercase;letter-spacing:.05em}
    </style>
    <div class="dist-hero mb-4"><div class="d-flex justify-content-between align-items-start gap-3 flex-wrap"><div><div class="small text-uppercase fw-bold opacity-75">Detalhamento de performance</div><div class="h3 fw-bold mb-1">{{ vendor['nome'] }}</div><div class="opacity-75">{{ det.title }} · {{ det.d0 }} a {{ det.d1 }}</div></div><a class="btn btn-light" href="{{ url_for('performance_vendedor_detalhe',vendor_id=vendor['id'],data_ref=det.data_ref) }}">Voltar</a></div></div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-xl-3"><div class="dist-stat"><div class="l">Registros</div><div class="n">{{ det.rows|length }}</div></div></div>
      <div class="col-6 col-xl-3"><div class="dist-stat"><div class="l">Valor total</div><div class="n" style="font-size:19px">{{ format_brl(det.total) }}</div></div></div>
      <div class="col-6 col-xl-3"><div class="dist-stat"><div class="l">Maior registro</div><div class="n" style="font-size:19px">{{ format_brl(det.maior) }}</div></div></div>
      <div class="col-6 col-xl-3"><div class="dist-stat"><div class="l">Concentração do maior</div><div class="n">{{ '%.1f'|format(det.maior_pct) }}%</div><div class="small text-muted">do valor total</div></div></div>
    </div>

    <div class="cardx p-0 overflow-hidden"><div class="p-3 border-bottom"><div class="section-title">Composição do indicador</div><div class="section-sub">Use esta lista para enxergar se o resultado está distribuído em vários clientes/negócios ou concentrado em poucos registros.</div></div>
      <div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Referência</th><th>Data</th><th>Cliente</th><th>Status</th><th>Pedido</th><th class="text-end">Valor</th><th></th></tr></thead><tbody>
      {% for r in det.rows %}<tr><td class="fw-bold">{{ r.ref }}</td><td>{{ r.data }}</td><td>{{ r.cliente }}</td><td><span class="badge bg-light text-dark border">{{ r.status }}</span></td><td>{{ r.pedido or '—' }}</td><td class="text-end fw-bold">{{ format_brl(r.valor) }}</td><td class="text-end">{% if r.orcamento %}<a class="btn btn-sm btn-outline-primary" href="{{ url_for('orcamento_detalhe',numero=r.orcamento,origem='performance',data_ref=det.data_ref) }}">Abrir</a>{% endif %}</td></tr>{% else %}<tr><td colspan="7" class="text-center py-5 text-muted">Nenhum registro neste indicador para o mês selecionado.</td></tr>{% endfor %}
      </tbody></table></div>
    </div>
    """, vendor=vendor, det=det, format_brl=format_brl)
    return render_page(f"Performance · {vendor['nome']} · {det['title']}", body, "performance_time")


@app.route("/leads")
@login_required
def leads():
    """Oportunidades derivadas diretamente do relatório de ORÇAMENTOS do Sisplan."""
    user = get_current_user()
    data_ref = (request.args.get("data_ref") or date.today().strftime("%Y-%m")).strip()
    try:
        ref_date = datetime.strptime(data_ref, "%Y-%m").date()
    except Exception:
        ref_date = date.today().replace(day=1)
        data_ref = ref_date.strftime("%Y-%m")
    d0, d1 = periodo_mes_calendario(ref_date)
    q = (request.args.get("q") or "").strip()
    etapa_filter = (request.args.get("etapa") or "").strip().upper()
    vendedor_nome = (request.args.get("vendedor_nome") or "").strip()
    mostrar_fechadas = (request.args.get("mostrar_fechadas") or "") == "1" or etapa_filter in SISPLAN_FINAL_STAGES

    with conectar() as conn:
        scope = _crm_scope_vendor_id(conn, user)
        rows_all = _sisplan_period_rows(conn, d0, d1, scope_vendor_id=scope, vendor_name=vendedor_nome, q=q)
        metrics = _sisplan_metrics_from_rows(rows_all)
        base_rows = rows_all if mostrar_fechadas else [r for r in rows_all if r["etapa_crm"] not in SISPLAN_FINAL_STAGES]
        rows = [r for r in base_rows if not etapa_filter or r["etapa_crm"] == etapa_filter]
        total_filtrado = len(rows)
        rows = rows[:500]
        if scope is None:
            vendors = conn.execute("""
                SELECT DISTINCT vendedor_nome AS nome
                FROM orcamentos
                WHERE data_inicio BETWEEN ? AND ? AND COALESCE(TRIM(vendedor_nome),'')<>''
                ORDER BY vendedor_nome
            """, (d0.isoformat(), d1.isoformat())).fetchall()
        else:
            vendors = []

    body = render_template_string(r"""
    <div class="tech-panel tech-grid p-4 mb-4">
      <div class="row align-items-center g-3 position-relative" style="z-index:1">
        <div class="col-12 col-xl-7">
          <div class="text-uppercase small fw-bold mb-2" style="color:#63dce8;letter-spacing:.12em">Sisplan Opportunity Intelligence</div>
          <div class="h3 fw-bold mb-2">Oportunidades do Sisplan</div>
          <div style="color:#a9b9cd;max-width:760px">Nada é cadastrado em duplicidade. Esta tela transforma os orçamentos já importados do Sisplan em uma visão comercial priorizada.</div>
        </div>
        <div class="col-12 col-xl-5"><div class="row g-2 text-center">
          <div class="col-4"><div class="p-2 rounded-3" style="background:rgba(255,255,255,.06)"><div class="small" style="color:#8da2bb">ABERTAS</div><div class="h4 mb-0">{{ metrics.active }}</div></div></div>
          <div class="col-4"><div class="p-2 rounded-3" style="background:rgba(255,255,255,.06)"><div class="small" style="color:#8da2bb">PIPELINE</div><div class="fw-bold">{{ format_brl(metrics.pipeline) }}</div></div></div>
          <div class="col-4"><div class="p-2 rounded-3" style="background:rgba(255,255,255,.06)"><div class="small" style="color:#8da2bb">14+ DIAS</div><div class="h4 mb-0 {{ 'text-warning' if metrics.stale else '' }}">{{ metrics.stale }}</div></div></div>
        </div></div>
      </div>
    </div>

    <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-3">
      <div><div class="section-title">Oportunidades comerciais</div><div class="section-sub">Fonte única: ORÇAMENTO do Sisplan. Atualize os CSVs e o funil acompanha automaticamente.</div></div>
      <div class="d-flex gap-2"><a class="btn btn-outline-secondary" href="{{ url_for('leads', data_ref=data_ref, mostrar_fechadas=(0 if mostrar_fechadas else 1)) }}"><i class="bi bi-archive me-1"></i>{{ 'Somente abertas' if mostrar_fechadas else 'Histórico / fechadas' }}</a><a class="btn btn-outline-primary" href="{{ url_for('funil', data_ref=data_ref) }}"><i class="bi bi-kanban me-1"></i>Ver funil</a><a class="btn btn-primary" href="{{ url_for('importar_csv') }}"><i class="bi bi-arrow-repeat me-1"></i>Atualizar Sisplan</a></div>
    </div>

    <form class="cardx p-3 mb-3" method="get">{% if mostrar_fechadas %}<input type="hidden" name="mostrar_fechadas" value="1">{% endif %}<div class="row g-2 align-items-end">
      <div class="col-12 col-lg-4"><label class="form-label">BUSCAR</label><input class="form-control" name="q" value="{{ q }}" placeholder="Cliente, orçamento ou pedido"></div>
      <div class="col-6 col-lg-2"><label class="form-label">MÊS</label><input class="form-control" type="month" name="data_ref" value="{{ data_ref }}"></div>
      <div class="col-6 col-lg-2"><label class="form-label">ETAPA</label><select class="form-select" name="etapa"><option value="">Todas</option>{% for key in stage_order %}<option value="{{ key }}" {% if etapa_filter==key %}selected{% endif %}>{{ stage_labels[key] }}</option>{% endfor %}</select></div>
      {% if can_all %}<div class="col-8 col-lg-2"><label class="form-label">VENDEDOR</label><select class="form-select" name="vendedor_nome"><option value="">Todos</option>{% for v in vendors %}<option value="{{ v['nome'] }}" {% if vendedor_nome==v['nome'] %}selected{% endif %}>{{ v['nome'] }}</option>{% endfor %}</select></div>{% endif %}
      <div class="col-4 col-lg-2"><button class="btn btn-outline-primary w-100"><i class="bi bi-funnel me-1"></i>Filtrar</button></div>
    </div></form>

    <div class="cardx overflow-hidden"><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Orçamento / Cliente</th><th>Etapa CRM</th><th>Status Sisplan</th><th>Vendedor</th><th>Data</th><th class="text-end">Valor</th><th></th></tr></thead><tbody>
      {% for r in rows %}<tr>
        <td><div class="fw-bold">#{{ r.numero or '—' }}</div><div class="small text-muted">{{ r.cliente_exibir }}</div></td>
        <td><span class="chip {% if r.etapa_crm=='GANHO' %}chip-success{% elif r.etapa_crm=='PERDIDO' %}chip-danger{% endif %}"><i class="bi {{ stage_icons[r.etapa_crm] }}"></i>{{ stage_labels[r.etapa_crm] }}</span>{% if r.etapa_crm not in ['GANHO','PERDIDO','ENCERRADO'] and r.dias_aberto>=14 %}<span class="stale-badge ms-1">{{ r.dias_aberto }}d</span>{% endif %}</td>
        <td><div class="small fw-semibold">{{ r.desc_status or 'SEM STATUS' }}</div>{% if r.pedido_id and r.pedido_id!='0' %}<div class="small text-success">Pedido {{ r.pedido_id }}</div>{% endif %}</td>
        <td class="small">{{ r.vendedor_nome or '—' }}</td><td class="small text-muted">{{ r.data_inicio or '—' }}</td>
        <td class="text-end fw-bold">{{ format_brl(r.valor_total or 0) }}</td>
        <td class="text-end"><a class="btn btn-sm btn-light border" href="{{ url_for('orcamento_detalhe', numero=r.numero, origem='oportunidades', data_ref=data_ref) }}">Abrir <i class="bi bi-arrow-up-right ms-1"></i></a></td>
      </tr>{% else %}<tr><td colspan="7" class="text-center py-5 text-muted"><i class="bi bi-inbox fs-2 d-block mb-2"></i>Nenhuma oportunidade neste filtro.</td></tr>{% endfor %}
    </tbody></table></div></div>
    {% if total_filtrado > rows|length %}<div class="small text-muted mt-2">Mostrando os 500 registros mais recentes de {{ total_filtrado }}. Use os filtros para refinar.</div>{% endif %}
    """, rows=rows, total_filtrado=total_filtrado, q=q, data_ref=data_ref, etapa_filter=etapa_filter, vendedor_nome=vendedor_nome,
         vendors=vendors, can_all=(scope is None), metrics=metrics, mostrar_fechadas=mostrar_fechadas, stage_order=SISPLAN_STAGE_ORDER, stage_labels=SISPLAN_STAGE_LABELS,
         stage_icons=SISPLAN_STAGE_ICONS, format_brl=format_brl)
    return render_page("Oportunidades", body, "leads")


@app.route("/lead/<int:lead_id>", methods=["GET", "POST"])
@login_required
def lead_detalhe(lead_id):
    user = get_current_user()
    with conectar() as conn:
        lead = _crm_can_access_lead(conn, lead_id, user)
        if not lead:
            flash("Lead não encontrado ou sem acesso.", "warning")
            return redirect(url_for("leads"))
        scope = _crm_scope_vendor_id(conn, user)
        if request.method == "POST":
            action = (request.form.get("action") or "update").strip()
            if action == "activity":
                desc = (request.form.get("descricao") or "").strip()
                if desc:
                    _crm_log_activity(conn, lead_id, (request.form.get("tipo") or "NOTA").strip().upper(), desc, user.get("nome"), (request.form.get("agendado_em") or "").strip() or None, 0 if request.form.get("agendado_em") else 1)
                    conn.commit(); flash("Atividade registrada.", "success")
                return redirect(url_for("lead_detalhe", lead_id=lead_id))
            if action == "done_activity":
                aid = safe_int(request.form.get("activity_id"), 0)
                conn.execute("UPDATE crm_lead_activities SET concluido=1 WHERE id=? AND lead_id=?", (aid, lead_id))
                conn.commit(); flash("Atividade concluída.", "success")
                return redirect(url_for("lead_detalhe", lead_id=lead_id))

            old_stage = lead["etapa"]
            new_stage = _crm_stage(request.form.get("etapa"))
            vendor_id = safe_int(request.form.get("vendor_id"), 0) or None
            if scope is not None: vendor_id = scope
            closed_at = now_iso() if new_stage in ("GANHO", "PERDIDO") else None
            conn.execute("""
                UPDATE crm_leads SET nome=?,empresa=?,telefone=?,email=?,origem=?,vendor_id=?,etapa=?,temperatura=?,valor_estimado=?,probabilidade=?,proxima_acao=?,proxima_acao_em=?,observacoes=?,motivo_perda=?,updated_at=?,closed_at=? WHERE id=?
            """, ((request.form.get("nome") or "").strip(), (request.form.get("empresa") or "").strip(), (request.form.get("telefone") or "").strip(), (request.form.get("email") or "").strip(),
                  (request.form.get("origem") or "Outro").strip(), vendor_id, new_stage, _crm_temp(request.form.get("temperatura")), safe_float(request.form.get("valor_estimado"),0),
                  max(0,min(100,safe_int(request.form.get("probabilidade"),CRM_STAGE_PROB.get(new_stage,10)))), (request.form.get("proxima_acao") or "").strip(), (request.form.get("proxima_acao_em") or "").strip() or None,
                  (request.form.get("observacoes") or "").strip(), (request.form.get("motivo_perda") or "").strip(), now_iso(), closed_at, lead_id))
            if old_stage != new_stage:
                _crm_log_activity(conn, lead_id, "NOTA", f"Etapa alterada de {CRM_STAGE_LABELS.get(old_stage,old_stage)} para {CRM_STAGE_LABELS.get(new_stage,new_stage)}.", user.get("nome"))
            conn.commit(); flash("Lead atualizado.", "success")
            return redirect(url_for("lead_detalhe", lead_id=lead_id))

        lead = conn.execute("SELECT l.*,v.nome AS vendedor_nome FROM crm_leads l LEFT JOIN vendors v ON v.id=l.vendor_id WHERE l.id=?", (lead_id,)).fetchone()
        activities = conn.execute("SELECT * FROM crm_lead_activities WHERE lead_id=? ORDER BY COALESCE(agendado_em,created_at) DESC,id DESC LIMIT 100", (lead_id,)).fetchall()
        vendors = conn.execute("SELECT id,nome FROM vendors WHERE ativo=1 ORDER BY nome").fetchall() if scope is None else conn.execute("SELECT id,nome FROM vendors WHERE id=?", (scope,)).fetchall()

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-start flex-wrap gap-2 mb-4"><div><a href="{{ url_for('leads') }}" class="small text-decoration-none"><i class="bi bi-arrow-left me-1"></i>Leads</a><div class="h3 fw-bold mb-1 mt-1">{{ lead['nome'] }}</div><div class="muted">{{ lead['empresa'] or 'Sem empresa' }} · {{ lead['vendedor_nome'] or 'Sem responsável' }}</div></div><div class="d-flex gap-2"><span class="chip"><span class="temp-dot temp-{{ lead['temperatura'] }}"></span>{{ lead['temperatura'].title() }}</span><span class="chip {% if lead['etapa']=='GANHO' %}chip-success{% elif lead['etapa']=='PERDIDO' %}chip-danger{% endif %}">{{ stage_labels[lead['etapa']] }}</span></div></div>
    <div class="row g-3">
      <div class="col-12 col-xl-8"><div class="cardx p-4"><div class="d-flex justify-content-between mb-3"><div><div class="section-title">Dados da oportunidade</div><div class="section-sub">Mantenha o responsável, etapa e próxima ação atualizados.</div></div><div class="metric-value">{{ format_brl(lead['valor_estimado'] or 0) }}</div></div>
      <form method="post"><input type="hidden" name="action" value="update"><div class="row g-3">
        <div class="col-md-6"><label class="form-label">NOME</label><input class="form-control" name="nome" value="{{ lead['nome'] }}" required></div><div class="col-md-6"><label class="form-label">EMPRESA</label><input class="form-control" name="empresa" value="{{ lead['empresa'] or '' }}"></div>
        <div class="col-md-4"><label class="form-label">ETAPA</label><select class="form-select" name="etapa" id="leadStage">{% for key,label in stage_labels.items() %}<option value="{{ key }}" {% if lead['etapa']==key %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></div><div class="col-md-4"><label class="form-label">TEMPERATURA</label><select class="form-select" name="temperatura">{% for t in ['FRIO','MORNO','QUENTE'] %}<option {% if lead['temperatura']==t %}selected{% endif %}>{{ t }}</option>{% endfor %}</select></div><div class="col-md-4"><label class="form-label">PROBABILIDADE %</label><input class="form-control" name="probabilidade" type="number" min="0" max="100" value="{{ lead['probabilidade'] }}"></div>
        <div class="col-md-4"><label class="form-label">POTENCIAL R$</label><input class="form-control" name="valor_estimado" type="number" step="0.01" value="{{ lead['valor_estimado'] or 0 }}"></div><div class="col-md-4"><label class="form-label">ORIGEM</label><select class="form-select" name="origem">{% for o in sources %}<option {% if lead['origem']==o %}selected{% endif %}>{{ o }}</option>{% endfor %}</select></div>{% if can_all %}<div class="col-md-4"><label class="form-label">RESPONSÁVEL</label><select class="form-select" name="vendor_id"><option value="">Sem responsável</option>{% for v in vendors %}<option value="{{ v['id'] }}" {% if lead['vendor_id']==v['id'] %}selected{% endif %}>{{ v['nome'] }}</option>{% endfor %}</select></div>{% endif %}
        <div class="col-md-6"><label class="form-label">TELEFONE</label><input class="form-control" name="telefone" value="{{ lead['telefone'] or '' }}"></div><div class="col-md-6"><label class="form-label">E-MAIL</label><input class="form-control" name="email" value="{{ lead['email'] or '' }}"></div>
        <div class="col-md-8"><label class="form-label">PRÓXIMA AÇÃO</label><input class="form-control" name="proxima_acao" value="{{ lead['proxima_acao'] or '' }}" placeholder="O próximo passo deve estar sempre claro"></div><div class="col-md-4"><label class="form-label">DATA</label><input class="form-control" name="proxima_acao_em" type="date" value="{{ lead['proxima_acao_em'] or '' }}"></div>
        <div class="col-12"><label class="form-label">OBSERVAÇÕES</label><textarea class="form-control" name="observacoes" rows="4">{{ lead['observacoes'] or '' }}</textarea></div><div class="col-12" id="lossWrap" {% if lead['etapa']!='PERDIDO' %}style="display:none"{% endif %}><label class="form-label">MOTIVO DA PERDA</label><input class="form-control" name="motivo_perda" value="{{ lead['motivo_perda'] or '' }}" placeholder="Preço, prazo, concorrente, sem retorno…"></div>
        <div class="col-12 text-end"><button class="btn btn-primary px-4"><i class="bi bi-check2 me-1"></i>Salvar alterações</button></div>
      </div></form></div></div>

      <div class="col-12 col-xl-4"><div class="cardx p-4 mb-3"><div class="section-title mb-3">Próxima ação</div>{% if lead['proxima_acao'] %}<div class="p-3 rounded-3 {% if date_state(lead['proxima_acao_em'])=='overdue' %}bg-danger-subtle{% elif date_state(lead['proxima_acao_em'])=='today' %}bg-warning-subtle{% else %}bg-light{% endif %}"><div class="fw-bold">{{ lead['proxima_acao'] }}</div><div class="small mt-1">{% if lead['proxima_acao_em'] %}<i class="bi bi-calendar-event me-1"></i>{{ lead['proxima_acao_em'][8:10] }}/{{ lead['proxima_acao_em'][5:7] }}/{{ lead['proxima_acao_em'][0:4] }}{% else %}Sem data definida{% endif %}</div></div>{% else %}<div class="text-muted small">Nenhuma próxima ação definida. Negócios sem próximo passo tendem a esfriar.</div>{% endif %}</div>
      <div class="cardx p-4"><div class="section-title mb-3">Registrar atividade</div><form method="post"><input type="hidden" name="action" value="activity"><div class="mb-2"><select class="form-select" name="tipo">{% for t in activity_types %}<option>{{ t }}</option>{% endfor %}</select></div><div class="mb-2"><textarea class="form-control" name="descricao" rows="3" required placeholder="Ex.: cliente pediu retorno com prazo e preço revisados"></textarea></div><div class="mb-3"><label class="form-label">AGENDAR PARA (OPCIONAL)</label><input class="form-control" type="datetime-local" name="agendado_em"></div><button class="btn btn-outline-primary w-100">Adicionar à timeline</button></form></div></div>
    </div>

    <div class="cardx p-4 mt-3"><div class="d-flex justify-content-between align-items-center mb-3"><div><div class="section-title">Timeline comercial</div><div class="section-sub">Histórico de contatos, mudanças de etapa e próximos compromissos.</div></div><span class="chip">{{ activities|length }} registros</span></div><div class="timeline">{% for a in activities %}<div class="timeline-item"><div class="d-flex justify-content-between gap-3"><div><div class="fw-bold">{{ a['tipo'] }} <span class="small text-muted fw-normal">· {{ a['created_by'] or 'Sistema' }}</span></div><div class="mt-1">{{ a['descricao'] }}</div><div class="small text-muted mt-1">Criado em {{ a['created_at'] }}{% if a['agendado_em'] %} · Agendado: {{ a['agendado_em'] }}{% endif %}</div></div>{% if not a['concluido'] %}<form method="post"><input type="hidden" name="action" value="done_activity"><input type="hidden" name="activity_id" value="{{ a['id'] }}"><button class="btn btn-sm btn-outline-success"><i class="bi bi-check2"></i></button></form>{% else %}<span class="chip chip-success h-100">Concluído</span>{% endif %}</div></div>{% else %}<div class="muted">Sem atividades ainda.</div>{% endfor %}</div></div>
    <script>document.getElementById('leadStage')?.addEventListener('change',function(){document.getElementById('lossWrap').style.display=this.value==='PERDIDO'?'block':'none';});</script>
    """, lead=lead, activities=activities, vendors=vendors, can_all=(scope is None), stage_labels=CRM_STAGE_LABELS, sources=CRM_SOURCES, activity_types=CRM_ACTIVITY_TYPES, date_state=_crm_date_state, format_brl=format_brl)
    return render_page("Lead 360", body, "leads")


@app.route("/funil")
@login_required
def funil():
    """Funil somente leitura, calculado a partir dos status do Sisplan."""
    user = get_current_user()
    data_ref = (request.args.get("data_ref") or date.today().strftime("%Y-%m")).strip()
    try:
        ref_date = datetime.strptime(data_ref, "%Y-%m").date()
    except Exception:
        ref_date = date.today().replace(day=1)
        data_ref = ref_date.strftime("%Y-%m")
    d0, d1 = periodo_mes_calendario(ref_date)
    vendedor_nome = (request.args.get("vendedor_nome") or "").strip()

    with conectar() as conn:
        scope = _crm_scope_vendor_id(conn, user)
        rows = _sisplan_period_rows(conn, d0, d1, scope_vendor_id=scope, vendor_name=vendedor_nome)
        metrics = _sisplan_metrics_from_rows(rows)
        board_stages = ["NOVO", "CONTATO", "NEGOCIACAO", "APROVACAO"]
        board = {s: [] for s in board_stages}
        for r in rows:
            if r["etapa_crm"] in board:
                board[r["etapa_crm"]].append(r)
        for stage in board_stages:
            board[stage] = sorted(board[stage], key=lambda x: (float(x.get("valor_total") or 0), str(x.get("data_inicio") or "")), reverse=True)[:120]
        if scope is None:
            vendors = conn.execute("""
                SELECT DISTINCT vendedor_nome AS nome
                FROM orcamentos
                WHERE data_inicio BETWEEN ? AND ? AND COALESCE(TRIM(vendedor_nome),'')<>''
                ORDER BY vendedor_nome
            """, (d0.isoformat(), d1.isoformat())).fetchall()
        else:
            vendors = []

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-start flex-wrap gap-3 mb-4">
      <div><div class="h3 fw-bold mb-1">Funil Comercial · Sisplan</div><div class="muted">Visão automática e somente leitura dos orçamentos importados. Para alterar o estágio real da negociação, altere no Sisplan e atualize os CSVs.</div></div>
      <form method="get" class="d-flex gap-2 align-items-end flex-wrap">
        <div><label class="form-label mb-1 small">MÊS</label><input type="month" class="form-control" name="data_ref" value="{{ data_ref }}"></div>
        {% if can_all %}<div><label class="form-label mb-1 small">VENDEDOR</label><select class="form-select" name="vendedor_nome"><option value="">Todos</option>{% for v in vendors %}<option value="{{ v['nome'] }}" {% if vendedor_nome==v['nome'] %}selected{% endif %}>{{ v['nome'] }}</option>{% endfor %}</select></div>{% endif %}
        <button class="btn btn-outline-primary"><i class="bi bi-funnel"></i></button>
        <a class="btn btn-primary" href="{{ url_for('importar_csv') }}"><i class="bi bi-arrow-repeat me-1"></i>Atualizar Sisplan</a>
      </form>
    </div>

    <div class="row g-3 mb-4">
      <div class="col-6 col-xl-3"><div class="cardx p-3"><div class="metric-label">Pipeline aberto</div><div class="metric-value">{{ format_brl(metrics.pipeline) }}</div><div class="small text-muted">{{ metrics.active }} oportunidades</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3"><div class="metric-label">Forecast estimado</div><div class="metric-value text-primary">{{ format_brl(metrics.weighted) }}</div><div class="small text-muted">ponderado pela etapa Sisplan</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3"><div class="metric-label">Conversão</div><div class="metric-value">{{ '%.1f'|format(metrics.conversion) }}%</div><div class="small text-muted">{{ metrics.won }} ganhos / {{ metrics.closed }} fechados</div></div></div>
      <div class="col-6 col-xl-3"><div class="cardx p-3"><div class="metric-label">Parados 14+ dias</div><div class="metric-value {{ 'text-danger' if metrics.stale else '' }}">{{ metrics.stale }}</div><div class="small text-muted">oportunidades abertas antigas</div></div></div>
    </div>

    <div class="alert alert-light border d-flex align-items-center gap-2 py-2 mb-3"><i class="bi bi-database-check text-primary"></i><div class="small"><b>Fonte única de verdade:</b> nenhum card é criado manualmente neste CRM. O funil é reconstruído a partir do relatório ORÇAMENTO do Sisplan.</div></div>

    <div class="pipeline-wrap"><div class="pipeline-board">
      {% for stage in board_stages %}<div class="pipeline-col"><div class="pipeline-head"><div class="pipeline-name"><i class="bi {{ stage_icons[stage] }} me-1"></i>{{ stage_labels[stage] }}</div><div><span class="pipeline-count">{{ board[stage]|length }}</span></div></div>
      {% for r in board[stage] %}<div class="deal-card">
        <a class="text-decoration-none" href="{{ url_for('orcamento_detalhe', numero=r.numero, origem='funil', data_ref=data_ref) }}" title="Abrir orçamento detalhado">
          <div class="d-flex justify-content-between align-items-start gap-2"><div class="min-w-0"><div class="deal-name">#{{ r.numero or '—' }} · {{ r.cliente_exibir }}</div><div class="deal-company">{{ r.desc_status or 'SEM STATUS' }}</div></div>{% if r.dias_aberto>=14 %}<span class="stale-badge">{{ r.dias_aberto }}d</span>{% endif %}</div>
          <div class="d-flex justify-content-between align-items-end mt-3"><div><div class="deal-value">{{ format_brl(r.valor_total or 0) }}</div><div class="deal-meta">{{ r.probabilidade }}% estimado · {{ r.vendedor_nome or 'Sem vendedor' }}</div></div></div>
          <div class="deal-meta mt-2"><i class="bi bi-calendar3 me-1"></i>{{ r.data_inicio or '—' }}{% if r.tipo_contato %} · {{ r.tipo_contato }}{% endif %}</div>
        </a>
      </div>{% else %}<div class="text-center py-4 small text-muted"><i class="bi bi-inbox"></i><br>Nenhuma oportunidade</div>{% endfor %}
      </div>{% endfor %}
    </div></div>
    <div class="small text-muted mt-3"><i class="bi bi-info-circle me-1"></i>O percentual exibido é uma estimativa visual por etapa; não altera nenhum dado do Sisplan.</div>
    """, board=board, board_stages=board_stages, stage_labels=SISPLAN_STAGE_LABELS, stage_icons=SISPLAN_STAGE_ICONS,
         metrics=metrics, vendors=vendors, vendedor_nome=vendedor_nome, data_ref=data_ref, can_all=(scope is None), format_brl=format_brl)
    return render_page("Funil Sisplan", body, "funil")


@app.route("/api/leads/<int:lead_id>/stage", methods=["POST"])
@login_required
def api_lead_stage(lead_id):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    new_stage = _crm_stage(payload.get("stage"))
    with conectar() as conn:
        lead = _crm_can_access_lead(conn, lead_id, user)
        if not lead:
            return jsonify({"ok": False, "error": "not_found"}), 404
        old_stage = lead["etapa"]
        if new_stage == old_stage:
            return jsonify({"ok": True})
        prob = CRM_STAGE_PROB.get(new_stage, int(lead["probabilidade"] or 0))
        closed = now_iso() if new_stage in ("GANHO", "PERDIDO") else None
        conn.execute("UPDATE crm_leads SET etapa=?,probabilidade=?,updated_at=?,closed_at=? WHERE id=?", (new_stage, prob, now_iso(), closed, lead_id))
        _crm_log_activity(conn, lead_id, "NOTA", f"Etapa alterada de {CRM_STAGE_LABELS.get(old_stage,old_stage)} para {CRM_STAGE_LABELS.get(new_stage,new_stage)}.", user.get("nome"))
        conn.commit()
    return jsonify({"ok": True, "stage": new_stage, "probability": prob})


def calcular_metricas_recompra_tarefa(conn, cliente_id=None, identity_key=""):
    """Calcula ticket médio e atraso de recompra para exibir na tela de tarefas.
    Usa a identidade comercial quando existir; se não existir, usa somente o cliente da tarefa.
    Retorna dict seguro mesmo quando não houver histórico suficiente.
    """
    from datetime import datetime

    def parse_dt(v):
        if not v:
            return None
        txt = str(v).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(txt, fmt)
            except Exception:
                pass
        return None

    ids = set()
    if cliente_id:
        try:
            ids.add(int(cliente_id))
        except Exception:
            pass

    ik = (identity_key or "").strip()
    if ik:
        try:
            for r in conn.execute("""
                SELECT customer_id
                FROM customer_identity_rules
                WHERE COALESCE(identity_key,'') = ?
            """, (ik,)).fetchall():
                if r["customer_id"]:
                    ids.add(int(r["customer_id"]))
        except Exception:
            pass

    if not ids:
        return {
            "ticket_medio": 0.0,
            "dias_fora_media": None,
            "media_dias_recompra": None,
            "ultima_compra": None,
            "qtd_compras": 0,
            "atrasado": False,
        }

    placeholders = ",".join(["?"] * len(ids))
    params = list(ids)

    def buscar_movimentos(tabela):
        try:
            return conn.execute(f"""
                SELECT data_emissao, COALESCE(valor,0) AS valor
                FROM {tabela}
                WHERE cliente_id IN ({placeholders})
                  AND COALESCE(valor,0) > 0
                  AND COALESCE(data_emissao,'') <> ''
                ORDER BY data_emissao ASC
            """, params).fetchall()
        except Exception:
            return []

    movimentos = buscar_movimentos("invoices")
    # Se não houver faturamento suficiente, usa pedidos como fallback para não deixar a tarefa muda.
    if len(movimentos) < 2:
        movimentos = buscar_movimentos("orders")

    compras_por_dia = {}
    for m in movimentos:
        dt = parse_dt(m["data_emissao"])
        if not dt:
            continue
        key = dt.strftime("%Y-%m-%d")
        compras_por_dia[key] = compras_por_dia.get(key, 0.0) + float(m["valor"] or 0)

    datas = sorted([parse_dt(d) for d in compras_por_dia.keys() if parse_dt(d)])
    valores = [compras_por_dia[d.strftime("%Y-%m-%d")] for d in datas]

    qtd = len(datas)
    ticket_medio = (sum(valores) / qtd) if qtd else 0.0
    ultima = datas[-1] if datas else None

    media_dias = None
    dias_fora = None
    atrasado = False

    if qtd >= 2:
        intervalos = [(datas[i] - datas[i-1]).days for i in range(1, len(datas)) if (datas[i] - datas[i-1]).days >= 0]
        if intervalos:
            media_dias = round(sum(intervalos) / len(intervalos), 1)
            dias_desde = (datetime.now() - ultima).days if ultima else 0
            dias_fora_calc = int(round(dias_desde - media_dias))
            if dias_fora_calc > 0:
                dias_fora = dias_fora_calc
                atrasado = True
            else:
                dias_fora = 0

    return {
        "ticket_medio": ticket_medio,
        "dias_fora_media": dias_fora,
        "media_dias_recompra": media_dias,
        "ultima_compra": ultima.strftime("%Y-%m-%d") if ultima else None,
        "qtd_compras": qtd,
        "atrasado": atrasado,
    }

@app.route("/tarefas", methods=["GET", "POST"])
@login_required
def tarefas():
    user = get_current_user()

    ALL_STATUS = ["ABERTA", "EM CONTATO", "AGUARDANDO RESPOSTA", "NEGOCIANDO", "CONCLUÍDA", "CANCELADA"]

    current_status = normalize_task_status(request.args.get("status", "ABERTA"))
    filter_vendor = request.args.get("vendor_id", "").strip()
    q = (request.args.get("q") or "").strip()
    ignorar_jit = (request.args.get("ignorar_jit") or "").strip() in ["1", "true", "on", "sim"]
    filtro_atraso = (request.args.get("filtro_atraso") or "").strip() in ["1", "true", "on", "sim"]
    page = max(1, safe_int(request.args.get("page"), 1))
    per_page = 100

    with conectar() as conn:
        user_vendor_id = get_user_vendor_id(conn, user)
        score_map = build_identity_score_map(conn, user=user)

        is_admin = (user["role"] == "admin")
        is_gestor = (user["role"] == "gestor")
        can_manage = (is_admin or is_gestor)

        target_vendor_id = None
        if not can_manage:
            target_vendor_id = user_vendor_id
        elif filter_vendor:
            try:
                target_vendor_id = int(filter_vendor)
            except:
                target_vendor_id = None

        # -----------------------------
        # POST
        # -----------------------------
        if request.method == "POST":
            # 1) Transferência em massa por identidade comercial
            if "bulk_transfer" in request.form and can_manage:
                task_ids = request.form.getlist("selected_tasks")
                target_vid_raw = request.form.get("target_vendor_id", "").strip()

                try:
                    target_vid = int(target_vid_raw)
                except:
                    target_vid = 0

                if not task_ids:
                    flash("Selecione ao menos uma tarefa para transferir.", "warning")
                elif target_vid <= 0:
                    flash("Selecione o novo vendedor responsável.", "warning")
                else:
                    moved, new_vendor_name = transfer_tasks_by_identity(
                        conn=conn,
                        selected_task_ids=task_ids,
                        target_vendor_id=target_vid,
                        actor_name=user["nome"],
                    )
                    conn.commit()

                    if moved > 0:
                        flash(
                            f"{moved} tarefa(s) transferida(s) com sucesso para {new_vendor_name}. "
                            f"Tarefas da mesma identidade comercial aberta também foram levadas junto.",
                            "success"
                        )
                    else:
                        flash("Nenhuma tarefa precisou ser transferida.", "info")

            # 2) Editar observação
            elif "edit_obs" in request.form:
                hist_id = request.form.get("hist_id")
                nova_obs = request.form.get("observacao", "").strip()

                if hist_id:
                    conn.execute(
                        "UPDATE task_history SET observacao=? WHERE id=?",
                        (nova_obs, hist_id)
                    )
                    conn.commit()
                    flash("Observação atualizada.", "success")

            # 3) Movimentação individual
            elif "new_status" in request.form:
                task_id = request.form.get("task_id")
                new_status = normalize_task_status(request.form.get("new_status"))
                observacao = request.form.get("observacao", "").strip()

                if task_id and new_status:
                    curr = conn.execute("""
                        SELECT id, status, vendor_id, cliente_id, COALESCE(identity_key, '') AS identity_key
                        FROM tasks
                        WHERE id = ?
                    """, (task_id,)).fetchone()

                    if curr:
                        old_status = normalize_task_status(curr["status"])

                        if can_manage:
                            conn.execute("""
                                UPDATE tasks
                                SET status=?, updated_at=?
                                WHERE id=?
                            """, (new_status, now_iso(), task_id))
                        else:
                            conn.execute("""
                                UPDATE tasks
                                SET status=?, updated_at=?
                                WHERE id=? AND vendor_id=?
                            """, (new_status, now_iso(), task_id, user_vendor_id))

                        if old_status != new_status or observacao:
                            conn.execute("""
                                INSERT INTO task_history (
                                    task_id, status_anterior, status_novo,
                                    observacao, alterado_por, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?)
                            """, (
                                task_id,
                                old_status,
                                new_status,
                                observacao,
                                user["nome"],
                                now_iso()
                            ))

                        conn.commit()
                        flash(f"Tarefa atualizada para {new_status}.", "success")

            return redirect(url_for(
                "tarefas",
                status=current_status,
                vendor_id=filter_vendor,
                q=q,
                ignorar_jit='1' if ignorar_jit else '',
                filtro_atraso='1' if filtro_atraso else '',
                page=page
            ))

        # -----------------------------
        # GET / dados auxiliares
        # -----------------------------
        vendors_list = []
        if can_manage:
            vendors_list = conn.execute("""
                SELECT id, nome
                FROM vendors
                WHERE COALESCE(ativo, 1) = 1
                ORDER BY nome
            """).fetchall()

        # -----------------------------
        # Query principal unificada
        # -----------------------------
        where_parts = ["t.status = ?"]
        params = [current_status]

        if target_vendor_id is not None:
            where_parts.append("t.vendor_id = ?")
            params.append(target_vendor_id)

        if ignorar_jit:
            where_parts.append("""
                NOT EXISTS (
                    SELECT 1
                    FROM orders oj
                    LEFT JOIN customer_identity_rules cirj ON cirj.customer_id = oj.cliente_id
                    WHERE (
                        oj.cliente_id = t.cliente_id
                        OR (COALESCE(t.identity_key,'') <> '' AND COALESCE(cirj.identity_key,'') = COALESCE(t.identity_key,''))
                    )
                    AND UPPER(REPLACE(TRIM(COALESCE(oj.condicao_entrega,'')),' ','')) LIKE '05%JIT%IMEDIATO%'
                )
            """)

        if q:
            like = f"%{q.upper()}%"
            where_parts.append("""
                (
                    UPPER(COALESCE(t.titulo, '')) LIKE ?
                    OR UPPER(COALESCE(t.descricao, '')) LIKE ?
                    OR UPPER(COALESCE(c.nome, '')) LIKE ?
                    OR UPPER(COALESCE(c.fantasia, '')) LIKE ?
                    OR UPPER(COALESCE(v.nome, '')) LIKE ?
                    OR UPPER(COALESCE(t.identity_key, '')) LIKE ?
                )
            """)
            params.extend([like, like, like, like, like, like])

        where_sql = " AND ".join(where_parts)

        raw_rows = conn.execute(f"""
            SELECT
                t.id,
                t.titulo,
                t.descricao,
                t.status,
                t.vendor_id,
                t.cliente_id,
                COALESCE(t.identity_key, '') AS identity_key,
                t.created_at,
                t.updated_at,
                v.nome AS vendedor,
                COALESCE(c.fantasia, c.nome, '') AS cliente
            FROM tasks t
            JOIN vendors v ON v.id = t.vendor_id
            LEFT JOIN customers c ON c.id = t.cliente_id
            WHERE {where_sql}
            ORDER BY
                COALESCE(t.updated_at, t.created_at) DESC,
                t.id DESC
        """, params).fetchall()

        # A ordenação depende de score/recompra, então enriquecemos os registros primeiro.
        # O histórico (mais pesado) é carregado somente para a página visível.
        rows = []
        for r in raw_rows:
            task = dict(r)

            identity_key = (task.get("identity_key") or "").strip()
            cliente_id = task.get("cliente_id")

            identity_nome = get_identity_display_name(
                conn,
                identity_key=identity_key,
                fallback_customer_id=cliente_id
            )

            identity_tipo = "CLIENTE"
            if identity_key.startswith("GRUPO:"):
                identity_tipo = "GRUPO"
            elif identity_key.startswith("REDE:"):
                identity_tipo = "REDE"

            task["history"] = []
            task["ultima_obs"] = None
            task["identity_nome"] = identity_nome
            task["identity_tipo"] = identity_tipo
            task["identity_label"] = identity_key or "SEM_IDENTIDADE"
            
            sc = score_map.get(identity_key, {})
            task["score"] = sc.get("score", 0)
            task["score_faixa"] = sc.get("faixa", "D")
            task["score_label"] = sc.get("label", "Baixa prioridade")
            task["score_cor"] = sc.get("cor", "secondary")
            task["score_recompra"] = sc.get("score_recompra", 0)
            task["score_valor"] = sc.get("score_valor", 0)

            metricas_recompra = calcular_metricas_recompra_tarefa(
                conn,
                cliente_id=cliente_id,
                identity_key=identity_key,
            )
            task["ticket_medio"] = metricas_recompra.get("ticket_medio", 0.0)
            task["dias_fora_media"] = metricas_recompra.get("dias_fora_media")
            task["media_dias_recompra"] = metricas_recompra.get("media_dias_recompra")
            task["ultima_compra"] = metricas_recompra.get("ultima_compra")
            task["qtd_compras"] = metricas_recompra.get("qtd_compras", 0)
            task["atrasado_recompra"] = metricas_recompra.get("atrasado", False)

            prio = get_task_priority_from_score(sc)

            # Correção anti-contradição:
            # A prioridade/ título antigo podia dizer "fora da média" enquanto a métrica
            # recalculada mostrava "Dentro da média". A tela agora confia sempre na
            # métrica recalculada em tempo real para exibir título e motivo.
            esta_fora_media = int(task.get("dias_fora_media") or 0) > 0
            titulo_original = str(task.get("titulo") or "")
            nome_identidade = str(task.get("identity_nome") or "Cliente").strip() or "Cliente"

            task["prioridade"] = prio.get("prioridade", "BAIXA")
            task["prioridade_cor"] = prio.get("cor", "secondary")
            task["prioridade_ordem"] = prio.get("ordem", 9)
            task["prioridade_motivo"] = prio.get("motivo", "")

            if esta_fora_media:
                task["titulo_exibicao"] = f"[{task['prioridade']}] Cliente fora da média de recompra: {nome_identidade}"
                if not task["prioridade_motivo"] or "fora" not in task["prioridade_motivo"].lower():
                    task["prioridade_motivo"] = "Cliente fora da janela ideal de recompra"
            else:
                # Remove mensagens antigas persistidas no banco que dizem "fora da média".
                if "fora da média" in titulo_original.lower() or "fora da media" in titulo_original.lower():
                    task["titulo_exibicao"] = f"[{task['prioridade']}] Cliente dentro da média de recompra: {nome_identidade}"
                else:
                    task["titulo_exibicao"] = titulo_original
                if "fora" in str(task["prioridade_motivo"]).lower():
                    if task["prioridade"] in ("URGENTE",):
                        task["prioridade"] = "ALTA"
                        task["prioridade_cor"] = "warning"
                        task["prioridade_ordem"] = 1
                    task["prioridade_motivo"] = "Cliente dentro da janela esperada de recompra"

            task["row_bg"] = "#ffffff"
            task["row_border"] = soft_border_from_bootstrap(task["score_cor"])

            rows.append(task)

        if filtro_atraso:
            # Mostra somente clientes realmente fora da média de recompra
            # e ordena do mais atrasado para o menos atrasado.
            rows = [r for r in rows if int(r.get("dias_fora_media") or 0) > 0]
            rows.sort(
                key=lambda x: (
                    -int(x.get("dias_fora_media") or 0),
                    str(x.get("identity_nome") or "").upper()
                )
            )
        else:
            rows.sort(
                key=lambda x: (
                    int(x.get("prioridade_ordem", 9)),
                    -int(x.get("score", 0) or 0),
                    str(x.get("updated_at") or x.get("created_at") or "")
                ),
                reverse=False
            )

        total_rows = len(rows)
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        rows = rows[start:start + per_page]

        # Histórico em lote somente das tarefas realmente exibidas.
        history_map = defaultdict(list)
        task_ids = [int(r["id"]) for r in rows]
        if task_ids:
            for i in range(0, len(task_ids), 800):
                lote = task_ids[i:i+800]
                ph = ",".join("?" for _ in lote)
                hist_rows = conn.execute(f"""
                    SELECT id, task_id, status_anterior, status_novo, observacao, created_at, alterado_por
                    FROM task_history
                    WHERE task_id IN ({ph})
                    ORDER BY task_id, created_at DESC, id DESC
                """, lote).fetchall()
                for h in hist_rows:
                    history_map[int(h["task_id"])].append(h)

        for task in rows:
            history = history_map.get(int(task["id"]), [])
            task["history"] = history
            task["ultima_obs"] = next((h["observacao"] for h in history if h["observacao"]), None)

    body = render_template_string("""
    <style>
        .timeline-sm { border-left: 2px solid #dee2e6; margin-left: 10px; padding-left: 15px; }
        .timeline-item { position: relative; margin-bottom: 10px; }
        .timeline-dot { width: 8px; height: 8px; background: #adb5bd; border-radius: 50%; position: absolute; left: -20px; top: 6px; border: 2px solid white; }
        .tr-history { background-color: #f8f9fa; box-shadow: inset 0 0 5px rgba(0,0,0,0.05); }
        .big-check { transform: scale(1.3); cursor: pointer; }
        .transfer-mode-hidden { display: none !important; }
        .floating-action {
            position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            z-index: 1050; background: white; padding: 15px 25px;
            border-radius: 50px; box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            display: none; align-items: center; gap: 15px; border: 1px solid #dee2e6;
        }
    </style>

    <div class="page-hero mb-4">
      <div class="d-flex flex-column flex-md-row justify-content-between align-items-md-center gap-3 position-relative" style="z-index:1">
      <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Central de execução</div><div class="hero-title">Tarefas</div><div class="hero-sub mt-1">Prioridades, recompra e movimentações comerciais em uma fila de trabalho limpa.</div></div>
      <div class="d-flex gap-2 flex-wrap">
          <a href="{{ url_for('historico_tarefas') }}" class="btn btn-outline-secondary d-flex align-items-center gap-2">
            📜 Histórico de Casos
          </a>
          {% if can_manage %}
          <a href="{{ url_for('diagnostico') }}" class="btn btn-outline-danger d-flex align-items-center gap-2">
            🧪 Diagnóstico
          </a>
          {% endif %}
          {% if can_manage %}
          <button onclick="toggleTransferMode()" id="btnTransferMode" class="btn btn-outline-dark d-flex align-items-center gap-2">
            <span class="fs-6">⇄</span> Transferir Tarefas
          </button>
          <form method="POST" action="{{ url_for('atualizar_tarefas_recompra') }}" class="d-inline">
              <button type="submit" class="btn btn-outline-primary">
                  Atualizar Tarefas
              </button>
          </form>
          {% endif %}
      </div>
      </div>
    </div>

    <div class="d-flex flex-wrap gap-1 p-1 bg-white border rounded-3 shadow-sm mb-3">
        {% for s in all_status %}
            <a href="{{ url_for('tarefas', status=s, vendor_id=filter_vendor, q=q, ignorar_jit='1' if ignorar_jit else '', filtro_atraso='1' if filtro_atraso else '') }}"
               class="btn btn-sm {% if s == current_status %}btn-primary{% else %}btn-light text-muted{% endif %}"
               style="font-weight:600; font-size:0.8rem;">
               {{ s }}
            </a>
        {% endfor %}
    </div>

    <div class="cardx p-3 mb-3">
      <form method="get" class="row g-2 align-items-end">
        <input type="hidden" name="status" value="{{ current_status }}">
        <div class="col-md-4">
          <label class="form-label small text-uppercase muted fw-bold">Buscar</label>
          <input type="text" name="q" class="form-control"
                 value="{{ q }}"
                 placeholder="Tarefa, cliente, rede, grupo, vendedor...">
        </div>
        {% if can_manage %}
        <div class="col-md-3">
          <label class="form-label small text-uppercase muted fw-bold">Vendedor</label>
          <select name="vendor_id" class="form-select">
            <option value="">Todos os vendedores</option>
            {% for v in vendors_list %}
              <option value="{{ v['id'] }}" {% if filter_vendor|string == v['id']|string %}selected{% endif %}>{{ v['nome'] }}</option>
            {% endfor %}
          </select>
        </div>
        {% endif %}
        <div class="col-md-2">
          <label class="form-label small text-uppercase muted fw-bold">JIT</label>
          <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" name="ignorar_jit" value="1" id="ignorarJit" {% if ignorar_jit %}checked{% endif %}>
            <label class="form-check-label small" for="ignorarJit">Ignorar JIT</label>
          </div>
        </div>
        <div class="col-md-2">
          <label class="form-label small text-uppercase muted fw-bold">Recompra</label>
          <div class="form-check form-switch mt-2">
            <input class="form-check-input" type="checkbox" name="filtro_atraso" value="1" id="filtroAtraso" {% if filtro_atraso %}checked{% endif %}>
            <label class="form-check-label small" for="filtroAtraso">Atraso da média</label>
          </div>
        </div>
        <div class="{% if can_manage %}col-md-1{% else %}col-md-3{% endif %} d-flex gap-2">
          <button class="btn btn-primary w-100">Aplicar</button>
          <a href="{{ url_for('tarefas', status=current_status) }}" class="btn btn-light border w-100">Limpar</a>
        </div>
      </form>
    </div>

    <form method="post" id="bulkForm">
        <input type="hidden" name="bulk_transfer" value="1">

        <div class="cardx p-0 overflow-hidden">
          <div class="p-3 border-bottom bg-light d-flex justify-content-between align-items-center">
             <span class="fw-bold text-uppercase text-primary small">Fase: {{ current_status }}</span>
             <span class="badge bg-secondary">{{ total_rows }} tarefa(s) · página {{ page }}/{{ total_pages }}</span>
          </div>

          <div class="table-responsive">
            <table class="table align-middle mb-0 table-hover">
              <thead class="bg-light">
                <tr>
                  {% if can_manage %}
                    <th style="width: 40px;" class="transfer-col transfer-mode-hidden text-center bg-warning bg-opacity-10 border-bottom-0">
                        <input type="checkbox" class="big-check" onclick="toggleAll(this)">
                    </th>
                  {% endif %}
                  <th style="padding-left:1.5rem;">Tarefa / Identidade</th>
                  <th>Vendedor</th>
                  <th>Criada em</th>
                  <th class="text-end" style="padding-right:1.5rem;">Ações</th>
                </tr>
              </thead>
              <tbody>
              {% for t in rows %}
                <tr style="background: {{ t['row_bg'] }}; border-left: 4px solid {{ t['row_border'] }};">
                  {% if can_manage %}
                    <td class="transfer-col transfer-mode-hidden text-center bg-warning bg-opacity-10">
                        <input type="checkbox" name="selected_tasks" value="{{ t.id }}" class="big-check task-check" onclick="updateCounter()">
                    </td>
                  {% endif %}

                  <td style="padding-left:1.5rem;">
                    <div class="fw-bold text-dark">{{ t['titulo_exibicao'] or t['titulo'] }}</div>
                    
                    <div class="small text-muted mt-1">
                      <span class="fw-semibold">{{ t['identity_nome'] }}</span>
                      <span class="badge rounded-pill text-bg-light border text-dark ms-1">{{ t['identity_tipo'] }}</span>
                    </div>
                    
                    <div class="small mt-1 d-flex flex-wrap gap-2">
                      <span class="badge rounded-pill text-bg-{{ t['score_cor'] }}">
                        Score {{ t['score'] }} · {{ t['score_faixa'] }} · {{ t['score_label'] }}
                      </span>
                    
                      <span class="badge rounded-pill text-bg-{{ t['prioridade_cor'] }}">
                        Prioridade {{ t['prioridade'] }}
                      </span>
                    </div>
                    
                    <div class="small mt-2 d-flex flex-wrap gap-2">
                      <span class="badge rounded-pill text-bg-info">
                        Ticket médio: R$ {{ "{:,.2f}".format(t['ticket_medio'] or 0).replace(',', 'X').replace('.', ',').replace('X', '.') }}
                      </span>
                      {% if t['dias_fora_media'] is not none and t['dias_fora_media']|int > 0 %}
                        <span class="badge rounded-pill text-bg-danger">
                          {{ t['dias_fora_media'] }} dias fora da média de recompra
                        </span>
                      {% elif t['media_dias_recompra'] %}
                        <span class="badge rounded-pill text-bg-success">
                          Dentro da média de recompra
                        </span>
                      {% else %}
                        <span class="badge rounded-pill text-bg-light border text-dark">
                          Sem histórico suficiente de recompra
                        </span>
                      {% endif %}
                    </div>
                    {% if t['media_dias_recompra'] %}
                      <div class="small text-muted mt-1">
                        Média de recompra: {{ t['media_dias_recompra'] }} dias · Última compra: {{ t['ultima_compra'] or '-' }} · {{ t['qtd_compras'] }} compra(s) no histórico
                      </div>
                    {% endif %}
                    {% if t['prioridade_motivo'] %}
                      <div class="small text-muted mt-2 fst-italic">
                        {{ t['prioridade_motivo'] }}
                      </div>
                    {% endif %}
                  </td>

                  <td><span class="badge rounded-pill text-bg-light border text-dark">{{ t['vendedor'] }}</span></td>
                  <td class="small text-muted">{{ (t['created_at'] or '')[:10] }}</td>
                  <td class="text-end" style="padding-right:1.5rem;">
                    <div class="d-flex justify-content-end gap-2">
                        <button class="btn btn-sm btn-light border text-muted" type="button" data-bs-toggle="collapse" data-bs-target="#hist{{ t.id }}">⏳ Histórico</button>
                        <button type="button" class="btn btn-sm btn-outline-primary fw-bold px-3"
                                data-bs-toggle="modal" data-bs-target="#moveModal"
                                data-id="{{ t.id }}" data-title="{{ t.titulo }}" data-status="{{ t.status }}">
                            Movimentar
                        </button>
                    </div>
                  </td>
                </tr>

                <tr class="collapse" id="hist{{ t.id }}">
                    <td colspan="{% if can_manage %}5{% else %}4{% endif %}" class="p-0 border-0">
                        <div class="tr-history p-3 border-bottom">
                            <div class="small fw-bold text-muted text-uppercase mb-2">Linha do Tempo Completa</div>
                            {% if t.history %}
                                <div class="timeline-sm">
                                    {% for h in t.history %}
                                    <div class="timeline-item">
                                        <div class="timeline-dot"></div>
                                        <div class="d-flex align-items-center gap-2 flex-wrap">
                                            <span class="badge bg-white border text-dark small">{{ h.status_anterior }}</span>
                                            <span class="text-muted small">➜</span>
                                            <span class="badge bg-secondary text-white small">{{ h.status_novo }}</span>
                                            <span class="text-muted ms-auto small">{{ h.created_at }} ({{ h.alterado_por }})</span>
                                        </div>
                                        {% if h.observacao %}
                                        <div class="mt-1 p-2 bg-white border rounded text-dark small position-relative">
                                            {{ h.observacao }}
                                            <a href="#" class="position-absolute top-0 end-0 m-1 text-decoration-none text-secondary"
                                               title="Editar observação"
                                               data-bs-toggle="modal" data-bs-target="#editObsModal"
                                               data-id="{{ h.id }}" data-obs="{{ h.observacao }}">
                                               ✏️
                                            </a>
                                        </div>
                                        {% endif %}
                                    </div>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <div class="text-muted small fst-italic ms-3">Nenhum histórico registrado.</div>
                            {% endif %}
                        </div>
                    </td>
                </tr>
              {% else %}
                <tr><td colspan="{% if can_manage %}5{% else %}4{% endif %}" class="text-center py-5 muted">📂 Nenhuma tarefa nesta fase.</td></tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
        </div>

        {% if total_pages > 1 %}
        <nav class="d-flex justify-content-center align-items-center gap-2 mt-3 mb-2" aria-label="Paginação de tarefas">
          <a class="btn btn-sm btn-outline-secondary {% if page <= 1 %}disabled{% endif %}"
             href="{{ url_for('tarefas', status=current_status, vendor_id=filter_vendor, q=q, ignorar_jit='1' if ignorar_jit else '', filtro_atraso='1' if filtro_atraso else '', page=page-1) }}">← Anterior</a>
          <span class="small muted">{{ total_rows }} tarefas · {{ page }}/{{ total_pages }}</span>
          <a class="btn btn-sm btn-outline-secondary {% if page >= total_pages %}disabled{% endif %}"
             href="{{ url_for('tarefas', status=current_status, vendor_id=filter_vendor, q=q, ignorar_jit='1' if ignorar_jit else '', filtro_atraso='1' if filtro_atraso else '', page=page+1) }}">Próxima →</a>
        </nav>
        {% endif %}

        <div id="transferBar" class="floating-action">
            <div class="fw-bold text-dark"><span id="selectedCount">0</span> tarefas selecionadas</div>
            <div class="vr"></div>
            <button type="button" class="btn btn-dark btn-sm px-4 rounded-pill" onclick="openTransferModal()">
                Transferir Selecionadas ➜
            </button>
            <button type="button" class="btn btn-light btn-sm rounded-pill border" onclick="toggleTransferMode()">
                Cancelar
            </button>
        </div>

        <div class="modal fade" id="bulkTransferModal" tabindex="-1">
          <div class="modal-dialog">
            <div class="modal-content">
              <div class="modal-header bg-warning bg-opacity-25">
                <h5 class="modal-title fw-bold">Transferir Tarefas</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body text-center p-4">
                <div class="mb-3" style="font-size:3rem;">🏗️</div>
                <h5 class="mb-3">Transferir responsabilidade?</h5>
                <p class="text-muted">
                  Você selecionou <b><span id="modalCount">0</span> tarefas</b>.
                  Ao confirmar, tarefas abertas da mesma identidade comercial também irão juntas.
                </p>

                <div class="form-floating mb-3 text-start">
                    <select name="target_vendor_id" class="form-select" id="targetVendorSelect" required>
                        <option value="" selected disabled>Selecione o novo dono...</option>
                        {% for v in vendors_list %}
                            <option value="{{ v.id }}">{{ v.nome }}</option>
                        {% endfor %}
                    </select>
                    <label>Novo Vendedor Responsável</label>
                </div>

                <div class="alert alert-light border small text-muted">
                    ℹ️ Esta ação ficará registrada no histórico de cada tarefa como uma transferência administrativa.
                </div>
              </div>
              <div class="modal-footer justify-content-center">
                <button type="button" class="btn btn-link text-muted text-decoration-none" data-bs-dismiss="modal">Não, cancelar</button>
                <button type="submit" class="btn btn-dark px-5">Sim, Transferir Agora</button>
              </div>
            </div>
          </div>
        </div>
    </form>

    <div class="modal fade" id="moveModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <form method="post" action="{{ url_for('tarefas', status=current_status, vendor_id=filter_vendor, q=q, ignorar_jit='1' if ignorar_jit else '', filtro_atraso='1' if filtro_atraso else '', page=page) }}">
              <input type="hidden" name="status" value="{{ current_status }}">
              <input type="hidden" name="vendor_id" value="{{ filter_vendor }}">
              <input type="hidden" name="q" value="{{ q }}">
              <input type="hidden" name="ignorar_jit" value="{{ '1' if ignorar_jit else '' }}">
              <input type="hidden" name="filtro_atraso" value="{{ '1' if filtro_atraso else '' }}">

              <div class="modal-header bg-light">
                <h5 class="modal-title">Movimentar Tarefa</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body">
                <input type="hidden" name="task_id" id="modalTaskId">
                <div class="mb-3">
                    <label class="form-label fw-bold small text-muted">TAREFA</label>
                    <input type="text" class="form-control-plaintext fw-bold" id="modalTaskTitle" readonly>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold small text-muted">NOVA FASE</label>
                    <select name="new_status" id="modalStatusSelect" class="form-select" required>
                        {% for s in all_status %}
                            <option value="{{ s }}">{{ s }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold small text-muted">OBSERVAÇÃO / MOTIVO</label>
                    <textarea name="observacao" class="form-control" rows="5" placeholder="Digite aqui..."></textarea>
                </div>
              </div>
              <div class="modal-footer bg-light">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancelar</button>
                <button type="submit" class="btn btn-primary px-4">Salvar</button>
              </div>
          </form>
        </div>
      </div>
    </div>

    <div class="modal fade" id="editObsModal" tabindex="-1">
      <div class="modal-dialog modal-sm">
        <form method="post" class="modal-content">
          <div class="modal-header bg-light py-2">
            <h6 class="modal-title">Corrigir Observação</h6>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body p-2">
            <input type="hidden" name="edit_obs" value="1">
            <input type="hidden" name="hist_id" id="editHistId">
            <textarea name="observacao" id="editHistText" class="form-control" rows="4" required></textarea>
          </div>
          <div class="modal-footer py-1 bg-light">
            <button type="submit" class="btn btn-sm btn-primary w-100">Salvar Correção</button>
          </div>
        </form>
      </div>
    </div>

    <script>
      function toggleTransferMode() {
        var cols = document.querySelectorAll('.transfer-col');
        var bar = document.getElementById('transferBar');
        var btn = document.getElementById('btnTransferMode');

        cols.forEach(el => el.classList.toggle('transfer-mode-hidden'));

        if (bar.style.display === 'flex') {
            bar.style.display = 'none';
            btn.classList.remove('active', 'bg-warning', 'text-dark', 'border-warning');
            btn.classList.add('btn-outline-dark');

            document.querySelectorAll('.task-check').forEach(ch => ch.checked = false);
            updateCounter();
        } else {
            bar.style.display = 'flex';
            btn.classList.remove('btn-outline-dark');
            btn.classList.add('active', 'bg-warning', 'text-dark', 'border-warning');
        }
      }

      function toggleAll(source) {
        var checkboxes = document.querySelectorAll('.task-check');
        for (var i = 0; i < checkboxes.length; i++) {
          checkboxes[i].checked = source.checked;
        }
        updateCounter();
      }

      function updateCounter() {
        var count = document.querySelectorAll('.task-check:checked').length;
        document.getElementById('selectedCount').innerText = count;
      }

      function openTransferModal() {
        var count = document.querySelectorAll('.task-check:checked').length;
        if (count === 0) {
            alert("Selecione pelo menos uma tarefa para transferir.");
            return;
        }
        document.getElementById('modalCount').innerText = count;
        var myModal = new bootstrap.Modal(document.getElementById('bulkTransferModal'));
        myModal.show();
      }

      var moveModal = document.getElementById('moveModal');
      moveModal.addEventListener('show.bs.modal', function (event) {
        var button = event.relatedTarget;
        var id = button.getAttribute('data-id');
        var title = button.getAttribute('data-title');
        var currentStatus = button.getAttribute('data-status');
        moveModal.querySelector('#modalTaskId').value = id;
        moveModal.querySelector('#modalTaskTitle').value = title;
        moveModal.querySelector('#modalStatusSelect').value = currentStatus;
      });

      var editObsModal = document.getElementById('editObsModal');
      editObsModal.addEventListener('show.bs.modal', function (event) {
        var button = event.relatedTarget;
        document.getElementById('editHistId').value = button.getAttribute('data-id');
        document.getElementById('editHistText').value = button.getAttribute('data-obs');
      });
    </script>
    """,
    rows=rows,
    all_status=ALL_STATUS,
    current_status=current_status,
    can_manage=can_manage,
    vendors_list=vendors_list,
    filter_vendor=filter_vendor,
    q=q,
    ignorar_jit=ignorar_jit,
    filtro_atraso=filtro_atraso,
    page=page,
    total_pages=total_pages,
    total_rows=total_rows
    )

    return render_page("Tarefas", body, "tarefas")



@app.route("/diagnostico")
@login_required
def diagnostico():
    user = get_current_user()
    if not user or user.get("role") not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("home"))

    q_diag = (request.args.get("q") or "").strip()

    with conectar() as conn:
        def one(sql, params=()):
            try:
                r = conn.execute(sql, params).fetchone()
                if r is None:
                    return 0
                return list(r)[0] or 0
            except Exception:
                return 0

        total_pedidos = one("SELECT COUNT(*) FROM orders")
        total_faturamento = one("SELECT COUNT(*) FROM invoices")
        total_tarefas = one("SELECT COUNT(*) FROM tasks")
        total_clientes = one("SELECT COUNT(*) FROM customers")
        pedidos_sem_vendedor = one("SELECT COUNT(*) FROM orders WHERE vendor_id IS NULL OR vendor_id = 0")
        pedidos_sem_cliente = one("SELECT COUNT(*) FROM orders WHERE cliente_id IS NULL OR cliente_id = 0")
        faturamento_sem_vendedor = one("SELECT COUNT(*) FROM invoices WHERE vendor_id IS NULL OR vendor_id = 0")
        faturamento_sem_cliente = one("SELECT COUNT(*) FROM invoices WHERE cliente_id IS NULL OR cliente_id = 0")
        tarefas_sem_vendedor = one("SELECT COUNT(*) FROM tasks WHERE vendor_id IS NULL OR vendor_id = 0")
        tarefas_sem_identidade = one("SELECT COUNT(*) FROM tasks WHERE COALESCE(identity_key,'') = ''")
        clientes_sem_identidade = one("""
            SELECT COUNT(*)
            FROM customers c
            LEFT JOIN customer_identity_rules cir ON cir.customer_id = c.id
            WHERE cir.id IS NULL
        """)
        duplicados_pedidos = one("""
            SELECT COUNT(*) FROM (
                SELECT dedupe_key FROM orders
                WHERE COALESCE(dedupe_key,'') <> ''
                GROUP BY dedupe_key HAVING COUNT(*) > 1
            ) x
        """)
        duplicados_faturamento = one("""
            SELECT COUNT(*) FROM (
                SELECT dedupe_key FROM invoices
                WHERE COALESCE(dedupe_key,'') <> ''
                GROUP BY dedupe_key HAVING COUNT(*) > 1
            ) x
        """)

        ultimas_importacoes = []
        try:
            ultimas_importacoes = conn.execute("""
                SELECT tipo, arquivo, linhas, registros, imported_at
                FROM imports
                ORDER BY imported_at DESC, id DESC
                LIMIT 10
            """).fetchall()
        except Exception:
            pass

        auditoria = {
            "pedidos": [],
            "faturamento": [],
            "tarefas": [],
            "clientes": [],
            "resumo": None,
        }

        if q_diag:
            like = f"%{q_diag}%"
            try:
                auditoria["pedidos"] = conn.execute(f"""
                    SELECT
                        o.data_emissao, o.pedido, COALESCE(c.fantasia, c.nome, o.cliente_nome_ref, '') AS cliente,
                        COALESCE(v.nome, o.vendor_nome_ref, '') AS vendedor, COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0) AS valor,
                        COALESCE(o.condicao_entrega,'') AS condicao, COALESCE(o.situacao_pedido,'') AS situacao,
                        COALESCE(o.motivo_pedido,'') AS motivo, COALESCE(o.desc_motivo_pedido,'') AS desc_motivo,
                        COALESCE(o.is_jit,0) AS is_jit, COALESCE(o.pedido_cancelado,0) AS cancelado,
                        CASE WHEN {sql_pedidos_realizados_excel_cond('o','v')} THEN 1 ELSE 0 END AS valido_oficial
                    FROM orders o
                    LEFT JOIN customers c ON c.id = o.cliente_id
                    LEFT JOIN vendors v ON v.id = o.vendor_id
                    WHERE COALESCE(o.pedido,'') LIKE ?
                       OR COALESCE(c.fantasia,'') LIKE ?
                       OR COALESCE(c.nome,'') LIKE ?
                       OR COALESCE(v.nome,'') LIKE ?
                       OR COALESCE(o.cliente_nome_ref,'') LIKE ?
                       OR COALESCE(o.vendor_nome_ref,'') LIKE ?
                    ORDER BY o.data_emissao DESC, o.id DESC
                    LIMIT 80
                """, (like, like, like, like, like, like)).fetchall()
            except Exception:
                auditoria["pedidos"] = []
            try:
                auditoria["faturamento"] = conn.execute("""
                    SELECT
                        i.data_emissao, COALESCE(i.pedido,'') AS pedido, COALESCE(c.fantasia, c.nome, i.cliente_nome_ref, '') AS cliente,
                        COALESCE(v.nome, i.vendor_nome_ref, '') AS vendedor, COALESCE(i.valor,0) AS valor,
                        COALESCE(i.is_jit,0) AS is_jit, COALESCE(i.comissionavel,1) AS comissionavel
                    FROM invoices i
                    LEFT JOIN customers c ON c.id = i.cliente_id
                    LEFT JOIN vendors v ON v.id = i.vendor_id
                    WHERE COALESCE(i.pedido,'') LIKE ?
                       OR COALESCE(c.fantasia,'') LIKE ?
                       OR COALESCE(c.nome,'') LIKE ?
                       OR COALESCE(v.nome,'') LIKE ?
                       OR COALESCE(i.cliente_nome_ref,'') LIKE ?
                       OR COALESCE(i.vendor_nome_ref,'') LIKE ?
                    ORDER BY i.data_emissao DESC, i.id DESC
                    LIMIT 80
                """, (like, like, like, like, like, like)).fetchall()
            except Exception:
                auditoria["faturamento"] = []
            try:
                auditoria["tarefas"] = conn.execute("""
                    SELECT t.id, t.titulo, t.status, t.identity_key, COALESCE(v.nome,'') AS vendedor,
                           t.created_at, t.updated_at
                    FROM tasks t
                    LEFT JOIN vendors v ON v.id = t.vendor_id
                    WHERE COALESCE(t.titulo,'') LIKE ?
                       OR COALESCE(t.descricao,'') LIKE ?
                       OR COALESCE(t.identity_key,'') LIKE ?
                       OR COALESCE(v.nome,'') LIKE ?
                    ORDER BY COALESCE(t.updated_at, t.created_at) DESC, t.id DESC
                    LIMIT 80
                """, (like, like, like, like)).fetchall()
            except Exception:
                auditoria["tarefas"] = []
            try:
                auditoria["clientes"] = conn.execute("""
                    SELECT c.id, COALESCE(c.fantasia, c.nome, '') AS cliente,
                           COALESCE(c.cidade,'') AS cidade, COALESCE(c.uf,'') AS uf,
                           COALESCE(cir.identity_key,'') AS identity_key, COALESCE(cir.tipo_vinculo,'') AS tipo_vinculo
                    FROM customers c
                    LEFT JOIN customer_identity_rules cir ON cir.customer_id = c.id
                    WHERE COALESCE(c.fantasia,'') LIKE ?
                       OR COALESCE(c.nome,'') LIKE ?
                       OR COALESCE(c.cidade,'') LIKE ?
                       OR COALESCE(cir.identity_key,'') LIKE ?
                    ORDER BY cliente
                    LIMIT 80
                """, (like, like, like, like)).fetchall()
            except Exception:
                auditoria["clientes"] = []

            soma_pedidos = sum(float(r["valor"] or 0) for r in auditoria["pedidos"])
            soma_faturamento = sum(float(r["valor"] or 0) for r in auditoria["faturamento"])
            auditoria["resumo"] = {
                "q": q_diag,
                "qtd_pedidos": len(auditoria["pedidos"]),
                "valor_pedidos": soma_pedidos,
                "qtd_faturamento": len(auditoria["faturamento"]),
                "valor_faturamento": soma_faturamento,
                "qtd_tarefas": len(auditoria["tarefas"]),
                "qtd_clientes": len(auditoria["clientes"]),
            }

        body = render_template_string("""
        <div class="d-flex justify-content-between align-items-center mb-4">
          <div>
            <div class="h3 mb-1">Diagnóstico do CRM</div>
            <div class="muted">Auditoria rápida para encontrar divergências de importação, pedidos, clientes e tarefas.</div>
          </div>
          <a href="{{ url_for('tarefas') }}" class="btn btn-outline-secondary">Voltar</a>
        </div>

        <div class="cardx p-3 mb-4 border border-primary border-opacity-25">
          <div class="d-flex flex-column flex-md-row justify-content-between gap-3 align-items-md-end">
            <div class="flex-grow-1">
              <div class="h5 mb-1">Auditoria individual</div>
              <div class="small text-muted mb-2">Digite um vendedor, cliente ou número de pedido para conferir o que entrou em pedidos, faturamento, tarefas e identidade comercial.</div>
              <form method="get" class="d-flex gap-2">
                <input name="q" class="form-control" value="{{ q_diag }}" placeholder="Ex.: Cecília, Amanda, Nacho Man, 21569...">
                <button class="btn btn-primary">Auditar</button>
                {% if q_diag %}<a class="btn btn-light border" href="{{ url_for('diagnostico') }}">Limpar</a>{% endif %}
              </form>
            </div>
          </div>
        </div>

        {% if auditoria.resumo %}
        <div class="cardx p-3 mb-4">
          <div class="h5 mb-3">Resultado da auditoria: {{ auditoria.resumo.q }}</div>
          <div class="row g-3 mb-3">
            <div class="col-md-3"><div class="p-3 rounded border bg-light"><div class="small text-muted">Pedidos encontrados</div><div class="h4 mb-0">{{ auditoria.resumo.qtd_pedidos }}</div><div class="fw-bold text-primary">R$ {{ moeda(auditoria.resumo.valor_pedidos) }}</div></div></div>
            <div class="col-md-3"><div class="p-3 rounded border bg-light"><div class="small text-muted">Faturamentos encontrados</div><div class="h4 mb-0">{{ auditoria.resumo.qtd_faturamento }}</div><div class="fw-bold text-success">R$ {{ moeda(auditoria.resumo.valor_faturamento) }}</div></div></div>
            <div class="col-md-3"><div class="p-3 rounded border bg-light"><div class="small text-muted">Tarefas encontradas</div><div class="h4 mb-0">{{ auditoria.resumo.qtd_tarefas }}</div></div></div>
            <div class="col-md-3"><div class="p-3 rounded border bg-light"><div class="small text-muted">Clientes/identidades</div><div class="h4 mb-0">{{ auditoria.resumo.qtd_clientes }}</div></div></div>
          </div>

          <div class="h6 mt-4">Pedidos</div>
          <div class="table-responsive"><table class="table table-sm align-middle">
            <thead><tr><th>Data</th><th>Pedido</th><th>Cliente</th><th>Vendedor</th><th>Condição</th><th>Situação</th><th>Regra</th><th class="text-end">Valor Item</th></tr></thead>
            <tbody>{% for r in auditoria.pedidos %}<tr>
              <td>{{ r.data_emissao }}</td><td>{{ r.pedido }}</td><td>{{ r.cliente }}</td><td>{{ r.vendedor }}</td>
              <td>{{ r.condicao }} {% if r.is_jit %}<span class="badge bg-secondary">JIT</span>{% endif %}</td>
              <td>{{ r.situacao }} {% if r.cancelado %}<span class="badge bg-danger">Cancelado</span>{% endif %}</td>
              <td>{% if r.valido_oficial %}<span class="badge bg-success">Conta no Dashboard</span>{% else %}<span class="badge bg-secondary">Ignorado</span>{% endif %}</td>
              <td class="text-end fw-bold">R$ {{ moeda(r.valor) }}</td>
            </tr>{% else %}<tr><td colspan="8" class="text-muted">Nenhum pedido encontrado.</td></tr>{% endfor %}</tbody>
          </table></div>

          <div class="h6 mt-4">Faturamento</div>
          <div class="table-responsive"><table class="table table-sm align-middle">
            <thead><tr><th>Data</th><th>Pedido</th><th>Cliente</th><th>Vendedor</th><th>Flags</th><th class="text-end">Valor</th></tr></thead>
            <tbody>{% for r in auditoria.faturamento %}<tr>
              <td>{{ r.data_emissao }}</td><td>{{ r.pedido }}</td><td>{{ r.cliente }}</td><td>{{ r.vendedor }}</td>
              <td>{% if r.is_jit %}<span class="badge bg-secondary">JIT</span>{% endif %} {% if not r.comissionavel %}<span class="badge bg-warning text-dark">Não comissionável</span>{% endif %}</td>
              <td class="text-end fw-bold">R$ {{ moeda(r.valor) }}</td>
            </tr>{% else %}<tr><td colspan="6" class="text-muted">Nenhum faturamento encontrado.</td></tr>{% endfor %}</tbody>
          </table></div>

          <div class="h6 mt-4">Tarefas</div>
          <div class="table-responsive"><table class="table table-sm align-middle">
            <thead><tr><th>Status</th><th>Título</th><th>Vendedor</th><th>Identidade</th><th>Atualizada</th></tr></thead>
            <tbody>{% for r in auditoria.tarefas %}<tr><td>{{ r.status }}</td><td>{{ r.titulo }}</td><td>{{ r.vendedor }}</td><td>{{ r.identity_key }}</td><td>{{ r.updated_at or r.created_at }}</td></tr>{% else %}<tr><td colspan="5" class="text-muted">Nenhuma tarefa encontrada.</td></tr>{% endfor %}</tbody>
          </table></div>

          <div class="h6 mt-4">Clientes e identidade comercial</div>
          <div class="table-responsive"><table class="table table-sm align-middle">
            <thead><tr><th>Cliente</th><th>Cidade/UF</th><th>Identidade</th><th>Vínculo</th></tr></thead>
            <tbody>{% for r in auditoria.clientes %}<tr><td>{{ r.cliente }}</td><td>{{ r.cidade }}/{{ r.uf }}</td><td>{{ r.identity_key }}</td><td>{{ r.tipo_vinculo }}</td></tr>{% else %}<tr><td colspan="4" class="text-muted">Nenhum cliente encontrado.</td></tr>{% endfor %}</tbody>
          </table></div>
        </div>
        {% endif %}

        <div class="row g-3 mb-4">
          {% for card in cards %}
          <div class="col-md-3">
            <div class="cardx p-3 h-100">
              <div class="small text-uppercase muted fw-bold">{{ card[0] }}</div>
              <div class="h3 mb-0 {{ card[2] }}">{{ card[1] }}</div>
            </div>
          </div>
          {% endfor %}
        </div>

        <div class="cardx p-3 mb-4">
          <div class="h5 mb-2">Alertas principais</div>
          <ul class="mb-0">
            <li>Pedidos sem vendedor: <b>{{ pedidos_sem_vendedor }}</b></li>
            <li>Pedidos sem cliente: <b>{{ pedidos_sem_cliente }}</b></li>
            <li>Faturamentos sem vendedor: <b>{{ faturamento_sem_vendedor }}</b></li>
            <li>Faturamentos sem cliente: <b>{{ faturamento_sem_cliente }}</b></li>
            <li>Tarefas sem vendedor: <b>{{ tarefas_sem_vendedor }}</b></li>
            <li>Tarefas sem identidade comercial: <b>{{ tarefas_sem_identidade }}</b></li>
            <li>Clientes sem identidade comercial: <b>{{ clientes_sem_identidade }}</b></li>
            <li>Chaves duplicadas em pedidos: <b>{{ duplicados_pedidos }}</b></li>
            <li>Chaves duplicadas em faturamento: <b>{{ duplicados_faturamento }}</b></li>
          </ul>
        </div>

        <div class="cardx p-3">
          <div class="h5 mb-3">Últimas importações</div>
          <div class="table-responsive">
            <table class="table table-sm align-middle">
              <thead><tr><th>Tipo</th><th>Arquivo</th><th>Linhas</th><th>Registros</th><th>Data</th></tr></thead>
              <tbody>
                {% for i in ultimas_importacoes %}
                  <tr><td>{{ i.tipo }}</td><td>{{ i.arquivo }}</td><td>{{ i.linhas }}</td><td>{{ i.registros }}</td><td>{{ i.imported_at }}</td></tr>
                {% else %}
                  <tr><td colspan="5" class="text-muted">Nenhuma importação encontrada.</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
        """,
        cards=[
            ("Pedidos", total_pedidos, "text-primary"),
            ("Faturamento", total_faturamento, "text-success"),
            ("Clientes", total_clientes, "text-info"),
            ("Problemas encontrados", pedidos_sem_vendedor + pedidos_sem_cliente + faturamento_sem_vendedor + faturamento_sem_cliente + duplicados_pedidos + duplicados_faturamento + tarefas_sem_identidade + tarefas_sem_vendedor, "text-danger"),
        ],
        pedidos_sem_vendedor=pedidos_sem_vendedor,
        pedidos_sem_cliente=pedidos_sem_cliente,
        faturamento_sem_vendedor=faturamento_sem_vendedor,
        faturamento_sem_cliente=faturamento_sem_cliente,
        duplicados_pedidos=duplicados_pedidos,
        duplicados_faturamento=duplicados_faturamento,
        clientes_sem_identidade=clientes_sem_identidade,
        tarefas_sem_identidade=tarefas_sem_identidade,
        tarefas_sem_vendedor=tarefas_sem_vendedor,
        ultimas_importacoes=ultimas_importacoes,
        q_diag=q_diag,
        auditoria=auditoria,
        moeda=format_brl)

    return render_page("Diagnóstico", body, "diagnostico")

@app.route("/tarefas/atualizar", methods=["POST"])
@login_required
def atualizar_tarefas_recompra():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("tarefas"))

    try:
        with conectar() as conn:
            actor_name = (user.get("nome") or "Sistema")
            moved = normalize_open_tasks_to_last_vendor(conn, actor_name=actor_name)
            removed = deduplicate_open_tasks(conn, actor_name=actor_name)
            check_repurchase_tasks(conn)
            moved += normalize_open_tasks_to_last_vendor(conn, actor_name=actor_name)
            removed += deduplicate_open_tasks(conn, actor_name=actor_name)
            conn.commit()

        if moved or removed:
            flash(f"Tarefas automáticas atualizadas com sucesso. {moved} tarefa(s) foram realocadas para o último responsável e {removed} duplicidade(s) aberta(s) foram canceladas.", "success")
        else:
            flash("Tarefas automáticas atualizadas com sucesso.", "success")
    except Exception as e:
        flash(f"Erro ao atualizar tarefas automáticas: {e}", "danger")

    return redirect(url_for("tarefas"))

@app.route("/historico_tarefas", methods=["GET", "POST"])
@login_required
def historico_tarefas():
    user = get_current_user()
    
    # Filtros
    filter_vendor = request.args.get("vendor_id", "")
    filter_status = request.args.get("status", "") 
    
    with conectar() as conn:
        user_vendor_id = get_user_vendor_id(conn, user)
        
        is_admin = (user["role"] == "admin")
        is_gestor = (user["role"] == "gestor")
        can_manage = (is_admin or is_gestor) # Quem pode ver tudo e editar
        
        target_vendor = user_vendor_id if not can_manage else (int(filter_vendor) if filter_vendor else None)

        # --- BLOCO DE EDIÇÃO E EXCLUSÃO (Colar logo abaixo de is_admin = ...) ---
       # --- PROCESSAMENTO DO POST ---
        if request.method == "POST" and can_manage:
            action = request.form.get("action")
            task_id = request.form.get("task_id")
            
            if action == "delete":
                if is_admin: # <--- TRAVA: Só admin entra aqui
                    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                    flash("Registro e histórico excluídos.", "success")
                else:
                    flash("Apenas Administradores podem excluir registros.", "danger")
            
            elif action == "edit":
                # Edição permitida para Admin e Gestor (can_manage)
                nt = request.form.get("titulo")
                nd = request.form.get("descricao")
                conn.execute("UPDATE tasks SET titulo=?, descricao=?, updated_at=? WHERE id=?", (nt, nd, now_iso(), task_id))
                conn.execute("INSERT INTO task_history (task_id, status_anterior, status_novo, observacao, alterado_por, created_at) VALUES (?, 'EDIÇÃO', 'EDIÇÃO', 'Dados alterados por Gestão.', ?, ?)", (task_id, user["nome"], now_iso()))
                flash("Dados atualizados.", "success")
            
            conn.commit()
            return redirect(url_for('historico_tarefas', vendor_id=filter_vendor, status=filter_status))

        # 1. Busca Tarefas Encerradas
        query = """
            SELECT t.id, t.titulo, t.descricao, t.status, t.created_at, t.updated_at,
                   COALESCE(c.fantasia, c.nome) as cliente, v.nome as vendedor
            FROM tasks t
            JOIN vendors v ON v.id = t.vendor_id
            LEFT JOIN customers c ON c.id = t.cliente_id
            WHERE t.status IN ('CONCLUÍDA', 'CANCELADA')
        """
        params = []
        
        if target_vendor:
            query += " AND t.vendor_id = ?"
            params.append(target_vendor)
            
        if filter_status:
            query += " AND t.status = ?"
            params.append(filter_status)
            
        query += " ORDER BY t.updated_at DESC LIMIT 50"
        
        tasks = conn.execute(query, params).fetchall()
        
        # 2. Processa histórico
        tasks_data = []
        for t in tasks:
            history = conn.execute("""
                SELECT status_anterior, status_novo, observacao, created_at, alterado_por 
                FROM task_history 
                WHERE task_id = ? 
                ORDER BY created_at DESC
            """, (t["id"],)).fetchall()
            
            # Pega a observação mais recente para o destaque amarelo
            last_obs = next((h["observacao"] for h in history if h["observacao"]), None)
            
            tasks_data.append({
                "info": t,
                "history": history,
                "motivo_final": last_obs
            })
            
        vendors = []
        if is_admin:
            vendors = conn.execute("SELECT id, nome FROM vendors ORDER BY nome").fetchall()

    body = render_template_string(r"""
    <style>
        .timeline-sm { border-left: 2px solid #dee2e6; margin-left: 10px; padding-left: 15px; }
        .timeline-item { position: relative; margin-bottom: 10px; }
        .timeline-dot { 
            width: 8px; height: 8px; background: #adb5bd; border-radius: 50%; 
            position: absolute; left: -20px; top: 6px; border: 2px solid white;
        }
        .tr-history { background-color: #f8f9fa; box-shadow: inset 0 0 5px rgba(0,0,0,0.05); }
    </style>

    <div class="d-flex justify-content-between align-items-center mb-4">
        <div>
            <div class="h3 mb-1">Histórico de Tratativas</div>
            <div class="muted">Casos encerrados (Concluídos ou Cancelados).</div>
        </div>
        <a href="{{ url_for('tarefas') }}" class="btn btn-outline-primary">Voltar para Kanban</a>
    </div>
    
    <div class="cardx p-3 mb-4">
        <form class="d-flex gap-3 align-items-end flex-wrap">
            {% if is_admin %}
            <div>
                <label class="form-label small fw-bold text-muted mb-0">Vendedor</label>
                <select name="vendor_id" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    {% for v in vendors %}
                    <option value="{{ v.id }}" {{ 'selected' if filter_vendor|string == v.id|string else '' }}>{{ v.nome }}</option>
                    {% endfor %}
                </select>
            </div>
            {% endif %}
            <div>
                <label class="form-label small fw-bold text-muted mb-0">Resultado</label>
                <select name="status" class="form-select form-select-sm">
                    <option value="">Todos</option>
                    <option value="CONCLUÍDA" {{ 'selected' if filter_status == 'CONCLUÍDA' else '' }}>Concluída (Sucesso)</option>
                    <option value="CANCELADA" {{ 'selected' if filter_status == 'CANCELADA' else '' }}>Cancelada (Perda)</option>
                </select>
            </div>
            <button class="btn btn-sm btn-primary">Filtrar</button>
        </form>
    </div>

    <div class="cardx p-0 overflow-hidden">
      <div class="p-3 border-bottom bg-light d-flex justify-content-between align-items-center">
         <span class="fw-bold text-uppercase text-primary small">Registros Encontrados</span>
         <span class="badge bg-secondary">{{ tasks_data|length }} caso(s)</span>
      </div>

      <div class="table-responsive">
        <table class="table align-middle mb-0 table-hover">
          <thead class="bg-light">
            <tr>
              <th style="padding-left:1.5rem;">Tarefa / Cliente</th>
              <th>Status Final</th>
              <th>Encerrado em</th>
              <th class="text-end" style="padding-right:1.5rem;">Ações</th>
            </tr>
          </thead>
          <tbody>
          {% for item in tasks_data %}
            <tr>
              <td style="padding-left:1.5rem;">
                <div class="fw-bold text-dark">{{ item.info.titulo }}</div>
                <div class="small text-muted">
                    Cliente: <b>{{ item.info.cliente or 'N/A' }}</b> • Vend.: {{ item.info.vendedor }}
                </div>
                
                {% if item.motivo_final %}
                    <div class="mt-2 p-1 px-2 bg-warning bg-opacity-10 border border-warning rounded d-inline-block">
                        <small class="text-warning fw-bold" style="font-size:0.7rem">MOTIVO FINAL / OBS:</small>
                        <small class="text-dark d-block fst-italic">"{{ item.motivo_final }}"</small>
                    </div>
                {% endif %}
              </td>
              <td>
                {% if item.info.status == 'CONCLUÍDA' %}
                    <span class="badge bg-success">CONCLUÍDA</span>
                {% else %}
                    <span class="badge bg-danger">CANCELADA</span>
                {% endif %}
              </td>
              <td class="small text-muted">{{ item.info.updated_at[:10] }}</td>
              <td class="text-end" style="padding-right:1.5rem;">
                 <div class="d-flex justify-content-end align-items-center gap-2">
                     <button class="btn btn-sm btn-light border text-muted text-nowrap" type="button" 
                            data-bs-toggle="collapse" 
                            data-bs-target="#hist{{ item.info.id }}" 
                            aria-expanded="false">
                        Ver Detalhes ▼
                    </button>
                    
                    <div class="d-flex align-items-center gap-2 border-start ps-2">
                        {% if can_manage %}
                        <a href="#" class="text-decoration-none text-muted" title="Editar"
                           data-bs-toggle="modal" data-bs-target="#editModal"
                           data-id="{{ item.info.id }}" 
                           data-titulo="{{ item.info.titulo }}" 
                           data-desc="{{ item.info.descricao }}">
                           ✏️
                        </a>
                        {% endif %}

                        {% if is_admin %}
                        <a href="#" class="text-decoration-none text-danger" title="Excluir"
                           data-bs-toggle="modal" data-bs-target="#deleteModal"
                           data-id="{{ item.info.id }}">
                           🗑️
                        </a>
                        {% endif %}
                    </div>
                 </div>
              </td>
            </tr>

            <tr class="collapse" id="hist{{ item.info.id }}">
                <td colspan="4" class="p-0 border-0">
                    <div class="tr-history p-3 border-bottom">
                        <div class="small fw-bold text-muted text-uppercase mb-2">Linha do Tempo Completa</div>
                        
                        {% if item.history %}
                            <div class="timeline-sm">
                                {% for h in item.history %}
                                <div class="timeline-item">
                                    <div class="timeline-dot"></div>
                                    <div class="d-flex align-items-center gap-2 flex-wrap">
                                        <span class="badge bg-white border text-dark small">{{ h.status_anterior }}</span>
                                        <span class="text-muted" style="font-size:0.7rem">➜</span>
                                        <span class="badge bg-secondary text-white small">{{ h.status_novo }}</span>
                                        <span class="text-muted ms-auto" style="font-size:0.75rem">{{ h.created_at }} ({{ h.alterado_por }})</span>
                                    </div>
                                    {% if h.observacao %}
                                        <div class="mt-1 p-2 bg-white border rounded text-dark small">
                                            {{ h.observacao }}
                                        </div>
                                    {% endif %}
                                </div>
                                {% endfor %}
                            </div>
                        {% else %}
                            <div class="text-muted small fst-italic ms-3">Nenhum histórico registrado.</div>
                        {% endif %}
                    </div>
                </td>
            </tr>
          {% else %}
            <tr>
                <td colspan="4" class="text-center py-5">
                    <div class="text-muted opacity-50 mb-2" style="font-size:2rem;">📂</div>
                    <div class="text-muted">Nenhum caso encerrado encontrado com esses filtros.</div>
                </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
                                  
    <div class="modal fade" id="editModal" tabindex="-1">
      <div class="modal-dialog">
        <form method="post" class="modal-content">
          <input type="hidden" name="action" value="edit">
          <input type="hidden" name="task_id" id="editId">
          <div class="modal-header">
            <h5 class="modal-title">Editar Dados</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
                <label class="form-label small fw-bold">Título</label>
                <input type="text" name="titulo" id="editTitle" class="form-control" required>
            </div>
            <div class="mb-3">
                <label class="form-label small fw-bold">Descrição</label>
                <textarea name="descricao" id="editDesc" class="form-control" rows="3"></textarea>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancelar</button>
            <button type="submit" class="btn btn-primary">Salvar</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal fade" id="deleteModal" tabindex="-1">
      <div class="modal-dialog modal-sm">
        <form method="post" class="modal-content">
          <input type="hidden" name="action" value="delete">
          <input type="hidden" name="task_id" id="deleteId">
          <div class="modal-body text-center p-4">
            <div class="fs-1 mb-3">🗑️</div>
            <h6>Tem certeza?</h6>
            <p class="small text-muted">Isso apagará o card e todo o histórico dele para sempre.</p>
            <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancelar</button>
            <button type="submit" class="btn btn-danger btn-sm px-4">Excluir</button>
          </div>
        </form>
      </div>
    </div>

    <script>
      // Script para preencher os Modals
      var editModal = document.getElementById('editModal');
      editModal.addEventListener('show.bs.modal', function (event) {
        var btn = event.relatedTarget;
        document.getElementById('editId').value = btn.getAttribute('data-id');
        document.getElementById('editTitle').value = btn.getAttribute('data-titulo');
        document.getElementById('editDesc').value = btn.getAttribute('data-desc');
      });

      var delModal = document.getElementById('deleteModal');
      delModal.addEventListener('show.bs.modal', function (event) {
        var btn = event.relatedTarget;
        document.getElementById('deleteId').value = btn.getAttribute('data-id');
      });
    </script>
    """, 
    tasks_data=tasks_data, vendors=vendors, 
    filter_vendor=filter_vendor, filter_status=filter_status, 
    is_admin=is_admin, can_manage=can_manage
    )
    
    return render_page("Histórico de Casos", body, "tarefas")

@app.route("/usuarios", methods=["GET", "POST"])
@login_required
def usuarios():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        if user["role"] != "admin":
            flash("Apenas Admin pode gerenciar usuários.", "danger")
            return redirect(url_for("usuarios"))

        action = request.form.get("action", "create")

        # --- CRIAR NOVO USUÁRIO ---
        if action == "create":
            nome = norm(request.form.get("nome",""))
            email = norm(request.form.get("email","")).lower()
            senha = request.form.get("senha","")
            role = request.form.get("role","user")
            vendor_codigo = norm(request.form.get("vendor_codigo", ""))
            
            if not nome or not email or not senha:
                flash("Preencha todos os campos.", "warning")
            elif role == "user" and not vendor_codigo:
                flash("Vendedores precisam de código.", "danger")
            else:
                with conectar() as conn:
                    if conn.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
                        flash("E-mail já cadastrado.", "danger")
                    else:
                        try:
                            conn.execute("INSERT INTO users(nome,email,role,senha_hash,ativo,vendor_codigo,created_at) VALUES(?,?,?,?,?,?,?)",
                                         (nome, email, role, generate_password_hash(senha), 1, vendor_codigo, now_iso()))
                            conn.commit()
                            flash("Usuário criado.", "success")
                        except Exception as e:
                            flash(f"Erro: {e}", "danger")

        # --- EDITAR USUÁRIO EXISTENTE ---
        elif action == "edit":
            uid = request.form.get("user_id")
            nome = norm(request.form.get("nome",""))
            email = norm(request.form.get("email","")).lower()
            role = request.form.get("role")
            vendor_codigo = norm(request.form.get("vendor_codigo", ""))
            nova_senha = request.form.get("senha","")

            with conectar() as conn:
                # Verifica se email duplicado pertence a OUTRO usuário
                exists = conn.execute("SELECT id FROM users WHERE lower(email)=? AND id!=?", (email, uid)).fetchone()
                if exists:
                    flash("E-mail já pertence a outro usuário.", "danger")
                else:
                    if nova_senha:
                        conn.execute("UPDATE users SET nome=?, email=?, role=?, vendor_codigo=?, senha_hash=? WHERE id=?",
                                     (nome, email, role, vendor_codigo, generate_password_hash(nova_senha), uid))
                        flash("Dados e senha atualizados.", "success")
                    else:
                        conn.execute("UPDATE users SET nome=?, email=?, role=?, vendor_codigo=? WHERE id=?",
                                     (nome, email, role, vendor_codigo, uid))
                        flash("Dados atualizados.", "success")
                    conn.commit()

        return redirect(url_for("usuarios"))

    with conectar() as conn:
        rows = conn.execute("SELECT id,nome,email,role,ativo,vendor_codigo,created_at FROM users ORDER BY id DESC").fetchall()

    body = render_template_string(r"""
    <div class="row g-3">
      <div class="col-12 col-lg-5">
        <div class="cardx p-4">
          <div class="h4 mb-2">Adicionar usuário</div>
          
          {% if user.role in ['admin','gestor'] %}
          <form method="post">
            <input type="hidden" name="action" value="create">
            <div class="mb-2"><label class="form-label">Nome</label><input class="form-control" name="nome" required></div>
            <div class="mb-2"><label class="form-label">E-mail</label><input class="form-control" name="email" required></div>
            <div class="mb-2"><label class="form-label">Senha</label><input class="form-control" name="senha" type="password" required></div>
            <div class="mb-3">
              <label class="form-label">Role</label>
              <select class="form-select" name="role" id="createRole" onchange="toggleVendorCode('create')">
                <option value="user" selected>Vendedor</option>
                <option value="gestor">Gestor</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <div class="mb-3" id="createCodeDiv">
                 <label class="form-label fw-bold">Código do Vendedor *</label>
                 <input class="form-control" name="vendor_codigo" placeholder="Ex: 9044">
            </div>
            <button class="btn btn-primary w-100">Salvar</button>
          </form>
          {% else %}
            <div class="alert alert-info">Apenas Administradores podem criar ou editar usuários.</div>
          {% endif %}
        </div>
      </div>
      
      <div class="col-12 col-lg-7">
        <div class="cardx p-3">
          <div class="h5 mb-2">Usuários</div>
          <div class="table-responsive">
            <table class="table align-middle mb-0">
              <thead><tr><th>Nome</th><th>Role</th><th>Cód.</th><th class="text-end">Ações</th></tr></thead>
              <tbody>
                {% for r in rows %}
                  <tr>
                    <td class="fw-semibold">{{ r['nome'] }}<br><small class="muted">{{ r['email'] }}</small></td>
                    <td><span class="badge text-bg-light border">{{ r['role'] }}</span></td>
                    <td>{{ r['vendor_codigo'] or '-' }}</td>
                    <td class="text-end">
                        {% if user.role in ['admin','gestor'] %}
                        <button class="btn btn-sm btn-outline-secondary me-1"
                                data-bs-toggle="modal" data-bs-target="#editUserModal"
                                data-id="{{ r['id'] }}" data-nome="{{ r['nome'] }}"
                                data-email="{{ r['email'] }}" data-code="{{ r['vendor_codigo'] or '' }}"
                                data-role="{{ r['role'] }}">✏️</button>
                        
                        <a href="{{ url_for('excluir_usuario', user_id=r['id']) }}" 
                           class="btn btn-sm btn-outline-danger"
                           onclick="return confirm('Excluir {{ r.nome }}?');">🗑️</a>
                        {% endif %}
                    </td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div class="modal fade" id="editUserModal" tabindex="-1">
      <div class="modal-dialog">
        <form method="post" class="modal-content">
          <input type="hidden" name="action" value="edit">
          <input type="hidden" name="user_id" id="editUid">
          
          <div class="modal-header bg-light">
            <h5 class="modal-title">Editar Usuário</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
                <label class="form-label">Nome</label>
                <input class="form-control" name="nome" id="editNome" required>
            </div>
            <div class="mb-3">
                <label class="form-label">E-mail</label>
                <input class="form-control" name="email" id="editEmail" required>
            </div>
            <div class="row g-2 mb-3">
                <div class="col-6">
                    <label class="form-label">Função</label>
                    <select class="form-select" name="role" id="editRole" onchange="toggleVendorCode('edit')">
                        <option value="user">Vendedor</option>
                        <option value="gestor">Gestor</option>
                        <option value="admin">Admin</option>
                    </select>
                </div>
                <div class="col-6" id="editCodeDiv">
                    <label class="form-label">Cód. Vendedor</label>
                    <input class="form-control" name="vendor_codigo" id="editCode">
                </div>
            </div>
            <div class="mb-3 p-3 bg-light rounded border">
                <label class="form-label fw-bold small text-uppercase">Trocar Senha</label>
                <input class="form-control form-control-sm" name="senha" type="password" placeholder="Deixe em branco para manter a atual">
            </div>
          </div>
          <div class="modal-footer bg-light">
            <button type="button" class="btn btn-light border" data-bs-dismiss="modal">Cancelar</button>
            <button type="submit" class="btn btn-primary">Salvar Alterações</button>
          </div>
        </form>
      </div>
    </div>

    <script>
      function toggleVendorCode(mode) {
          var roleId = mode === 'create' ? 'createRole' : 'editRole';
          var divId = mode === 'create' ? 'createCodeDiv' : 'editCodeDiv';
          
          var role = document.getElementById(roleId).value;
          var div = document.getElementById(divId);
          
          if (role === 'admin' || role === 'gestor') {
              div.style.visibility = 'hidden'; // Mantém layout mas esconde
          } else {
              div.style.visibility = 'visible';
          }
      }

      var editUserModal = document.getElementById('editUserModal');
      editUserModal.addEventListener('show.bs.modal', function (event) {
        var btn = event.relatedTarget;
        document.getElementById('editUid').value = btn.getAttribute('data-id');
        document.getElementById('editNome').value = btn.getAttribute('data-nome');
        document.getElementById('editEmail').value = btn.getAttribute('data-email');
        document.getElementById('editCode').value = btn.getAttribute('data-code');
        
        var role = btn.getAttribute('data-role');
        document.getElementById('editRole').value = role;
        
        // Ajusta visibilidade do campo código no modal
        toggleVendorCode('edit');
      });
      
      // Inicializa estado do create
      toggleVendorCode('create');
    </script>
    """, rows=rows, user=user)
    return render_page("Usuários", body, "usuarios")

@app.route("/usuarios/<int:user_id>/excluir")
@login_required
def excluir_usuario(user_id):
    user = get_current_user()
    
    # 1. Segurança: Apenas Admin
    if not user or user["role"] != "admin":
        flash("Acesso negado.", "danger")
        return redirect(url_for("dashboard"))

    # 2. Segurança: Não pode se auto-excluir
    if user["id"] == user_id:
        flash("Você não pode excluir seu próprio usuário.", "warning")
        return redirect(url_for("usuarios"))

    with conectar() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()

    flash("Usuário removido com sucesso.", "success")
    return redirect(url_for("usuarios"))

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Acesso negado.", "warning")
        return redirect(url_for("dashboard"))

    mes_ref = request.args.get("mes_ref", date.today().strftime("%Y-%m"))

    if request.method == "POST":
        acao = request.form.get("acao", "salvar_metas")
        mes_post = request.form.get("mes_ref") or mes_ref
        with conectar() as conn:
            ensure_goals_table(conn)
            ensure_ciclos_faturamento_table(conn)

            if acao == "salvar_ciclo_faturamento":
                dia_raw = request.form.get("dia_fechamento", "24")
                try:
                    dia_fechamento = int(str(dia_raw).strip())
                except Exception:
                    dia_fechamento = 24
                if dia_fechamento < 1 or dia_fechamento > 31:
                    flash("Dia de fechamento inválido. Use um número entre 1 e 31.", "warning")
                    return redirect(url_for("settings", mes_ref=mes_post))
                set_dia_fechamento_faturamento(conn, mes_post, dia_fechamento)
                conn.commit()
                flash(f"Fechamento do ciclo de faturamento de {mes_post} salvo para o dia {dia_fechamento}.", "success")
                return redirect(url_for("settings", mes_ref=mes_post))

            if acao == "salvar_metas_faturamento":
                set_meta_equipe(
                    conn,
                    mes_post,
                    "faturamento",
                    parse_br_money(request.form.get("meta_fat_interno", "0")),
                    parse_br_money(request.form.get("meta_fat_externo", "0")),
                )
                conn.commit()
                flash(f"Metas de faturamento de {mes_post} atualizadas.", "success")
                return redirect(url_for("settings", mes_ref=mes_post))

            # salvar_metas_pedidos (padrao) e legado salvar_metas
            set_meta_equipe(
                conn,
                mes_post,
                "pedidos",
                parse_br_money(request.form.get("meta_ped_interno", request.form.get("meta_interno", "0"))),
                parse_br_money(request.form.get("meta_ped_externo", request.form.get("meta_externo", "0"))),
            )
            conn.commit()
        
        flash(f"Metas de pedidos de {mes_post} atualizadas.", "success")
        return redirect(url_for("settings", mes_ref=mes_post))

    with conectar() as conn:
        ensure_goals_table(conn)
        ensure_ciclos_faturamento_table(conn)
        ped_int, ped_ext = get_meta_equipe(conn, mes_ref, "pedidos")
        fat_int, fat_ext = get_meta_equipe(conn, mes_ref, "faturamento")
        dia_fechamento = get_dia_fechamento_faturamento(conn, mes_ref, default=24)

    try:
        ref_dt = datetime.strptime(mes_ref + "-01", "%Y-%m-%d").date()
    except Exception:
        ref_dt = date.today()
    d0_fat, d1_fat = periodo_ciclo_faturamento_manual(ref_dt)

    body = render_template_string(r"""
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <div>
        <div class="h3 mb-0">Configurações</div>
        <div class="text-muted small">Metas e ciclo de faturamento por mês de referência.</div>
      </div>
      <form method="get" class="d-flex align-items-center">
        <input type="month" name="mes_ref" class="form-control form-control-sm fw-bold" value="{{ mes_ref }}" onchange="this.form.submit()">
      </form>
    </div>
    <div class="row justify-content-center g-4">
      <div class="col-12 col-lg-7 d-flex flex-column gap-4">
        <div class="cardx p-4">
          <div class="h4 mb-1">Configurar Metas Pedidos</div>
          <div class="text-muted small mb-3">Meta comercial de pedidos para {{ mes_ref }} (Interna e Externa).</div>
          <form method="post">
            <input type="hidden" name="acao" value="salvar_metas_pedidos">
            <input type="hidden" name="mes_ref" value="{{ mes_ref }}">
            <div class="mb-3">
              <label class="form-label fw-bold text-primary">Meta Equipe INTERNA (R$)</label>
              <input class="form-control form-control-lg" name="meta_ped_interno" value="{{ format_brl(ped_int) }}">
            </div>
            <div class="mb-4">
              <label class="form-label fw-bold text-success">Meta Equipe EXTERNA (R$)</label>
              <input class="form-control form-control-lg" name="meta_ped_externo" value="{{ format_brl(ped_ext) }}">
            </div>
            <div class="d-flex gap-2 flex-wrap">
              <button class="btn btn-primary">Salvar Metas Pedidos</button>
              <a class="btn btn-outline-secondary" href="{{ url_for('dashboard') }}">Voltar</a>
            </div>
          </form>
        </div>
        <div class="cardx p-4">
          <div class="h4 mb-1">Configurar Metas Faturamento</div>
          <div class="text-muted small mb-3">Meta comercial de faturamento para {{ mes_ref }} (Interna e Externa).</div>
          <form method="post">
            <input type="hidden" name="acao" value="salvar_metas_faturamento">
            <input type="hidden" name="mes_ref" value="{{ mes_ref }}">
            <div class="mb-3">
              <label class="form-label fw-bold text-primary">Meta Equipe INTERNA (R$)</label>
              <input class="form-control form-control-lg" name="meta_fat_interno" value="{{ format_brl(fat_int) }}">
            </div>
            <div class="mb-4">
              <label class="form-label fw-bold text-success">Meta Equipe EXTERNA (R$)</label>
              <input class="form-control form-control-lg" name="meta_fat_externo" value="{{ format_brl(fat_ext) }}">
            </div>
            <button class="btn btn-primary">Salvar Metas Faturamento</button>
          </form>
        </div>
      </div>
      <div class="col-12 col-lg-5">
        <div class="cardx p-4 border border-warning-subtle">
          <div class="d-flex align-items-start justify-content-between gap-2 mb-2">
            <div>
              <div class="h5 mb-1">Fechamento do Ciclo de Faturamento</div>
              <div class="text-muted small">Ajuste manual do fechamento real do financeiro, mês a mês.</div>
            </div>
            <span class="badge bg-warning text-dark">{{ mes_ref }}</span>
          </div>
          <div class="alert alert-light border small mb-3">
            Ciclo atual do faturamento: <b>{{ d0_fat.strftime('%d/%m/%Y') }}</b> até <b>{{ d1_fat.strftime('%d/%m/%Y') }}</b>.
          </div>
          <form method="post" class="row g-2 align-items-end">
            <input type="hidden" name="acao" value="salvar_ciclo_faturamento">
            <input type="hidden" name="mes_ref" value="{{ mes_ref }}">
            <div class="col-12 col-md-7">
              <label class="form-label fw-bold">Dia de fechamento</label>
              <input type="number" min="1" max="31" step="1" class="form-control form-control-lg" name="dia_fechamento" value="{{ dia_fechamento }}">
              <div class="form-text">Padrão: dia 25 = 26/mês anterior até 25/mês atual.</div>
            </div>
            <div class="col-12 col-md-5 d-grid">
              <button class="btn btn-warning btn-lg">Salvar Ciclo</button>
            </div>
          </form>
        </div>
      </div>
    </div>
    """, mes_ref=mes_ref, ped_int=ped_int, ped_ext=ped_ext, fat_int=fat_int, fat_ext=fat_ext,
         format_brl=format_brl, dia_fechamento=dia_fechamento, d0_fat=d0_fat, d1_fat=d1_fat)
    return render_page("Configurações", body, "dashboard")

@app.route("/importar_orcamentos", methods=["POST"])
@login_required
def route_importar_orcamentos():
    user = get_current_user()
    if user["role"] != "admin":
        flash("Acesso negado.", "danger")
        return redirect(url_for("home"))
    
    file = request.files.get("file_orc")
    if not file or file.filename == "":
        flash("Nenhum arquivo selecionado.", "warning")
        return redirect(url_for("importacoes"))

    try:
        data = file.read()
        import_id, linhas, registros = import_orcamentos_csv(data, file.filename)
        flash(f"Sucesso! {linhas} linhas lidas | {registros} registros de orçamento processados.", "success")
    except Exception as e:
        flash(f"Erro ao importar orçamentos: {e}", "danger")
    return redirect(url_for("importacoes"))


# -----------------------------
# Metas automáticas / manuais (Empresa + Vendedor)
# AGORA BASEADAS EM PEDIDOS (ENTRADA)
# -----------------------------
# -----------------------------
# Metas automáticas / manuais (Empresa + Vendedor)
# CORRIGIDO: Agrupa por 'pedido' para não somar itens duplicados
# -----------------------------
_SQL_FAT_BASE_COMISSAO = """
CASE
    WHEN UPPER(TRIM(COALESCE(tipo_item_ref,''))) = 'D' THEN 0
    WHEN COALESCE(valor, 0) < 0 THEN 0
    ELSE COALESCE(valor, 0)
END
"""


def _sum_faturamento_comissao(
    conn: sqlite3.Connection, d0: date, d1: date, vendor_id: Optional[int] = None
) -> float:
    """Soma o faturamento da comissão (NF saída + DEMO, sem devolução)."""
    if vendor_id is None:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM({_SQL_FAT_BASE_COMISSAO}), 0) AS total
            FROM invoices
            WHERE data_emissao IS NOT NULL
              AND date(data_emissao) BETWEEN date(?) AND date(?)
            """,
            (d0.isoformat(), d1.isoformat()),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM({_SQL_FAT_BASE_COMISSAO}), 0) AS total
            FROM invoices
            WHERE vendor_id=?
              AND data_emissao IS NOT NULL
              AND date(data_emissao) BETWEEN date(?) AND date(?)
            """,
            (vendor_id, d0.isoformat(), d1.isoformat()),
        ).fetchone()
    return float(row["total"] or 0)


def _faturamento_comissao_por_vendedor(
    conn: sqlite3.Connection, d0: date, d1: date
) -> dict:
    rows = conn.execute(
        f"""
        SELECT vendor_id, COALESCE(SUM({_SQL_FAT_BASE_COMISSAO}), 0) AS total
        FROM invoices
        WHERE data_emissao IS NOT NULL
          AND date(data_emissao) BETWEEN date(?) AND date(?)
        GROUP BY vendor_id
        """,
        (d0.isoformat(), d1.isoformat()),
    ).fetchall()
    return {int(r["vendor_id"] or 0): float(r["total"] or 0) for r in rows}


def _sum_pedidos_periodo(conn: sqlite3.Connection, d0: date, d1: date, vendor_id: Optional[int] = None) -> float:
    """Soma PEDIDOS/REALIZADO com a mesma regra do Dashboard/Excel."""
    if vendor_id is None:
        row = conn.execute(f"""
            SELECT COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS total
            FROM orders o
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
              AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
              AND {sql_pedidos_realizados_excel_cond('o', 'v')}
        """, (d0.isoformat(), d1.isoformat())).fetchone()
    else:
        row = conn.execute(f"""
            SELECT COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0), o.valor, 0)),0) AS total
            FROM orders o
            LEFT JOIN vendors v ON v.id = o.vendor_id
            WHERE o.vendor_id=?
              AND o.data_emissao IS NOT NULL
              AND date(o.data_emissao) BETWEEN date(?) AND date(?)
              AND {sql_pedidos_realizados_excel_cond('o', 'v')}
        """, (vendor_id, d0.isoformat(), d1.isoformat())).fetchone()
    return float(row["total"] or 0)

def get_metrics_orcamentos(conn, dt_inicio, dt_fim, vendor_id=None, vendor_nome=None):
    from crm_npd.repositories.sqlite import orcamentos as repo_orc
    return repo_orc.metricas(conn, dt_inicio, dt_fim, vendor_id=vendor_id, vendor_nome=vendor_nome)

def meta_empresa_calc(conn: sqlite3.Connection, meses: int = 3, fator: float = 1.0) -> float:
    totals = []
    ref = date.today()
    for _ in range(max(1, meses)):
        s, e = ultimo_periodo_fechado(ref)
        val = _sum_pedidos_periodo(conn, s, e, vendor_id=None)
        if val > 0: totals.append(val)
        ref = s - timedelta(days=1)
    if not totals: return 0.0
    return (sum(totals) / len(totals)) * fator

def meta_empresa(conn: sqlite3.Connection, ref: Optional[date] = None) -> float:
    raw = get_setting(conn, "meta_mensal", "").strip()
    if raw: return parse_br_money(raw)
    return meta_empresa_calc(conn, meses=3, fator=1.0) # Fator 1.0 = Média Pura

def meta_vendedor_get_manual(conn: sqlite3.Connection, vendor_id: int) -> Optional[float]:
    row = conn.execute("SELECT meta_manual FROM metas_vendedores WHERE vendor_id=?", (vendor_id,)).fetchone()
    if not row: return None
    v = row["meta_manual"]
    return float(v) if v is not None else None

def meta_vendedor_set_manual(conn: sqlite3.Connection, vendor_id: int, meta: float) -> None:
    conn.execute(
        "INSERT INTO metas_vendedores(vendor_id, meta_manual, updated_at) VALUES(?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET meta_manual=excluded.meta_manual, updated_at=excluded.updated_at",
        (vendor_id, float(meta), now_iso())
    )

def meta_vendedor_calc(conn: sqlite3.Connection, vendor_id: int, meses: int = 3, fator: float = 1.0) -> float:
    totals = []
    ref = date.today()
    for _ in range(max(1, meses)):
        s, e = ultimo_periodo_fechado(ref)
        val = _sum_faturamento_comissao(conn, s, e, vendor_id=vendor_id)
        if val > 0: totals.append(val)
        ref = s - timedelta(days=1)
    if not totals: return 0.0
    return (sum(totals) / len(totals)) * fator

def meta_vendedor(conn: sqlite3.Connection, vendor_id: int) -> float:
    manual = meta_vendedor_get_manual(conn, vendor_id)
    if manual is not None and manual > 0: return manual
    return meta_vendedor_calc(conn, vendor_id, meses=3, fator=1.0)

# --- BLOCO 1: NOVAS FUNÇÕES E ROTA DE TIPO ---

def ensure_vendor_type_column(conn):
    """Garante que a coluna 'tipo' exista na tabela vendors."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(vendors)")]
    if "tipo" not in cols:
        conn.execute("ALTER TABLE vendors ADD COLUMN tipo TEXT DEFAULT 'Externo'")
        conn.commit()

@app.route("/vendedores/alterar_tipo/<int:vendor_id>", methods=["POST"])
@login_required
def alterar_tipo_vendedor(vendor_id):
    user = get_current_user()
    if not user or user["role"] != "admin":
        flash("Acesso negado.", "warning")
        return redirect(url_for("vendedores"))

    novo_tipo = request.form.get("novo_tipo")
    if novo_tipo in ["Interno", "Externo", "Site"]:
        with conectar() as conn:
            conn.execute("UPDATE vendors SET tipo=? WHERE id=?", (novo_tipo, vendor_id))
            conn.commit()
        flash(f"Vendedor alterado para {novo_tipo}.", "success")
    return redirect(url_for("vendedores"))

@app.route("/vendedor/<int:vendor_id>/meta", methods=["POST"])
@login_required
def vendedor_meta(vendor_id: int):
    # --- TRAVA DE SEGURANÇA: ADMIN E GESTOR ---
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Apenas Admin ou Gestor pode alterar meta do vendedor.", "warning")
        return redirect(url_for("vendedor", vendor_id=vendor_id))

    meta_txt = request.form.get("meta_mensal", "").strip()
    meta = parse_br_money(meta_txt)

    if meta <= 0:
        flash("Meta inválida.", "warning")
        return redirect(url_for("vendedor", vendor_id=vendor_id))

    with conectar() as conn:
        meta_vendedor_set_manual(conn, vendor_id, meta)
        conn.commit()

    flash("Meta do vendedor atualizada.", "success")
    return redirect(url_for("vendedor", vendor_id=vendor_id))

@app.route("/vendedor/<int:vendor_id>/meta_hist", methods=["POST"])
@login_required
def vendedor_meta_hist(vendor_id):
    user = get_current_user()
    if not user or user["role"] not in ["admin", "gestor"]:
        flash("Apenas Admin ou Gestor pode alterar meta individual.", "warning")
        return redirect(url_for("home"))
    
    mes = request.form.get("mes_ref")
    valor = parse_br_money(request.form.get("meta_valor"))
    
    if mes and valor > 0:
        with conectar() as conn:
            ensure_goals_table(conn)
            set_goal_history(conn, mes, "VENDEDOR", valor, entity_id=vendor_id)
            conn.commit()
        flash("Meta individual atualizada.", "success")
        
    return redirect(url_for("vendedor", vendor_id=vendor_id, data_ref=mes))


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    init_db()
    with conectar() as conn:
        ensure_users_schema(conn)
    ensure_admin_seed()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8089")), debug=True)

