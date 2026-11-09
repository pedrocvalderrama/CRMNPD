// Transforma os selects nativos em campos pesquisáveis com seleção múltipla.
document.querySelectorAll('.desempenho-multi').forEach((select) => {
  const label = select.getAttribute('aria-label') || 'opções';
  const box = document.createElement('div');
  box.className = 'desempenho-multiselect';

  const control = document.createElement('div');
  control.className = 'desempenho-multi-control';
  const tags = document.createElement('div');
  tags.className = 'desempenho-multi-tags';
  const search = document.createElement('input');
  search.type = 'search';
  search.className = 'desempenho-multi-search';
  search.placeholder = 'Digite para buscar';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', 'Buscar ' + label);
  search.setAttribute('aria-expanded', 'false');

  const arrow = document.createElement('button');
  arrow.type = 'button';
  arrow.className = 'desempenho-multi-arrow';
  arrow.innerHTML = '&#9662;';
  arrow.setAttribute('aria-label', 'Mostrar ' + label);

  const panel = document.createElement('div');
  panel.className = 'desempenho-multi-panel';
  panel.hidden = true;
  const empty = document.createElement('div');
  empty.className = 'desempenho-multi-empty';
  empty.textContent = 'Nenhuma opção encontrada';
  empty.hidden = true;
  panel.append(empty);

  const rows = [...select.options].map((option) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'desempenho-multi-option';
    row.textContent = option.textContent;
    row.onclick = () => {
      option.selected = true;
      search.value = '';
      render();
      filterOptions();
      select.dispatchEvent(new Event('change', {bubbles: true}));
      search.focus();
    };
    panel.insertBefore(row, empty);
    return {option, row};
  });

  const open = () => {
    panel.hidden = false;
    search.setAttribute('aria-expanded', 'true');
    filterOptions();
  };
  const close = () => {
    panel.hidden = true;
    search.setAttribute('aria-expanded', 'false');
  };
  const filterOptions = () => {
    const term = search.value.trim().toLocaleLowerCase('pt-BR');
    let visible = 0;
    rows.forEach(({option, row}) => {
      const matches = option.textContent.toLocaleLowerCase('pt-BR').includes(term);
      const show = !option.selected && !option.hidden && matches;
      row.hidden = !show;
      if (show) visible += 1;
    });
    empty.hidden = visible !== 0;
  };
  const render = () => {
    tags.replaceChildren();
    rows.forEach(({option}) => {
      if (!option.selected) return;
      const tag = document.createElement('span');
      tag.className = 'desempenho-multi-tag';
      tag.append(document.createTextNode(option.textContent));
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remover ' + option.textContent);
      remove.onclick = (event) => {
        event.stopPropagation();
        option.selected = false;
        render();
        filterOptions();
        select.dispatchEvent(new Event('change', {bubbles: true}));
        search.focus();
      };
      tag.append(remove);
      tags.append(tag);
    });
  };

  search.onfocus = open;
  search.onclick = open;
  search.oninput = () => {
    open();
    filterOptions();
  };
  search.onkeydown = (event) => {
    if (event.key === 'Escape') close();
    if (event.key === 'Enter') {
      const first = rows.find(({row}) => !row.hidden);
      if (first) {
        event.preventDefault();
        first.row.click();
      }
    }
  };
  arrow.onclick = () => {
    if (panel.hidden) {
      open();
      search.focus();
    } else {
      close();
    }
  };
  select.addEventListener('change', () => {
    render();
    filterOptions();
  });
  control.onclick = (event) => {
    if (event.target === control || event.target === tags) search.focus();
  };
  document.addEventListener('click', (event) => {
    if (!box.contains(event.target)) close();
  });

  control.append(tags, search, arrow);
  box.append(control, panel);
  select.after(box);
  select.hidden = true;
  render();
});
