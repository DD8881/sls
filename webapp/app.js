const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const $ = (id) => document.getElementById(id);

let state = {
  city: localStorage.getItem('sls_city') || null,
  chain: null,
  store: null,
  category: null,
  subcategory: null,
  search: '',
  offset: 0,
  cities: [],
  index: null,
  products: [],
  subcategories: [],
  storesData: null,
  storeProductIds: null,
  filtered: [],
  userLat: null,
  userLng: null,
  geoSorted: false,
  geoCity: null,
};

const ITEMS_PER_PAGE = 30;
const CHAIN_LABELS = { silpo: 'Silpo', novus: 'Novus', metro: 'Metro', varus: 'Varus', atb: 'АТБ', fora: 'Fora', auchan: 'Ашан' };

async function fetchJSON(path) {
  const res = await fetch('/data/' + path);
  if (!res.ok) return null;
  return res.json();
}

function escapeHtml(t) {
  const d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}

// ---- City ----
let cityOpen = false;

async function loadCities() {
  state.cities = await fetchJSON('cities.json') || [];
  renderCityBtn();
}

function renderCityBtn() {
  const btn = $('city-btn');
  btn.textContent = state.city || 'Оберіть місто';
  btn.classList.toggle('has-selection', !!state.city);
}

function renderCityDropdown() {
  const dd = $('city-dropdown');
  if (!state.cities.length) {
    dd.innerHTML = '<div class="city-option">Немає міст</div>';
    return;
  }
  // Alphabetical (uk), with the geolocated city floated to the top.
  const ordered = [...state.cities].sort((a, b) => a.city.localeCompare(b.city, 'uk'));
  if (state.geoCity) {
    const i = ordered.findIndex(c => c.city === state.geoCity);
    if (i > 0) ordered.unshift(ordered.splice(i, 1)[0]);
  }
  // Geo button (mirrors the store sheet's "За відстанню"): shows the detected
  // city, or offers to detect it.
  const geoActive = !!state.geoCity;
  const geoLabel = geoActive
    ? `📍 Поряд: ${escapeHtml(state.geoCity)} ✓`
    : '📍 Визначити моє місто';
  let html = `<div class="city-geo-row">
    <button type="button" class="geo-sort-btn ${geoActive ? 'active' : ''}" id="city-geo-btn">${geoLabel}</button>
  </div>`;
  for (const c of ordered) {
    const active = state.city === c.city ? 'active' : '';
    const isGeo = state.geoCity === c.city;
    html += `<div class="city-option ${active}" data-city="${escapeHtml(c.city)}">
      <span>${isGeo ? '📍 ' : ''}${escapeHtml(c.city)}</span><span class="city-count">${c.store_cnt} маг.</span>
    </div>`;
  }
  dd.innerHTML = html;
  const gbtn = $('city-geo-btn');
  if (gbtn) gbtn.onclick = (e) => { e.stopPropagation(); requestCityGeo(); };
  dd.querySelectorAll('.city-option').forEach(opt => {
    opt.onclick = () => selectCity(opt.dataset.city, true);  // explicit pick → geo won't override
  });
}

function toggleCityDD() {
  cityOpen = !cityOpen;
  $('city-btn').classList.toggle('open', cityOpen);
  $('city-dropdown').classList.toggle('open', cityOpen);
  if (cityOpen) renderCityDropdown();
}

function closeCityDD() {
  cityOpen = false;
  $('city-btn').classList.remove('open');
  $('city-dropdown').classList.remove('open');
}

$('city-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  toggleCityDD();
});

// ---- Chains ----
function getChainCounts() {
  // Counts reflect the active search (the only cross-chain filter) so each tab
  // shows how many matching products that chain has. Every chain present stays
  // listed (with 0) so the active tab never disappears mid-search.
  const counts = {};
  const q = state.search ? state.search.toLowerCase() : null;
  for (const p of state.products) {
    if (!(p.ch in counts)) counts[p.ch] = 0;
    if (q && !p.t.toLowerCase().includes(q)) continue;
    counts[p.ch] += 1;
  }
  return counts;
}

function renderChains() {
  const el = $('store-tabs');
  if (!state.products.length) { el.innerHTML = ''; return; }
  const counts = getChainCounts();
  const chains = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  let html = `<button class="store-tab ${!state.chain ? 'active' : ''}" data-chain="">Всі</button>`;
  for (const ch of chains) {
    const active = state.chain === ch ? 'active' : '';
    const label = CHAIN_LABELS[ch] || ch;
    html += `<button class="store-tab ${active}" data-chain="${ch}">${label}<span class="count">${counts[ch]}</span></button>`;
  }
  el.innerHTML = html;
  el.querySelectorAll('.store-tab').forEach(btn => {
    btn.onclick = () => {
      state.chain = btn.dataset.chain || null;
      state.store = null;
      state.storeProductIds = null;
      state.subcategory = null;
      state.offset = 0;
      renderChains();
      renderStoreFilter();
      applyFilters();
    };
  });
}

// ---- Store selector (bottom sheet) ----
function getStoreDisplayName(store) {
  return store.name || store.addr || '';
}

function getStoresForChain(chain) {
  if (!state.index || !state.index.stores) return [];
  const entries = Object.entries(state.index.stores);
  const filtered = chain
    ? entries.filter(([, s]) => s.chain === chain)
    : entries;
  return filtered.map(([id, s]) => ({ id, ...s, display: getStoreDisplayName(s), lat: s.lat, lng: s.lng }))
    .sort((a, b) => a.display.localeCompare(b.display, 'uk'));
}

function renderStoreFilter() {
  const wrap = $('store-filter-wrap');
  const stores = getStoresForChain(state.chain);
  if (stores.length < 2 || !state.products.length) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = 'block';
  const btn = $('store-filter-btn');
  if (state.store) {
    const s = state.index.stores[state.store];
    btn.textContent = s ? getStoreDisplayName(s) : state.store;
    btn.classList.add('has-selection');
  } else {
    btn.textContent = 'Всі магазини';
    btn.classList.remove('has-selection');
  }
}

function openStoreSheet() {
  $('sheet-overlay').classList.add('open');
  $('store-sheet').classList.add('open');
  $('sheet-search-input').value = '';
  renderStoreList('');
  setTimeout(() => $('sheet-search-input').focus(), 100);
}

function closeStoreSheet() {
  $('sheet-overlay').classList.remove('open');
  $('store-sheet').classList.remove('open');
}

function haversineKm(lat1, lng1, lat2, lng2) {
  const toRad = x => x * Math.PI / 180;
  const R = 6371;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function formatDist(km) {
  if (km < 1) return Math.round(km * 1000) + ' м';
  return km.toFixed(1) + ' км';
}

// Resolve the user's coordinates via Telegram's LocationManager (preferred) or
// the browser geolocation API. Caches the result so repeat calls don't re-prompt.
// opts.prompt: if false (default), never trigger the system permission prompt —
// only resolve when access is already granted (used for the silent auto-detect
// on open). Always fails after a timeout so the UI never hangs.
function getUserPosition(onOk, onErr, opts) {
  opts = opts || {};
  if (state.userLat != null) { onOk(state.userLat, state.userLng); return; }

  let done = false;
  const ok = (lat, lng) => { if (done) return; done = true; clearTimeout(timer); state.userLat = lat; state.userLng = lng; onOk(lat, lng); };
  const fail = () => { if (done) return; done = true; clearTimeout(timer); onErr && onErr(); };
  // Access not granted and can't be (re)prompted inline → caller should guide
  // the user to settings. Falls back to onErr when no onDenied is given.
  const denied = () => { if (done) return; done = true; clearTimeout(timer); (opts.onDenied || onErr) && (opts.onDenied || onErr)(); };
  const timer = setTimeout(fail, 12000);  // getLocation can silently never call back

  const lm = tg.LocationManager;
  if (lm) {
    lm.init(() => {
      if (lm.isAccessGranted) {
        lm.getLocation((loc) => loc ? ok(loc.latitude, loc.longitude) : fail());
      } else if (opts.prompt) {
        // Explicit tap: let Telegram show its own prompt (or, when iOS-level
        // location is off, its "enable in Settings" alert with a Параметри
        // button). Returns null if the user dismisses it.
        lm.getLocation((loc) => loc ? ok(loc.latitude, loc.longitude) : denied());
      } else {
        fail();  // silent auto-detect: never prompt
      }
    });
  } else if ('geolocation' in navigator) {
    const getPos = () => navigator.geolocation.getCurrentPosition(
      (pos) => ok(pos.coords.latitude, pos.coords.longitude), fail,
      { enableHighAccuracy: false, timeout: 10000 }
    );
    if (!opts.prompt && navigator.permissions) {
      navigator.permissions.query({ name: 'geolocation' })
        .then(p => p.state === 'granted' ? getPos() : fail()).catch(fail);
    } else {
      getPos();
    }
  } else {
    fail();
  }
}

// Nearest city (by centroid) within 50 km, or null.
const GEO_CITY_MAX_KM = 50;
function nearestCity(lat, lng) {
  let best = null, bestD = Infinity;
  for (const c of state.cities) {
    if (c.lat == null || c.lng == null) continue;
    const d = haversineKm(lat, lng, c.lat, c.lng);
    if (d < bestD) { bestD = d; best = c.city; }
  }
  return (best && bestD <= GEO_CITY_MAX_KM) ? best : null;
}

function selectCity(city, isManual) {
  state.city = city;
  localStorage.setItem('sls_city', city);
  if (isManual) localStorage.setItem('sls_city_manual', '1');
  else localStorage.removeItem('sls_city_manual');  // geo selection follows the user
  closeCityDD();
  renderCityBtn();
  loadCityData();
}

// Auto on open: float the user's city to the top and select it unless they
// picked one manually. Best-effort, silent.
function detectCityByGeo() {
  if (!state.cities.length) return;
  getUserPosition((lat, lng) => {
    const best = nearestCity(lat, lng);
    if (!best) return;
    state.geoCity = best;
    if (cityOpen) renderCityDropdown();
    if (!localStorage.getItem('sls_city_manual') && state.city !== best) {
      selectCity(best, false);
    }
  });
}

// Manual trigger from the "📍" button in the city dropdown.
function requestCityGeo() {
  if (state.geoCity) { selectCity(state.geoCity, false); return; }
  const btn = $('city-geo-btn');
  if (btn) { btn.textContent = '...'; btn.disabled = true; }
  getUserPosition(
    (lat, lng) => {
      const best = nearestCity(lat, lng);
      if (best) { state.geoCity = best; selectCity(best, false); }
      else cityGeoFail('Місто не знайдено');
    },
    () => cityGeoFail('Немає доступу'),
    { prompt: true, onDenied: () => cityGeoFail('Дозвольте локацію в налаштуваннях') }
  );
}
function cityGeoFail(msg) {
  const btn = $('city-geo-btn');
  if (!btn) return;
  btn.textContent = msg;
  btn.disabled = true;
  setTimeout(() => { if (cityOpen) renderCityDropdown(); }, 2000);  // restore so it can be retried
}

function requestGeoSort() {
  const btn = $('geo-sort-btn');
  if (!btn) return;
  btn.textContent = '...';
  btn.disabled = true;
  const resetBtn = (msg) => {
    btn.textContent = msg;
    setTimeout(() => { btn.textContent = 'За відстанню'; btn.disabled = false; }, 2000);
  };
  getUserPosition(
    () => { state.geoSorted = true; renderStoreList($('sheet-search-input').value.trim()); },
    () => resetBtn('Немає доступу'),
    { prompt: true, onDenied: () => resetBtn('Дозвольте в налаштуваннях') }
  );
}

function renderStoreList(query) {
  const list = $('sheet-list');
  let stores = getStoresForChain(state.chain);
  if (query) {
    const q = query.toLowerCase();
    stores = stores.filter(s =>
      s.display.toLowerCase().includes(q) ||
      (s.addr && s.addr.toLowerCase().includes(q))
    );
  }

  if (state.geoSorted && state.userLat != null) {
    for (const s of stores) {
      s._dist = (s.lat != null && s.lng != null)
        ? haversineKm(state.userLat, state.userLng, s.lat, s.lng)
        : Infinity;
    }
    stores.sort((a, b) => a._dist - b._dist);
  }

  const geoAvailable = 'geolocation' in navigator;
  const geoActive = state.geoSorted && state.userLat != null;

  let html = `<div class="sheet-store ${!state.store ? 'active' : ''}" data-sid="">
    <div class="sheet-store-info"><span class="sheet-store-name">Всі магазини</span></div>
    ${geoAvailable ? `<button type="button" class="geo-sort-btn ${geoActive ? 'active' : ''}" id="geo-sort-btn">${geoActive ? 'За відстанню ✓' : 'За відстанню'}</button>` : ''}
  </div>`;
  for (const s of stores) {
    const active = state.store === s.id ? 'active' : '';
    const chainLabel = CHAIN_LABELS[s.chain] || s.chain;
    const showChain = !state.chain;
    const distHtml = (geoActive && s._dist !== Infinity) ? `<span class="sheet-store-dist">${formatDist(s._dist)}</span>` : '';
    html += `<div class="sheet-store ${active}" data-sid="${escapeHtml(s.id)}">
      <div class="sheet-store-info">
        <span class="sheet-store-name">${showChain ? `<span class="chain-badge ${s.chain}" style="margin-right:6px;font-size:10px">${chainLabel}</span>` : ''}${escapeHtml(s.display)}</span>
        ${s.addr !== s.name ? `<span class="sheet-store-addr">${escapeHtml(s.addr)}</span>` : ''}
      </div>
      ${distHtml}
    </div>`;
  }
  list.innerHTML = html;

  const geoBtn = $('geo-sort-btn');
  if (geoBtn) {
    geoBtn.onclick = (e) => {
      e.stopPropagation();
      if (state.geoSorted) {
        state.geoSorted = false;
        renderStoreList(query);
      } else {
        requestGeoSort();
      }
    };
  }

  list.querySelectorAll('.sheet-store').forEach(el => {
    el.onclick = () => {
      const sid = el.dataset.sid || null;
      state.store = sid;
      state.storeProductIds = null;
      state.offset = 0;
      closeStoreSheet();
      renderStoreFilter();
      loadStoreFilterAndApply();
    };
  });
}

async function loadStoreFilterAndApply() {
  if (!state.store || !state.category) {
    state.storeProductIds = null;
    applyFilters();
    return;
  }
  if (!state.storesData) {
    $('loading').style.display = 'flex';
    state.storesData = await fetchJSON(`${encodeURIComponent(state.city)}/${state.category}_stores.json`);
    $('loading').style.display = 'none';
  }
  if (state.storesData) {
    const ids = new Set();
    for (const [pid, entries] of Object.entries(state.storesData)) {
      if (entries.some(e => e.s === state.store)) ids.add(parseInt(pid));
    }
    state.storeProductIds = ids;
  }
  applyFilters();
}

$('store-filter-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  openStoreSheet();
});

$('sheet-overlay').addEventListener('click', closeStoreSheet);

$('sheet-search-input').addEventListener('input', (e) => {
  renderStoreList(e.target.value.trim());
});

// ---- Categories ----
let catOpen = false;

function getCatTitle() {
  const cats = (state.index && state.index.categories) || [];
  const sel = cats.find(c => c.slug === state.category);
  if (!sel) return 'Всі категорії';
  if (state.subcategory) return `${sel.title} → ${state.subcategory}`;
  return `${sel.title} (${sel.cnt})`;
}

function renderCategories() {
  const wrap = $('category-wrap');
  const btn = $('category-btn');
  const cats = (state.index && state.index.categories) || [];
  if (!cats.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  btn.textContent = getCatTitle();
  btn.classList.toggle('has-selection', !!state.category);
}

function renderCatDropdownContent() {
  const dd = $('category-dropdown');
  if (state.category && state.products.length) {
    renderSubcatList(dd);
  } else {
    renderCatList(dd);
  }
}

function renderCatList(dd) {
  let html = `<div class="category-dropdown-search"><input type="text" id="cat-search" placeholder="Пошук категорії..."></div>`;
  const cats = (state.index && state.index.categories) || [];
  html += `<div class="cat-option ${!state.category ? 'active' : ''}" data-slug="">Всі категорії</div>`;
  for (const c of cats) {
    const active = state.category === c.slug ? 'active' : '';
    html += `<div class="cat-option ${active}" data-slug="${c.slug}">
      <span>${escapeHtml(c.title)}</span><span class="cat-count">${c.cnt}</span>
    </div>`;
  }
  dd.innerHTML = html;
  dd.querySelectorAll('.cat-option').forEach(opt => {
    opt.onclick = () => {
      const slug = opt.dataset.slug || null;
      closeCatDD();
      if (slug !== state.category) {
        state.category = slug;
        state.subcategory = null;
        state.offset = 0;
        renderCategories();
        loadCategoryProducts();
      }
    };
  });
  const si = $('cat-search');
  if (si) {
    si.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim();
      const all = (state.index && state.index.categories) || [];
      const filtered = q ? all.filter(c => c.title.toLowerCase().includes(q)) : all;
      dd.innerHTML = '';
      renderCatList(dd);
      // refilter after re-render
      const opts = dd.querySelectorAll('.cat-option');
      opts.forEach(o => {
        if (q && !o.textContent.toLowerCase().includes(q)) o.style.display = 'none';
      });
      const ni = $('cat-search');
      if (ni) { ni.value = e.target.value; ni.focus(); }
    });
  }
}

function renderSubcatList(dd) {
  const cats = (state.index && state.index.categories) || [];
  const sel = cats.find(c => c.slug === state.category);

  let items = state.products;
  if (state.chain) items = items.filter(p => p.ch === state.chain);
  const counts = {};
  for (const p of items) {
    if (p.sub) counts[p.sub] = (counts[p.sub] || 0) + 1;
  }
  const subs = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  let html = `<div class="cat-back" id="cat-back-btn">Всі категорії</div>`;
  html += `<div class="cat-option ${!state.subcategory ? 'active' : ''}" data-slug="${state.category}" data-sub="">
    <span>Все в «${escapeHtml(sel ? sel.title : '')}»</span><span class="cat-count">${items.length}</span>
  </div>`;
  for (const [title, cnt] of subs) {
    const active = state.subcategory === title ? 'active' : '';
    html += `<div class="subcat-option ${active}" data-sub="${escapeHtml(title)}">
      <span>${escapeHtml(title)}</span><span class="cat-count">${cnt}</span>
    </div>`;
  }
  dd.innerHTML = html;

  $('cat-back-btn').onclick = () => {
    state.category = null;
    state.subcategory = null;
    state.products = [];
    state.filtered = [];
    state.offset = 0;
    closeCatDD();
    renderCategories();
    renderChains();
    renderProducts();
  };

  dd.querySelector('.cat-option').onclick = () => {
    state.subcategory = null;
    state.offset = 0;
    closeCatDD();
    renderCategories();
    applyFilters();
  };

  dd.querySelectorAll('.subcat-option').forEach(opt => {
    opt.onclick = () => {
      state.subcategory = opt.dataset.sub || null;
      state.offset = 0;
      closeCatDD();
      renderCategories();
      applyFilters();
    };
  });
}

function toggleCatDD() {
  catOpen = !catOpen;
  $('category-btn').classList.toggle('open', catOpen);
  $('category-dropdown').classList.toggle('open', catOpen);
  if (catOpen) renderCatDropdownContent();
}

function closeCatDD() {
  catOpen = false;
  $('category-btn').classList.remove('open');
  $('category-dropdown').classList.remove('open');
}

$('category-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  toggleCatDD();
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.city-select-wrap')) closeCityDD();
  if (!e.target.closest('.category-select-wrap')) closeCatDD();
  if (!e.target.closest('.sheet') && !e.target.closest('.store-filter-btn')) closeStoreSheet();
});


// ---- Products ----
function renderProduct(p) {
  const chainLabel = CHAIN_LABELS[p.ch] || p.ch;
  const img = p.img
    ? `<img class="product-img" src="${p.img}" alt="" loading="lazy" onerror="this.classList.add('placeholder');this.src='';">`
    : `<div class="product-img placeholder"></div>`;

  let priceHtml = `<span class="price-new">${p.p.toFixed(2)} ₴</span>`;
  if (p.op) priceHtml += `<span class="price-old">${p.op.toFixed(2)} ₴</span>`;
  if (p.d) priceHtml += `<span class="discount-badge">-${p.d}%</span>`;

  let meta = `<span class="chain-badge ${p.ch}">${chainLabel}</span>`;
  if (p.sc >= 1) meta += `<span class="store-count-badge" data-pid="${p.id}">${p.sc} маг.</span>`;
  if (p.end) meta += `<span class="promo-date">до ${p.end}</span>`;

  return `
    <div class="product-card" ${p.url ? `data-url="${escapeHtml(p.url)}"` : ''}>
      ${img}
      <div class="product-info">
        <div class="product-title">${escapeHtml(p.t)}</div>
        <div class="product-price-row">${priceHtml}</div>
        <div class="product-meta">${meta}</div>
        <div class="product-stores-list" id="psl-${p.id}" style="display:none"></div>
      </div>
    </div>`;
}

function bindCardEvents(container) {
  container.querySelectorAll('.store-count-badge').forEach(b => {
    b.onclick = (e) => { e.stopPropagation(); toggleStores(parseInt(b.dataset.pid)); };
  });
  container.querySelectorAll('.product-card[data-url]').forEach(card => {
    card.onclick = () => tg.openLink(card.dataset.url);
  });
}

function renderProducts() {
  const grid = $('products-grid');
  const slice = state.filtered.slice(0, state.offset + ITEMS_PER_PAGE);
  grid.innerHTML = slice.map(renderProduct).join('');
  state.offset = slice.length;

  bindCardEvents(grid);

  $('loading').style.display = 'none';
  $('empty-state').style.display = state.filtered.length === 0 ? 'block' : 'none';
  $('load-more').style.display = state.offset < state.filtered.length ? 'block' : 'none';
}

function appendProducts() {
  const grid = $('products-grid');
  const next = state.filtered.slice(state.offset, state.offset + ITEMS_PER_PAGE);
  const tmp = document.createElement('div');
  tmp.innerHTML = next.map(renderProduct).join('');
  bindCardEvents(tmp);
  while (tmp.firstChild) grid.appendChild(tmp.firstChild);
  state.offset += next.length;

  $('load-more').style.display = state.offset < state.filtered.length ? 'block' : 'none';
}

async function toggleStores(pid) {
  const el = $(`psl-${pid}`);
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }

  el.innerHTML = '<div class="stores-loading">Завантаження...</div>';
  el.style.display = 'block';

  if (!state.storesData && state.category) {
    state.storesData = await fetchJSON(`${encodeURIComponent(state.city)}/${state.category}_stores.json`);
  }

  const stores = state.index ? state.index.stores : {};
  const entries = (state.storesData && state.storesData[String(pid)]) || [];

  if (!entries.length) {
    el.innerHTML = '<div class="stores-loading">Немає даних</div>';
    return;
  }

  const product = state.products.find(p => p.id === pid);
  const basePrice = product ? product.p : 0;

  let html = '';
  for (const e of entries) {
    const store = stores[e.s] || {};
    const price = e.p !== undefined ? e.p : basePrice;
    const chainLabel = CHAIN_LABELS[store.chain] || store.chain || '';
    html += `<div class="store-row">
      <span class="store-row-chain ${store.chain || ''}">${chainLabel}</span>
      <span class="store-row-addr">${escapeHtml(store.addr || store.name || e.s)}</span>
      <span class="store-row-price">${price.toFixed(2)} ₴</span>
    </div>`;
  }
  el.innerHTML = html;
}

// ---- Data loading ----
async function loadCityData() {
  if (!state.city) return;
  state.chain = null;
  state.store = null;
  state.storeProductIds = null;
  state.category = null;
  state.subcategory = null;
  state.search = '';
  state.offset = 0;
  $('search-input').value = '';

  $('loading').style.display = 'flex';
  state.index = await fetchJSON(`${encodeURIComponent(state.city)}/index.json`);
  renderCategories();
  renderStoreFilter();

  state.products = [];
  state.filtered = [];
  renderChains();
  renderProducts();
}

async function loadCategoryProducts() {
  if (!state.city || !state.category) {
    state.products = [];
    state.storesData = null;
    state.storeProductIds = null;
    state.subcategory = null;
    renderChains();
    applyFilters();
    return;
  }

  $('loading').style.display = 'flex';
  $('products-grid').innerHTML = '';
  state.storesData = null;
  state.storeProductIds = null;
  state.subcategory = null;

  const data = await fetchJSON(`${encodeURIComponent(state.city)}/${state.category}.json`);
  state.products = (data && data.products) || [];
  state.offset = 0;
  renderChains();
  if (state.store) {
    await loadStoreFilterAndApply();
  } else {
    applyFilters();
  }
}

function applyFilters() {
  let items = state.products;

  if (state.chain) {
    items = items.filter(p => p.ch === state.chain);
  }

  if (state.store && state.storeProductIds) {
    items = items.filter(p => state.storeProductIds.has(p.id));
  }

  if (state.subcategory) {
    items = items.filter(p => p.sub === state.subcategory);
  }

  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter(p => p.t.toLowerCase().includes(q));
  }

  items.sort((a, b) => (b.d || 0) - (a.d || 0));

  state.filtered = items;
  state.offset = 0;
  renderProducts();
}

// Search
let searchTimeout;
$('search-input').addEventListener('input', (e) => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    state.search = e.target.value.trim();
    state.offset = 0;
    renderChains();
    applyFilters();
  }, 200);
});

$('load-more-btn').addEventListener('click', () => appendProducts());

// Init
(async () => {
  await loadCities();
  if (state.city) {
    await loadCityData();
  }
  detectCityByGeo();  // float the user's city to the top (and select it if none chosen)
})();
