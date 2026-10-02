# Templates Jinja

Os 33 blocos `render_template_string` ainda vivem em
`crm_npd/legacy_monolith.py` (constante `BASE_HTML` na linha 2289 e as rotas
abaixo). A extração é incremental: cada rota que sair do monolito ganha um
arquivo nesta pasta e passa a usar `render_template`.

Não edite o HTML no monolito e aqui ao mesmo tempo.
