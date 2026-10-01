from __future__ import annotations
import sqlite3
from .db import ensure_column


def _safe_index(conn: sqlite3.Connection, sql: str) -> None:
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass


def apply_safe_migrations(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "tasks", "identity_key TEXT")
    ensure_column(conn, "tasks", "identity_nome TEXT")
    ensure_column(conn, "tasks", "cliente_nome_cache TEXT")

    ensure_column(conn, "invoices", "pedido TEXT")
    ensure_column(conn, "invoices", "quantidade REAL")
    ensure_column(conn, "invoices", "produto TEXT")
    ensure_column(conn, "invoices", "formato TEXT")
    ensure_column(conn, "invoices", "medida_ref TEXT")
    ensure_column(conn, "invoices", "cliente_nome_ref TEXT")
    ensure_column(conn, "invoices", "vendor_nome_ref TEXT")
    ensure_column(conn, "invoices", "dedupe_key TEXT")
    ensure_column(conn, "invoices", "is_jit INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "invoices", "rede_nome TEXT")
    ensure_column(conn, "invoices", "rede_key TEXT")
    ensure_column(conn, "invoices", "comissionavel INTEGER NOT NULL DEFAULT 1")

    ensure_column(conn, "orders", "condicao_entrega TEXT")
    ensure_column(conn, "orders", "is_jit INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "orders", "rede_nome TEXT")
    ensure_column(conn, "orders", "rede_key TEXT")
    ensure_column(conn, "orders", "medida_ref TEXT")
    ensure_column(conn, "orders", "cliente_nome_ref TEXT")
    ensure_column(conn, "orders", "vendor_nome_ref TEXT")
    ensure_column(conn, "orders", "dedupe_key TEXT")
    ensure_column(conn, "orders", "situacao_pedido TEXT")
    ensure_column(conn, "orders", "motivo_pedido TEXT")
    ensure_column(conn, "orders", "desc_motivo_pedido TEXT")
    ensure_column(conn, "orders", "pedido_cancelado INTEGER NOT NULL DEFAULT 0")

    # Campos do relatório 104 usados na visão detalhada do orçamento.
    # São aditivos: não alteram a lógica existente de pedidos, ciclo, metas ou faturamento.
    ensure_column(conn, "orcamentos", "quantidade REAL DEFAULT 0")
    ensure_column(conn, "orcamentos", "valor_base REAL DEFAULT 0")
    ensure_column(conn, "orcamentos", "observacao TEXT")
    ensure_column(conn, "orcamentos", "obs TEXT")
    ensure_column(conn, "orcamentos", "data_prev TEXT")
    ensure_column(conn, "orcamentos", "data_retorno TEXT")
    ensure_column(conn, "orcamentos", "status_os TEXT")
    ensure_column(conn, "orcamentos", "prospeccao TEXT")
    ensure_column(conn, "orcamentos", "desc_prospeccao TEXT")
    ensure_column(conn, "orcamentos", "funcionario TEXT")
    ensure_column(conn, "orcamentos", "nome_funcionario TEXT")
    ensure_column(conn, "orcamentos", "cliente_codigo TEXT")
    ensure_column(conn, "orcamentos", "cidade TEXT")

    ensure_column(conn, "import_identity_pending", "similarity_score REAL")
    ensure_column(conn, "import_identity_pending", "suggested_identity_key TEXT")
    ensure_column(conn, "import_identity_pending", "acao_sugerida TEXT")
    ensure_column(conn, "import_identity_pending", "review_notes TEXT")

    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_vendor_status ON tasks(vendor_id, status)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_cliente_id ON tasks(cliente_id)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_identity_key ON tasks(identity_key)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_task_history_task_created ON task_history(task_id, created_at DESC)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_cliente_data ON orders(cliente_id, data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_vendor_data ON orders(vendor_id, data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_rede_key ON orders(rede_key)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_is_jit ON orders(is_jit)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_invoices_cliente_data ON invoices(cliente_id, data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_invoices_vendor_data ON invoices(vendor_id, data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_invoices_rede_key ON invoices(rede_key)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_identity_rules_identity ON customer_identity_rules(identity_key)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_identity_rules_matriz ON customer_identity_rules(matriz_customer_id)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_identity_rules_tipo ON customer_identity_rules(tipo_vinculo)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_pending_status_import ON import_identity_pending(status, import_id)")

    # Índices extras para acelerar telas pesadas: tarefas, diagnóstico, vendedor e buscas por cliente/pedido.
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_pedido ON orders(pedido)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_data ON orders(data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orders_cancelado ON orders(pedido_cancelado)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_invoices_pedido ON invoices(pedido)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_invoices_data ON invoices(data_emissao)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_customers_fantasia ON customers(fantasia)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_customers_nome ON customers(nome)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_customers_codigo ON customers(codigo)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_vendors_nome ON vendors(nome)")
    # CRM 4.1 - acelera orçamento/oportunidades e filtros por período/status/vendedor.
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orc_data_status ON orcamentos(data_inicio, desc_status)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orc_data_vendedor_nome ON orcamentos(data_inicio, vendedor_nome)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orc_data_pedido ON orcamentos(data_inicio, pedido_id)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orc_pedido_id ON orcamentos(pedido_id)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_orc_vendedor_codigo ON orcamentos(vendedor_id)")

    # Trava física contra duplicação futura. Linhas legítimas repetidas dos CSVs
    # recebem sufixo OCC:n no importador, portanto dedupe_key deve ser sempre única.
    _safe_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_dedupe_key ON orders(dedupe_key) WHERE dedupe_key IS NOT NULL AND TRIM(dedupe_key)<>''")
    _safe_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_dedupe_key ON invoices(dedupe_key) WHERE dedupe_key IS NOT NULL AND TRIM(dedupe_key)<>''")
    _safe_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_liquidez_dedupe_key ON liquidez(dedupe_key) WHERE dedupe_key IS NOT NULL AND TRIM(dedupe_key)<>''")
    _safe_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ux_orcamentos_dedupe_key ON orcamentos(dedupe_key) WHERE dedupe_key IS NOT NULL AND TRIM(dedupe_key)<>''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ciclos_faturamento(
            periodo TEXT PRIMARY KEY,
            dia_fechamento INTEGER NOT NULL DEFAULT 24,
            updated_at TEXT NOT NULL
        )
    """)

    # CRM 4.0 - Leads, oportunidades e atividades comerciais.
    # Tabelas independentes para não alterar as regras consolidadas de pedidos/faturamento.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            empresa TEXT,
            telefone TEXT,
            email TEXT,
            origem TEXT,
            vendor_id INTEGER,
            customer_id INTEGER,
            etapa TEXT NOT NULL DEFAULT 'NOVO',
            temperatura TEXT NOT NULL DEFAULT 'MORNO',
            valor_estimado REAL NOT NULL DEFAULT 0,
            probabilidade INTEGER NOT NULL DEFAULT 10,
            proxima_acao TEXT,
            proxima_acao_em TEXT,
            observacoes TEXT,
            motivo_perda TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            FOREIGN KEY(vendor_id) REFERENCES vendors(id),
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_lead_activities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'NOTA',
            descricao TEXT NOT NULL,
            agendado_em TEXT,
            concluido INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(lead_id) REFERENCES crm_leads(id) ON DELETE CASCADE
        )
    """)
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_leads_etapa ON crm_leads(etapa)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_leads_vendor ON crm_leads(vendor_id)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_leads_next_action ON crm_leads(proxima_acao_em)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_leads_updated ON crm_leads(updated_at)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_activity_lead ON crm_lead_activities(lead_id, created_at DESC)")
    _safe_index(conn, "CREATE INDEX IF NOT EXISTS idx_crm_activity_schedule ON crm_lead_activities(concluido, agendado_em)")

    # CRM 4.1 - atualiza a base operacional da IA em bancos que já possuíam os seeds antigos.
    try:
        conn.execute("""
            UPDATE ia_knowledge
               SET resposta='Entre em **Atualizar Sisplan**. Na importação inteligente, selecione de uma vez os CSVs de **PEDIDOS, FATURAMENTO, ORÇAMENTO e LIQUIDEZ**; o CRM identifica cada arquivo automaticamente, importa na ordem correta e reprocessa redes/matrizes uma única vez no final. Se precisar corrigir só PEDIDOS, use a importação avulsa.',
                   palavras_chave='importar pedidos csv sisplan atualizar dados pacote'
             WHERE pergunta='Como importar pedidos?'
        """)
        conn.execute("""
            UPDATE ia_knowledge
               SET resposta='Entre em **Atualizar Sisplan** e, de preferência, selecione os quatro relatórios juntos. O CRM detecta o arquivo de **FATURAMENTO** automaticamente. Para uma correção pontual, use a seção **Importação avulsa**.',
                   palavras_chave='importar faturamento notas invoices csv atualizar sisplan'
             WHERE pergunta='Como importar faturamento?'
        """)
        conn.execute("""
            UPDATE ia_knowledge
               SET resposta='Entre em **Atualizar Sisplan** e selecione os relatórios exportados. O CRM reconhece **LIQUIDEZ** automaticamente; para uma correção isolada, use **Importação avulsa**. A liquidez é usada principalmente em regras financeiras/comerciais já configuradas no CRM.',
                   palavras_chave='importar liquidez comissão externos atualizar sisplan'
             WHERE pergunta='Como importar liquidez?'
        """)
        conn.execute("""
            INSERT INTO ia_knowledge(categoria,pergunta,resposta,permissao,palavras_chave,ativo)
            SELECT 'oportunidades','Como cadastrar um novo lead?',
                   'Você **não precisa cadastrar lead manualmente**. O CRM usa o relatório de **ORÇAMENTOS do Sisplan** como fonte única. Entre em **Oportunidades** para ver os orçamentos transformados em oportunidades e em **Funil** para acompanhar as etapas. Para atualizar, use **Atualizar Sisplan**.',
                   'all','novo lead cadastrar oportunidade sisplan orçamento funil',1
            WHERE NOT EXISTS (SELECT 1 FROM ia_knowledge WHERE pergunta='Como cadastrar um novo lead?')
        """)
        conn.execute("""
            INSERT INTO ia_knowledge(categoria,pergunta,resposta,permissao,palavras_chave,ativo)
            SELECT 'funil','Como funciona o funil comercial?',
                   'O **Funil** é uma visão automática e somente leitura dos status de orçamento do Sisplan. Ele não cria uma base paralela. Se a negociação mudar de fase, atualize o status no Sisplan e depois rode **Atualizar Sisplan** no CRM.',
                   'all','funil etapa orçamento sisplan oportunidade negociação',1
            WHERE NOT EXISTS (SELECT 1 FROM ia_knowledge WHERE pergunta='Como funciona o funil comercial?')
        """)
    except sqlite3.OperationalError:
        pass

    conn.commit()
