/* Auktionshuset Hunter, dashboard.
 *
 * Al JavaScript her er en forbedring, ikke en forudsætning: siderne er
 * server-renderede, og uden JS virker søgning, filtre og markering stadig.
 * Scriptet gør filtrering og sortering øjeblikkelig og husker markeringer.
 *
 * Kortene i #cards-container er sandheden. Ved anden sortering end 'nyeste'
 * KLONES de synlige kort ind i #flat-list, så originalerne bliver stående i
 * deres dato-grupper. Flytter man dem i stedet, forsvinder de fra grupperne
 * når man skifter tilbage til 'nyeste'.
 */

(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  const kr = n => Number(n).toLocaleString('da-DK');

  /* ---- markering af fund ------------------------------------------------ */

  function paint(lotId, action) {
    $$('.card[data-lot="' + CSS.escape(lotId) + '"]').forEach(card => {
      card.dataset.feedback = action;
      $$('.fb-btn', card).forEach(btn => {
        btn.setAttribute('aria-pressed',
          String(action !== '' && btn.dataset.action === action));
      });
    });
  }

  async function saveFeedback(btn) {
    const card = btn.closest('.card');
    if (!card) return;

    const lotId = btn.dataset.lotId;
    const action = btn.dataset.action;
    const previous = card.dataset.feedback || '';
    const next = previous === action ? '' : action;

    // Opdatér med det samme; serveren er hurtigere end øjet følger med.
    paint(lotId, next);

    const titleEl = $('.card-title', card);
    try {
      const response = await fetch('/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lot_id: lotId,
          category_key: btn.dataset.catKey || '',
          action: next,
          title: (titleEl ? titleEl.textContent : '').trim()
        })
      });
      if (!response.ok) throw new Error(String(response.status));
    } catch (_) {
      // Netværksfejl: vis sandheden igen ved næste reload.
      paint(lotId, previous);
    }
  }

  document.addEventListener('click', event => {
    const btn = event.target.closest && event.target.closest('.fb-btn');
    if (!btn) return;
    event.preventDefault();
    saveFeedback(btn);
  });

  /* ---- bekraeftelse foer fjernelse ------------------------------------- */

  // Ligger her og ikke i en inline onsubmit: et noegleord med et citationstegn
  // i ville ellers kunne bryde ud af JavaScript-strengen i attributten.
  document.addEventListener('submit', event => {
    const form = event.target.closest && event.target.closest('form[data-confirm]');
    if (!form) return;
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  /* ---- pladsholder naar et billede ikke kan hentes ---------------------- */

  // 'error' bobler ikke, saa den fanges i capture-fasen. Ligger her fordi
  // inline onerror ville kraeve 'unsafe-inline' i script-src.
  document.addEventListener('error', event => {
    const img = event.target;
    if (img && img.tagName === 'IMG') img.classList.add('is-broken');
  }, true);

  /* ---- filtrering og sortering på fund-siderne -------------------------- */

  function initFindPage() {
    const container = document.getElementById('cards-container');
    if (!container) return;

    const flat = document.getElementById('flat-list');
    const noResults = document.getElementById('no-results');
    const counter = document.getElementById('visible-count');
    const toggle = document.getElementById('filter-toggle');
    const panel = document.getElementById('filter-panel');
    const endingBtn = document.getElementById('ending-chip');
    const minInput = document.getElementById('price-min');
    const maxInput = document.getElementById('price-max');
    const resetBtn = document.getElementById('reset-btn');
    const activeBox = document.getElementById('active-filters');

    const cards = $$('.card', container);
    const total = cards.length;
    const groups = $$('.date-group', container);

    const catLabels = {};
    $$('.chip[data-cat]').forEach(chip => {
      catLabels[chip.dataset.cat] = chip.textContent.trim();
    });

    const state = {
      cats: new Set(), period: 'all', sort: 'newest',
      min: 0, max: Infinity, ending: false
    };

    // Filtrene gemmes pr. side, så de overlever at man lige åbner et lot og
    // kommer tilbage. De er synlige som chips, så de kan ikke ligge skjult.
    const storageKey = 'fund-filtre:' + location.pathname;

    function restore() {
      let saved = null;
      try {
        saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      } catch (_) {
        saved = null;
      }
      if (!saved || typeof saved !== 'object') return;
      if (Array.isArray(saved.cats)) {
        saved.cats.forEach(cat => { if (catLabels[cat]) state.cats.add(cat); });
      }
      if (['all', 'today', 'week'].indexOf(saved.period) !== -1) state.period = saved.period;
      if (['newest', 'oldest', 'ending', 'price_desc', 'price_asc'].indexOf(saved.sort) !== -1) {
        state.sort = saved.sort;
      }
      if (Number.isFinite(saved.min) && saved.min > 0) state.min = saved.min;
      if (Number.isFinite(saved.max) && saved.max > 0) state.max = saved.max;
      state.ending = saved.ending === true;
    }

    function store() {
      try {
        sessionStorage.setItem(storageKey, JSON.stringify({
          cats: Array.from(state.cats), period: state.period, sort: state.sort,
          min: state.min, max: state.max, ending: state.ending
        }));
      } catch (_) { /* privat tilstand uden storage er helt fint */ }
    }

    const num = (card, key) => parseInt(card.dataset[key], 10) || 0;

    function compare(a, b) {
      switch (state.sort) {
        case 'oldest':     return num(a, 'ts') - num(b, 'ts');
        case 'price_asc':  return num(a, 'price') - num(b, 'price');
        case 'price_desc': return num(b, 'price') - num(a, 'price');
        case 'ending': {
          // Lots uden sluttidspunkt lægges sidst.
          const ea = num(a, 'endsin'), eb = num(b, 'endsin');
          return (ea > 0 ? ea : Infinity) - (eb > 0 ? eb : Infinity);
        }
        default: return 0;
      }
    }

    function activeList() {
      const list = [];
      state.cats.forEach(cat => list.push({
        key: 'cat:' + cat, label: catLabels[cat] || cat, kind: 'cat', value: cat
      }));
      if (state.period !== 'all') list.push({
        key: 'period', kind: 'period', value: state.period,
        label: state.period === 'today' ? 'Sendt i dag' : 'Sendt inden for 7 dage'
      });
      if (state.ending) list.push({
        key: 'ending', kind: 'ending', label: 'Slutter inden for 24 timer'
      });
      if (state.min > 0) list.push({
        key: 'min', kind: 'min', label: 'Fra ' + kr(state.min) + ' kr'
      });
      if (state.max < Infinity) list.push({
        key: 'max', kind: 'max', label: 'Til ' + kr(state.max) + ' kr'
      });
      return list;
    }

    function renderActive() {
      const list = activeList();

      if (activeBox) {
        activeBox.innerHTML = '';
        if (list.length) {
          const label = document.createElement('span');
          label.className = 'active-filters-label';
          label.textContent = 'Filtre';
          activeBox.appendChild(label);

          list.forEach(item => {
            const chip = document.createElement('span');
            chip.className = 'filter-item';
            chip.appendChild(document.createTextNode(item.label));

            const remove = document.createElement('button');
            remove.type = 'button';
            remove.dataset.remove = item.key;
            remove.setAttribute('aria-label', 'Fjern filteret ' + item.label);
            remove.innerHTML = '<svg class="ic" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
            chip.appendChild(remove);
            activeBox.appendChild(chip);
          });

          const clear = document.createElement('button');
          clear.type = 'button';
          clear.className = 'filter-clear';
          clear.dataset.remove = 'all';
          clear.textContent = 'Ryd alle';
          activeBox.appendChild(clear);

          activeBox.hidden = false;
        } else {
          activeBox.hidden = true;
        }
      }

      if (toggle) {
        const badge = $('.filter-count', toggle);
        if (badge) {
          badge.textContent = String(list.length);
          badge.hidden = list.length === 0;
        }
      }
    }

    // "I dag" er kalenderdagen i brugerens tid, ikke de sidste 24 timer. Et
    // fund fra kl. 23 i går hører til i går.
    const todayStart = () => {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.getTime() / 1000;
    };

    function apply(syncInputs) {
      const from = {
        all: -Infinity,
        today: todayStart(),
        week: todayStart() - 6 * 86400
      }[state.period];
      let visible = 0;

      cards.forEach(card => {
        const cat = card.dataset.cat;
        const price = num(card, 'price');
        const endsIn = num(card, 'endsin');
        const shown =
          (state.cats.size === 0 || state.cats.has(cat)) &&
          num(card, 'ts') >= from &&
          price >= state.min && price <= state.max &&
          (!state.ending || (endsIn > 0 && endsIn <= 86400));
        if (shown) { card.removeAttribute('data-hidden'); visible++; }
        else { card.setAttribute('data-hidden', '1'); }
      });

      if (counter) {
        counter.textContent = visible === total
          ? total + ' fund'
          : visible + ' af ' + total + ' fund';
      }
      if (noResults) noResults.hidden = visible !== 0;

      $$('.chip[data-cat]').forEach(chip => {
        chip.setAttribute('aria-pressed', String(state.cats.has(chip.dataset.cat)));
      });
      $$('.chip[data-period]').forEach(chip => {
        chip.setAttribute('aria-pressed', String(chip.dataset.period === state.period));
      });
      if (endingBtn) endingBtn.setAttribute('aria-pressed', String(state.ending));
      $$('.sort-btn').forEach(btn => {
        btn.setAttribute('aria-pressed', String(btn.dataset.sort === state.sort));
      });

      if (syncInputs) {
        if (minInput) minInput.value = state.min > 0 ? String(state.min) : '';
        if (maxInput) maxInput.value = isFinite(state.max) ? String(state.max) : '';
      }

      if (state.sort === 'newest') {
        if (flat) { flat.hidden = true; flat.innerHTML = ''; }
        groups.forEach(group => {
          group.hidden = !$$('.card', group).some(c => !c.hasAttribute('data-hidden'));
        });
      } else {
        groups.forEach(group => { group.hidden = true; });
        if (flat) {
          const shown = cards.filter(c => !c.hasAttribute('data-hidden')).slice().sort(compare);
          flat.innerHTML = '';
          shown.forEach(card => flat.appendChild(card.cloneNode(true)));
          flat.hidden = false;
        }
      }

      renderActive();
      store();
    }

    function reset() {
      state.cats.clear();
      state.period = 'all';
      state.sort = 'newest';
      state.min = 0;
      state.max = Infinity;
      state.ending = false;
      apply(true);
    }

    if (toggle && panel) {
      toggle.addEventListener('click', () => {
        const open = panel.hidden;
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
      });
    }

    $$('.chip[data-cat]').forEach(chip => {
      chip.addEventListener('click', () => {
        const cat = chip.dataset.cat;
        if (state.cats.has(cat)) state.cats.delete(cat);
        else state.cats.add(cat);
        apply(false);
      });
    });

    $$('.chip[data-period]').forEach(chip => {
      chip.addEventListener('click', () => {
        state.period = chip.dataset.period;
        apply(false);
      });
    });

    if (endingBtn) {
      endingBtn.addEventListener('click', () => {
        state.ending = !state.ending;
        apply(false);
      });
    }

    $$('.sort-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        state.sort = btn.dataset.sort;
        apply(false);
      });
    });

    if (minInput) {
      minInput.addEventListener('input', () => {
        state.min = parseInt(minInput.value, 10) || 0;
        apply(false);
      });
    }
    if (maxInput) {
      maxInput.addEventListener('input', () => {
        const parsed = parseInt(maxInput.value, 10);
        state.max = isNaN(parsed) ? Infinity : parsed;
        apply(false);
      });
    }

    if (resetBtn) resetBtn.addEventListener('click', reset);

    if (activeBox) {
      activeBox.addEventListener('click', event => {
        const btn = event.target.closest('[data-remove]');
        if (!btn) return;
        const key = btn.dataset.remove;
        if (key === 'all') { reset(); return; }
        if (key.indexOf('cat:') === 0) state.cats.delete(key.slice(4));
        else if (key === 'period') state.period = 'all';
        else if (key === 'ending') state.ending = false;
        else if (key === 'min') state.min = 0;
        else if (key === 'max') state.max = Infinity;
        apply(true);
      });
    }

    restore();
    apply(true);
  }

  /* ---- interesser: husk hvilke kategorier der var foldet ud -------------- */

  function initInterestBlocks() {
    const blocks = $$('details[data-cat-block]');
    if (!blocks.length) return;

    let open = [];
    try {
      open = JSON.parse(sessionStorage.getItem('interesser-aabne') || '[]');
    } catch (_) {
      open = [];
    }

    blocks.forEach(block => {
      if (open.indexOf(block.dataset.catBlock) !== -1) block.open = true;
      block.addEventListener('toggle', () => {
        const now = blocks.filter(b => b.open).map(b => b.dataset.catBlock);
        try {
          sessionStorage.setItem('interesser-aabne', JSON.stringify(now));
        } catch (_) { /* privat tilstand uden storage er helt fint */ }
      });
    });
  }

  /* ---- arkiv: vælg et filter og slippe for et ekstra klik ---------------- */

  function initArchiveForm() {
    $$('form[data-autosubmit] select, form[data-autosubmit] input[type=number]')
      .forEach(field => {
        // Samme model for alle filtre: rører man en kontrol, slår den igennem.
        // Fritekstfeltet kræver stadig Enter eller knappen.
        field.addEventListener('change', () => {
          if (field.form) field.form.submit();
        });
      });
  }

  /* ---- '/'-genvej til søgefeltet ---------------------------------------- */

  function initSearchShortcut() {
    // Autofokus er rart med tastatur og mus, men på en telefon åbner det
    // skærmtastaturet med det samme og skubber indholdet væk.
    const wide = window.matchMedia('(min-width: 800px) and (hover: hover)');
    if (wide.matches) {
      const field = $('input[data-search]');
      if (field && !field.value) field.focus();
    }

    document.addEventListener('keydown', event => {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
                     target.tagName === 'SELECT' || target.isContentEditable)) return;
      const field = $('input[data-search]');
      if (!field) return;
      event.preventDefault();
      field.focus();
      field.select();
    });
  }

  initFindPage();
  initInterestBlocks();
  initArchiveForm();
  initSearchShortcut();
})();
