(() => {
  'use strict';
  const cfg = window.RIPLET_CONFIG || {};
  const unavailableMessage = 'The downloader is disconnected. Please try again later.';
  const $ = selector => document.querySelector(selector);
  const urlInput = $('#media-url');
  const form = $('#converter-form');
  const format = $('#format');
  const quality = $('#quality');
  const ripButton = $('#convert-btn');
  const messageWrap = $('#inline-info');
  const message = $('#status-message');
  const cancelButton = $('#cancel-job');
  const videoFormats = new Set(['MP4', 'WebM']);
  const freeFormats = [['MP3','MP3'], ['MP4','MP4']];
  const supporterFormats = [...freeFormats, ['WAV','WAV']];
  const freeAudioQualities = [['128','128 kbps'], ['192','192 kbps']];
  const supporterAudioQualities = [...freeAudioQualities, ['320','320 kbps']];
  const freeVideoQualities = [['720','720p'], ['1080','1080p']];
  const supporterVideoQualities = [...freeVideoQualities, ['2160','4K']];
  let supporterActive = false;
  let supporterKey = '';

  const tickSvg = '<svg class="check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
  const pickers = {};
  let openPicker = null;
  let activeController = null;
  let activeJobId = '';

  function announce(text = '', kind = 'info') {
    message.textContent = text;
    messageWrap.classList.toggle('has-message', Boolean(text));
    messageWrap.dataset.kind = kind;
  }
  function busy(on) {
    ripButton.disabled = on;
    ripButton.querySelector('.rip-label').textContent = on ? 'Ripping…' : 'Rip!';
    ripButton.setAttribute('aria-busy', String(on));
    cancelButton.hidden = !on;
  }
  function selectedOptions(picker) { return [...picker.menu.querySelectorAll('[role="option"]')]; }
  function syncPicker(picker) {
    const selected = [...picker.select.options].find(x => x.value === picker.select.value);
    const fullLabel = selected?.textContent || '';
    picker.value.textContent = picker.id === 'quality' ? fullLabel.replace(/ kbps$/, 'k') : fullLabel;
    picker.trigger.title = fullLabel;
    selectedOptions(picker).forEach(el => el.setAttribute('aria-selected', String(el.dataset.value === picker.select.value)));
  }
  function closePicker(picker, returnFocus = false) {
    picker.menu.hidden = true;
    picker.trigger.setAttribute('aria-expanded', 'false');
    if (openPicker === picker) openPicker = null;
    if (returnFocus) picker.trigger.focus();
  }
  function openDropdown(picker) {
    if (openPicker && openPicker !== picker) closePicker(openPicker);
    picker.menu.hidden = false;
    picker.trigger.setAttribute('aria-expanded', 'true');
    openPicker = picker;
    const selected = selectedOptions(picker).find(x => x.dataset.value === picker.select.value);
    (selected || selectedOptions(picker)[0])?.focus();
  }
  function chooseOption(picker, value) {
    const changed = picker.select.value !== value;
    picker.select.value = value;
    syncPicker(picker);
    closePicker(picker, true);
    if (changed) picker.select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function optionKeydown(event, picker) {
    const options = selectedOptions(picker);
    const current = options.indexOf(document.activeElement);
    let next = current;
    switch (event.key) {
      case 'ArrowDown': next = (current + 1) % options.length; break;
      case 'ArrowUp': next = (current - 1 + options.length) % options.length; break;
      case 'Home': next = 0; break;
      case 'End': next = options.length - 1; break;
      case 'Enter': case ' ': event.preventDefault(); chooseOption(picker, document.activeElement.dataset.value); return;
      case 'Escape': event.preventDefault(); closePicker(picker, true); return;
      case 'Tab': closePicker(picker); return;
      default: return;
    }
    event.preventDefault(); options[next]?.focus();
  }
  function setItems(picker, items, value) {
    picker.select.replaceChildren(...items.map(([id, name]) => {
      const o = document.createElement('option'); o.value = id; o.textContent = name; return o;
    }));
    picker.select.value = value;
    picker.menu.replaceChildren(...items.map(([id, name]) => {
      const el = document.createElement('div');
      el.className = 'dropdown-option';
      el.id = `${picker.id}-item-${id.toLowerCase()}`;
      el.role = 'option'; el.tabIndex = -1; el.dataset.value = id;
      const title = document.createElement('span'); title.textContent = name;
      el.append(title); el.insertAdjacentHTML('beforeend', tickSvg);
      el.addEventListener('click', () => chooseOption(picker, id));
      el.addEventListener('keydown', ev => optionKeydown(ev, picker));
      return el;
    }));
    syncPicker(picker);
  }
  function createPicker(id) {
    const picker = {
      id, select: $(`#${id}`), menu: $(`#${id}-options`),
      trigger: $(`#${id}-trigger`), value: $(`#${id}-selection`),
    };
    picker.trigger.addEventListener('click', () => openPicker === picker ? closePicker(picker, true) : openDropdown(picker));
    picker.trigger.addEventListener('keydown', ev => {
      if (['Enter',' ','ArrowDown','ArrowUp'].includes(ev.key)) { ev.preventDefault(); if (openPicker !== picker) openDropdown(picker); }
      if (ev.key === 'Escape' && openPicker === picker) { ev.preventDefault(); closePicker(picker, true); }
    });
    picker.select.addEventListener('change', () => syncPicker(picker));
    pickers[id] = picker;
  }
  createPicker('format'); createPicker('quality');
  const requestedFormat = new URLSearchParams(location.search).get('format')?.toUpperCase();
  $('.brand').addEventListener('click', ev => {
    ev.preventDefault();
    window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  });
  ripButton.addEventListener('click', () => {
    if (typeof navigator.vibrate === 'function') navigator.vibrate(12);
  });
  setItems(pickers.format, freeFormats, freeFormats.some(([value]) => value === requestedFormat) ? requestedFormat : 'MP3');
  function updateQuality() {
    const isVideo = videoFormats.has(format.value);
    const items = isVideo ? (supporterActive ? supporterVideoQualities : freeVideoQualities) : (supporterActive ? supporterAudioQualities : freeAudioQualities);
    const chosen = items.some(([value]) => value === quality.value) ? quality.value : (isVideo ? '720' : '128');
    setItems(pickers.quality, items, chosen);
    $('#quality-label').textContent = isVideo ? 'Video resolution' : 'Audio quality';
    $('#format-type-icon').innerHTML = isVideo
      ? '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m10 9 5 3-5 3V9Z"/>'
      : '<path d="M9 18V6l12-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>';
    $('#quality-type-icon').innerHTML = isVideo
      ? '<path d="M5 4v16M12 4v16M19 4v16"/><rect x="2.5" y="8" width="5" height="5" rx="1.5" fill="var(--field)"/><rect x="9.5" y="13" width="5" height="5" rx="1.5" fill="var(--field)"/><rect x="16.5" y="6" width="5" height="5" rx="1.5" fill="var(--field)"/>'
      : '<path d="M4 13v-2m4 6V7m4 13V4m4 13V7m4 6v-2"/>';
    $('#format-heading').textContent = format.value;
    document.title = `Riplet • YouTube to ${format.value} Converter (Ad-Free)`;
    announce();
  }
  function updateEntitlement(active) {
    supporterActive = active;
    const current = format.value;
    const items = active ? supporterFormats : freeFormats;
    setItems(pickers.format, items, items.some(([value]) => value === current) ? current : 'MP3');
    updateQuality();
    $('#support-show-activate').textContent = active ? 'Your supporter access is active' : 'Already have a key? Activate it';
  }
  format.addEventListener('change', updateQuality);
  document.addEventListener('pointerdown', ev => {
    if (openPicker && !openPicker.trigger.contains(ev.target) && !openPicker.menu.contains(ev.target)) closePicker(openPicker);
  });
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && openPicker) closePicker(openPicker, true); });
  updateQuality();

  // The frontend never stores or handles the converted binary; the NAS streams it directly.
  const rawApiBase = String(cfg.apiBase || '').trim().replace(/\/+$/, '');
  function apiBase() {
    if (!rawApiBase || rawApiBase.includes('YOUR_')) throw Error(unavailableMessage);
    let parsed;
    try { parsed = new URL(rawApiBase); } catch { throw Error(unavailableMessage); }
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.pathname !== '/' || (location.protocol === 'https:' && parsed.protocol !== 'https:')) {
      throw Error(unavailableMessage);
    }
    return parsed.origin;
  }
  function validatedVideoUrl(input) {
    try {
      const parsed = new URL(input);
      const allowed = new Set(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com','youtu.be','www.youtu.be']);
      return parsed.protocol === 'https:' && !parsed.username && !parsed.password && !parsed.port && allowed.has(parsed.hostname.toLowerCase());
    } catch { return false; }
  }
  function apiPath(path, base) {
    const parsed = new URL(path, base);
    if (parsed.origin !== base || !['http:', 'https:'].includes(parsed.protocol)) throw Error(unavailableMessage);
    return parsed.href;
  }
  async function errorFor(response) {
    if (response.status === 400 || response.status === 401) {
      const data = await response.json().catch(() => ({}));
      if (/requires an active supporter key/i.test(data.error || '')) return new Error(data.error);
    }
    if ([408, 429, 502, 503, 504].includes(response.status) || response.status >= 500) return new Error(unavailableMessage);
    return new Error('This video could not be converted. Please try another link.');
  }
  async function delay(ms, signal) {
    if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
      const onAbort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }
  async function poll(id, base, signal) {
    for (let attempt = 0; attempt < 18000; attempt++) {
      await delay(1500, signal);
      const response = await fetch(apiPath(`/api/jobs/${encodeURIComponent(id)}`, base), { signal, cache: 'no-store', credentials: 'omit' });
      if (!response.ok) throw await errorFor(response);
      const job = await response.json();
      if (job.status === 'ready') return job;
      if (job.status === 'failed') {
        const detail = String(job.error || '');
        throw Error(/^Video is longer than the \d+-minute limit\.$/.test(detail)
          ? detail : 'This video could not be converted. Please try another link.');
      }
      if (job.status !== 'working') throw Error(unavailableMessage);
      const phase = String(job.phase || '').toLowerCase();
      announce(phase.includes('metadata') ? 'Checking the video…' : phase.includes('download') ? 'Downloading the source…' : phase.includes('convert') || phase.includes('encode') ? 'Converting your file…' : 'Ripping your file…');
    }
    throw Error(unavailableMessage);
  }
  async function runConversion(ev) {
    ev.preventDefault();
    if (activeController) return;
    announce();
    const userUrl = urlInput.value.trim();
    if (!validatedVideoUrl(userUrl)) { announce('Enter a valid HTTPS YouTube video or Shorts URL.', 'error'); urlInput.focus(); return; }
    let base;
    try { base = apiBase(); } catch (err) { announce(err.message, 'error'); return; }
    const controller = new AbortController();
    activeController = controller;
    activeJobId = '';
    busy(true);
    announce('Starting your rip…');
    try {
      const response = await fetch(apiPath('/api/jobs', base), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(supporterActive && supporterKey ? { 'X-Riplet-Key': supporterKey } : {}) },
        credentials: 'omit', cache: 'no-store', signal: controller.signal,
        body: JSON.stringify({ url: userUrl, format: format.value.toLowerCase(), quality: quality.value }),
      });
      if (!response.ok) throw await errorFor(response);
      const start = await response.json();
      if (!start.id || !/^[0-9a-f-]{36}$/i.test(start.id)) throw Error(unavailableMessage);
      activeJobId = start.id;
      announce('Processing your file…');
      const job = await poll(start.id, base, controller.signal);
      const downloadPath = job.downloadUrl || `/api/jobs/${start.id}/download`;
      const downloadUrl = apiPath(downloadPath, base);
      const link = $('#download-link');
      link.href = downloadUrl;
      // The NAS returns Content-Disposition: attachment. Do not set a cross-origin download name here.
      link.removeAttribute('download');
      announce('Finished converting. Starting your download…', 'success');
      form.hidden = true;
      $('#ready-actions').hidden = false;
      // Safari and other browsers can require another tap after asynchronous work.
      link.click();
    } catch (err) {
      if (err.name !== 'AbortError') {
        announce(err instanceof TypeError ? unavailableMessage : (err.message || unavailableMessage), 'error');
      }
    } finally {
      if (activeController === controller) {
        activeController = null; activeJobId = '';
        busy(false);
      }
    }
  }
  form.addEventListener('submit', runConversion);
  $('#convert-again').addEventListener('click', () => {
    $('#ready-actions').hidden = true;
    form.hidden = false;
    announce();
    urlInput.focus();
  });
  cancelButton.addEventListener('click', () => {
    if (!activeController) return;
    const jobId = activeJobId;
    activeController.abort();
    activeController = null;
    activeJobId = '';
    busy(false);
    announce('Conversion cancelled.');
    if (jobId) {
      try { const base = apiBase(); void fetch(apiPath(`/api/jobs/${jobId}`, base), { method: 'DELETE', credentials: 'omit', cache: 'no-store' }).catch(() => {}); }
      catch { /* local cancellation still completed */ }
    }
  });

  $('#paste-btn').addEventListener('click', async () => {
    if (!navigator.clipboard?.readText) {
      urlInput.focus();
      announce('Touch and hold the link field, then tap Paste.', 'info');
      return;
    }
    try {
      // A direct tap is required for mobile browser clipboard permission.
      const pasted = (await navigator.clipboard.readText()).trim();
      if (!pasted) { announce('Your clipboard is empty.', 'info'); return; }
      urlInput.value = pasted;
      urlInput.dispatchEvent(new Event('input', { bubbles: true }));
      announce();
    } catch {
      urlInput.focus();
      announce('Allow Paste when asked, or touch and hold the link field and tap Paste.', 'info');
    }
  });
  // Tab from an empty URL field fills the sample link while preserving normal Tab navigation.
  // Only populate the example when the field is completely empty; never replace visitor input.
  urlInput.addEventListener('keydown', event => {
    if (event.key !== 'Tab' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    if (urlInput.value.trim()) return;
    urlInput.value = 'https://www.youtube.com/watch?v=xvFZjo5PgG0';
    urlInput.dispatchEvent(new Event('input', { bubbles: true }));
  });
  urlInput.addEventListener('paste', () => { setTimeout(() => announce(), 0); });

  // Ko-fi is configured by the site owner; never silently send visitors to someone else's page.
  function koFiProfile() {
    try {
      const u = new URL(String(cfg.koFiUrl || ''));
      if (u.protocol === 'https:' && ['ko-fi.com','www.ko-fi.com'].includes(u.hostname) && /^\/[A-Za-z0-9_-]+\/?$/.test(u.pathname) && !/your|example|replace|username/i.test(u.pathname)) return u.href;
    } catch { /* no profile configured yet */ }
    return '';
  }
  const profile = koFiProfile();
  document.querySelectorAll('[data-ko-fi-link]').forEach(link => {
    if (profile) { link.href = profile; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    else {
      link.href = '#support';
      link.setAttribute('title', 'Support is temporarily unavailable');
      link.addEventListener('click', ev => {
        ev.preventDefault();
        announce('Support is temporarily unavailable. Please try again later.', 'info');
      });
    }
  });

  const supportSheet = $('#support-sheet');
  const supportBackdrop = $('#support-backdrop');
  const supportContent = supportSheet.querySelector('.support-sheet-content');
  let supportLastFocus = null;
  let supportCloseTimer = 0;
  let supportScrollY = 0;
  const activationIntro = $('#support-screen-activate .support-sheet-intro');
  function showSupportScreen(name) {
    $('#support-screen-info').hidden = name !== 'info';
    $('#support-screen-activate').hidden = name !== 'activate';
    $('#support-back').hidden = name !== 'activate';
    supportContent.scrollTop = 0;
  }
  function openSupport() {
    clearTimeout(supportCloseTimer);
    supportLastFocus = document.activeElement;
    showSupportScreen('info');
    supportBackdrop.hidden = false;
    supportSheet.hidden = false;
    supportSheet.style.removeProperty('--sheet-drag');
    supportBackdrop.style.removeProperty('--backdrop-progress');
    supportScrollY = window.scrollY;
    document.body.style.top = `-${supportScrollY}px`;
    document.body.classList.add('support-sheet-open');
    document.documentElement.classList.add('support-sheet-open');
    requestAnimationFrame(() => {
      supportBackdrop.classList.add('is-open');
      supportSheet.classList.add('is-open');
    });
    $('#support-sheet-close').focus({ preventScroll: true });
  }
  function closeSupport() {
    supportBackdrop.classList.remove('is-open');
    supportSheet.classList.remove('is-open');
    supportSheet.style.removeProperty('--sheet-drag');
    supportBackdrop.style.removeProperty('--backdrop-progress');
    supportSheet.classList.remove('is-dragging');
    supportBackdrop.classList.remove('is-dragging');
    document.body.classList.remove('support-sheet-open');
    document.documentElement.classList.remove('support-sheet-open');
    document.body.style.top = '';
    window.scrollTo({top:supportScrollY,behavior:'instant'});
    supportCloseTimer = setTimeout(() => {
      supportBackdrop.hidden = true;
      supportSheet.hidden = true;
      supportLastFocus?.focus?.({ preventScroll: true });
    }, window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 330);
  }
  $('#support-nav').addEventListener('click', openSupport);
  document.querySelectorAll('.support-screen [data-ko-fi-link]').forEach(link => link.addEventListener('click', () => {
    if (!profile) return;
    activationIntro.textContent = "Ko-fi is open in another tab. As soon as your support clears, we'll email your activation key. Head back here and paste it in.";
    showSupportScreen('activate');
  }));
  $('#support-sheet-close').addEventListener('click', closeSupport);
  supportBackdrop.addEventListener('click', closeSupport);
  $('#support-show-activate').addEventListener('click', () => {
    activationIntro.textContent = "Paste the key from your email. It's a one-time step per browser.";
    showSupportScreen('activate');
  });
  $('#support-back').addEventListener('click', () => showSupportScreen('info'));
  $('#support-lost-key').addEventListener('click', () => {
    $('#support-resend-form').hidden = false;
    $('#support-email').focus();
    setTimeout(() => $('#support-email').scrollIntoView({ block: 'center', behavior: 'smooth' }), 300);
  });
  async function checkSupportKey(key) {
    const response = await fetch(apiPath('/api/support/status', apiBase()), {
      headers: { 'X-Riplet-Key': key }, credentials: 'omit', cache: 'no-store',
    });
    if (!response.ok) return false;
    return Boolean((await response.json()).active);
  }
  try {
    supporterKey = localStorage.getItem('riplet-support-key') || '';
    if (supporterKey) void checkSupportKey(supporterKey).then(active => {
      if (active) updateEntitlement(true);
      else { supporterKey = ''; localStorage.removeItem('riplet-support-key'); updateEntitlement(false); }
    }).catch(() => {});
  } catch { /* local storage may be unavailable */ }
  $('#support-activate-form').addEventListener('submit', async event => {
    event.preventDefault();
    const key = $('#support-key').value.toUpperCase().trim();
    const feedback = $('#support-feedback');
    feedback.textContent = 'Checking key…'; feedback.dataset.kind = 'info';
    try {
      const response = await fetch(apiPath('/api/support/activate', apiBase()), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }), credentials: 'omit', cache: 'no-store',
      });
      if (!response.ok) throw new Error('Invalid or expired key.');
      supporterKey = key;
      try { localStorage.setItem('riplet-support-key', key); } catch {}
      updateEntitlement(true);
      feedback.textContent = 'Supporter access is active on this browser.';
      feedback.dataset.kind = 'success';
    } catch (error) { feedback.textContent = error.message; feedback.dataset.kind = 'error'; }
  });
  $('#support-resend-form').addEventListener('submit', async event => {
    event.preventDefault();
    const feedback = $('#support-feedback');
    feedback.textContent = 'Sending…'; feedback.dataset.kind = 'info';
    try {
      const response = await fetch(apiPath('/api/support/resend', apiBase()), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: $('#support-email').value.trim() }), credentials: 'omit', cache: 'no-store',
      });
      if (!response.ok) throw new Error('Unable to send the key right now.');
      feedback.textContent = 'If that email has active support, a new key is on its way.';
      feedback.dataset.kind = 'success';
    } catch (error) { feedback.textContent = error.message; feedback.dataset.kind = 'error'; }
  });

  document.addEventListener('keydown', event => {
    if (supportSheet.hidden) return;
    if (event.key === 'Escape') closeSupport();
    if (event.key === 'Tab') {
      const focusable = [...supportSheet.querySelectorAll('button:not([hidden]),a[href],input:not([hidden])')]
        .filter(el => !el.closest('[hidden]'));
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  let sheetTouchY = null;
  let draggingSheet = false;
  function updateSheetDrag(distance) {
    const travel = Math.max(1, supportSheet.getBoundingClientRect().height);
    const offset = Math.max(0, Math.min(distance, travel));
    supportSheet.style.setProperty('--sheet-drag', `${offset}px`);
    supportBackdrop.style.setProperty('--backdrop-progress', String(Math.max(0, 1 - offset / travel)));
  }
  supportSheet.addEventListener('touchstart', event => {
    if (event.touches.length !== 1 || event.target.closest('input,button,a')) return;
    sheetTouchY = event.touches[0].clientY;
    draggingSheet = false;
  }, { passive: true });
  supportSheet.addEventListener('touchmove', event => {
    if (sheetTouchY === null || event.touches.length !== 1) return;
    const delta = event.touches[0].clientY - sheetTouchY;
    if (delta <= 0 || (!draggingSheet && supportContent.scrollTop > 0)) return;
    event.preventDefault();
    draggingSheet = true;
    supportSheet.classList.add('is-dragging');
    supportBackdrop.classList.add('is-dragging');
    updateSheetDrag(delta);
  }, { passive: false });
  supportSheet.addEventListener('touchend', event => {
    if (sheetTouchY === null) return;
    const delta = event.changedTouches[0].clientY - sheetTouchY;
    sheetTouchY = null;
    if (!draggingSheet) return;
    draggingSheet = false;
    supportSheet.classList.remove('is-dragging');
    supportBackdrop.classList.remove('is-dragging');
    if (delta > Math.min(180, supportSheet.getBoundingClientRect().height * .3)) closeSupport();
    else { supportSheet.style.removeProperty('--sheet-drag'); supportBackdrop.style.removeProperty('--backdrop-progress'); }
  });
  supportSheet.addEventListener('touchcancel', () => {
    sheetTouchY = null; draggingSheet = false;
    supportSheet.classList.remove('is-dragging');
    supportBackdrop.classList.remove('is-dragging');
    supportSheet.style.removeProperty('--sheet-drag');
    supportBackdrop.style.removeProperty('--backdrop-progress');
  });

  const themeToggle = $('#theme-toggle');
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('#theme-color').content = theme === 'dark' ? '#202020' : '#f7f7fa';
    const dark = theme === 'dark';
    themeToggle.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} mode`);
    themeToggle.title = `Switch to ${dark ? 'light' : 'dark'} mode`;
    $('#theme-icon').innerHTML = dark
      ? '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>'
      : '<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5 8.5 8.5 0 1 0 20.5 14.5Z"/>';
  }
  applyTheme(document.documentElement.dataset.theme || 'light');
  themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('riplet-theme', next); } catch {}
  });
  if (cfg.showAds) $('#ad-slot').hidden = false;
  $('#year').textContent = String(new Date().getFullYear());
})();
