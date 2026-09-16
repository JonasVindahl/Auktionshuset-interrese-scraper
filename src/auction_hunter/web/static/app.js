/* Filtrering, sortering og markering på fund-siderne.
 *
 * Kortene i #cards-container er sandheden. Ved anden sortering end 'nyeste'
 * klones de synlige kort ind i #flat-list; derfor scopes alle opslag til
 * containeren, ellers ville klonerne tælle med og listen vokse ved hvert klik.
 */

(function () {
  'use strict';

  const container = () => document.getElementById('cards-container');
  const srcCards = () => container() ? [...container().querySelectorAll('.card[data-cat]')] : [];
  const srcGroups = () => container() ? [...container().querySelectorAll('.date-group')] : [];

  let activeCats = new Set();
  let activePeriod = 'all';
  let activeSort = 'newest';
  let priceMin = 0;
  let priceMax = Infinity;
  let endingOnly = false;

  function applyFilters() {
    if (!container()) return;
    const now = Date.now();
    const periodMs = { all: Infinity, today: 86400e3, week: 7 * 86400e3 }[activePeriod] ?? Infinity;
    let visible = 0;

    srcCards().forEach(card => {
      const catOk = activeCats.size === 0 || activeCats.has(card.dataset.cat);
      const tsOk = (now - parseInt(card.dataset.ts, 10) * 1000) <= periodMs;
      const price = parseInt(card.dataset.price, 10) || 0;
      const priceOk = price >= priceMin && price <= priceMax;
      const endsIn = parseInt(card.dataset.endsin, 10) || 0;
      const endingOk = !endingOnly || (endsIn > 0 && endsIn <= 86400);
      const show = catOk && tsOk && priceOk && endingOk;
      show ? card.removeAttribute('data-hidden') : card.setAttribute('data-hidden', '1');
      if (show) visible++;
    });

    const noRes = document.getElementById('no-results');
    if (noRes) noRes.style.display = visible === 0 ? '' : 'none';

    const counter = document.getElementById('visible-count');
    if (counter) {
      const total = srcCards().length;
      counter.textContent = visible === total ? `${total} fund` : `${visible} af ${total} fund`;
    }
    applySort();
  }

  function applySort() {
    const flat = document.getElementById('flat-list');
    if (!container()) return;

    if (activeSort === 'newest') {
      if (flat) { flat.style.display = 'none'; flat.innerHTML = ''; }
      srcGroups().forEach(group => {
        const any = [...group.querySelectorAll('.card')].some(c => !c.hasAttribute('data-hidden'));
        group.style.display = any ? '' : 'none';
      });
      return;
    }

    srcGroups().forEach(group => { group.style.display = 'none'; });
    if (!flat) return;

    const num = (card, key) => parseInt(card.dataset[key], 10) || 0;
    const visible = srcCards().filter(c => !c.hasAttribute('data-hidden'));
    const sorted = visible.slice().sort((a, b) => {
      switch (activeSort) {
        case 'oldest': return num(a, 'ts') - num(b, 'ts');
        case 'price_asc': return num(a, 'price') - num(b, 'price');
        case 'price_desc': return num(b, 'price') - num(a, 'price');
        case 'ending': {
          // Lots uden sluttidspunkt sidst.
          const ea = num(a, 'endsin'), eb = num(b, 'endsin');
          return (ea > 0 ? ea : Infinity) - (eb > 0 ? eb : Infinity);
        }
        default: return 0;
      }
    });

    flat.innerHTML = '';
    sorted.forEach(card => flat.appendChild(card.cloneNode(true)));
    flat.querySelectorAll('.fb-btn').forEach(bindFeedback);
    flat.style.display = '';
  }

  async function sendFeedback(btn) {
    const card = btn.closest('.card');
    if (!card) return;
    const { lotId, catKey, action } = btn.dataset;
    const title = (card.querySelector('.title')?.textContent || '').trim();
    const next = card.dataset.feedback === action ? '' : action;

    // Opdatér med det samme; serveren er hurtigere end brugeren kan nå at se.
    document.querySelectorAll(`.card[data-lot="${CSS.escape(lotId)}"]`).forEach(c => {
      c.dataset.feedback = next;
      c.querySelectorAll('.fb-btn').forEach(b => {
        b.classList.toggle('active', next !== '' && b.dataset.action === next);
      });
    });

    try {
      await fetch('/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lot_id: lotId, category_key: catKey, action: next, title })
      });
    } catch (_) {
      // Markeringen står stadig visuelt; næste reload henter sandheden.
    }
  }

  function bindFeedback(btn) {
    btn.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      sendFeedback(btn);
    });
  }

  function resetFilters() {
    activeCats.clear();
    activePeriod = 'all';
    priceMin = 0;
    priceMax = Infinity;
    endingOnly = false;

    document.querySelectorAll('.chip[data-cat]').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.chip[data-period]').forEach(b =>
      b.classList.toggle('active', b.dataset.period === 'all'));
    document.getElementById('ending-chip')?.classList.remove('active');

    const min = document.getElementById('price-min');
    const max = document.getElementById('price-max');
    if (min) min.value = '';
    if (max) max.value = '';
    applyFilters();
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.chip[data-cat]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cat = btn.dataset.cat;
        if (activeCats.has(cat)) { activeCats.delete(cat); btn.classList.remove('active'); }
        else { activeCats.add(cat); btn.classList.add('active'); }
        applyFilters();
      });
    });

    document.querySelectorAll('.chip[data-period]').forEach(btn => {
      btn.addEventListener('click', () => {
        activePeriod = btn.dataset.period;
        document.querySelectorAll('.chip[data-period]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        applyFilters();
      });
    });

    const ending = document.getElementById('ending-chip');
    if (ending) ending.addEventListener('click', () => {
      endingOnly = !endingOnly;
      ending.classList.toggle('active', endingOnly);
      applyFilters();
    });

    document.querySelectorAll('.sort-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeSort = btn.dataset.sort;
        document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        applySort();
      });
    });

    const min = document.getElementById('price-min');
    const max = document.getElementById('price-max');
    if (min) min.addEventListener('input', () => { priceMin = parseInt(min.value) || 0; applyFilters(); });
    if (max) max.addEventListener('input', () => { priceMax = parseInt(max.value) || Infinity; applyFilters(); });

    document.getElementById('reset-btn')?.addEventListener('click', resetFilters);
    document.querySelectorAll('.fb-btn').forEach(bindFeedback);

    applyFilters();
  });
})();
