"""Rotas de relatorios, A Faturar, analise historica e vendedores inativos."""
from __future__ import annotations


def registrar_rotas(m) -> None:
    """Registra as telas novas no app Flask do monolito."""
    from datetime import date, datetime, timedelta
    import json

    from flask import abort, flash, redirect, render_template_string, request, url_for

    from crm_npd.services import planejamento as P
    from crm_npd.services import relatorios as R
    from crm_npd.services import vendedores_ciclo as V

    app = m.app
    login_required = m.login_required
    conectar = m.conectar
    render_page = m.render_page
    get_current_user = m.get_current_user
    get_setting = m.get_setting
    format_brl = m.format_brl
    bar_chart = m.bar_chart
    create_task_unificada = m.create_task_unificada
    now_iso = m.now_iso
    ensure_vendors_schema = m.ensure_vendors_schema
    sql_pedidos_realizados_excel_cond = m.sql_pedidos_realizados_excel_cond
    sql_pedido_cancelado_cond = m.sql_pedido_cancelado_cond
    format_num_br = R.format_num_br
    norm = m.norm
    _sisplan_stage = m._sisplan_stage
    _performance_build_context = m._performance_build_context
    _performance_vendor_catalog = m._performance_vendor_catalog
    _performance_resolve_orc_vendor = m._performance_resolve_orc_vendor

    _parse_report_ref = R._parse_report_ref
    _report_date_in_months = R._report_date_in_months
    _resolve_report_period = R._resolve_report_period
    _report_datasets = R._report_datasets
    _report_selected_months = R._report_selected_months
    _report_months_label = R._report_months_label
    _report_apply_dataset_filters = R._report_apply_dataset_filters
    _xlsx_response = R._xlsx_response
    _a_faturar_data = R._a_faturar_data
    _analysis_scope_sql = R._analysis_scope_sql
    _analysis_scope_label = R._analysis_scope_label
    _MONTHS_PT = R.MONTHS_PT
    _MONTH_NAME_PT = R.MONTH_NAME_PT
    _planning_analysis_data = P._planning_analysis_data
    _sync_vendedores_inativos = V._sync_vendedores_inativos
    _vendor_team_alias_key = V._vendor_team_alias_key
    _vendor_alias_ids = V._vendor_alias_ids
    _vendor_cache_clear = V._vendor_cache_clear

    @app.route("/vendedores/inativos")
    @login_required
    def vendedores_inativos():
        user = get_current_user()
        if not user or (user.get("role") or "").strip().lower() not in ("admin", "gestor"):
            flash("Acesso restrito ao gestor.", "warning")
            return redirect(url_for("vendedores"))
        with conectar() as conn:
            ensure_vendors_schema(conn)
            _sync_vendedores_inativos(conn)
            raw_rows = [dict(r) for r in conn.execute("""
                SELECT v.*,
                       (SELECT MAX(date(o.data_emissao)) FROM orders o WHERE o.vendor_id=v.id) AS ultima_venda
                FROM vendors v
                WHERE COALESCE(v.ativo,1)=0
                  AND COALESCE(UPPER(TRIM(v.nome)), '') <> 'SITE'
                  AND COALESCE(UPPER(TRIM(v.tipo)), '') <> 'SITE'
                ORDER BY COALESCE(v.inativado_em,'') DESC, v.nome ASC
            """).fetchall()]
            grouped = {}
            for r in raw_rows:
                key = _vendor_team_alias_key(r.get("nome")) or f"ID:{r['id']}"
                if key not in grouped:
                    grouped[key] = r
                    grouped[key]["alias_ids"] = [int(r["id"])]
                else:
                    grouped[key]["alias_ids"].append(int(r["id"]))
                    if (r.get("ultima_venda") or "") > (grouped[key].get("ultima_venda") or ""):
                        grouped[key]["ultima_venda"] = r.get("ultima_venda")
                    if (r.get("inativado_em") or "") > (grouped[key].get("inativado_em") or ""):
                        grouped[key]["inativado_em"] = r.get("inativado_em")
                        grouped[key]["inativado_motivo"] = r.get("inativado_motivo")
            rows = sorted(grouped.values(), key=lambda r: ((r.get("inativado_em") or ""), str(r.get("nome") or "")), reverse=True)
        body = render_template_string(r"""
        <div class="d-flex justify-content-between align-items-start mb-3 flex-wrap gap-2"><div><h3 class="mb-1">Vendedores inativos</h3><div class="muted">Ficam fora da equipe atual, Performance, metas e listas operacionais até serem recuperados.</div></div><a class="btn btn-light border" href="{{ url_for('vendedores') }}"><i class="bi bi-arrow-left me-1"></i>Voltar aos ativos</a></div>
        <div class="cardx p-0 overflow-visible"><div class="table-responsive overflow-visible"><table class="table table-hover align-middle mb-0"><thead><tr><th class="ps-3">Vendedor</th><th>Última venda</th><th>Motivo</th><th>Inativado em</th><th class="text-end pe-3">Ações</th></tr></thead><tbody>
        {% for v in rows %}<tr><td class="ps-3"><div class="fw-bold">{{ v['nome'] }}</div><div class="small text-muted">{{ v['tipo'] or '—' }}{% if v['codigo'] %} · {{ v['codigo'] }}{% endif %}</div></td><td>{{ v['ultima_venda'] or 'Sem venda registrada' }}</td><td><span class="badge text-bg-light border text-dark">{{ 'Sem venda há 3 meses' if v['inativado_motivo']=='SEM_VENDA_3_MESES' else 'Inativado manualmente' }}</span></td><td>{{ v['inativado_em'] or '—' }}</td><td class="text-end pe-3"><div class="dropdown d-inline-block"><button class="btn btn-sm btn-light border" data-bs-toggle="dropdown" title="Mais opções"><i class="bi bi-three-dots-vertical"></i></button><ul class="dropdown-menu dropdown-menu-end shadow-sm"><li><form method="post" action="{{ url_for('vendedor_acao', vendor_id=v['id']) }}"><input type="hidden" name="acao" value="reativar"><button class="dropdown-item text-success"><i class="bi bi-person-check me-2"></i>Recuperar vendedor</button></form></li></ul></div></td></tr>
        {% else %}<tr><td colspan="5" class="text-center py-5 text-muted"><i class="bi bi-people fs-2 d-block mb-2"></i>Nenhum vendedor inativo.</td></tr>{% endfor %}
        </tbody></table></div></div>
        """, rows=rows)
        return render_page("Vendedores inativos", body, "vendedores")

    @app.route("/vendedores/acao/<int:vendor_id>", methods=["POST"])
    @login_required
    def vendedor_acao(vendor_id):
        user = get_current_user()
        if not user or (user.get("role") or "").strip().lower() not in ("admin", "gestor"):
            flash("Apenas Admin ou Gestor pode alterar vendedores.", "warning")
            return redirect(url_for("vendedores"))
        acao = (request.form.get("acao") or "").strip().lower()
        if acao not in ("interno", "externo", "inativar", "reativar"):
            flash("Ação inválida.", "warning")
            return redirect(url_for("vendedores"))
        with conectar() as conn:
            ensure_vendors_schema(conn)
            atual = conn.execute("SELECT id,nome,tipo,ativo FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not atual:
                flash("Vendedor não encontrado.", "warning")
                return redirect(url_for("vendedores"))
            ids = _vendor_alias_ids(conn, vendor_id)
            marks = ",".join("?" for _ in ids)
            if acao in ("interno", "externo"):
                novo_tipo = "Interno" if acao == "interno" else "Externo"
                conn.execute(f"UPDATE vendors SET tipo=?, performance_ativo=1 WHERE id IN ({marks})", [novo_tipo, *ids])
                msg = f"{atual['nome']} movido para {novo_tipo}."
                dest = "vendedores"
            elif acao == "inativar":
                conn.execute(f"""UPDATE vendors SET ativo=0, performance_ativo=0,
                    inativado_em=?, inativado_motivo='MANUAL'
                    WHERE id IN ({marks})""", [now_iso(), *ids])
                msg = f"{atual['nome']} foi inativado e saiu da equipe operacional."
                dest = "vendedores"
            else:
                conn.execute(f"""UPDATE vendors SET ativo=1, performance_ativo=1,
                    inativado_em=NULL, inativado_motivo=NULL, reativado_em=?
                    WHERE id IN ({marks})""", [now_iso(), *ids])
                msg = f"{atual['nome']} foi recuperado e voltou à equipe."
                dest = "vendedores_inativos"
            conn.commit()
        _vendor_cache_clear()
        flash(msg, "success")
        return redirect(url_for(dest))

    @app.route("/dashboard/detalhe-pedidos")
    @login_required
    def dashboard_pedidos_detalhe():
        """Abre a composição exata dos valores de Pedidos Realizados do Dashboard."""
        user = get_current_user()
        if not user or user["role"] not in ["admin", "gestor"]:
            flash("Acesso negado.", "warning")
            return redirect(url_for("home"))

        periodo = (request.args.get("periodo") or "mes").strip().lower()
        if periodo not in {"dia", "semana", "mes", "trimestre", "semestre", "ano"}:
            periodo = "mes"
        data_ref = (request.args.get("data_ref") or (date.today().strftime("%Y-%m") if periodo=="mes" else date.today().isoformat())).strip()
        scope = (request.args.get("escopo") or "TOTAL").strip().upper()
        if scope not in {"TOTAL", "INTERNO", "EXTERNO", "SITE"}:
            scope = "TOTAL"
        ref = _parse_report_ref(data_ref, date.today())
        d0, d1, periodo_label = _resolve_report_period(periodo, ref)
        scope_sql = _analysis_scope_sql(scope, "v")

        with conectar() as conn:
            rows = conn.execute(f"""
                SELECT
                    TRIM(COALESCE(o.pedido,'')) AS pedido,
                    MAX(date(o.data_emissao)) AS data,
                    COALESCE(
                        MAX(NULLIF(TRIM(c.rede_nome),'')),
                        MAX(NULLIF(TRIM(c.fantasia),'')),
                        MAX(NULLIF(TRIM(c.nome),'')),
                        MAX(NULLIF(TRIM(o.cliente_nome_ref),'')), '—'
                    ) AS cliente,
                    COALESCE(MAX(NULLIF(TRIM(v.nome),'')), '—') AS vendedor,
                    MAX(CASE
                        WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE' THEN 'SITE'
                        ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO')
                    END) AS time,
                    GROUP_CONCAT(DISTINCT NULLIF(TRIM(o.medida_ref),'')) AS medidas,
                    GROUP_CONCAT(DISTINCT NULLIF(TRIM(o.papel_ref),'')) AS papeis,
                    COALESCE(SUM(COALESCE(o.quantidade,0)),0) AS quantidade,
                    COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) AS valor
                FROM orders o
                LEFT JOIN customers c ON c.id=o.cliente_id
                LEFT JOIN vendors v ON v.id=o.vendor_id
                WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND {scope_sql}
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY TRIM(COALESCE(o.pedido,''))
                HAVING TRIM(COALESCE(o.pedido,''))<>''
                ORDER BY valor DESC, data DESC, pedido DESC
            """, (d0.isoformat(), d1.isoformat())).fetchall()

            vendors = conn.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(v.nome),''),'Sem vendedor') AS nome,
                       CASE WHEN COALESCE(UPPER(TRIM(v.nome)),'')='SITE' OR COALESCE(UPPER(TRIM(v.tipo)),'')='SITE'
                            THEN 'SITE' ELSE COALESCE(UPPER(TRIM(v.tipo)),'EXTERNO') END AS time,
                       COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS pedidos,
                       COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) AS valor
                FROM orders o
                LEFT JOIN vendors v ON v.id=o.vendor_id
                WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND {scope_sql}
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY v.id, nome, time
                ORDER BY valor DESC
            """, (d0.isoformat(), d1.isoformat())).fetchall()

            clients = conn.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(c.rede_nome),''),NULLIF(TRIM(c.fantasia),''),NULLIF(TRIM(c.nome),''),NULLIF(TRIM(o.cliente_nome_ref),''),'—') AS cliente,
                       COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS pedidos,
                       COALESCE(SUM(COALESCE(o.quantidade,0)),0) AS quantidade,
                       COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) AS valor
                FROM orders o
                LEFT JOIN customers c ON c.id=o.cliente_id
                LEFT JOIN vendors v ON v.id=o.vendor_id
                WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND {scope_sql}
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY cliente
                ORDER BY valor DESC
                LIMIT 30
            """, (d0.isoformat(), d1.isoformat())).fetchall()

            papers = conn.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(o.papel_ref),''),'Sem papel informado') AS papel,
                       COUNT(DISTINCT TRIM(COALESCE(o.pedido,''))) AS pedidos,
                       COALESCE(SUM(COALESCE(o.quantidade,0)),0) AS quantidade,
                       COALESCE(SUM(COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0)),0) AS valor
                FROM orders o
                LEFT JOIN vendors v ON v.id=o.vendor_id
                WHERE date(o.data_emissao) BETWEEN date(?) AND date(?)
                  AND {scope_sql}
                  AND {sql_pedidos_realizados_excel_cond('o','v')}
                GROUP BY papel
                ORDER BY quantidade DESC, valor DESC
                LIMIT 20
            """, (d0.isoformat(), d1.isoformat())).fetchall()

        total_value = sum(float(r["valor"] or 0) for r in rows)
        total_qty = sum(float(r["quantidade"] or 0) for r in rows)
        client_count = len({(r["cliente"] or "").strip().upper() for r in rows if (r["cliente"] or "").strip()})
        ticket = total_value / len(rows) if rows else 0.0

        body = render_template_string("""
        <div class="page-hero mb-4">
          <div class="d-flex justify-content-between align-items-start flex-wrap gap-3">
            <div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#1678c4">Auditoria do realizado</div>
            <div class="hero-title">Composição de Pedidos · {{ scope_label }}</div>
            <div class="hero-sub mt-1">{{ periodo_label }} · a soma abaixo usa exatamente a mesma regra oficial do Dashboard.</div></div>
            <div class="d-flex gap-2 flex-wrap"><a class="btn btn-success" href="{{ url_for('relatorios_exportar',relatorio='operacional',periodo=periodo,data_ref=data_ref,escopo=scope,conteudo='PEDIDOS') }}"><i class="bi bi-file-earmark-excel me-1"></i>Exportar Excel</a><a class="btn btn-light border" href="{{ url_for('dashboard', data_ref=data_ref, periodo=periodo) }}"><i class="bi bi-arrow-left me-1"></i>Voltar ao Dashboard</a></div>
          </div>
        </div>

        <div class="d-flex gap-2 flex-wrap mb-4">
          {% for code,label in [('TOTAL','Total'),('INTERNO','Interno'),('EXTERNO','Externo'),('SITE','Site')] %}
            <a class="btn {{ 'btn-primary' if scope==code else 'btn-light border' }}" href="{{ url_for('dashboard_pedidos_detalhe', data_ref=data_ref, periodo=periodo, escopo=code) }}">{{ label }}</a>
          {% endfor %}
        </div>

        <div class="row g-3 mb-4">
          <div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Valor realizado</div><div class="num">{{ format_brl(total_value) }}</div><div class="desc">soma das linhas válidas</div></div></div>
          <div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Pedidos</div><div class="num">{{ rows|length }}</div><div class="desc">pedidos distintos</div></div></div>
          <div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Clientes</div><div class="num">{{ client_count }}</div><div class="desc">na composição selecionada</div></div></div>
          <div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Ticket médio</div><div class="num">{{ format_brl(ticket) }}</div><div class="desc">por pedido</div></div></div>
        </div>

        <div class="row g-4 mb-4">
          <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="section-title mb-1">Por vendedor</div><div class="section-sub mb-3">Quem compõe o realizado deste recorte.</div><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Vendedor</th><th>Time</th><th class="text-end">Pedidos</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in vendors %}<tr><td class="fw-bold">{{ r.nome }}</td><td><span class="chip">{{ r.time }}</span></td><td class="text-end">{{ r.pedidos }}</td><td class="text-end fw-bold">{{ format_brl(r.valor or 0) }}</td></tr>{% else %}<tr><td colspan="4" class="text-center text-muted py-3">Sem dados.</td></tr>{% endfor %}</tbody></table></div></div></div>
          <div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="section-title mb-1">Principais clientes</div><div class="section-sub mb-3">Concentração de valor e quantidade no período.</div><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Cliente</th><th class="text-end">Pedidos</th><th class="text-end">Qtd.</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in clients[:12] %}<tr><td class="fw-bold">{{ r.cliente }}</td><td class="text-end">{{ r.pedidos }}</td><td class="text-end">{{ '%.0f'|format(r.quantidade or 0) }}</td><td class="text-end fw-bold">{{ format_brl(r.valor or 0) }}</td></tr>{% else %}<tr><td colspan="4" class="text-center text-muted py-3">Sem dados.</td></tr>{% endfor %}</tbody></table></div></div></div>
        </div>

        <div class="cardx p-4 mb-4"><div class="section-title mb-1">Pedidos que formam o valor</div><div class="section-sub mb-3">Cada linha abaixo é um pedido consolidado; papel e medida vêm dos itens que compõem o pedido.</div>
          <div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Data</th><th>Pedido</th><th>Cliente</th><th>Vendedor</th><th>Time</th><th>Medida</th><th>Papel</th><th class="text-end">Qtd.</th><th class="text-end">Valor</th></tr></thead><tbody>
          {% for r in rows %}<tr><td class="text-muted">{{ r.data or '—' }}</td><td class="fw-bold text-primary">{{ r.pedido }}</td><td>{{ r.cliente }}</td><td>{{ r.vendedor }}</td><td><span class="chip">{{ r.time }}</span></td><td class="small">{{ r.medidas or '—' }}</td><td class="small">{{ r.papeis or '—' }}</td><td class="text-end">{{ '%.0f'|format(r.quantidade or 0) }}</td><td class="text-end fw-bold">{{ format_brl(r.valor or 0) }}</td></tr>
          {% else %}<tr><td colspan="9" class="text-center text-muted py-4">Sem pedidos válidos para este recorte.</td></tr>{% endfor %}
          </tbody><tfoot><tr class="fw-bold"><td colspan="7">TOTAL</td><td class="text-end">{{ '%.0f'|format(total_qty) }}</td><td class="text-end">{{ format_brl(total_value) }}</td></tr></tfoot></table></div>
        </div>

        <div class="cardx p-4"><div class="section-title mb-1">Composição por papel</div><div class="section-sub mb-3">Ajuda a transformar venda em leitura de demanda de matéria-prima.</div><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Papel</th><th class="text-end">Pedidos</th><th class="text-end">Quantidade</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in papers %}<tr><td class="fw-bold">{{ r.papel }}</td><td class="text-end">{{ r.pedidos }}</td><td class="text-end">{{ '%.0f'|format(r.quantidade or 0) }}</td><td class="text-end">{{ format_brl(r.valor or 0) }}</td></tr>{% endfor %}</tbody></table></div></div>
        """,
        rows=rows, vendors=vendors, clients=clients, papers=papers,
        total_value=total_value, total_qty=total_qty, client_count=client_count, ticket=ticket,
        scope=scope, scope_label=_analysis_scope_label(scope), data_ref=data_ref, periodo=periodo, periodo_label=periodo_label,
        ref=ref, month_name=_MONTH_NAME_PT.get(ref.month, str(ref.month)), format_brl=format_brl)
        return render_page(f"Pedidos · {_analysis_scope_label(scope)}", body, "dashboard")

    @app.route("/a-faturar")
    @login_required
    def a_faturar():
        user=get_current_user()
        if not user or user['role'] not in ['admin','gestor']:
            flash('Acesso restrito à gestão.','warning'); return redirect(url_for('home'))
        scope=(request.args.get('escopo') or 'TOTAL').strip().upper()
        vendedor=(request.args.get('vendedor') or '').strip(); cliente=(request.args.get('cliente') or '').strip()
        produto=(request.args.get('produto') or request.args.get('grupo') or '').strip(); medida=(request.args.get('medida') or '').strip()
        status=(request.args.get('status') or '').strip().upper(); entrega_ini=(request.args.get('entrega_ini') or '').strip(); entrega_fim=(request.args.get('entrega_fim') or '').strip()
        selected_months=_report_selected_months(request.args)
        try:
            filter_year=int(request.args.get('ano_filtro') or 0)
        except Exception:
            filter_year=0
        # Sem nenhum filtro explícito, abre automaticamente no mês mais recente com pedidos válidos.
        explicit_filter = bool(vendedor or cliente or produto or medida or status or entrega_ini or entrega_fim or scope!='TOTAL' or selected_months or filter_year)
        default_period_label=''
        with conectar() as conn:
            if not explicit_filter:
                latest_row=conn.execute(f"""
                    SELECT MAX(date(o.data_emissao))
                    FROM orders o
                    LEFT JOIN vendors v ON v.id=o.vendor_id
                    WHERE o.data_emissao IS NOT NULL
                      AND date(o.data_emissao) <= date('now','localtime')
                      AND NOT {sql_pedido_cancelado_cond('o')}
                      AND TRIM(COALESCE(o.pedido,''))<>''
                """).fetchone()
                latest_date=(latest_row[0] if latest_row else None) or date.today().isoformat()
                try:
                    ld=datetime.strptime(str(latest_date)[:10],'%Y-%m-%d').date()
                except Exception:
                    ld=date.today()
                filter_year=ld.year; selected_months=[ld.month]
                default_period_label=f"{_MONTH_NAME_PT.get(ld.month,str(ld.month))}/{ld.year}"
            data=_a_faturar_data(conn,scope=scope,vendedor=vendedor,cliente=cliente,produto=produto,medida=medida,status=status,entrega_ini=entrega_ini,entrega_fim=entrega_fim,year=(filter_year if selected_months else None),months=selected_months)
            cobertura = conn.execute(
                "SELECT AVG(CASE WHEN TRIM(COALESCE(entrega_pedido,''))<>'' THEN 1.0 ELSE 0.0 END) FROM orders WHERE TRIM(COALESCE(pedido,''))<>''"
            ).fetchone()
            entrega_ok = float((cobertura[0] if cobertura else 0) or 0) > 0
            vendors=[r[0] for r in conn.execute("SELECT DISTINCT COALESCE(NULLIF(TRIM(o.vendor_nome_ref),''),NULLIF(TRIM(v.nome),'')) nome FROM orders o LEFT JOIN vendors v ON v.id=o.vendor_id WHERE TRIM(COALESCE(o.pedido,''))<>'' AND TRIM(COALESCE(COALESCE(NULLIF(TRIM(o.vendor_nome_ref),''),NULLIF(TRIM(v.nome),'')),''))<>'' ORDER BY nome").fetchall()]
            measures=[r[0] for r in conn.execute("SELECT DISTINCT medida_ref FROM orders WHERE TRIM(COALESCE(medida_ref,''))<>'' ORDER BY medida_ref").fetchall()]
        export_params=dict(relatorio='a_faturar',escopo=scope,vendedor=vendedor,cliente=cliente,grupo=produto,medida=medida,status=status,entrega_ini=entrega_ini,entrega_fim=entrega_fim)
        if selected_months:
            export_params['ano_filtro']=filter_year
            export_params['meses']=','.join(str(m) for m in selected_months)
        export_url=url_for('relatorios_exportar',**export_params)
        body=render_template_string(r'''
        <div class="page-hero mb-4"><div class="d-flex justify-content-between align-items-start flex-wrap gap-3"><div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Pedido → Faturamento → Liquidez</div><div class="hero-title">A Faturar</div><div class="hero-sub mt-1">O que ainda precisa ser faturado por vendedor, cliente, produto, medida e data prevista de entrega.</div>{% if default_period_label %}<div class="mt-2"><span class="chip"><i class="bi bi-calendar3 me-1"></i>Visão padrão: {{ default_period_label }}</span></div>{% endif %}</div><div class="d-flex gap-2"><button class="btn btn-primary" type="button" data-bs-toggle="collapse" data-bs-target="#afFilters"><i class="bi bi-funnel me-1"></i>Filtros</button>{% if entrega_ok %}<a class="btn btn-success" href="{{ export_url }}"><i class="bi bi-file-earmark-excel me-1"></i>Excel</a>{% else %}<a class="btn btn-warning" href="{{ url_for('importar_csv') }}"><i class="bi bi-arrow-repeat me-1"></i>Atualizar Sisplan</a>{% endif %}</div></div></div>
        {% if not entrega_ok %}<div class="alert alert-warning border mb-4"><b>Datas de entrega ainda não estão no CRM.</b> Use <b>Atualizar Sisplan</b> para reler o FDB; o campo vem de <code>PEDIDO_001.ENTREGA</code>.</div>{% endif %}
        <div class="collapse {% if vendedor or cliente or produto or medida or status or entrega_ini or entrega_fim or scope!='TOTAL' %}show{% endif %}" id="afFilters"><div class="cardx p-4 mb-4"><form method="get" class="row g-3"><div class="col-12 col-md-3"><label class="form-label fw-bold">Time</label><select class="form-select" name="escopo"><option value="TOTAL">Todos</option>{% for t in ['INTERNO','EXTERNO','SITE'] %}<option value="{{t}}" {% if scope==t %}selected{% endif %}>{{t.title()}}</option>{% endfor %}</select></div><div class="col-12 col-md-3"><label class="form-label fw-bold">Vendedor</label><select class="form-select" name="vendedor"><option value="">Todos</option>{% for x in vendors %}<option value="{{x}}" {% if vendedor==x %}selected{% endif %}>{{x}}</option>{% endfor %}</select></div><div class="col-12 col-md-3"><label class="form-label fw-bold">Cliente</label><input class="form-control" name="cliente" value="{{cliente}}" placeholder="Todos"></div><div class="col-12 col-md-3"><label class="form-label fw-bold">Produto / grupo / papel</label><input class="form-control" name="produto" value="{{produto}}" placeholder="Todos"></div><div class="col-12 col-md-3"><label class="form-label fw-bold">Medida</label><input class="form-control" name="medida" value="{{medida}}" list="afMeasures" placeholder="Todas"><datalist id="afMeasures">{% for x in measures %}<option value="{{x}}">{% endfor %}</datalist></div><div class="col-12 col-md-3"><label class="form-label fw-bold">Situação</label><select class="form-select" name="status"><option value="">Todas pendências</option>{% for st in ['NÃO FATURADO','PARCIAL','ATRASADO','A VENCER','SEM PREVISÃO'] %}<option value="{{st}}" {% if status==st %}selected{% endif %}>{{st}}</option>{% endfor %}</select></div><div class="col-6 col-md-3"><label class="form-label fw-bold">Entrega de</label><input type="date" class="form-control" name="entrega_ini" value="{{entrega_ini}}"></div><div class="col-6 col-md-3"><label class="form-label fw-bold">Entrega até</label><input type="date" class="form-control" name="entrega_fim" value="{{entrega_fim}}"></div><div class="col-12 d-flex gap-2 justify-content-end"><a class="btn btn-light border" href="{{ url_for('a_faturar') }}">Limpar</a><button class="btn btn-primary"><i class="bi bi-funnel me-1"></i>Aplicar</button></div></form></div></div>
        <div class="row g-3 mb-4"><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Falta faturar</div><div class="num">{{format_brl(d.total_falta)}}</div><div class="desc">{{d.total_pedidos}} pedidos em aberto</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Faturado nesses pedidos</div><div class="num">{{format_brl(d.total_faturado)}}</div><div class="desc">já conciliado por nº do pedido</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Entregas atrasadas</div><div class="num">{{d.atrasados}}</div><div class="desc">pendentes com data vencida</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Faturamento parcial</div><div class="num">{{d.parciais}}</div><div class="desc">pedidos faturados em parte</div></div></div></div>
        {% if d.delivery_coverage < 95 %}<div class="alert alert-warning border"><b>Previsão de entrega parcial:</b> {{'%.1f'|format(d.delivery_coverage)}}% dos pedidos desta visão têm data/condição suficiente. A data exata vem de <code>PEDIDO_001.ENTREGA</code> após <b>Atualizar Sisplan</b>.</div>{% endif %}
        <div class="cardx p-4 mb-4"><div class="section-title mb-1">Por vendedor</div><div class="section-sub mb-3">Saldo a faturar consolidado dos pedidos selecionados.</div><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Vendedor</th><th>Time</th><th class="text-end">Pedidos</th><th class="text-end">Já faturado</th><th class="text-end">Falta faturar</th><th class="text-end">Liquidado</th></tr></thead><tbody>{% for r in d.vendors %}<tr><td class="fw-bold">{{r.vendedor}}</td><td><span class="chip">{{r.time}}</span></td><td class="text-end">{{r.pedidos}}</td><td class="text-end">{{format_brl(r.faturado)}}</td><td class="text-end fw-bold text-primary">{{format_brl(r.falta)}}</td><td class="text-end">{{format_brl(r.liquidado)}}</td></tr>{% else %}<tr><td colspan="6" class="text-center text-muted py-4">Sem pendências.</td></tr>{% endfor %}</tbody></table></div></div>
        <div class="cardx p-4"><div class="d-flex justify-content-between align-items-start gap-3 mb-3"><div><div class="section-title">Pedidos pendentes de faturamento</div><div class="section-sub">Clique em Itens para ver produto, medida, papel, quantidade e valor de cada linha do pedido.</div></div><span class="chip">Conciliação por nº do pedido</span></div><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Entrega</th><th>Pedido</th><th>Cliente</th><th>Vendedor</th><th>Produto</th><th>Medida</th><th class="text-end">Pedido</th><th class="text-end">Faturado</th><th class="text-end">Falta</th><th>Situação</th><th>Status</th><th></th></tr></thead><tbody>{% for r in d.rows %}<tr><td><div class="fw-bold {% if r.entrega_status.startswith('ATRASADO') %}text-danger{% endif %}">{{r.entrega or '—'}}</div><div class="small text-muted">{{r.entrega_status}}</div></td><td class="fw-bold text-primary">{{r.pedido}}</td><td>{{r.cliente}}</td><td><div>{{r.vendedor}}</div><span class="chip">{{r.time}}</span></td><td class="small" style="min-width:210px;max-width:310px">{{r.produtos}}</td><td class="small">{{r.medidas}}</td><td class="text-end">{{format_brl(r.valor_pedido)}}</td><td class="text-end"><div>{{format_brl(r.faturado)}}</div><div class="small text-muted">{{'%.1f'|format(r.pct_faturado)}}%</div></td><td class="text-end fw-bold text-primary">{{format_brl(r.falta_faturar)}}</td><td><span class="badge text-bg-secondary">{{r.situacao_721}}</span></td><td><span class="badge {% if r.status=='PARCIAL' %}text-bg-warning{% else %}text-bg-danger{% endif %}">{{r.status}}</span><div class="small text-muted mt-1">Liq. {{format_brl(r.liquidado)}}</div></td><td><button class="btn btn-sm btn-light border" data-bs-toggle="collapse" data-bs-target="#af{{loop.index}}">Itens</button></td></tr><tr class="collapse" id="af{{loop.index}}"><td colspan="12" class="p-0"><div class="p-3 bg-light"><div class="small text-muted mb-2">NFs: {{r.nfs or '—'}} · Última NF: {{r.ultima_nf or '—'}} · Último pagamento: {{r.ultimo_pagamento or '—'}} · Condição: {{r.condicao_entrega or '—'}} · {{r.entrega_origem or ''}}{% if r.observacao %} · Obs.: {{r.observacao}}{% endif %}</div><div class="table-responsive"><table class="table table-sm mb-0"><thead><tr><th>Produto</th><th>Código</th><th>Descrição 721</th><th>Medida</th><th>Papel</th><th class="text-end">Qtd.</th><th class="text-end">Preço un.</th><th class="text-end">Valor linha</th></tr></thead><tbody>{% for it in r['items'] %}<tr><td>{{it.produto}}</td><td>{{it.codigo or '—'}}</td><td class="small">{{it.descricao or '—'}}</td><td>{{it.medida or '—'}}</td><td>{{it.papel or '—'}}</td><td class="text-end">{{format_num_br(it.quantidade,0)}}</td><td class="text-end">{{format_brl(it.preco_unitario)}}</td><td class="text-end">{{format_brl(it.valor_item)}}</td></tr>{% endfor %}</tbody></table></div></div></td></tr>{% else %}<tr><td colspan="12" class="text-center text-muted py-5">Nenhum pedido pendente com os filtros selecionados.</td></tr>{% endfor %}</tbody></table></div></div>
        ''',d=data,scope=scope,vendedor=vendedor,cliente=cliente,produto=produto,medida=medida,status=status,entrega_ini=entrega_ini,entrega_fim=entrega_fim,vendors=vendors,measures=measures,export_url=export_url,default_period_label=default_period_label,entrega_ok=entrega_ok,format_brl=format_brl,format_num_br=format_num_br)
        return render_page('A Faturar',body,'a_faturar')


    @app.route("/relatorios")
    @login_required
    def relatorios():
        user = get_current_user()
        if not user or user["role"] not in ["admin", "gestor"]:
            flash("Acesso restrito à gestão.", "warning")
            return redirect(url_for("home"))

        periodo = (request.args.get("periodo") or "mes").strip().lower()
        if periodo not in {"dia", "semana", "mes", "trimestre", "semestre", "ano", "meses"}:
            periodo = "mes"
        ref = _parse_report_ref(request.args.get("data_ref"), date.today())
        selected_months = _report_selected_months(request.args)
        try:
            filter_year = int(request.args.get("ano_filtro") or ref.year)
        except Exception:
            filter_year = ref.year
        scope = (request.args.get("escopo") or "TOTAL").strip().upper()
        if scope not in {"TOTAL", "INTERNO", "EXTERNO", "SITE"}: scope = "TOTAL"
        conteudo = (request.args.get("conteudo") or "AMBOS").strip().upper()
        if conteudo not in {"AMBOS", "PEDIDOS", "FATURAMENTO"}: conteudo = "AMBOS"
        vendedor = (request.args.get("vendedor") or "").strip()
        cliente = (request.args.get("cliente") or "").strip()
        grupo = (request.args.get("grupo") or "").strip()
        papel = (request.args.get("papel") or "").strip()
        medida = (request.args.get("medida") or "").strip()
        status = (request.args.get("status") or "").strip().upper()

        if selected_months:
            d0, d1 = date(filter_year, 1, 1), date(filter_year, 12, 31)
            period_label = _report_months_label(selected_months, filter_year)
            periodo = "meses"
        else:
            d0, d1, period_label = _resolve_report_period(periodo if periodo != "meses" else "mes", ref)

        with conectar() as conn:
            ds = _report_datasets(conn, d0, d1, scope)
            ds = _report_apply_dataset_filters(ds, year=filter_year if selected_months else None, months=selected_months,
                                               vendedor=vendedor, cliente=cliente, grupo=grupo, papel=papel, medida=medida)
            vendor_options = [dict(r) for r in conn.execute("""
                SELECT id,nome,tipo,codigo,COALESCE(performance_ativo,1) performance_ativo,COALESCE(ativo,1) ativo
                FROM vendors WHERE COALESCE(nome,'')<>'' ORDER BY nome
            """).fetchall()]
            client_options = [r[0] for r in conn.execute("""
                SELECT DISTINCT COALESCE(NULLIF(TRIM(fantasia),''),NULLIF(TRIM(nome),'')) x
                FROM customers WHERE COALESCE(NULLIF(TRIM(fantasia),''),NULLIF(TRIM(nome),'') ) IS NOT NULL
                ORDER BY x LIMIT 1500
            """).fetchall() if r[0]]
            group_options = sorted({str(r[0]).strip() for r in conn.execute("SELECT DISTINCT grupo_ref FROM orders WHERE TRIM(COALESCE(grupo_ref,''))<>''").fetchall() if r[0]})
            paper_options = sorted({str(r[0]).strip() for r in conn.execute("SELECT DISTINCT papel_ref FROM orders WHERE TRIM(COALESCE(papel_ref,''))<>''").fetchall() if r[0]})
            measure_options = sorted({str(r[0]).strip() for r in conn.execute("SELECT DISTINCT medida_ref FROM orders WHERE TRIM(COALESCE(medida_ref,''))<>''").fetchall() if r[0]})
            db_years = [int(r[0]) for r in conn.execute("SELECT DISTINCT CAST(strftime('%Y',data_emissao) AS INTEGER) y FROM orders WHERE data_emissao IS NOT NULL ORDER BY y DESC").fetchall() if r[0]]
            fav_key = f"report_favorites_{int(user['id'])}"
            fav_row = conn.execute("SELECT value FROM settings WHERE key=?", (fav_key,)).fetchone()
            try:
                favorites = json.loads(fav_row[0]) if fav_row and fav_row[0] else []
                if not isinstance(favorites, list): favorites = []
            except Exception:
                favorites = []

        by_team = {}
        for kind, rows in (("Pedidos", ds["orders"]), ("Faturamento", ds["invoices"])):
            for r in rows:
                team = r.get("time") or "EXTERNO"
                key = (team, kind)
                acc = by_team.setdefault(key, {"time": team, "tipo": kind, "valor": 0.0, "quantidade": 0.0, "docs": set()})
                acc["valor"] += float(r.get("valor") or 0); acc["quantidade"] += float(r.get("quantidade") or 0)
                doc = r.get("pedido") if kind == "Pedidos" else r.get("nf")
                if str(doc or "").strip(): acc["docs"].add(str(doc).strip())
        team_rows = sorted([{**v, "documentos": len(v["docs"])} for v in by_team.values()], key=lambda x:(x["tipo"], x["time"]))

        report_cards = [
            {"id":"operacional","title":"Resumo comercial","icon":"bi-speedometer2","desc":"Pedidos + faturamento, ticket, quantidade e base detalhada.","screen":"dashboard"},
            {"id":"pedidos","title":"Pedidos realizados","icon":"bi-bag-check","desc":"Pedidos válidos com cliente, vendedor, medida, papel, quantidade e valor.","screen":"dashboard"},
            {"id":"faturamento","title":"Faturamento","icon":"bi-receipt","desc":"Notas fiscais por período, cliente, vendedor, produto, papel e quantidade.","screen":"dashboard"},
            {"id":"performance","title":"Performance do time","icon":"bi-bar-chart-line","desc":"Orçamentos, conversão, pedidos, faturamento, meta, clientes e score.","screen":"performance_time"},
            {"id":"orcamentos","title":"Orçamentos","icon":"bi-file-earmark-text","desc":"Pipeline de orçamentos, etapa, status, cidade, medida, quantidade e valor.","screen":"orcamentos_detalhe"},
            {"id":"clientes","title":"Clientes","icon":"bi-buildings","desc":"Carteira, compras, valor, frequência, vendedor e situação comercial.","screen":"clientes"},
            {"id":"vendedores","title":"Vendedores","icon":"bi-people","desc":"Time atual, classificação Interno/Externo/Site, atividade e situação.","screen":"vendedores"},
            {"id":"inativos","title":"Vendedores inativos","icon":"bi-person-x","desc":"Inativados, motivo, última venda e possibilidade de recuperação.","screen":"vendedores_inativos"},
            {"id":"tarefas","title":"Tarefas comerciais","icon":"bi-check2-square","desc":"Status, cliente, vendedor, datas e acompanhamento do Kanban comercial.","screen":"tarefas"},
            {"id":"oportunidades","title":"Oportunidades","icon":"bi-lightning-charge","desc":"Leads, etapa, temperatura, valor estimado, probabilidade e próxima ação.","screen":"leads"},
            {"id":"funil","title":"Funil comercial","icon":"bi-kanban","desc":"Oportunidades agrupadas por etapa e vendedor, com valores estimados.","screen":"funil"},
            {"id":"medidas","title":"Medidas / formatos","icon":"bi-rulers","desc":"Mix de medidas, clientes, quantidade e participação no período.","screen":"medidas"},
            {"id":"a_faturar","title":"A faturar","icon":"bi-receipt-cutoff","desc":"Pedidos ainda não faturados ou faturados parcialmente, com entrega, produto, medida, faturamento e liquidez.","screen":"a_faturar"},
            {"id":"historico","title":"Comparativo histórico","icon":"bi-clock-history","desc":"Comparação sazonal ano a ano usando o mesmo conjunto de meses selecionado.","screen":"analise_historica"},
            {"id":"planejamento","title":"Planejamento & Forecast","icon":"bi-calendar3-range","desc":"Sazonalidade, três cenários automáticos, clientes esperados e risco de receita.","screen":"analise_historica"},
            {"id":"consumo","title":"Consumo previsto de papel","icon":"bi-box-seam","desc":"Kg e toneladas por gramatura, mês e Top 5 medidas para Financeiro/Compras.","screen":"analise_historica"},
            {"id":"site","title":"Canal Site","icon":"bi-globe2","desc":"Pedidos/faturamento do canal Site/Marketplace no mesmo recorte.","screen":"site_analytics"},
        ]

        if not db_years:
            db_years = list(range(date.today().year, date.today().year-6, -1))
        elif date.today().year not in db_years:
            db_years.insert(0, date.today().year)

        body = render_template_string(r"""
        <div class="page-hero mb-4"><div class="d-flex justify-content-between align-items-start flex-wrap gap-3"><div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Central gerencial</div><div class="hero-title">Relatórios</div><div class="hero-sub mt-1">Escolha o relatório e abra o filtro somente quando precisar. Meses podem ser combinados livremente, igual no Excel.</div></div><span class="chip"><i class="bi bi-file-earmark-spreadsheet"></i> {{ report_cards|length }} relatórios</span></div></div>

        <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-4">
          <div class="small text-muted"><i class="bi bi-info-circle me-1"></i>Trimestre e semestre são atalhos: eles apenas marcam os meses correspondentes. Você pode depois incluir ou remover qualquer mês.</div>
          <button class="btn btn-outline-primary" type="button" onclick="openReportFilter('operacional','Resumo comercial')"><i class="bi bi-funnel me-1"></i>Filtro geral / Excel</button>
        </div>

        <div class="row g-3 mb-4"><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Pedidos</div><div class="num">{{ ds.p_orders }}</div><div class="desc">{{ format_brl(ds.p_value) }} · {{ format_num_br(ds.p_qty,0) }} un.</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Faturamento</div><div class="num">{{ ds.f_nfs }} NFs</div><div class="desc">{{ format_brl(ds.f_value) }} · {{ format_num_br(ds.f_qty,0) }} un.</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Recorte atual</div><div class="num" style="font-size:1.15rem">{{ period_label }}</div><div class="desc">prévia da Central</div></div></div><div class="col-6 col-xl-3"><div class="metric-pill h-100"><div class="metric-label">Diferença</div><div class="num">{{ format_brl(ds.p_value-ds.f_value) }}</div><div class="desc">pedidos − faturamento</div></div></div></div>

        <div class="section-heading"><div><h2>Biblioteca de relatórios</h2><p>Cada card tem seu próprio filtro. Abra, marque os meses e demais campos e exporte o recorte exato.</p></div></div>
        <div class="row g-3">
          {% for r in report_cards %}
          <div class="col-12 col-md-6 col-xxl-4"><div class="cardx p-4 h-100 d-flex flex-column"><div class="d-flex gap-3"><div class="rounded-3 d-flex align-items-center justify-content-center" style="width:44px;height:44px;background:#eef6ff"><i class="bi {{ r.icon }} fs-5 text-primary"></i></div><div><div class="section-title">{{ r.title }}</div><div class="section-sub">{{ r.desc }}</div></div></div><div class="mt-auto pt-3 d-flex gap-2 flex-wrap"><a class="btn btn-sm btn-light border" href="{{ url_for(r.screen, data_ref=ref.strftime('%Y-%m')) if r.screen in ['performance_time','orcamentos_detalhe'] else url_for(r.screen) }}"><i class="bi bi-box-arrow-up-right me-1"></i>Abrir</a><button type="button" class="btn btn-sm btn-primary" onclick='openReportFilter({{ r.id|tojson }},{{ r.title|tojson }})'><i class="bi bi-funnel me-1"></i>Filtro</button></div></div></div>
          {% endfor %}
        </div>

        <div class="cardx p-4 mt-4"><div class="section-title mb-1">Resumo por time</div><div class="section-sub mb-3">Conferência rápida do recorte atual da Central.</div><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Time</th><th>Tipo</th><th class="text-end">Documentos</th><th class="text-end">Quantidade</th><th class="text-end">Valor</th></tr></thead><tbody>{% for r in team_rows %}<tr><td><span class="chip">{{ r.time }}</span></td><td>{{ r.tipo }}</td><td class="text-end">{{ r.documentos }}</td><td class="text-end">{{ format_num_br(r.quantidade,0) }}</td><td class="text-end fw-bold">{{ format_brl(r.valor) }}</td></tr>{% endfor %}</tbody></table></div></div>

        <div class="modal fade" id="reportFilterModal" tabindex="-1" aria-hidden="true"><div class="modal-dialog modal-xl modal-dialog-scrollable"><div class="modal-content">
          <form method="get" action="{{ url_for('relatorios_exportar') }}" id="reportFilterForm">
            <div class="modal-header"><div><h5 class="modal-title"><i class="bi bi-funnel me-2"></i>Filtrar <span id="reportFilterTitle"></span></h5><div class="small text-muted">Marque qualquer combinação de meses. Os atalhos de trimestre/semestre só preenchem as caixas para você.</div></div><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
            <div class="modal-body">
              <input type="hidden" name="relatorio" id="reportFilterId" value="operacional">
              <input type="hidden" name="periodo" value="meses">
              <div class="row g-3">
                <div class="col-12 col-lg-3"><label class="form-label fw-bold">Ano</label><select class="form-select" name="ano_filtro" id="filterYear">{% for y in db_years %}<option value="{{ y }}" {% if y==filter_year %}selected{% endif %}>{{ y }}</option>{% endfor %}</select></div>
                <div class="col-12 col-lg-9"><label class="form-label fw-bold">Atalhos</label><div class="d-flex flex-wrap gap-2"><button type="button" class="btn btn-sm btn-outline-primary" onclick="selectMonths([1,2,3])">1º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="selectMonths([4,5,6])">2º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="selectMonths([7,8,9])">3º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="selectMonths([10,11,12])">4º tri</button><button type="button" class="btn btn-sm btn-outline-info" onclick="selectMonths([1,2,3,4,5,6])">1º semestre</button><button type="button" class="btn btn-sm btn-outline-info" onclick="selectMonths([7,8,9,10,11,12])">2º semestre</button><button type="button" class="btn btn-sm btn-light border" onclick="selectMonths([1,2,3,4,5,6,7,8,9,10,11,12])">Ano todo</button><button type="button" class="btn btn-sm btn-light border" onclick="selectMonths([])">Limpar</button></div></div>
                <div class="col-12"><div class="border rounded-3 p-3 bg-light"><div class="row g-2">{% for m,n in months_pt %}<div class="col-4 col-md-2"><label class="form-check d-flex align-items-center gap-2 mb-0"><input class="form-check-input report-month" type="checkbox" name="mes" value="{{ m }}" {% if m in selected_months %}checked{% elif not selected_months and m==ref.month %}checked{% endif %}><span>{{ n[:3] }}</span></label></div>{% endfor %}</div></div></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Time</label><select class="form-select" name="escopo"><option value="TOTAL">Todos</option><option value="INTERNO">Interno</option><option value="EXTERNO">Externo</option><option value="SITE">Site</option></select></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Vendedor</label><select class="form-select" name="vendedor"><option value="">Todos</option>{% for v in vendor_options %}<option value="{{ v.nome }}">{{ v.nome }}</option>{% endfor %}</select></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Conteúdo</label><select class="form-select" name="conteudo"><option value="AMBOS">Pedidos + Faturamento</option><option value="PEDIDOS">Pedidos</option><option value="FATURAMENTO">Faturamento</option></select></div>
                <div class="col-12 col-md-6"><label class="form-label fw-bold">Cliente</label><input class="form-control" name="cliente" list="reportClients" placeholder="Todos"><datalist id="reportClients">{% for x in client_options %}<option value="{{ x }}">{% endfor %}</datalist></div>
                <div class="col-12 col-md-3"><label class="form-label fw-bold">Grupo / produto</label><input class="form-control" name="grupo" list="reportGroups" placeholder="Todos"><datalist id="reportGroups">{% for x in group_options %}<option value="{{ x }}">{% endfor %}</datalist></div>
                <div class="col-12 col-md-3"><label class="form-label fw-bold">Papel / gramatura</label><input class="form-control" name="papel" list="reportPapers" placeholder="Todos"><datalist id="reportPapers">{% for x in paper_options %}<option value="{{ x }}">{% endfor %}</datalist></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Medida / formato</label><input class="form-control" name="medida" list="reportMeasures" placeholder="Todos"><datalist id="reportMeasures">{% for x in measure_options %}<option value="{{ x }}">{% endfor %}</datalist></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Status / etapa</label><input class="form-control" name="status" placeholder="Todos"></div>
                <div class="col-12 col-md-4"><label class="form-label fw-bold">Data específica (opcional)</label><input class="form-control" type="date" name="data_ref" value="{{ ref.isoformat() }}"><div class="form-text">Usada apenas em relatórios que trabalham por dia/semana.</div></div>
              </div>
            </div>
            <div class="modal-footer"><button type="button" class="btn btn-light border" data-bs-dismiss="modal">Cancelar</button><button type="submit" class="btn btn-success"><i class="bi bi-file-earmark-excel me-1"></i>Filtrar e exportar Excel</button></div>
          </form>
        </div></div></div>

        <script>
        function selectMonths(list){
          const wanted=new Set(list.map(Number));
          document.querySelectorAll('.report-month').forEach(cb=>cb.checked=wanted.has(Number(cb.value)));
        }
        function openReportFilter(id,title){
          document.getElementById('reportFilterId').value=id;
          document.getElementById('reportFilterTitle').textContent=title || '';
          const el=document.getElementById('reportFilterModal');
          bootstrap.Modal.getOrCreateInstance(el).show();
        }
        </script>
        """, periodo=periodo, ref=ref, filter_year=filter_year, selected_months=selected_months, scope=scope, conteudo=conteudo,
             vendedor=vendedor, cliente=cliente, grupo=grupo, papel=papel, medida=medida, status=status,
             period_label=period_label, ds=ds, team_rows=team_rows, favorites=favorites,
             report_cards=report_cards, vendor_options=vendor_options, client_options=client_options,
             group_options=group_options, paper_options=paper_options, measure_options=measure_options,
             db_years=db_years, months_pt=_MONTHS_PT, format_brl=format_brl, format_num_br=format_num_br)
        return render_page("Relatórios", body, "relatorios")


    @app.route("/relatorios/favoritos", methods=["POST"])
    @login_required
    def relatorios_favoritos():
        user=get_current_user()
        if not user or user["role"] not in ["admin","gestor"]: abort(403)
        key=f"report_favorites_{int(user['id'])}"
        with conectar() as conn:
            row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
            try:
                favs=json.loads(row[0]) if row and row[0] else []
                if not isinstance(favs,list): favs=[]
            except Exception: favs=[]
            action=(request.form.get('action') or 'save').strip().lower()
            if action=='delete':
                fid=(request.form.get('fav_id') or '').strip(); favs=[f for f in favs if str(f.get('id'))!=fid]
                flash('Favorito removido.','success')
            else:
                nome=(request.form.get('nome') or 'Relatório favorito').strip()[:60]
                fav={
                    'id':hashlib.md5(f"{now_iso()}|{nome}".encode()).hexdigest()[:10],
                    'nome':nome,
                    'periodo':(request.form.get('periodo') or 'mes').strip().lower(),
                    'data_ref':(request.form.get('data_ref') or date.today().isoformat()).strip(),
                    'escopo':(request.form.get('escopo') or 'TOTAL').strip().upper(),
                    'conteudo':(request.form.get('conteudo') or 'AMBOS').strip().upper(),
                    'vendedor':(request.form.get('vendedor') or '').strip(),
                    'status':(request.form.get('status') or '').strip().upper(),
                }
                favs=[fav]+favs[:11]; flash('Visualização salva nos favoritos.','success')
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(favs,ensure_ascii=False)))
            conn.commit()
        return redirect(request.referrer or url_for('relatorios'))


    @app.route("/relatorios/exportar")
    @login_required
    def relatorios_exportar():
        user = get_current_user()
        if not user or user["role"] not in ["admin", "gestor"]:
            abort(403)
        relatorio = (request.args.get("relatorio") or "operacional").strip().lower()
        periodo = (request.args.get("periodo") or "mes").strip().lower()
        if periodo not in {"dia", "semana", "mes", "trimestre", "semestre", "ano", "meses"}: periodo = "mes"
        ref = _parse_report_ref(request.args.get("data_ref") or request.args.get("data_base"), date.today())
        scope = (request.args.get("escopo") or "TOTAL").strip().upper()
        if scope not in {"TOTAL", "INTERNO", "EXTERNO", "SITE"}: scope = "TOTAL"
        conteudo = (request.args.get("conteudo") or "AMBOS").strip().upper()
        if conteudo not in {"AMBOS", "PEDIDOS", "FATURAMENTO"}: conteudo = "AMBOS"
        vendedor_filtro = (request.args.get("vendedor") or "").strip()
        status_filtro_global = (request.args.get("status") or "").strip().upper()
        cliente_filtro = (request.args.get("cliente") or "").strip()
        grupo_filtro = (request.args.get("grupo") or "").strip()
        papel_filtro = (request.args.get("papel") or "").strip()
        medida_filtro = (request.args.get("medida") or "").strip()
        selected_months = _report_selected_months(request.args)
        try:
            filter_year = int(request.args.get("ano_filtro") or request.args.get("ano") or ref.year)
        except Exception:
            filter_year = ref.year

        def _export_bounds():
            if selected_months:
                return date(filter_year,1,1), date(filter_year,12,31), _report_months_label(selected_months, filter_year)
            return _resolve_report_period(periodo if periodo != "meses" else "mes", ref)

        if relatorio == "a_faturar":
            entrega_ini=(request.args.get("entrega_ini") or "").strip()
            entrega_fim=(request.args.get("entrega_fim") or "").strip()
            with conectar() as conn:
                data=_a_faturar_data(conn,scope=scope,vendedor=vendedor_filtro,cliente=cliente_filtro,produto=grupo_filtro,medida=medida_filtro,status=status_filtro_global,entrega_ini=entrega_ini,entrega_fim=entrega_fim,year=(filter_year if selected_months else None),months=selected_months)
            rows=[[r.get("entrega"),r.get("entrega_status"),r.get("entrega_origem"),r.get("data_pedido"),r.get("pedido"),r.get("cliente"),r.get("vendedor"),r.get("time"),r.get("produtos"),r.get("codigos_produto"),r.get("descricoes_produto"),r.get("medidas"),r.get("papeis"),r.get("quantidade"),r.get("valor_pedido"),r.get("faturado"),r.get("falta_faturar"),r.get("pct_faturado")/100.0,r.get("liquidado"),r.get("a_receber"),r.get("situacao_721"),r.get("status"),r.get("nfs"),r.get("ultima_nf"),r.get("ultimo_pagamento"),r.get("condicao_entrega"),r.get("observacao")] for r in data["rows"]]
            sheets=[{"name":"A Faturar","title":"Pedidos pendentes de faturamento","headers":["Entrega prevista","Situação entrega","Origem previsão","Data pedido","Pedido","Cliente","Vendedor","Time","Produto","Código produto","Descrição","Medida","Papel","Quantidade","Valor pedido","Já faturado","Falta faturar","% faturado","Liquidado","A receber","Situação","Status faturamento","NFs","Última NF","Último pagamento","Condição entrega","Observação"],"rows":rows}]
            metadata=[("Relatório","A Faturar"),("Time",_analysis_scope_label(scope)),("Vendedor",vendedor_filtro or "Todos"),("Cliente",cliente_filtro or "Todos"),("Produto/grupo",grupo_filtro or "Todos"),("Medida",medida_filtro or "Todas"),("Pedidos pendentes",data["total_pedidos"]),("Falta faturar",data["total_falta"]),("Já faturado",data["total_faturado"]),("Liquidado",data["total_liquidado"]),("Entregas atrasadas",data["atrasados"]),("Gerado em",datetime.now().strftime("%d/%m/%Y %H:%M"))]
            return _xlsx_response("a_faturar", "CRM NPD · A Faturar", metadata, sheets)

        if relatorio == "performance":
            perf_months = selected_months or [ref.month]
            perf_year = filter_year if selected_months else ref.year
            monthly_rows=[]
            with conectar() as conn:
                for mm in perf_months:
                    ctx = _performance_build_context(conn, f"{perf_year:04d}-{mm:02d}", include_customer_mix=True)
                    monthly_rows.extend(ctx.get("rows", []))
            if len(perf_months) <= 1:
                rows = monthly_rows
            else:
                agg={}
                for r in monthly_rows:
                    key=(str(r.get('nome') or ''),str(r.get('tipo') or ''),str(r.get('codigo') or ''))
                    if key not in agg:
                        agg[key]=dict(r)
                        continue
                    a=agg[key]
                    for fld in ['orc_qtd','orc_valor','conv_qtd','ped_qtd','ped_valor','fat_qtd','fat_valor','fat_ciclo','clientes_novos','clientes_repeticao','clientes_recuperados','meta_mensal']:
                        a[fld]=float(a.get(fld) or 0)+float(r.get(fld) or 0)
                rows=list(agg.values())
                for r in rows:
                    r['conv_pct']=(float(r.get('conv_qtd') or 0)/float(r.get('orc_qtd') or 0)*100.0) if float(r.get('orc_qtd') or 0)>0 else 0.0
                    r['ticket_fat']=(float(r.get('fat_valor') or 0)/float(r.get('fat_qtd') or 0)) if float(r.get('fat_qtd') or 0)>0 else 0.0
                    fat_meta=float(r.get('fat_ciclo') or r.get('fat_valor') or 0)
                    r['meta_pct']=(fat_meta/float(r.get('meta_mensal') or 0)*100.0) if float(r.get('meta_mensal') or 0)>0 else 0.0
            perf_ref = _report_months_label(perf_months, perf_year)
            if scope in {"INTERNO", "EXTERNO"}:
                rows = [r for r in rows if str(r.get("tipo") or "").upper() == scope]
            if vendedor_filtro:
                rows = [r for r in rows if norm(vendedor_filtro) in norm(r.get("nome") or "")]
            rows = sorted(rows, key=lambda r: (str(r.get("tipo") or ""), -float(r.get("score") or 0), str(r.get("nome") or "")))
            data_rows = [[
                r.get("nome"), r.get("tipo"), r.get("codigo"), int(r.get("orc_qtd") or 0), float(r.get("orc_valor") or 0),
                int(r.get("conv_qtd") or 0), float(r.get("conv_pct") or 0)/100.0, int(r.get("ped_qtd") or 0), float(r.get("ped_valor") or 0),
                int(r.get("fat_qtd") or 0), float(r.get("fat_valor") or 0), float(r.get("ticket_fat") or 0),
                int(r.get("clientes_novos") or 0), int(r.get("clientes_repeticao") or 0), int(r.get("clientes_recuperados") or 0),
                float(r.get("meta_mensal") or 0), float(r.get("meta_pct") or 0)/100.0, float(r.get("score") or 0)
            ] for r in rows]
            sheets=[{"name":"Performance","title":f"Performance do time · {perf_ref}","headers":["Vendedor","Time","Código","Orçamentos","Valor orçado","Convertidos","Conversão %","Pedidos","Valor pedidos","NFs","Valor faturado","Ticket faturado","Clientes novos","Recorrentes","Recuperados","Meta mensal","Meta %","Score"],"rows":data_rows}]
            metadata=[("Relatório","Performance"),("Mês",perf_ref),("Time",_analysis_scope_label(scope)),("Vendedores",len(rows)),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))]
            return _xlsx_response(f"performance_{perf_ref}_{scope.lower()}", "CRM NPD · Performance", metadata, sheets)

        if relatorio == "orcamentos":
            d0,d1,period_label=_export_bounds()
            status_filtro=(request.args.get("status_filtro") or status_filtro_global or "").strip().upper()
            vendedores_filtro=[x.strip() for x in request.args.getlist("vendedor_nome") if x and x.strip()]
            if vendedor_filtro and not vendedores_filtro:
                vendedores_filtro=[vendedor_filtro]
            with conectar() as conn:
                catalog=_performance_vendor_catalog(conn)
                vendors, _byid, bycode = catalog
                orcs=[dict(r) for r in conn.execute("""
                    SELECT numero,cliente_nome_ref,cliente_nome,vendedor_id,vendedor_nome,data_inicio,status,desc_status,pedido_id,valor_total,uf,cidade,quantidade,medida_ref
                    FROM orcamentos WHERE date(data_inicio) BETWEEN date(?) AND date(?) ORDER BY date(data_inicio), id
                """,(d0.isoformat(),d1.isoformat())).fetchall()]
            out=[]
            for r in orcs:
                if selected_months and not _report_date_in_months(r.get('data_inicio'), filter_year, selected_months): continue
                if cliente_filtro and norm(cliente_filtro) not in norm(r.get('cliente_nome_ref') or r.get('cliente_nome') or ''): continue
                if medida_filtro and norm(medida_filtro) not in norm(r.get('medida_ref') or ''): continue
                _key, meta = _performance_resolve_orc_vendor(vendors, bycode, r.get('vendedor_id'), r.get('vendedor_nome'))
                time = str(meta.get('tipo') or 'NÃO CLASSIFICADO').upper()
                if scope in {"INTERNO","EXTERNO"} and time != scope: continue
                if scope == "SITE" and time != "SITE": continue
                if vendedores_filtro and str(r.get('vendedor_nome') or '').strip() not in vendedores_filtro: continue
                etapa=_sisplan_stage(r.get('desc_status'),r.get('pedido_id'),r.get('status'))
                status_nome = 'PEDIDO APROVADO' if str(r.get('pedido_id') or '').strip() not in {'','0'} else str(r.get('desc_status') or 'SEM STATUS').strip().upper()
                if status_filtro and status_nome != status_filtro: continue
                out.append([r.get('data_inicio'),r.get('numero'),r.get('cliente_nome_ref') or r.get('cliente_nome'),r.get('vendedor_nome'),time,etapa,r.get('desc_status'),r.get('pedido_id'),r.get('uf'),r.get('cidade'),r.get('medida_ref'),float(r.get('quantidade') or 0),float(r.get('valor_total') or 0)])
            sheets=[{"name":"Orçamentos","title":f"Orçamentos · {period_label}","headers":["Data","Orçamento","Cliente","Vendedor","Time","Etapa","Status Sisplan","Pedido","UF","Cidade","Medida","Quantidade","Valor"],"rows":out}]
            metadata=[("Relatório","Orçamentos"),("Período",period_label),("Time",_analysis_scope_label(scope)),("Status",status_filtro or "Todos"),("Vendedores",", ".join(vendedores_filtro) if vendedores_filtro else "Todos"),("Registros",len(out)),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))]
            return _xlsx_response(f"orcamentos_{periodo}_{d0.isoformat()}_{d1.isoformat()}", "CRM NPD · Orçamentos", metadata, sheets)

        if relatorio in {"pedidos", "faturamento", "clientes", "medidas", "site"}:
            d0,d1,period_label=_export_bounds()
            effective_scope = "SITE" if relatorio=="site" else scope
            with conectar() as conn:
                ds=_report_datasets(conn,d0,d1,effective_scope)
            ds=_report_apply_dataset_filters(ds, year=filter_year if selected_months else None, months=selected_months, vendedor=vendedor_filtro, cliente=cliente_filtro, grupo=grupo_filtro, papel=papel_filtro, medida=medida_filtro)
            metadata=[("Relatório",relatorio.capitalize()),("Período",period_label),("Time",_analysis_scope_label(effective_scope)),("Vendedor",vendedor_filtro or "Todos"),("Cliente",cliente_filtro or "Todos"),("Grupo",grupo_filtro or "Todos"),("Papel",papel_filtro or "Todos"),("Medida",medida_filtro or "Todas"),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))]
            if relatorio=="pedidos":
                rows=[[r.get(k) for k in ['data','pedido','cliente','vendedor','time','grupo','medida','papel','quantidade','valor']] for r in ds['orders']]
                return _xlsx_response(f"pedidos_{d0}_{d1}","CRM NPD · Pedidos realizados",metadata,[{"name":"Pedidos","title":f"Pedidos realizados · {period_label}","headers":["Data","Pedido","Cliente","Vendedor","Time","Grupo","Medida","Papel","Quantidade","Valor"],"rows":rows}])
            if relatorio=="faturamento" or relatorio=="site":
                if relatorio=="site":
                    rows=[]
                    rows += [["PEDIDO",r.get('data'),r.get('pedido'),'',r.get('cliente'),r.get('vendedor'),r.get('time'),r.get('grupo'),r.get('medida'),r.get('papel'),'',r.get('quantidade'),r.get('valor')] for r in ds['orders']]
                    rows += [["FATURAMENTO",r.get('data'),r.get('pedido'),r.get('nf'),r.get('cliente'),r.get('vendedor'),r.get('time'),r.get('grupo'),r.get('formato'),r.get('papel'),r.get('produto'),r.get('quantidade'),r.get('valor')] for r in ds['invoices']]
                    sheets=[{"name":"Dados","title":f"Canal Site · {period_label}","headers":["Tipo","Data","Pedido","NF","Cliente","Vendedor","Time","Grupo","Medida/Formato","Papel","Produto","Quantidade","Valor"],"rows":rows}]
                else:
                    rows=[[r.get(k) for k in ['data','nf','pedido','cliente','vendedor','time','grupo','formato','papel','produto','quantidade','valor']] for r in ds['invoices']]
                    sheets=[{"name":"Faturamento","title":f"Faturamento · {period_label}","headers":["Data","NF","Pedido","Cliente","Vendedor","Time","Grupo","Formato","Papel","Produto","Quantidade","Valor"],"rows":rows}]
                return _xlsx_response(f"{relatorio}_{d0}_{d1}",f"CRM NPD · {'Canal Site' if relatorio=='site' else 'Faturamento'}",metadata,sheets)
            if relatorio=="clientes":
                agg={}
                for r in ds['orders']:
                    k=str(r.get('cliente') or '—'); a=agg.setdefault(k,{'cliente':k,'pedidos':set(),'valor_ped':0.0,'qtd_ped':0.0,'fat':0.0,'qtd_fat':0.0,'nfs':set(),'vendedor':r.get('vendedor'),'time':r.get('time')})
                    if r.get('pedido'): a['pedidos'].add(str(r.get('pedido'))); a['valor_ped']+=float(r.get('valor') or 0); a['qtd_ped']+=float(r.get('quantidade') or 0)
                for r in ds['invoices']:
                    k=str(r.get('cliente') or '—'); a=agg.setdefault(k,{'cliente':k,'pedidos':set(),'valor_ped':0.0,'qtd_ped':0.0,'fat':0.0,'qtd_fat':0.0,'nfs':set(),'vendedor':r.get('vendedor'),'time':r.get('time')})
                    if r.get('nf'): a['nfs'].add(str(r.get('nf'))); a['fat']+=float(r.get('valor') or 0); a['qtd_fat']+=float(r.get('quantidade') or 0); a['vendedor']=r.get('vendedor') or a['vendedor']; a['time']=r.get('time') or a['time']
                rows=[[a['cliente'],a['vendedor'],a['time'],len(a['pedidos']),a['qtd_ped'],a['valor_ped'],len(a['nfs']),a['qtd_fat'],a['fat'],(a['fat']/len(a['nfs']) if a['nfs'] else 0)] for a in sorted(agg.values(),key=lambda x:x['fat']+x['valor_ped'],reverse=True)]
                return _xlsx_response(f"clientes_{d0}_{d1}","CRM NPD · Clientes",metadata,[{"name":"Clientes","title":f"Clientes · {period_label}","headers":["Cliente","Vendedor","Time","Pedidos","Qtd. pedidos","Valor pedidos","NFs","Qtd. faturada","Faturamento","Ticket NF"],"rows":rows}])
            # medidas
            agg={}
            for r in ds['orders']:
                k=str(r.get('medida') or 'Sem medida').strip() or 'Sem medida'; a=agg.setdefault(k,{'pedidos':set(),'clientes':set(),'qtd':0.0,'valor':0.0,'papel':set()})
                if r.get('pedido'): a['pedidos'].add(str(r.get('pedido')))
                if r.get('cliente'): a['clientes'].add(str(r.get('cliente')))
                if r.get('papel'): a['papel'].add(str(r.get('papel')))
                a['qtd']+=float(r.get('quantidade') or 0); a['valor']+=float(r.get('valor') or 0)
            rows=[[k,len(a['pedidos']),len(a['clientes']),a['qtd'],a['valor'],', '.join(sorted(a['papel']))] for k,a in sorted(agg.items(),key=lambda kv:kv[1]['qtd'],reverse=True)]
            return _xlsx_response(f"medidas_{d0}_{d1}","CRM NPD · Medidas",metadata,[{"name":"Medidas","title":f"Mix de medidas · {period_label}","headers":["Medida","Pedidos","Clientes","Quantidade","Valor","Papéis"],"rows":rows,"chart":{"type":"bar","title":"Quantidade por medida","category_col":1,"value_col":4,"y_title":"Unidades"}}])

        if relatorio in {"vendedores", "inativos"}:
            with conectar() as conn:
                rows_db=[dict(r) for r in conn.execute("""
                  SELECT v.id,v.codigo,v.nome,v.tipo,COALESCE(v.ativo,1) ativo,COALESCE(v.performance_ativo,1) performance_ativo,v.inativado_em,v.inativado_motivo,v.reativado_em,
                         MAX(date(o.data_emissao)) ultima_venda,
                         SUM(CASE WHEN date(o.data_emissao)>=date('now','-3 months') THEN COALESCE(NULLIF(o.valor_item_ref,0),o.valor,0) ELSE 0 END) valor_3m
                  FROM vendors v LEFT JOIN orders o ON o.vendor_id=v.id
                  GROUP BY v.id ORDER BY v.nome
                """).fetchall()]
            want_inactive=(relatorio=="inativos")
            out=[]
            for r in rows_db:
                inactive=(int(r.get('performance_ativo') or 0)==0 or int(r.get('ativo') or 0)==0 or bool(r.get('inativado_em')))
                if inactive!=want_inactive: continue
                if scope in {'INTERNO','EXTERNO','SITE'} and str(r.get('tipo') or '').upper()!=scope: continue
                if vendedor_filtro and str(r.get('nome') or '').strip()!=vendedor_filtro: continue
                out.append([r.get('codigo'),r.get('nome'),r.get('tipo'),'INATIVO' if inactive else 'ATIVO',r.get('ultima_venda'),float(r.get('valor_3m') or 0),r.get('inativado_em'),r.get('inativado_motivo'),r.get('reativado_em')])
            title="Vendedores inativos" if want_inactive else "Vendedores ativos"
            return _xlsx_response(f"{relatorio}_{date.today().isoformat()}",f"CRM NPD · {title}",[("Time",_analysis_scope_label(scope)),("Registros",len(out)),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))],[{"name":"Vendedores","title":title,"headers":["Código","Vendedor","Time","Situação","Última venda","Vendas últimos 3m","Inativado em","Motivo","Reativado em"],"rows":out}])

        if relatorio in {"tarefas", "oportunidades", "funil"}:
            d0,d1,period_label=_export_bounds()
            with conectar() as conn:
                if relatorio=="tarefas":
                    data=[dict(r) for r in conn.execute("""SELECT t.id,t.created_at,t.updated_at,t.status,t.titulo,t.descricao,COALESCE(c.fantasia,c.nome,t.cliente_nome_cache,'—') cliente,COALESCE(v.nome,'—') vendedor,COALESCE(v.tipo,'') time FROM tasks t LEFT JOIN customers c ON c.id=t.cliente_id LEFT JOIN vendors v ON v.id=t.vendor_id WHERE date(t.created_at) BETWEEN date(?) AND date(?) ORDER BY t.created_at""",(d0.isoformat(),d1.isoformat())).fetchall()]
                    if selected_months: data=[r for r in data if _report_date_in_months(r.get('created_at'),filter_year,selected_months)]
                    if status_filtro_global: data=[r for r in data if str(r.get('status') or '').upper()==status_filtro_global]
                    if vendedor_filtro: data=[r for r in data if norm(vendedor_filtro) in norm(r.get('vendedor') or '')]
                    if cliente_filtro: data=[r for r in data if norm(cliente_filtro) in norm(r.get('cliente') or '')]
                    if scope in {'INTERNO','EXTERNO','SITE'}: data=[r for r in data if str(r.get('time') or '').upper()==scope]
                    rows=[[r.get(k) for k in ['id','created_at','updated_at','status','titulo','cliente','vendedor','time','descricao']] for r in data]
                    return _xlsx_response(f"tarefas_{d0}_{d1}","CRM NPD · Tarefas comerciais",[("Período",period_label),("Status",status_filtro_global or 'Todos'),("Vendedor",vendedor_filtro or 'Todos')],[{"name":"Tarefas","title":f"Tarefas · {period_label}","headers":["ID","Criada em","Atualizada em","Status","Título","Cliente","Vendedor","Time","Descrição"],"rows":rows}])
                data=[dict(r) for r in conn.execute("""SELECT l.id,l.created_at,l.updated_at,l.nome,l.empresa,l.origem,l.etapa,l.temperatura,l.valor_estimado,l.probabilidade,l.proxima_acao,l.proxima_acao_em,l.motivo_perda,COALESCE(v.nome,'—') vendedor,COALESCE(v.tipo,'') time FROM crm_leads l LEFT JOIN vendors v ON v.id=l.vendor_id WHERE date(l.created_at) BETWEEN date(?) AND date(?) ORDER BY l.created_at""",(d0.isoformat(),d1.isoformat())).fetchall()]
            if selected_months: data=[r for r in data if _report_date_in_months(r.get('created_at'),filter_year,selected_months)]
            if status_filtro_global: data=[r for r in data if str(r.get('etapa') or '').upper()==status_filtro_global]
            if vendedor_filtro: data=[r for r in data if norm(vendedor_filtro) in norm(r.get('vendedor') or '')]
            if cliente_filtro: data=[r for r in data if norm(cliente_filtro) in norm(f"{r.get('nome') or ''} {r.get('empresa') or ''}")]
            if scope in {'INTERNO','EXTERNO','SITE'}: data=[r for r in data if str(r.get('time') or '').upper()==scope]
            rows=[[r.get(k) for k in ['id','created_at','nome','empresa','vendedor','time','origem','etapa','temperatura','valor_estimado','probabilidade','proxima_acao','proxima_acao_em','motivo_perda']] for r in data]
            sheet_name="Funil" if relatorio=="funil" else "Oportunidades"
            return _xlsx_response(f"{relatorio}_{d0}_{d1}",f"CRM NPD · {sheet_name}",[("Período",period_label),("Etapa",status_filtro_global or 'Todas'),("Vendedor",vendedor_filtro or 'Todos')],[{"name":sheet_name,"title":f"{sheet_name} · {period_label}","headers":["ID","Criada em","Contato","Empresa","Vendedor","Time","Origem","Etapa","Temperatura","Valor estimado","Probabilidade %","Próxima ação","Data próxima ação","Motivo perda"],"rows":rows}])

        if relatorio in {"historico", "planejamento", "consumo"}:
            try: target_year=int(request.args.get("ano") or request.args.get("ano_filtro") or ref.year)
            except Exception: target_year=ref.year
            with conectar() as conn:
                a=_planning_analysis_data(conn,periodo,ref,target_year,scope,selected_months=selected_months)
            comp=[[r['year'],r['orders'],r['clients'],r['qty'],r['value'],(r['variation']/100.0 if r['variation'] is not None else None)] for r in sorted(a['year_rows'],key=lambda x:x['year'])]
            expected_rows=[[c['name'],", ".join(map(str,c['years'])),c['recurrence'],c['likely_month_name'],c['projected'],c['target_value'],c['gap'],c['status'],c['frequency'],c['last_purchase'].isoformat() if c['last_purchase'] else '',c['next_purchase'].isoformat() if c['next_purchase'] else ''] for c in a['expected_clients']]
            risk_rows=[[c['name'],c['projected'],c['target_value'],c['gap'],c['status'],c['likely_month_name'],c['frequency']] for c in a['expected_clients'] if c['gap']>0]
            consumption_rows=[[x['gram'],x['kg'],x['ton'],x['pct']/100.0] for x in a['paper_consumption']]
            monthly_rows=[]
            for m in a['monthly_consumption']:
                row=[m['name'],m['kg'],m['ton']]
                by={x['gram']:x['kg'] for x in m['grams']}
                row += [by.get(g['gram'],0.0) for g in a['paper_consumption']]
                monthly_rows.append(row)
            month_sales=[[m['name'],m['baseline'],m['forecast'],m['actual']] for m in a['month_forecast']]
            criteria=[
                ['Pedidos','Pedidos realizados conforme regras oficiais do CRM; cancelados e exclusões JIT seguem a mesma regra do Dashboard.'],
                ['Sazonalidade',f"Comparação por {periodo}: clientes podem se deslocar entre meses dentro da mesma janela (ex.: outubro → novembro no mesmo trimestre)."],
                ['Base de forecast',f"Média ponderada dos anos {a['source_text']} (50% / 30% / 20% quando disponíveis)."],
                ['Cenários automáticos',f"Conservador {a['scenarios']['conservative_growth']:+.1f}%, provável {a['scenarios']['probable_growth']:+.1f}% e otimista {a['scenarios']['optimistic_growth']:+.1f}% sobre a base. A faixa vem da variação histórica da própria janela."],
                ['Consumo de papel',f"Estimativa geométrica para sacolas/cartuchos: 2×(largura+fundo) × (altura+fundo) × gramatura; margem técnica do sistema {a['waste_pct']:.1f}%."],
                ['Cobertura do consumo',f"{a['paper_coverage']:.1f}% das unidades elegíveis tinham medida e gramatura suficientes para estimativa."],
                ['Atenção','Kg projetado é previsão para compras/financeiro e deve ser calibrado com ficha técnica real quando houver consumo/metro por SKU.'],
            ]
            planning_rows=[]
            for r in sorted(a['year_rows'],key=lambda x:x['year']):
                planning_rows.append(["Comparativo",str(r['year']),r['orders'],r['qty'],r['value'],(r['variation']/100.0 if r['variation'] is not None else None),""])
            for m in a['month_forecast']:
                planning_rows.append(["Forecast mensal",m['name'],None,None,m['forecast'],None,f"Base {m['baseline']:.2f} | realizado {m['actual']:.2f}"])
            for x in a['paper_consumption']:
                planning_rows.append(["Consumo gramatura",x['gram'],None,None,None,x['pct']/100.0,f"{x['kg']:.2f} kg | {x['ton']:.3f} t"])
            for m in a['monthly_consumption']:
                planning_rows.append(["Consumo mensal",m['name'],None,None,None,None,f"{m['kg']:.2f} kg | {m['ton']:.3f} t"])
            for x in a['fam_proj']:
                planning_rows.append(["Mix produto",x['name'],None,x['qty'],None,x['pct']/100.0,""])
            for x in a['top_measures']:
                planning_rows.append(["Top medida",x['name'],None,x['qty'],None,x['pct']/100.0,f"≈ {x['kg']:.2f} kg" if x['kg'] else ""])
            planning_rows += [["Critério",r[0],None,None,None,None,r[1]] for r in criteria]
            if relatorio != "consumo":
                for c in a['expected_clients']:
                    planning_rows.append(["Cliente esperado",c['name'],None,None,c['projected'],None,f"{c['status']} | mês provável {c['likely_month_name']} | realizado {c['target_value']:.2f} | gap {c['gap']:.2f} | {c['frequency']}"])
                sheets=[{"name":"Planejamento","title":f"Planejamento gerencial · {a['period_name']}","headers":["Bloco","Item","Documentos","Quantidade","Valor","Participação / variação","Observação"],"rows":planning_rows}]
            else:
                consumo_rows=[]
                for x in a['paper_consumption']: consumo_rows.append(["Gramatura",x['gram'],None,x['kg'],x['ton'],x['pct']/100.0])
                for m in a['monthly_consumption']: consumo_rows.append(["Mês",m['name'],None,m['kg'],m['ton'],None])
                for x in a['top_measures']: consumo_rows.append(["Top medida",x['name'],x['qty'],x['kg'],x['kg']/1000.0 if x['kg'] else 0,x['pct']/100.0])
                sheets=[{"name":"Consumo","title":"Plano de consumo projetado","headers":["Tipo","Item","Quantidade (un.)","Kg","Toneladas","Participação"],"rows":consumo_rows}]
            metadata=[
                ("Relatório","Consumo projetado de papel" if relatorio=="consumo" else "Planejamento gerencial"),("Recorte",a['period_name']),("Ano-alvo",target_year),("Time",_analysis_scope_label(scope)),
                ("Realizado",a['actual_target_value']),("Conservador",a['scenarios']['conservative']),("Forecast provável",a['forecast_value']),("Otimista",a['scenarios']['optimistic']),
                ("Gap potencial",a['revenue_risk']),("Papel projetado kg",a['total_paper_kg']),("Cobertura consumo",f"{a['paper_coverage']:.1f}%"),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))
            ]
            return _xlsx_response(f"planejamento_{periodo}_{target_year}_{scope.lower()}", "CRM NPD · Planejamento Gerencial", metadata, sheets)

        d0,d1,period_label=_export_bounds()
        with conectar() as conn: ds=_report_datasets(conn,d0,d1,scope)
        ds=_report_apply_dataset_filters(ds, year=filter_year if selected_months else None, months=selected_months, vendedor=vendedor_filtro, cliente=cliente_filtro, grupo=grupo_filtro, papel=papel_filtro, medida=medida_filtro)
        if relatorio == 'pedidos': conteudo='PEDIDOS'
        if relatorio == 'faturamento': conteudo='FATURAMENTO'
        rows=[]
        if conteudo in {"AMBOS","PEDIDOS"}: rows += [["PEDIDO",r.get('data'),r.get('pedido'),'',r.get('cliente'),r.get('vendedor'),r.get('time'),r.get('grupo'),r.get('medida'),r.get('papel'),'',r.get('quantidade'),r.get('valor')] for r in ds['orders']]
        if conteudo in {"AMBOS","FATURAMENTO"}: rows += [["FATURAMENTO",r.get('data'),r.get('pedido'),r.get('nf'),r.get('cliente'),r.get('vendedor'),r.get('time'),r.get('grupo'),r.get('formato'),r.get('papel'),r.get('produto'),r.get('quantidade'),r.get('valor')] for r in ds['invoices']]
        sheets=[{"name":"Dados","title":"Base comercial consolidada","headers":["Tipo","Data","Pedido","NF","Cliente","Vendedor","Time","Grupo","Medida/Formato","Papel","Produto","Quantidade","Valor"],"rows":rows}]
        metadata=[("Período",period_label),("Início",d0.strftime('%d/%m/%Y')),("Fim",d1.strftime('%d/%m/%Y')),("Time",_analysis_scope_label(scope)),("Conteúdo",conteudo),("Pedidos",ds['p_orders']),("Valor pedidos",ds['p_value']),("NFs",ds['f_nfs']),("Faturamento",ds['f_value']),("Gerado em",datetime.now().strftime('%d/%m/%Y %H:%M'))]
        return _xlsx_response(f"relatorio_{periodo}_{d0.isoformat()}_{d1.isoformat()}", "CRM NPD · Relatório comercial", metadata, sheets)

    @app.route("/analises/historico")
    @login_required
    def analise_historica():
        """Planejamento sazonal: comercial, risco de receita e consumo de papel."""
        user=get_current_user()
        if not user or user["role"] not in ["admin","gestor"]:
            flash("Acesso restrito à gestão.","warning"); return redirect(url_for("home"))
        today=date.today(); default_target=(today.replace(day=28)+timedelta(days=4)).replace(day=1)
        periodo=(request.args.get("period_select") or request.args.get("periodo") or "mes").strip().lower()
        if periodo not in {"dia","semana","mes","trimestre","semestre","ano","meses"}: periodo="mes"
        selected_months=_report_selected_months(request.args)
        if selected_months:
            periodo="meses"
        if request.args.get("data_base"): anchor=_parse_report_ref(request.args.get("data_base"),default_target)
        elif selected_months:
            try: anchor=date(int(request.args.get("ano") or default_target.year),selected_months[0],1)
            except Exception: anchor=default_target
        else: anchor=default_target
        try: target_year=int(request.args.get("ano") or anchor.year)
        except Exception: target_year=anchor.year
        growth = None
        band = None
        waste = None
        scope=(request.args.get("escopo") or "TOTAL").strip().upper()
        if scope not in {"TOTAL","INTERNO","EXTERNO","SITE"}: scope="TOTAL"
        with conectar() as conn:
            a=_planning_analysis_data(conn,periodo,anchor,target_year,scope,selected_months=selected_months)
        chart_rows=sorted(a['year_rows'],key=lambda x:x['year'])
        sales_chart=bar_chart([str(x['year']) for x in chart_rows],[x['value'] for x in chart_rows],f"{a['period_name']} · histórico de pedidos") if chart_rows else None
        year_options=list(range(max(a['max_db_year']+2,target_year+1),a['min_db_year']-1,-1))
        export_url=url_for('relatorios_exportar',relatorio='planejamento',periodo=periodo,data_base=anchor.isoformat(),ano=target_year,ano_filtro=target_year,meses=','.join(map(str,selected_months)),escopo=scope,conteudo='AMBOS')
        body=render_template_string(r"""
        <div class="page-hero mb-4"><div class="d-flex justify-content-between align-items-start flex-wrap gap-3"><div><div class="small text-uppercase fw-bold mb-2" style="letter-spacing:.12em;color:#86dbe6">Planejamento comercial, financeiro e produção</div><div class="hero-title">Análise Histórica & Forecast</div><div class="hero-sub mt-1">Sazonalidade por mês, trimestre ou semestre sem perder clientes que deslocam a compra dentro da janela. Forecast de vendas, risco e consumo de papel em kg.</div></div><span class="chip"><i class="bi bi-database-check"></i> Histórico desde {{ a.min_db_year }}</span></div></div>

        <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-4"><div class="small text-muted"><i class="bi bi-calendar3 me-1"></i>{{ a.period_name }} · {{ target_year }} · {{ scope_label }}</div><div class="d-flex gap-2"><button class="btn btn-primary" type="button" data-bs-toggle="collapse" data-bs-target="#analysisFilters"><i class="bi bi-funnel me-1"></i>Filtros</button><a class="btn btn-success" href="{{ export_url }}"><i class="bi bi-file-earmark-excel me-1"></i>Excel</a><a class="btn btn-outline-primary" href="{{ url_for('relatorios') }}" title="Central de relatórios"><i class="bi bi-file-earmark-spreadsheet"></i></a></div></div>
        <div class="collapse mb-4" id="analysisFilters"><form class="cardx p-4" method="get"><input type="hidden" name="periodo" value="meses"><div class="row g-3">
          <div class="col-12 col-lg-3"><label class="form-label fw-bold">Ano-alvo</label><select class="form-select" name="ano">{% for y in year_options %}<option value="{{ y }}" {% if y==target_year %}selected{% endif %}>{{ y }}</option>{% endfor %}</select></div>
          <div class="col-12 col-lg-3"><label class="form-label fw-bold">Time</label><select class="form-select" name="escopo"><option value="TOTAL" {% if scope=='TOTAL' %}selected{% endif %}>Empresa inteira</option><option value="INTERNO" {% if scope=='INTERNO' %}selected{% endif %}>Interno</option><option value="EXTERNO" {% if scope=='EXTERNO' %}selected{% endif %}>Externo</option><option value="SITE" {% if scope=='SITE' %}selected{% endif %}>Site</option></select></div>
          <div class="col-12 col-lg-6"><label class="form-label fw-bold">Atalhos</label><div class="d-flex flex-wrap gap-2"><button type="button" class="btn btn-sm btn-outline-primary" onclick="analysisMonths([1,2,3])">1º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="analysisMonths([4,5,6])">2º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="analysisMonths([7,8,9])">3º tri</button><button type="button" class="btn btn-sm btn-outline-primary" onclick="analysisMonths([10,11,12])">4º tri</button><button type="button" class="btn btn-sm btn-outline-info" onclick="analysisMonths([1,2,3,4,5,6])">1º semestre</button><button type="button" class="btn btn-sm btn-outline-info" onclick="analysisMonths([7,8,9,10,11,12])">2º semestre</button><button type="button" class="btn btn-sm btn-light border" onclick="analysisMonths([1,2,3,4,5,6,7,8,9,10,11,12])">Ano todo</button></div></div>
          <div class="col-12"><label class="form-label fw-bold">Meses que entram na comparação</label><div class="border rounded-3 p-3 bg-light"><div class="row g-2">{% for m,n in months_pt %}<div class="col-4 col-md-2"><label class="form-check d-flex align-items-center gap-2 mb-0"><input class="form-check-input analysis-month" type="checkbox" name="mes" value="{{ m }}" {% if m in selected_months %}checked{% elif not selected_months and m==anchor.month %}checked{% endif %}><span>{{ n[:3] }}</span></label></div>{% endfor %}</div></div><div class="form-text">Você pode marcar qualquer combinação: por exemplo Out + Nov + Dez, ou Jan + Mar + Jul.</div></div>
          <div class="col-12 d-flex justify-content-end gap-2"><button class="btn btn-primary"><i class="bi bi-check2 me-1"></i>Aplicar filtros</button></div>
        </div></form></div>
        <script>function analysisMonths(list){const wanted=new Set(list.map(Number));document.querySelectorAll('.analysis-month').forEach(cb=>cb.checked=wanted.has(Number(cb.value)));}</script>

        <div class="section-heading"><div><h2>{{ a.period_name|capitalize }} · alvo {{ target_year }} · {{ scope_label }}</h2><p>Base ponderada {{ a.source_text }}. No trimestre/semestre, a recorrência considera a janela inteira — o cliente pode trocar outubro por novembro e continuar previsto.</p></div></div>

        <div class="row g-3 mb-4">
          <div class="col-6 col-xl-2"><div class="stat-card h-100"><div class="stat-label">Realizado</div><div class="stat-value">{{ format_brl(a.actual_target_value) }}</div><div class="stat-sub">ano-alvo no recorte</div></div></div>
          <div class="col-6 col-xl-2"><div class="stat-card h-100"><div class="stat-label">Conservador</div><div class="stat-value">{{ format_brl(a.scenarios.conservative) }}</div><div class="stat-sub">tendência {{ '%+.1f'|format(a.scenarios.conservative_growth) }}%</div></div></div>
          <div class="col-6 col-xl-2"><div class="stat-card h-100"><div class="stat-label">Provável</div><div class="stat-value">{{ format_brl(a.scenarios.probable) }}</div><div class="stat-sub">tendência {{ '%+.1f'|format(a.scenarios.probable_growth) }}%</div></div></div>
          <div class="col-6 col-xl-2"><div class="stat-card h-100"><div class="stat-label">Otimista</div><div class="stat-value">{{ format_brl(a.scenarios.optimistic) }}</div><div class="stat-sub">tendência {{ '%+.1f'|format(a.scenarios.optimistic_growth) }}%</div></div></div>
          <div class="col-6 col-xl-2"><div class="stat-card h-100"><div class="stat-label">Gap em risco</div><div class="stat-value">{{ format_brl(a.revenue_risk) }}</div><div class="stat-sub">{{ a.pending_clients }} clientes para atacar</div></div></div>
          <div class="col-6 col-xl-2"><button type="button" class="stat-card h-100 w-100 text-start border-0" data-bs-toggle="modal" data-bs-target="#paperBreakdownModal" style="cursor:pointer"><div class="stat-label">Papel previsto <i class="bi bi-box-arrow-up-right ms-1"></i></div><div class="stat-value">{{ format_num_br(a.total_paper_kg,0) }} kg</div><div class="stat-sub">{{ format_num_br(a.total_paper_kg/1000,2) }} t · clique para ver por gramatura</div></button></div>
        </div>

        <div class="row g-4 mb-4">
          <div class="col-12 col-xl-7"><div class="cardx p-4 h-100"><div class="section-title">Histórico do mesmo {{ periodo_label }}</div><div class="section-sub mb-3">O valor anual compara janelas equivalentes. O detalhamento mensal abaixo mostra onde a venda tende a cair dentro do trimestre/semestre.</div>{% if sales_chart %}<div class="chart-frame"><img class="chart-img" src="data:image/png;base64,{{ sales_chart }}"></div>{% endif %}</div></div>
          <div class="col-12 col-xl-5"><div class="cardx p-4 h-100"><div class="section-title">Distribuição dentro da janela</div><div class="section-sub mb-3">Base ponderada → forecast → realizado.</div><div class="table-responsive"><table class="table table-sm"><thead><tr><th>Mês</th><th class="text-end">Base</th><th class="text-end">Previsto</th><th class="text-end">Realizado</th></tr></thead><tbody>{% for m in a.month_forecast %}<tr><td class="fw-bold">{{ m.name }}</td><td class="text-end">{{ format_brl(m.baseline) }}</td><td class="text-end fw-bold">{{ format_brl(m.forecast) }}</td><td class="text-end">{{ format_brl(m.actual) }}</td></tr>{% endfor %}</tbody></table></div>{% if a.pipeline_value>0 %}<div class="alert alert-light border mb-0"><b>Pipeline aberto:</b> {{ format_brl(a.pipeline_value) }} em {{ a.pipeline_count }} orçamento(s). Não é somado automaticamente ao forecast para evitar dupla contagem.</div>{% endif %}</div></div>
        </div>

        <div class="cardx p-4 mb-4" id="clientes-esperados"><div class="d-flex justify-content-between align-items-start flex-wrap gap-2 mb-3"><div><div class="section-title">Clientes esperados e próxima ação</div><div class="section-sub">Quem historicamente compra nessa janela, em qual mês tende a comprar, frequência e quanto ainda falta realizar.</div></div><span class="chip">{{ a.expected_clients|length }} mapeados</span></div><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Cliente</th><th>Mês provável</th><th>Frequência</th><th>Próxima compra</th><th class="text-end">Projetado</th><th class="text-end">Realizado</th><th class="text-end">Gap</th><th>Status</th><th></th></tr></thead><tbody>{% for c in a.expected_clients[:100] %}<tr><td><div class="fw-bold">{{ c.name }}</div><div class="small text-muted">Histórico: {{ c.years|join(', ') }}</div></td><td>{{ c.likely_month_name }}</td><td>{{ c.frequency }}</td><td>{{ c.next_purchase.strftime('%d/%m/%Y') if c.next_purchase else '—' }}</td><td class="text-end fw-bold">{{ format_brl(c.projected) }}</td><td class="text-end">{{ format_brl(c.target_value) }}</td><td class="text-end {{ 'text-danger' if c.gap>0 else 'text-success' }}">{{ format_brl(c.gap) }}</td><td><span class="chip {{ 'chip-success' if c.status=='Já comprou' else 'chip-warning' }}">{{ c.status }}</span></td><td>{% if c.cliente_id and c.vendor_id and c.gap>0 %}<form method="post" action="{{ url_for('forecast_criar_tarefa') }}"><input type="hidden" name="cliente_id" value="{{ c.cliente_id }}"><input type="hidden" name="vendor_id" value="{{ c.vendor_id }}"><input type="hidden" name="cliente_nome" value="{{ c.name }}"><input type="hidden" name="gap" value="{{ c.gap }}"><input type="hidden" name="periodo_nome" value="{{ a.period_name }}"><input type="hidden" name="return_url" value="{{ request.full_path }}"><button class="btn btn-sm btn-outline-primary" title="Criar tarefa comercial"><i class="bi bi-check2-square"></i></button></form>{% endif %}</td></tr>{% else %}<tr><td colspan="9" class="text-center text-muted py-4">Sem histórico suficiente.</td></tr>{% endfor %}</tbody></table></div></div>

        <div class="section-heading" id="plano-consumo"><div><h2>Plano de consumo para Financeiro / Compras</h2><p>Kg projetados por gramatura e por mês da janela. A margem técnica é padronizada pelo sistema e não precisa ser informada a cada análise.</p></div></div>
        <div class="row g-4 mb-4"><div class="col-12 col-xl-5"><div class="cardx p-4 h-100"><div class="section-title mb-3">Consumo por gramatura</div>{% for r in a.paper_consumption %}<div class="mb-3"><div class="d-flex justify-content-between"><div class="fw-bold">{{ r.gram }}</div><div><b>{{ '%.0f'|format(r.kg) }} kg</b> · {{ '%.2f'|format(r.ton) }} t · {{ '%.1f'|format(r.pct) }}%</div></div><div class="progress mt-1" style="height:7px"><div class="progress-bar bg-success" style="width:{{ r.pct }}%"></div></div></div>{% else %}<div class="text-muted">Sem medida/gramatura suficiente.</div>{% endfor %}<div class="small text-muted mt-3">Cobertura da estimativa: {{ '%.1f'|format(a.paper_coverage) }}% das unidades elegíveis do histórico-base.</div></div></div><div class="col-12 col-xl-7"><div class="cardx p-4 h-100"><div class="section-title mb-3">Necessidade mês a mês</div><div class="table-responsive"><table class="table table-sm"><thead><tr><th>Mês</th><th class="text-end">Kg</th><th class="text-end">Toneladas</th>{% for g in a.paper_consumption %}<th class="text-end">{{ g.gram }}</th>{% endfor %}</tr></thead><tbody>{% for m in a.monthly_consumption %}<tr><td class="fw-bold">{{ m.name }}</td><td class="text-end fw-bold">{{ '%.0f'|format(m.kg) }}</td><td class="text-end">{{ '%.2f'|format(m.ton) }}</td>{% for g in a.paper_consumption %}<td class="text-end">{{ '%.0f'|format(m.gram_map.get(g.gram,0)) }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div></div></div></div>

        <div class="row g-4 mb-4"><div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="section-title mb-3">Mix previsto de produto</div>{% for r in a.fam_proj %}<div class="mb-3"><div class="d-flex justify-content-between"><b>{{ r.name }}</b><span>{{ format_num_br(r.qty,0) }} un. · {{ '%.1f'|format(r.pct) }}%</span></div><div class="progress mt-1" style="height:7px"><div class="progress-bar" style="width:{{ r.pct }}%"></div></div></div>{% endfor %}</div></div><div class="col-12 col-xl-6"><div class="cardx p-4 h-100"><div class="section-title mb-1">Top 5 medidas mais consumidas</div><div class="section-sub mb-3">Quantidade prevista em unidades na mesma janela sazonal.</div>{% for r in a.top_measures %}<div class="mb-3"><div class="d-flex justify-content-between gap-3"><b>{{ r.name }}</b><span class="text-end">{{ format_num_br(r.qty,0) }} un. · {{ '%.1f'|format(r.pct) }}%{% if r.kg>0 %}<br><small class="text-muted">≈ {{ format_num_br(r.kg,0) }} kg</small>{% endif %}</span></div><div class="progress mt-1" style="height:7px"><div class="progress-bar bg-info" style="width:{{ r.pct }}%"></div></div></div>{% else %}<div class="text-muted">Sem medidas suficientes no histórico-base.</div>{% endfor %}</div></div></div>

        <div class="modal fade" id="paperBreakdownModal" tabindex="-1" aria-hidden="true"><div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content"><div class="modal-header"><div><h5 class="modal-title">Papel previsto por gramatura</h5><div class="small text-muted">Consumo físico estimado para {{ a.period_name }} · {{ target_year }}</div></div><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><div class="table-responsive"><table class="table table-hover align-middle mb-0"><thead><tr><th>Gramatura</th><th class="text-end">Kg previstos</th><th class="text-end">Toneladas</th><th class="text-end">% do total</th></tr></thead><tbody>{% for r in a.paper_consumption %}<tr><td class="fw-bold">{{ r.gram }}</td><td class="text-end fw-bold">{{ format_num_br(r.kg,0) }} kg</td><td class="text-end">{{ format_num_br(r.ton,3) }} t</td><td class="text-end">{{ '%.1f'|format(r.pct) }}%</td></tr>{% else %}<tr><td colspan="4" class="text-center text-muted py-4">Sem medida e gramatura suficientes para calcular o consumo.</td></tr>{% endfor %}</tbody><tfoot><tr class="fw-bold"><td>Total</td><td class="text-end">{{ format_num_br(a.total_paper_kg,0) }} kg</td><td class="text-end">{{ format_num_br(a.total_paper_kg/1000,3) }} t</td><td class="text-end">100%</td></tr></tfoot></table></div><div class="alert alert-light border mt-3 mb-0">Cobertura da estimativa: <b>{{ '%.1f'|format(a.paper_coverage) }}%</b>. Para compras/financeiro, o Excel de Planejamento também traz essa quebra mês a mês.</div></div><div class="modal-footer"><a class="btn btn-success" href="{{ url_for('relatorios_exportar', relatorio='consumo', periodo=periodo, data_base=anchor.isoformat(), ano=target_year, escopo=scope) }}"><i class="bi bi-file-earmark-excel me-1"></i>Exportar consumo</a><button class="btn btn-secondary" data-bs-dismiss="modal">Fechar</button></div></div></div></div>

        <details class="cardx p-4 mb-4"><summary class="fw-bold" style="cursor:pointer"><i class="bi bi-calculator me-2"></i>Como o sistema chegou nesses números?</summary><div class="mt-3 small"><p><b>Forecast:</b> média sazonal ponderada dos até 3 anos anteriores (50% / 30% / 20%). Os três cenários calculam automaticamente a tendência e a dispersão do crescimento observado no mesmo período, sem percentual manual.</p><p><b>Trimestre/Semestre:</b> o cliente é considerado recorrente se comprar em qualquer mês da mesma janela. O “mês provável” usa onde ele concentrou valor nos anos-base.</p><p><b>Risco de receita:</b> valor projetado por cliente menos o realizado no período-alvo, nunca abaixo de zero.</p><p><b>Kg de papel:</b> estimativa geométrica para sacolas/cartuchos com medida L×A×F: área ≈ 2×(L+F)×(A+F); depois × gramatura × quantidade e + {{ '%.1f'|format(a.waste_pct) }}% de margem técnica padronizada. <b>Não substitui ficha técnica/BOM</b>; cobertura atual {{ '%.1f'|format(a.paper_coverage) }}%.</p><p class="mb-0"><b>Fonte:</b> pedidos realizados + faturamento do próprio CRM, time {{ scope_label }}, base {{ a.source_text }}.</p></div></details>
        """, a=a,periodo=periodo,periodo_label={"dia":"dia","semana":"semana","mes":"mês","trimestre":"trimestre","semestre":"semestre","ano":"ano","meses":"meses selecionados"}.get(periodo,periodo),anchor=anchor,target_year=target_year,year_options=year_options,scope=scope,scope_label=_analysis_scope_label(scope),sales_chart=sales_chart,export_url=export_url,selected_months=selected_months,months_pt=_MONTHS_PT,format_brl=format_brl,format_num_br=format_num_br,request=request,dict=dict)
        return render_page("Análise Histórica",body,"analise_historica")


    @app.route("/analise-historica/criar-tarefa", methods=["POST"])
    @login_required
    def forecast_criar_tarefa():
        user=get_current_user()
        if not user or user['role'] not in ['admin','gestor']:
            abort(403)
        try: cliente_id=int(request.form.get('cliente_id') or 0); vendor_id=int(request.form.get('vendor_id') or 0)
        except Exception: cliente_id=vendor_id=0
        cliente_nome=(request.form.get('cliente_nome') or 'Cliente').strip()
        periodo_nome=(request.form.get('periodo_nome') or 'período').strip()
        try: gap=float(request.form.get('gap') or 0)
        except Exception: gap=0.0
        ret=(request.form.get('return_url') or url_for('analise_historica')).strip()
        if cliente_id<=0 or vendor_id<=0:
            flash('Não foi possível identificar cliente/vendedor para criar a tarefa.','warning'); return redirect(ret)
        with conectar() as conn:
            # evita duplicar a mesma ação aberta para o cliente
            exists=conn.execute("SELECT 1 FROM tasks WHERE cliente_id=? AND vendor_id=? AND status NOT IN ('CONCLUÍDA','CANCELADA') AND titulo LIKE 'Forecast:%' LIMIT 1",(cliente_id,vendor_id)).fetchone()
            if not exists:
                create_task_unificada(conn,vendor_id=vendor_id,cliente_id=cliente_id,titulo=f"Forecast: contatar {cliente_nome}",descricao=f"Cliente esperado em {periodo_nome}. Receita potencial ainda não realizada: {format_brl(gap)}. Validar recompra e previsão de pedido.",criada_por=user.get('nome') or 'Gestão',status='ABERTA')
                conn.commit(); flash('Tarefa comercial criada a partir do forecast.','success')
            else: flash('Já existe uma tarefa de forecast aberta para esse cliente/vendedor.','info')
        return redirect(ret)
