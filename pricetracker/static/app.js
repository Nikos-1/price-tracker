"use strict";
// ---------------------------------------------------------------- βοηθητικά
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CUR_SYMBOLS = { EUR: "€", USD: "$", GBP: "£", JPY: "¥", CNY: "¥", PLN: "zł", CZK: "Kč", SEK: "kr", DKK: "kr", NOK: "kr", HUF: "Ft", RON: "lei", BGN: "лв", CAD: "C$", AUD: "A$", TRY: "₺" };
const eur = (v, cur) => (v == null ? "–" : v.toLocaleString("el-GR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + (CUR_SYMBOLS[cur || "EUR"] || cur));
// Τιμή σε ξένο νόμισμα μαζί με τη μετατροπή σε €: «450,00 $ / ≈401,28 €»
const CURRENCIES = ["EUR", "USD", "GBP", "CHF", "PLN", "CZK", "SEK", "DKK", "NOK", "HUF", "RON", "CAD", "AUD", "JPY", "CNY", "TRY"];
const money = (v, cur, v_eur) => (v == null ? "–" : !cur || cur === "EUR" || v_eur == null ? eur(v, cur)
  : `${eur(v, cur)} <span class="conv" title="Με την ισοτιμία της Ευρωπαϊκής Κεντρικής Τράπεζας">/ ≈${eur(v_eur)}</span>`);
const fmtDate = (s) => (s ? new Date(s.replace(" ", "T")).toLocaleString("el-GR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "–");
// Ημερομηνία του υπολογιστή (όχι UTC): μετά τα μεσάνυχτα να μη βγαίνει η χθεσινή
const localDate = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const num2 = (v) => (v == null || v === "" ? "" : Number(v).toLocaleString("el-GR", { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false }));

async function api(method, url, body) {
  const headers = { "X-PriceTracker": "1" };  // ο τοπικός server δέχεται αλλαγές μόνο από αυτό το παράθυρο
  if (body) headers["Content-Type"] = "application/json";
  const r = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Σφάλμα " + r.status);
  return data;
}
function toast(msg, ms = 4500, action = null) {
  const t = $("#toast");
  t.textContent = msg;
  if (action) {
    const b = document.createElement("button");
    b.className = "small toast-act"; b.textContent = action.label;
    b.onclick = () => { t.classList.add("hidden"); action.fn(); };
    t.append(" ", b);
  }
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
// Τα μηνύματα σφάλματος μένουν μέχρι να τα κλείσεις (ή να έρθει άλλο μήνυμα)
function toastError(msg) {
  toast("⚠ " + msg, 15000, { label: "OK", fn: () => {} });
}
let runDepth = 0;
async function run(fn) {
  // Το κουμπί που πατήθηκε μένει ανενεργό όσο τρέχει το αίτημα: ένα διπλό κλικ δεν φτιάχνει διπλό προϊόν.
  // Μόνο στο εξωτερικό run (ένα run μέσα σε άλλο, π.χ. render, δεν αγγίζει το κουμπί).
  const ae = document.activeElement;
  const b = runDepth === 0 && ae && ae.closest ? ae.closest("button") : null;
  if (b && b.dataset.busy) return;
  if (b) { b.dataset.busy = "1"; b.disabled = true; b.classList.add("busy"); }
  runDepth++;
  try { return await fn(); } catch (e) { toastError(e.message); }
  finally {
    runDepth--;
    if (b) { delete b.dataset.busy; b.disabled = false; b.classList.remove("busy"); }
  }
}

function modal(html, { wide = false, onMount, focus } = {}) {
  const root = $("#modal-root");
  const opener = document.activeElement;
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</div>`;
  const box = bg.querySelector(".modal");
  const h = box.querySelector("h2");
  if (h) { h.id = h.id || "dlg-" + Math.random().toString(36).slice(2); box.setAttribute("aria-labelledby", h.id); }
  const close = () => { bg.remove(); if (opener && opener.focus && document.contains(opener)) opener.focus(); };
  bg._close = close;
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
  bg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
  // Το Tab μένει μέσα στο παράθυρο (όχι στη λίστα από πίσω)
  bg.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = [...box.querySelectorAll("button,input,select,textarea,a[href],[tabindex]:not([tabindex='-1'])")]
      .filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  root.appendChild(bg);
  if (onMount) onMount(box, close);
  const first = (focus && box.querySelector(focus)) || box.querySelector("input,select,textarea") || box.querySelector(".btns .primary");
  if (first) first.focus();
  return close;
}
function confirmBox(title, text, okLabel = "Ναι", danger = false) {
  return new Promise((resolve) => {
    modal(`<h2>${esc(title)}</h2><p>${text}</p>
      <div class="btns"><button data-close id="cb-no">Άκυρο</button><button class="${danger ? "danger" : "primary"}" id="cb-ok">${esc(okLabel)}</button></div>`,
      { onMount: (m, close) => {
          $("#cb-ok", m).onclick = () => { close(); resolve(true); };
          $("#cb-no", m).onclick = () => resolve(false);
        } });
  });
}

// ---------------------------------------------------------------- κατάσταση
const state = { category: "all", purchased: "0", view: "list", productId: null, categories: [], status: {} };

// ---------------------------------------------------------------- κατηγορίες
async function loadCategories() {
  state.categories = await api("GET", "/api/categories");
  const ul = $("#categories");
  const items = [{ id: "all", name: "Όλες" }, ...state.categories, { id: 0, name: "Χωρίς κατηγορία" }];
  ul.innerHTML = items.map((c) => `
    <li data-id="${c.id}" tabindex="0" role="button" class="${String(state.category) === String(c.id) ? "active" : ""}">
      <span>${esc(c.name)}</span>
      ${c.product_count != null ? `<span class="n">${c.product_count}</span>` : ""}
      ${typeof c.id === "number" && c.id > 0 ? `<span class="ops"><button title="Μετονομασία" data-edit="${c.id}">✎</button><button title="Διαγραφή" data-del="${c.id}">🗑</button></span>` : ""}
    </li>`).join("");
  ul.querySelectorAll("li").forEach((li) => li.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    state.category = li.dataset.id;
    state.view = "list";
    loadCategories(); render();
  }));
  ul.querySelectorAll("li").forEach((li) => li.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === li) { e.preventDefault(); li.click(); }
  }));
  ul.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => editCategory(+b.dataset.edit));
  ul.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => deleteCategory(+b.dataset.del));
}
function categoryForm(title, name, onSave) {
  modal(`<h2>${esc(title)}</h2><label>Όνομα</label><input id="cat-name" value="${esc(name)}" placeholder="π.χ. Τροφοδοτικά (PSU)">
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="cat-save">Αποθήκευση</button></div>`,
    { onMount: (m, close) => {
        const save = () => run(async () => { await onSave($("#cat-name", m).value); close(); await loadCategories(); render(); });
        $("#cat-save", m).onclick = save;
        $("#cat-name", m).onkeydown = (e) => { if (e.key === "Enter") save(); };
      } });
}
const addCategory = () => categoryForm("Νέα κατηγορία", "", (name) => api("POST", "/api/categories", { name }));
function editCategory(id) {
  const c = state.categories.find((x) => x.id === id);
  categoryForm("Μετονομασία κατηγορίας", c.name, (name) => api("PUT", "/api/categories/" + id, { name }));
}
async function deleteCategory(id) {
  const c = state.categories.find((x) => x.id === id);
  const u = await run(() => api("GET", `/api/categories/${id}/usage`));
  if (!u) return;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  let text = `Να διαγραφεί η κατηγορία «${esc(c.name)}»;`;
  if (u.tracked) text += `<br><br>Θα διαγραφούν και ${plural(u.tracked, "προϊόν", "προϊόντα")} που παρακολουθείς, μαζί με τις τιμές, το ιστορικό και τα screenshots τους.`;
  if (u.purchased) text += `<br><br>${u.purchased === 1 ? "Το 1 αγορασμένο προϊόν μένει" : `Τα ${u.purchased} αγορασμένα προϊόντα μένουν`} (χωρίς κατηγορία), για να φαίνονται η ημερομηνία αγοράς, η εγγύηση και η απόδειξη.`;
  if (!(await confirmBox("Διαγραφή κατηγορίας", text, "Διαγραφή", true))) return;
  await run(async () => {
    await api("DELETE", `/api/categories/${id}`);
    if (String(state.category) === String(id)) state.category = "all";
    toast("Η κατηγορία διαγράφηκε");
    state.view = state.view === "detail" ? "list" : state.view;
    await loadCategories(); render();
  });
}
function categoryOptions(selected, allowNew = false) {
  return `<option value="">— Χωρίς κατηγορία —</option>` +
    state.categories.map((c) => `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("") +
    (allowNew ? `<option value="__new">+ Νέα κατηγορία…</option>` : "");
}
// Επιλογή κατηγορίας με δυνατότητα δημιουργίας νέας επί τόπου
function categoryPicker(id, selected) {
  const none = !state.categories.length;
  return `<select id="${id}" ${none ? 'style="display:none"' : ""}>${categoryOptions(selected, true)}</select>
    <input id="${id}-new" placeholder="${none ? "Νέα κατηγορία (προαιρετικό), π.χ. Τροφοδοτικά" : "Όνομα νέας κατηγορίας"}" style="${none ? "" : "display:none;margin-top:6px"}">
    ${none ? `<div class="hint">Δεν υπάρχει ακόμα καμία κατηγορία. Γράψε ένα όνομα για να δημιουργηθεί, ή άφησέ το κενό.</div>` : ""}`;
}
function bindCategoryPicker(m, id) {
  const sel = $("#" + id, m), inp = $("#" + id + "-new", m);
  if (!state.categories.length) return;
  sel.onchange = () => { inp.style.display = sel.value === "__new" ? "" : "none"; if (sel.value === "__new") inp.focus(); };
}
async function pickedCategory(m, id) {
  const sel = $("#" + id, m), inp = $("#" + id + "-new", m);
  const creating = !state.categories.length || sel.value === "__new";
  if (!creating) return sel.value;
  const name = inp.value.trim();
  if (!name) {
    if (state.categories.length) throw new Error("Γράψε το όνομα της νέας κατηγορίας");
    return "";
  }
  const existing = state.categories.find((c) => c.name.toLowerCase() === name.toLowerCase());
  if (existing) return existing.id;
  const r = await api("POST", "/api/categories", { name });
  return r.id;
}

// ---------------------------------------------------------------- γενικές κατηγορίες καταστημάτων
// Π.χ. «Ηλεκτρικές συσκευές», «Ρούχα». Ένα προϊόν με γενική κατηγορία ψάχνεται μόνο σε καταστήματα αυτής της
// κατηγορίας και σε όσα δεν έχουν δηλωμένη καμία (π.χ. BestPrice, Amazon).
async function loadSectors() { state.sectors = await api("GET", "/api/sectors"); return state.sectors; }
function sectorPicker(id, selected) {
  return `<select id="${id}"><option value="">Όλα τα καταστήματα</option>` +
    (state.sectors || []).map((x) => `<option value="${x.id}" ${x.id === selected ? "selected" : ""}>${esc(x.name)}</option>`).join("") +
    `<option value="__new">+ Νέα γενική κατηγορία…</option></select>
    <input id="${id}-new" placeholder="Όνομα νέας γενικής κατηγορίας" style="display:none;margin-top:6px">`;
}
function bindSectorPicker(m, id, catId) {
  const sel = $("#" + id, m), inp = $("#" + id + "-new", m);
  sel.onchange = () => { inp.style.display = sel.value === "__new" ? "" : "none"; if (sel.value === "__new") inp.focus(); };
  // Αλλαγή κατηγορίας: προτείνεται η γενική κατηγορία που έχει ήδη αυτή η κατηγορία
  const cat = catId && $("#" + catId, m);
  if (cat) cat.addEventListener("change", () => {
    const c = state.categories.find((x) => String(x.id) === cat.value);
    if (c && c.sector_id) { sel.value = c.sector_id; sel.onchange(); }
  });
}
async function pickedSector(m, id) {
  const sel = $("#" + id, m), inp = $("#" + id + "-new", m);
  if (sel.value !== "__new") return sel.value;
  const name = inp.value.trim();
  if (!name) throw new Error("Γράψε το όνομα της νέας γενικής κατηγορίας");
  const existing = (state.sectors || []).find((x) => x.name.toLowerCase() === name.toLowerCase());
  if (existing) return existing.id;
  const r = await api("POST", "/api/sectors", { name });
  await loadSectors();
  return r.id;
}
const SECTOR_HINT = `<div class="hint">Η αναζήτηση γίνεται μόνο στα καταστήματα αυτής της γενικής κατηγορίας (και σε όσα πουλάνε απ' όλα). Οι γενικές κατηγορίες των καταστημάτων ορίζονται στις Ρυθμίσεις.</div>`;
function catSector(catId) { const c = state.categories.find((x) => x.id === +catId); return c ? c.sector_id || null : null; }

// ---------------------------------------------------------------- λίστα προϊόντων
async function renderList() {
  const q = new URLSearchParams({ category: state.category, purchased: state.purchased });
  const products = await api("GET", "/api/products?" + q);
  const main = $("#main");
  if (!products.length) {
    main.innerHTML = `<div class="empty"><p>Δεν υπάρχουν προϊόντα εδώ.</p>
      <p>Πάτα <b>«+ Προσθήκη προϊόντος»</b> και επικόλλησε το link από ένα κατάστημα (BestPrice, Plaisio, Kotsovolos…).</p></div>`;
    return;
  }
  main.innerHTML = `<div class="grid">${products.map(cardHtml).join("")}</div>`;
  main.querySelectorAll("[data-open]").forEach((el) => el.onclick = () => openProduct(+el.dataset.open));
  main.querySelectorAll("[data-act]").forEach((b) => b.onclick = () => productAction(b.dataset.act, +b.dataset.id, products.find((p) => p.id === +b.dataset.id)));
}
function priceBox(label, b, checked = true) {
  return `<div class="price-box"><div class="lbl">${label}</div>
    <div class="val">${b ? (b.currency && b.currency !== "EUR" ? "≈" : "") + eur(b.price) : "–"}</div>${b && b.currency && b.currency !== "EUR" ? `<div class="shop">${eur(b.orig_price, b.currency)}</div>` : ""}<div class="shop">${b ? esc(b.shop) + (b.country && b.country !== "Ελλάδα" ? " · " + esc(b.country) : "") : (checked === false ? "δεν έχει ελεγχθεί ακόμα" : "δεν βρέθηκε διαθέσιμο")}</div></div>`;
}
function cardHtml(p) {
  const chips = [];
  if (p.purchased) chips.push(`<span class="chip ok">✔ Αγοράστηκε${p.purchased_price ? " " + eur(p.purchased_price) : ""}</span>`);
  else if (p.any_in_stock) chips.push(`<span class="chip ok">Διαθέσιμο</span>`);
  else if (p.last_checked) chips.push(`<span class="chip bad">Μη διαθέσιμο</span>`);
  else chips.push(`<span class="chip">Αναμονή ελέγχου…</span>`);
  if (p.target_price) chips.push(`<span class="chip">Στόχος ${eur(p.target_price)}</span>`);
  if (p.candidate_count) chips.push(`<span class="chip warn">${p.candidate_count} ${p.candidate_count === 1 ? "πρόταση" : "προτάσεις"} για επιβεβαίωση</span>`);
  if (p.need_currency) chips.push(`<span class="chip warn">Διάλεξε νόμισμα τιμής</span>`);
  if (p.error_count) chips.push(`<span class="chip bad">${p.error_count} ${p.error_count === 1 ? "σφάλμα" : "σφάλματα"} ελέγχου</span>`);
  return `<div class="card">
    <div class="title" data-open="${p.id}">${p.image_url ? `<img src="${esc(p.image_url)}" alt="" onerror="this.remove()">` : ""}<span>${esc(p.name)}</span></div>
    <div class="meta">${esc(p.category_name || "Χωρίς κατηγορία")}${p.sector_name ? ` · <span title="Η αναζήτηση γίνεται μόνο σε σχετικά καταστήματα">${esc(p.sector_name)}</span>` : ""} · ${p.listing_count} καταστήματα · Χαμηλότερη ποτέ: ${eur(p.min_ever)}</div>
    <div class="prices${p.best.OTHER ? " three" : ""}">${priceBox("Ελλάδα", p.best.GR)}${priceBox("Ε.Ε.", p.best.EU)}${p.best.OTHER ? priceBox("Εκτός Ε.Ε.", p.best.OTHER) : ""}</div>
    <div class="row">${chips.join("")}</div>
    <div class="meta">Τελευταίος έλεγχος: ${fmtDate(p.last_checked)}</div>
    <div class="row">
      <button class="small" data-open="${p.id}">Λεπτομέρειες</button>
      <button class="small" data-act="edit" data-id="${p.id}">✎ Τροποποίηση</button>
      <button class="small" data-act="${p.purchased ? "unbuy" : "buy"}" data-id="${p.id}">${p.purchased ? "↺ Ξανά παρακολούθηση" : "✔ Αγοράστηκε"}</button>
      <button class="small danger" data-act="delete" data-id="${p.id}">🗑</button>
    </div></div>`;
}

async function productAction(act, id, p) {
  if (act === "edit") return editProduct(p);
  if (act === "buy") return markPurchased(p);
  if (act === "unbuy") return run(async () => { await api("POST", `/api/products/${id}/purchased`, { purchased: false }); refresh(); });
  if (act === "delete") {
    const extra = p.purchased
      ? `Το «${esc(p.name)}» έχει αγοραστεί. Θα σβηστούν μόνο τα δεδομένα της αναζήτησης: καταστήματα, τιμές, ιστορικό και screenshots.<br><br>Το προϊόν μένει στα Αγορασμένα, με το κατάστημα και την ημερομηνία αγοράς, την εγγύηση και την απόδειξη.`
      : `Να διαγραφεί το «${esc(p.name)}» μαζί με τις τιμές, το ιστορικό και τα screenshots του;`;
    if (await confirmBox(p.purchased ? "Διαγραφή δεδομένων αναζήτησης" : "Διαγραφή προϊόντος", extra, "Διαγραφή", true)) {
      await run(() => api("DELETE", "/api/products/" + id));
      if (state.view === "detail") state.view = "list";
      refresh();
    }
  }
}
async function editProduct(p) {
  await loadSectors();
  modal(`<h2>Τροποποίηση προϊόντος</h2>
    <label>Όνομα</label><input id="ep-name" value="${esc(p.name)}">
    <div class="two"><div><label>Κατηγορία</label>${categoryPicker("ep-cat", p.category_id)}</div>
    <div><label>Τιμή-στόχος (€)</label><input id="ep-target" value="${p.target_price ?? ""}" placeholder="π.χ. 99,90"></div></div>
    <label>Γενική κατηγορία (σε ποια καταστήματα να ψάχνεται)</label>${sectorPicker("ep-sec", p.sector_id || catSector(p.category_id))}${SECTOR_HINT}
    <label>Σημειώσεις</label><textarea id="ep-notes" rows="3">${esc(p.notes || "")}</textarea>
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="ep-save">Αποθήκευση</button></div>`,
    { onMount: (m, close) => {
        bindCategoryPicker(m, "ep-cat");
        bindSectorPicker(m, "ep-sec", "ep-cat");
        $("#ep-save", m).onclick = () => run(async () => {
          const category_id = await pickedCategory(m, "ep-cat");
          const sector_id = await pickedSector(m, "ep-sec");
          await api("PUT", "/api/products/" + p.id, { name: $("#ep-name", m).value, category_id, sector_id,
            target_price: $("#ep-target", m).value, notes: $("#ep-notes", m).value });
          close(); await loadCategories(); refresh();
        });
      } });
}
// ---------------------------------------------------------------- αγορά, απόδειξη, εγγύηση
function warrantyInput(prefix, months = 24) {
  const years = months && months % 12 === 0;
  const n = months ? (years ? months / 12 : months) : 0;
  return `<span class="warranty-in" data-w="${prefix}">
    <input type="number" min="0" max="240" step="1" value="${n}" data-wn style="width:80px">
    <select data-wu style="width:auto"><option value="12" ${years || !months ? "selected" : ""}>Έτη</option><option value="1" ${months && !years ? "selected" : ""}>Μήνες</option></select></span>`;
}
function warrantyMonths(el) {
  const n = parseFloat($("[data-wn]", el).value || "0");
  return Math.round(n * +$("[data-wu]", el).value) || 0;
}
const ACCEPT_RECEIPT = ".pdf,.jpg,.jpeg,.png,.webp,.heic,.gif,.bmp,.tif,.tiff,image/*,application/pdf";

async function markPurchased(p) {
  // Προτεινόμενη τιμή αγοράς: η καλύτερη στην Ελλάδα (αλλιώς Ε.Ε.), σε €, με κόμμα
  const best = p.best?.GR || p.best?.EU || p.best?.OTHER;
  const others = (await api("GET", "/api/products?purchased=0&category=all")).filter((o) => o.id !== p.id);
  const otherRows = others.map((o) => {
    const ob = [o.best?.GR, o.best?.EU, o.best?.OTHER].filter(Boolean).sort((a, b) => a.price - b.price)[0];
    return `<tr data-other="${o.id}"><td><label class="inline"><input type="checkbox" data-pick> ${esc(o.name)}</label></td>
      <td><input data-price value="${ob ? num2(ob.price) : ""}" placeholder="τιμή €" style="width:100px"></td><td>${warrantyInput("o" + o.id)}</td></tr>`;
  }).join("");
  modal(`<h2>Αγοράστηκε: ${esc(p.name)}</h2>
    <p class="hint">Τα αγορασμένα προϊόντα σταματούν να ελέγχονται. Το ιστορικό τιμών μένει.</p>
    <div class="two"><div><label>Τιμή αγοράς (€)</label><input id="pp-price" value="${best ? num2(best.price) : ""}"></div>
    <div><label>Ημερομηνία αγοράς</label><input id="pp-date" type="date" value="${localDate()}"></div></div>
    <div class="two"><div><label>Κατάστημα</label><input id="pp-shop" value="${esc(best ? best.shop : "")}"></div>
    <div><label>Εγγύηση</label>${warrantyInput("main", 24)}</div></div>
    <div class="section"><h3>Θέλεις να ανεβάσεις την απόδειξη ή το τιμολόγιο;</h3>
      <input type="file" id="pp-file" accept="${ACCEPT_RECEIPT}">
      <div class="hint">PDF ή φωτογραφία, προαιρετικό. Αποθηκεύεται στα Έγγραφα\\PriceTracker\\Αποδείξεις, με όνομα την ημερομηνία, το κατάστημα και τα προϊόντα.</div>
      <label class="inline" style="margin-top:12px"><input type="checkbox" id="pp-multi"> Η απόδειξη περιέχει κι άλλα προϊόντα</label>
      <div id="pp-others" style="display:none">
        ${others.length ? `<p class="hint">Διάλεξε όσα είναι στην ίδια απόδειξη. Το καθένα κρατάει τη δική του τιμή και εγγύηση.</p>
          <table><tr><th>Προϊόν</th><th>Τιμή</th><th>Εγγύηση</th></tr>${otherRows}</table>` : ""}
        <table id="pp-new"><tr class="pp-new-head" style="display:none"><th>Προϊόν</th><th>Κατηγορία</th><th>Τιμή</th><th>Εγγύηση</th><th></th></tr></table>
        <button class="link" id="pp-add-new">+ Προϊόν της απόδειξης που δεν παρακολουθούσα</button>
      </div>
    </div>
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="pp-save">✔ Αγοράστηκε</button></div>`,
    { wide: true, onMount: (m, close) => {
        $("#pp-multi", m).onchange = () => ($("#pp-others", m).style.display = $("#pp-multi", m).checked ? "" : "none");
        let nNew = 0;
        $("#pp-add-new", m).onclick = () => {
          nNew++;
          const tr = document.createElement("tr");
          tr.dataset.new = nNew;
          tr.innerHTML = `<td><input data-name placeholder="Όνομα προϊόντος"></td><td><select data-cat>${categoryOptions(p.category_id)}</select></td>
            <td><input data-price placeholder="τιμή €" style="width:100px"></td><td>${warrantyInput("n" + nNew)}</td>
            <td><button class="small danger" data-rm>✕</button></td>`;
          $("#pp-new", m).appendChild(tr);
          $(".pp-new-head", m).style.display = "";
          $("[data-rm]", tr).onclick = () => { tr.remove(); if (!m.querySelector("#pp-new tr[data-new]")) $(".pp-new-head", m).style.display = "none"; };
          $("[data-name]", tr).focus();
        };
        m.querySelectorAll("tr[data-other] [data-price], tr[data-other] .warranty-in").forEach((el) =>
          el.addEventListener("input", () => { el.closest("tr").querySelector("[data-pick]").checked = true; }));
        $("#pp-save", m).onclick = () => run(async () => {
          const items = [{ product_id: p.id, price: $("#pp-price", m).value, warranty_months: warrantyMonths($("[data-w=main]", m)) }];
          if ($("#pp-multi", m).checked) {
            m.querySelectorAll("tr[data-other]").forEach((tr) => {
              if ($("[data-pick]", tr).checked) items.push({ product_id: +tr.dataset.other, price: $("[data-price]", tr).value, warranty_months: warrantyMonths($(".warranty-in", tr)) });
            });
            m.querySelectorAll("#pp-new tr[data-new]").forEach((tr) => {
              const name = $("[data-name]", tr).value.trim();
              if (name) items.push({ name, category_id: $("[data-cat]", tr).value, price: $("[data-price]", tr).value, warranty_months: warrantyMonths($(".warranty-in", tr)) });
            });
          }
          const fd = new FormData();
          fd.append("items", JSON.stringify(items));
          fd.append("date", $("#pp-date", m).value);
          fd.append("shop", $("#pp-shop", m).value);
          const f = $("#pp-file", m).files[0];
          if (f) fd.append("receipt", f);
          const r = await apiForm("/api/purchase", fd);
          close(); refresh();
          toast(r.products.length > 1 ? `Καταχωρήθηκαν ${r.products.length} προϊόντα ως αγορασμένα` : "Σημειώθηκε ως αγορασμένο");
        });
      } });
}

async function apiForm(url, fd) {
  const r = await fetch(url, { method: "POST", headers: { "X-PriceTracker": "1" }, body: fd });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || (r.status === 413 ? "Το αρχείο είναι πολύ μεγάλο (έως 30 MB)" : "Σφάλμα " + r.status));
  return data;
}

async function renderWarranties() {
  const rows = await api("GET", "/api/warranties?category=" + encodeURIComponent(state.category));
  const main = $("#main");
  const catName = state.category === "all" ? "" : +state.category === 0 ? "Χωρίς κατηγορία"
    : (state.categories.find((c) => String(c.id) === String(state.category)) || {}).name || "";
  if (!rows.length && catName) {
    main.innerHTML = `<div class="empty"><p>Δεν υπάρχουν αγορασμένα προϊόντα στην κατηγορία «${esc(catName)}».</p></div>`;
    return;
  }
  if (!rows.length) {
    main.innerHTML = `<div class="empty"><p>Δεν υπάρχουν αγορασμένα προϊόντα ακόμα.</p><p>Όταν πατήσεις <b>«✔ Αγοράστηκε»</b> σε ένα προϊόν, θα εμφανιστεί εδώ με την εγγύηση και την απόδειξή του.</p></div>`;
    return;
  }
  const chip = (r) => r.warranty_state === "none" ? `<span class="chip">Χωρίς στοιχεία</span>`
    : r.warranty_state === "expired" ? `<span class="chip bad">Έληξε</span>`
    : r.warranty_state === "soon" ? `<span class="chip warn">Λήγει σε ${r.warranty_days_left} μέρες</span>`
    : `<span class="chip ok">Σε ισχύ, ${r.warranty_days_left} μέρες</span>`;
  main.innerHTML = `<h2 style="margin-top:0">Εγγυήσεις και αποδείξεις${catName ? ` · ${esc(catName)}` : ""}</h2>
    <p class="hint">Οι αποδείξεις βρίσκονται και στα Έγγραφα\\PriceTracker\\Αποδείξεις. <button class="link" id="open-receipts">Άνοιγμα φακέλου</button></p>
    <div class="section"><table>
    <tr><th>Προϊόν</th><th>Αγορά</th><th>Εγγύηση</th><th>Λήξη</th><th>Απόδειξη</th><th></th></tr>
    ${rows.map((r) => `<tr>
      <td><b>${esc(r.name)}</b><div><select class="cat-pick" data-cat-of="${r.id}" title="Κατηγορία">${categoryOptions(r.category_id, true)}</select></div></td>
      <td>${r.purchased_at ? new Date(r.purchased_at + "T00:00:00").toLocaleDateString("el-GR") : "–"}<div class="hint">${esc(r.purchased_shop || "")} ${r.purchased_price ? eur(r.purchased_price) : ""}</div></td>
      <td>${esc(r.warranty_label)}</td>
      <td>${r.warranty_end ? new Date(r.warranty_end + "T00:00:00").toLocaleDateString("el-GR") : "–"}<div>${chip(r)}</div></td>
      <td>${r.receipt_id ? `<button class="small" data-open-r="${r.receipt_id}">📄 Άνοιγμα</button><div class="hint">${esc((r.receipt_file || "").split("/").pop())}</div>`
        : `<button class="small" data-up="${r.id}">⬆ Ανέβασμα</button>`}</td>
      <td style="white-space:nowrap"><button class="small" data-ew="${r.id}">✎</button>
        <button class="small" data-unbuy="${r.id}" title="Ξανά παρακολούθηση τιμής">↺</button></td></tr>`).join("")}
    </table></div>`;
  $("#open-receipts").onclick = () => api("POST", "/api/open-folder", { sub: "Αποδείξεις" });
  main.querySelectorAll("[data-open-r]").forEach((b) => b.onclick = () => run(async () => {
    const r = await api("POST", `/api/receipts/${b.dataset.openR}/open`);
    if (!r.opened && r.url) window.open(r.url, "_blank");
  }));
  main.querySelectorAll("[data-up]").forEach((b) => b.onclick = () => uploadReceipt(rows.find((r) => r.id === +b.dataset.up), rows));
  main.querySelectorAll("[data-cat-of]").forEach((sel) => sel.onchange = () => run(async () => {
    const pid = +sel.dataset.catOf;
    const prev = rows.find((r) => r.id === pid).category_id;
    const assign = async (cid) => { await api("PUT", "/api/products/" + pid, { category_id: cid }); await refresh(); };
    if (sel.value !== "__new") return assign(sel.value);
    sel.value = prev ?? "";
    categoryForm("Νέα κατηγορία", "", async (name) => { const r = await api("POST", "/api/categories", { name }); await api("PUT", "/api/products/" + pid, { category_id: r.id }); });
  }));
  main.querySelectorAll("[data-ew]").forEach((b) => b.onclick = () => editWarranty(rows.find((r) => r.id === +b.dataset.ew)));
  main.querySelectorAll("[data-unbuy]").forEach((b) => b.onclick = async () => {
    if (await confirmBox("Ξανά παρακολούθηση", "Να ξαναρχίσει η παρακολούθηση τιμής; Τα στοιχεία αγοράς και εγγύησης θα σβηστούν (το αρχείο της απόδειξης μένει στον φάκελο).", "Ναι")) {
      await run(() => api("POST", `/api/products/${b.dataset.unbuy}/purchased`, { purchased: false })); refresh();
    }
  });
}

function uploadReceipt(p, all) {
  const others = all.filter((r) => r.id !== p.id && !r.receipt_id);
  modal(`<h2>Απόδειξη για: ${esc(p.name)}</h2>
    <input type="file" id="ur-file" accept="${ACCEPT_RECEIPT}">
    ${others.length ? `<label style="margin-top:14px">Η ίδια απόδειξη αφορά και:</label>
      ${others.map((o) => `<label class="inline"><input type="checkbox" value="${o.id}" data-also> ${esc(o.name)} <span class="hint">${esc(o.purchased_at || "")}</span></label>`).join("")}` : ""}
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="ur-save">Ανέβασμα</button></div>`,
    { onMount: (m, close) => {
        $("#ur-save", m).onclick = () => run(async () => {
          const f = $("#ur-file", m).files[0];
          if (!f) throw new Error("Διάλεξε αρχείο");
          const fd = new FormData();
          fd.append("receipt", f);
          m.querySelectorAll("[data-also]:checked").forEach((c) => fd.append("also", c.value));
          await apiForm(`/api/products/${p.id}/receipt`, fd);
          close(); render(); toast("Η απόδειξη αποθηκεύτηκε");
        });
      } });
}

function editWarranty(p) {
  modal(`<h2>Στοιχεία αγοράς: ${esc(p.name)}</h2>
    <div class="two"><div><label>Ημερομηνία αγοράς</label><input id="ew-date" type="date" value="${esc(p.purchased_at || "")}"></div>
    <div><label>Εγγύηση</label>${warrantyInput("ew", p.warranty_months || 0)}</div></div>
    <div class="two"><div><label>Τιμή (€)</label><input id="ew-price" value="${p.purchased_price ?? ""}"></div>
    <div><label>Κατάστημα</label><input id="ew-shop" value="${esc(p.purchased_shop || "")}"></div></div>
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="ew-save">Αποθήκευση</button></div>`,
    { onMount: (m, close) => {
        $("#ew-save", m).onclick = () => run(async () => {
          await api("PUT", `/api/products/${p.id}/warranty`, { date: $("#ew-date", m).value, warranty_months: warrantyMonths($("[data-w=ew]", m)),
            price: $("#ew-price", m).value, shop: $("#ew-shop", m).value });
          close(); render();
        });
      } });
}

async function addProduct() {
  await loadSectors();
  const cat0 = typeof state.category === "string" && +state.category > 0 ? +state.category : null;
  modal(`<h2>Προσθήκη προϊόντος</h2>
    <label>Link προϊόντος από κατάστημα</label>
    <input id="ap-url" placeholder="https://www.bestprice.gr/item/... ή https://www.plaisio.gr/...">
    <div class="hint">Η εφαρμογή θα διαβάσει όνομα και τιμή από τη σελίδα και μετά θα ψάξει το ίδιο προϊόν στα άλλα καταστήματα. Οι αντιστοιχίες που βρίσκει εμφανίζονται για επιβεβαίωση.</div>
    <label>Όνομα (προαιρετικό – συμπληρώνεται αυτόματα)</label><input id="ap-name" placeholder="π.χ. Corsair RM850x">
    <div class="two"><div><label>Κατηγορία</label>${categoryPicker("ap-cat", typeof state.category === "string" && +state.category > 0 ? +state.category : null)}</div>
    <div><label>Τιμή-στόχος (€, προαιρετικό)</label><input id="ap-target" placeholder="π.χ. 99,90"></div></div>
    <label>Γενική κατηγορία (π.χ. Ηλεκτρικές συσκευές, Ρούχα)</label>${sectorPicker("ap-sec", catSector(cat0))}${SECTOR_HINT}
    <label class="inline"><input type="checkbox" id="ap-search" checked> Αναζήτηση και στα άλλα καταστήματα</label>
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="ap-save">Προσθήκη</button></div>`,
    { onMount: (m, close) => {
        bindCategoryPicker(m, "ap-cat");
        bindSectorPicker(m, "ap-sec", "ap-cat");
        $("#ap-save", m).onclick = () => run(async () => {
          const category_id = await pickedCategory(m, "ap-cat");
          const sector_id = await pickedSector(m, "ap-sec");
          const r = await api("POST", "/api/products", { url: $("#ap-url", m).value, name: $("#ap-name", m).value,
            category_id, sector_id, target_price: $("#ap-target", m).value, search: $("#ap-search", m).checked });
          close(); toast("Προστέθηκε. Γίνεται ο πρώτος έλεγχος…");
          await loadCategories(); openProduct(r.id);
        });
      } });
}

// ---------------------------------------------------------------- λεπτομέρειες προϊόντος
let chart;
async function openProduct(id) {
  state.view = "detail"; state.productId = id;
  await renderDetail();
}
async function renderDetail() {
  const p = await api("GET", "/api/products/" + state.productId);
  const main = $("#main");
  const groups = { GR: [], EU: [], OTHER: [] };
  p.listings.forEach((l) => (groups[l.region] || groups.OTHER).push(l));
  const listingRows = (arr) => arr.map((l) => `<tr>
      <td><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.shop_name)}</a>${l.country_label && l.region !== "GR" ? ` <span class="chip">${esc(l.country_label)}</span>` : ""}<div class="hint">${esc((l.title || "").slice(0, 90))}</div><div class="hint only-narrow">Έλεγχος: ${fmtDate(l.last_checked)}</div></td>
      <td class="num">${l.last_price != null && !l.last_currency
        ? `<b>${eur(l.last_price, "EUR").replace(" €", "")}</b><div class="warn-box">Σε ποιο νόμισμα είναι;<br><select data-cur="${l.id}"><option value="">Διάλεξε…</option>${CURRENCIES.map((c) => `<option value="${c}">${c} ${CUR_SYMBOLS[c] || ""}</option>`).join("")}</select></div>`
        : `<b>${money(l.last_price, l.last_currency, l.last_price_eur)}</b>`}</td>
      <td>${l.last_in_stock == null ? "–" : l.last_in_stock ? '<span class="chip ok">Ναι</span>' : '<span class="chip bad">Όχι</span>'}
          ${l.last_error ? `<div class="err">${esc(l.last_error)}</div>` : ""}</td>
      <td class="num">${eur(l.analysis.min_30)}<div class="hint">συνήθης ${eur(l.analysis.median_30)}</div></td>
      <td class="verdict ${l.analysis.verdict}">${esc(l.analysis.label)}</td>
      <td class="col-shot">${l.last_screenshot ? `<img class="thumb" src="/shots/${encodeURI(l.last_screenshot.replaceAll("\\", "/"))}" data-shot="${esc(l.last_screenshot)}" alt="screenshot">` : "–"}</td>
      <td class="num col-checked">${fmtDate(l.last_checked)}</td>
      <td style="white-space:nowrap"><button class="small" data-check-l="${l.id}" title="Έλεγχος τώρα">⟳</button>
          <button class="small danger" data-del-l="${l.id}" title="Αφαίρεση καταστήματος">✕</button></td></tr>`).join("");
  const table = (title, arr) => arr.length ? `<div class="section"><h3>${title}</h3><table>
      <tr><th>Κατάστημα</th><th>Τιμή</th><th>Διαθέσιμο</th><th>Χαμηλότερη 30 ημ.</th><th>Εκτίμηση προσφοράς</th><th class="col-shot">Screenshot</th><th class="col-checked">Έλεγχος</th><th></th></tr>
      ${listingRows(arr)}</table></div>` : "";
  const cands = p.candidates.length ? `<div class="section"><h3>Πιθανές αντιστοιχίες σε άλλα καταστήματα (${p.candidates.length})</h3>
      <p class="hint">Η εφαρμογή τα βρήκε με αναζήτηση. Άνοιξε τον σύνδεσμο και σύγκρινε το μοντέλο (π.χ. «RM850e» ≠ «RM850x»). Πάτα ✔ μόνο αν είναι ακριβώς το ίδιο προϊόν. Η τιμή του φαίνεται μετά τον πρώτο έλεγχο.</p><table>
      <tr><th>Κατάστημα</th><th>Τίτλος</th><th></th></tr>
      ${p.candidates.map((c) => `<tr><td>${esc(c.shop_name)} <span class="chip">${esc(c.country_label || c.region_label)}</span></td>
        <td><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title || c.url)}</a></td>
        <td style="white-space:nowrap"><button class="small primary" data-acc="${c.id}">✔ Είναι αυτό</button> <button class="small" data-rej="${c.id}">✕ Όχι, άλλο προϊόν</button></td></tr>`).join("")}
      </table></div>` : "";
  const shotsRows = p.history.filter((h) => h.screenshot).slice(-40).reverse();
  main.innerHTML = `<div class="detail">
    <button class="link" id="back">← Πίσω στη λίστα</button>
    <h2>${esc(p.name)}</h2>
    <div class="meta">${esc(p.category_name || "Χωρίς κατηγορία")}${p.sector_name ? ` · <span title="Η αναζήτηση γίνεται μόνο σε σχετικά καταστήματα">${esc(p.sector_name)}</span>` : ""} ${p.ean ? "· EAN " + esc(p.ean) : ""} ${p.mpn ? "· Κωδ. " + esc(p.mpn) : ""}
      ${p.target_price ? "· Στόχος " + eur(p.target_price) : ""} ${p.purchased ? `· <b>Αγοράστηκε ${esc(p.purchased_at || "")} ${p.purchased_price ? eur(p.purchased_price) : ""} ${esc(p.purchased_shop || "")}</b>` : ""}</div>
    ${p.notes ? `<p>${esc(p.notes)}</p>` : ""}
    <div class="row" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button id="d-check">⟳ Έλεγχος τώρα</button>
      <button id="d-search">🔍 Αναζήτηση σε άλλα καταστήματα</button>
      <button id="d-add-link">+ Πρόσθεσε link καταστήματος</button>
      <button id="d-export">⬇ Excel</button>
      <button data-act="edit">✎ Τροποποίηση</button>
      <button data-act="${p.purchased ? "unbuy" : "buy"}">${p.purchased ? "↺ Ξανά παρακολούθηση" : "✔ Αγοράστηκε"}</button>
      <button class="danger" data-act="delete">🗑 Διαγραφή</button>
    </div>
    <div class="prices${p.best.OTHER ? " three" : ""}" style="margin-top:12px;max-width:${p.best.OTHER ? 780 : 520}px">${priceBox("Καλύτερη τιμή Ελλάδα", p.best.GR)}${priceBox("Καλύτερη τιμή Ε.Ε.", p.best.EU)}${p.best.OTHER ? priceBox("Καλύτερη τιμή εκτός Ε.Ε.", p.best.OTHER) : ""}</div>
    ${cands}
    <div class="section"><h3>Εξέλιξη τιμής</h3>
      ${p.history.some((h) => h.price) ? `<div class="chart-wrap"><canvas id="chart"></canvas></div>
      <p class="hint">Οι κάθετες γραμμές δείχνουν Black Friday / Cyber Monday. Μια προσφορά είναι πραγματική αν η τιμή είναι κάτω από τη χαμηλότερη των προηγούμενων εβδομάδων, όχι μόνο κάτω από μια «αρχική» τιμή.</p>`
      : `<p class="hint">Δεν υπάρχουν ακόμα τιμές. Ο πρώτος έλεγχος μπορεί να πάρει λίγα λεπτά.</p>`}
    </div>
    ${table("🇬🇷 Ελλάδα", groups.GR)}${table("🇪🇺 Ευρωπαϊκή Ένωση", groups.EU)}${table("🌍 Εκτός Ε.Ε. <span class=\"hint\">· η τιμή δεν περιλαμβάνει πιθανούς δασμούς και ΦΠΑ στο τελωνείο</span>", groups.OTHER)}
    ${shotsRows.length ? `<div class="section"><h3>Screenshots (αρχείο αναφοράς)</h3><table>
      <tr><th>Ημερομηνία</th><th>Κατάστημα</th><th>Τιμή</th><th></th></tr>
      ${shotsRows.map((h) => `<tr><td>${fmtDate(h.checked_at)}</td><td>${esc(h.shop_name)}</td><td class="num">${money(h.price, h.currency, h.price_eur)}</td>
        <td><button class="small" data-shot="${esc(h.screenshot)}">Προβολή</button></td></tr>`).join("")}
      </table><button class="link" id="open-shots">Άνοιγμα φακέλου screenshots</button></div>` : ""}
  </div>`;
  $("#back").onclick = () => { state.view = "list"; render(); };
  $("#d-check").onclick = () => run(async () => { await api("POST", "/api/check", { product_id: p.id }); toast("Ο έλεγχος ξεκίνησε"); });
  $("#d-search").onclick = () => run(async () => { await api("POST", `/api/products/${p.id}/search`); toast("Η αναζήτηση ξεκίνησε. Τα αποτελέσματα θα εμφανιστούν εδώ."); });
  $("#d-export").onclick = () => exportXlsx(p.id);
  $("#d-add-link").onclick = () => addLink(p.id);
  const op = $("#open-shots"); if (op) op.onclick = () => api("POST", "/api/open-folder", { sub: "screenshots" });
  main.querySelectorAll("[data-act]").forEach((b) => b.onclick = () => productAction(b.dataset.act, p.id, p));
  const shopOf = (id) => (p.candidates.find((c) => c.id === +id) || {}).shop_name || "";
  const undo = (id) => run(async () => { await api("POST", `/api/listings/${id}/undo`); toast("Αναιρέθηκε"); renderDetail(); });
  main.querySelectorAll("[data-acc]").forEach((b) => b.onclick = () => run(async () => {
    const id = b.dataset.acc, shop = shopOf(id);
    await api("POST", `/api/listings/${id}/accept`); await renderDetail();
    toast(`Προστέθηκε το ${shop}. Ο έλεγχος τιμής ξεκίνησε.`, 10000, { label: "Αναίρεση", fn: () => undo(id) });
  }));
  main.querySelectorAll("[data-rej]").forEach((b) => b.onclick = () => run(async () => {
    const id = b.dataset.rej, shop = shopOf(id);
    await api("POST", `/api/listings/${id}/reject`); await renderDetail();
    toast(`Απορρίφθηκε η πρόταση από ${shop}.`, 10000, { label: "Αναίρεση", fn: () => undo(id) });
  }));
  main.querySelectorAll("[data-cur]").forEach((sel) => sel.onchange = () => run(async () => {
    if (!sel.value) return;
    await api("PUT", `/api/listings/${sel.dataset.cur}/currency`, { currency: sel.value });
    toast("Αποθηκεύτηκε. Θα χρησιμοποιείται και στους επόμενους ελέγχους.");
    renderDetail();
  }));
  main.querySelectorAll("[data-check-l]").forEach((b) => b.onclick = () => run(async () => { await api("POST", "/api/check", { product_id: p.id }); toast("Ο έλεγχος ξεκίνησε"); }));
  main.querySelectorAll("[data-del-l]").forEach((b) => b.onclick = async () => {
    if (await confirmBox("Αφαίρεση καταστήματος", "Να αφαιρεθεί αυτό το κατάστημα και το ιστορικό του από το προϊόν;", "Αφαίρεση", true)) {
      await run(() => api("DELETE", "/api/listings/" + b.dataset.delL)); renderDetail();
    }
  });
  main.querySelectorAll("[data-shot]").forEach((el) => el.onclick = () => showShot(el.dataset.shot));
  drawChart(p);
}
function addLink(pid) {
  modal(`<h2>Πρόσθεσε link καταστήματος</h2><label>Link του ίδιου προϊόντος σε άλλο κατάστημα</label><input id="al-url" placeholder="https://...">
    <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="al-save">Προσθήκη</button></div>`,
    { onMount: (m, close) => {
        $("#al-save", m).onclick = () => run(async () => { await api("POST", `/api/products/${pid}/listings`, { url: $("#al-url", m).value }); close(); renderDetail(); toast("Προστέθηκε, γίνεται έλεγχος…"); });
      } });
}
function showShot(path) {
  modal(`<img class="full" src="/shots/${encodeURI(path.replaceAll("\\", "/"))}" alt="screenshot"><div class="btns"><button data-close>Κλείσιμο</button></div>`, { wide: true });
}

const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4b5563", "#65a30d", "#ea580c"];
const eventLines = {
  id: "eventLines",
  afterDraw(c, _a, opts) {
    const x = c.scales.x; if (!x) return;
    const ctx = c.ctx;
    let drawn = 0;
    (opts.events || []).forEach((ev) => {
      const px = x.getPixelForValue(new Date(ev.date + "T12:00:00").getTime());
      if (px < x.left || px > x.right) return;
      const dark = matchMedia("(prefers-color-scheme: dark)").matches;
      ctx.save(); ctx.strokeStyle = dark ? "rgba(229,231,235,.55)" : "rgba(17,24,39,.5)"; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(px, c.chartArea.top); ctx.lineTo(px, c.chartArea.bottom); ctx.stroke();
      ctx.fillStyle = dark ? "#e5e7eb" : "#111827"; ctx.font = "600 11px Segoe UI"; const w = ctx.measureText(ev.label).width;
      const tx = px + 4 + w > x.right ? px - 4 - w : px + 4;
      ctx.fillText(ev.label, tx, c.chartArea.top + 12 + (drawn++ % 2) * 16);
      ctx.restore();
    });
  },
};
function drawChart(p) {
  const cv = $("#chart"); if (!cv) return;
  if (chart) chart.destroy();
  const byListing = {};
  // Το γράφημα είναι σε €, ώστε να συγκρίνονται καταστήματα με διαφορετικό νόμισμα
  p.history.forEach((h) => { const y = h.price_eur ?? (h.currency === "EUR" ? h.price : null); if (y) (byListing[h.listing_id] ||= []).push({ x: new Date(h.checked_at.replace(" ", "T")).getTime(), y, s: h.in_stock }); });
  // Κάθε κατάστημα έχει πάντα το ίδιο χρώμα, σε όλα τα προϊόντα
  // (με τη «δική» του θέση στην παλέτα· αν δύο πέσουν στο ίδιο, ο δεύτερος παίρνει το επόμενο ελεύθερο)
  const used = new Set();
  const colorFor = {};
  const colorOf = (name) => {
    if (colorFor[name]) return colorFor[name];
    let h = 0; for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    let i = h % COLORS.length;
    for (let k = 0; k < COLORS.length && used.has(COLORS[i]); k++) i = (i + 1) % COLORS.length;
    used.add(COLORS[i]);
    return (colorFor[name] = COLORS[i]);
  };
  const datasets = p.listings.filter((l) => byListing[l.id]).map((l) => ({
    label: `${l.shop_name} (${l.region_label})`, data: byListing[l.id], borderColor: colorOf(l.shop_name),
    backgroundColor: colorOf(l.shop_name), stepped: true, pointRadius: 2, borderWidth: 2,
    segment: { borderDash: (ctx) => (ctx.p1.raw && ctx.p1.raw.s === 0 ? [3, 3] : undefined) },
  }));
  if (p.target_price) {
    const xs = p.history.map((h) => new Date(h.checked_at.replace(" ", "T")).getTime());
    datasets.push({ label: "Τιμή-στόχος", data: [{ x: Math.min(...xs), y: p.target_price }, { x: Math.max(...xs), y: p.target_price }],
      borderColor: matchMedia("(prefers-color-scheme: dark)").matches ? "#d1d5db" : "#374151", borderDash: [6, 4], pointRadius: 0, borderWidth: 2 });
  }
  // Δείξε και την επερχόμενη Black Friday / Cyber Monday στον άξονα
  const allX = datasets.flatMap((d) => d.data.map((pt) => pt.x));
  let xMax = Math.max(...allX);
  const upcoming = p.events.map((e) => new Date(e.date + "T23:59:00").getTime()).filter((t) => t > xMax && t - Date.now() < 45 * 86400000);
  if (upcoming.length) xMax = Math.max(...upcoming) + 86400000;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  Chart.defaults.color = dark ? "#9ca3af" : "#4b5563";
  Chart.defaults.borderColor = dark ? "#2a2f38" : "#e5e7eb";
  chart = new Chart(cv, {
    type: "line", data: { datasets }, plugins: [eventLines],
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false },
      scales: { x: { type: "time", max: xMax, time: { tooltipFormat: "dd/MM/yyyy HH:mm", displayFormats: { day: "dd/MM", hour: "dd/MM HH:mm" } } },
                y: { ticks: { callback: (v) => eur(v) } } },
      plugins: { eventLines: { events: p.events },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${eur(c.parsed.y)}${c.raw.s === 0 ? " (μη διαθέσιμο)" : ""}` } } } },
  });
}

// ---------------------------------------------------------------- ρυθμίσεις
async function openSettings() {
  state.view = "settings";
  window.scrollTo(0, 0);
  await render();
}
async function renderSettings() {
  const s = await api("GET", "/api/settings");
  const shops = await api("GET", "/api/shops");
  const countries = await api("GET", "/api/countries");
  await loadSectors();
  const secLabel = (ids) => { const n = (state.sectors || []).filter((x) => ids.includes(x.id)).map((x) => x.name); return n.length ? n.join(", ") : "Όλες"; };
  const secCell = (ids) => `<button class="small sec-btn" data-secs title="Σε ποιες γενικές κατηγορίες ψάχνεται αυτό το κατάστημα">${esc(secLabel(ids))}</button>`;
  const sectorRow = (x) => `<tr data-sector="${x.id}"><td><input data-sn value="${esc(x.name)}"></td>
    <td class="hint">${x.shop_count || 0} ${x.shop_count === 1 ? "κατάστημα" : "καταστήματα"}</td>
    <td><button class="small danger" data-del-sector title="Διαγραφή">🗑</button></td></tr>`;
  const countrySel = (c, attr = 'data-f="country"', auto = false) => {
    const grp = (region, label) => `<optgroup label="${label}">` + countries.filter((x) => x.region === region && x.code)
      .sort((a, b) => a.name.localeCompare(b.name, "el")).map((x) => `<option value="${x.code}" ${x.code === c ? "selected" : ""}>${esc(x.name)}</option>`).join("") + "</optgroup>";
    return `<select ${attr}>${auto ? `<option value="auto" selected>Αυτόματα, από τη διεύθυνση</option>` : ""}` +
      grp("GR", "Ελλάδα") + grp("EU", "Ευρωπαϊκή Ένωση") + grp("OTHER", "Εκτός Ε.Ε.") +
      `<option value="" ${!auto && !c ? "selected" : ""}>Άγνωστη</option></select>`;
  };
  const REGION_TITLES = { GR: "Ελλάδα", EU: "Ευρωπαϊκή Ένωση", OTHER: "Εκτός Ε.Ε. (πιθανοί δασμοί και ΦΠΑ στο τελωνείο)" };
  const shopRow = (sh) => `<tr data-shop="${sh.id}" data-used="${sh.used || 0}" data-sectors="${esc(JSON.stringify(sh.sector_ids || []))}">
    <td style="text-align:center"><input type="checkbox" data-sel title="Επιλογή για διαγραφή"></td>
    <td><input data-f="name" value="${esc(sh.name)}"></td>
    <td class="hint">${esc(sh.domain)}${sh.used ? `<div>${sh.used} ${sh.used === 1 ? "προϊόν" : "προϊόντα"}</div>` : ""}</td>
    <td>${countrySel(sh.country || "")}</td>
    <td><input data-f="search_url" value="${esc(sh.search_url || "")}" placeholder="https://.../search?q={q}">
      <div class="hint" data-detect-msg></div>${sh.search_url ? "" : `<button class="link" data-detect="${sh.id}">🔍 Αυτόματη εύρεση</button>`}</td>
    <td>${secCell(sh.sector_ids || [])}</td>
    <td style="text-align:center"><input type="checkbox" data-f="search_enabled" ${sh.search_enabled ? "checked" : ""}></td>
    <td><button class="small danger" data-del-shop="${sh.id}" title="Διαγραφή καταστήματος">🗑</button></td></tr>`;
  let lastRegion = null;
  const shopRows = shops.map((sh) => {
    const head = sh.region !== lastRegion ? `<tr class="group-row"><td colspan="8">${esc(REGION_TITLES[sh.region] || sh.region)}</td></tr>` : "";
    lastRegion = sh.region;
    return head + shopRow(sh);
  }).join("");
  const main = $("#main");
  main.innerHTML = `<div class="page settings-page">
    <div class="page-head"><button id="s-back">← Πίσω</button><h2>Ρυθμίσεις</h2><span class="grow"></span>
      <button id="s-cancel">Άκυρο</button><button class="primary" id="s-save">Αποθήκευση</button></div>
    <div class="section"><h3>Έλεγχοι</h3>
      <div class="two"><div><label>Κάθε πόσες ώρες (κανονικά)</label><input id="s-int" type="number" min="0.25" step="0.25" value="${s.check_interval_hours}"></div>
      <div><label>Κάθε πόσες ώρες στην περίοδο Black Friday</label><input id="s-bfint" type="number" min="0.25" step="0.25" value="${s.bf_interval_hours}"></div></div>
      <label class="inline"><input type="checkbox" id="s-bfauto" ${s.bf_window_auto ? "checked" : ""}> Περίοδος Black Friday αυτόματα: 20/11 έως 2/12</label>
      <div class="two" id="s-bfdates"><div><label>Αρχή (ΜΜ-ΗΗ)</label><input id="s-bfs" value="${esc(s.bf_start || "11-20")}"></div><div><label>Τέλος (ΜΜ-ΗΗ)</label><input id="s-bfe" value="${esc(s.bf_end || "12-02")}"></div></div>
      <label class="inline"><input type="checkbox" id="s-research" ${s.research_daily ? "checked" : ""}> Μία φορά τη μέρα αναζήτηση των προϊόντων και στα καταστήματα που δεν τα έχουν ακόμα</label>
      <label>Screenshots</label><select id="s-shot"><option value="changes" ${s.screenshot_mode === "changes" ? "selected" : ""}>Μόνο όταν είναι διαθέσιμο: την πρώτη φορά, όταν ξαναγίνει διαθέσιμο και όταν πέφτει η τιμή</option><option value="always" ${s.screenshot_mode === "always" ? "selected" : ""}>Σε κάθε έλεγχο (όταν είναι διαθέσιμο)</option></select>
      <label>Παράθυρο browser κατά τους ελέγχους</label><select id="s-bmode">
        <option value="auto" ${s.browser_mode === "auto" ? "selected" : ""}>Κρυφό· ανοίγει μόνο όταν χρειάζεται ενέργειά σου (π.χ. captcha)</option>
        <option value="hidden" ${s.browser_mode === "hidden" ? "selected" : ""}>Πάντα κρυφό (οι σελίδες με captcha παραλείπονται)</option>
        <option value="visible" ${s.browser_mode === "visible" ? "selected" : ""}>Πάντα ορατό</option></select>
    </div>
    <div class="section"><h3>Ειδοποιήσεις email</h3>
      <label class="inline"><input type="checkbox" id="s-email" ${s.email_enabled ? "checked" : ""}> Αποστολή email</label>
      <div class="two"><div><label>Πάροχος</label><select id="s-prov"><option value="gmail" ${s.smtp_provider === "gmail" ? "selected" : ""}>Gmail</option><option value="outlook" ${s.smtp_provider === "outlook" ? "selected" : ""}>Outlook / Hotmail</option><option value="custom" ${s.smtp_provider === "custom" ? "selected" : ""}>Άλλο (SMTP)</option></select></div>
      <div><label>Λογαριασμός αποστολής</label><input id="s-user" value="${esc(s.smtp_user)}" placeholder="το-email-σου@gmail.com"></div></div>
      <div class="two"><div><label>Κωδικός εφαρμογής (App Password) ${s.has_password ? "✔ αποθηκευμένος" : ""}</label><input id="s-pass" type="password" placeholder="${s.has_password ? "•••••••• (άφησέ το κενό για να μείνει ίδιος)" : "16 χαρακτήρες από τη Google"}"></div>
      <div><label>Αποστολή ειδοποιήσεων σε</label><input id="s-to" value="${esc(s.email_to)}" placeholder="π.χ. και στο Hotmail σου"></div></div>
      <div class="two" id="s-smtp"><div><label>SMTP server</label><input id="s-host" value="${esc(s.smtp_host)}"></div><div><label>Θύρα</label><input id="s-port" type="number" value="${s.smtp_port}"></div></div>
      <p class="hint">Gmail: ενεργοποίησε την «Επαλήθευση σε 2 βήματα» και φτιάξε κωδικό στο myaccount.google.com/apppasswords. Ο κωδικός αποθηκεύεται στη Διαχείριση διαπιστευτηρίων των Windows.</p>
      <label class="inline"><input type="checkbox" id="s-n1" ${s.notify_back_in_stock ? "checked" : ""}> Όταν ένα προϊόν γίνει διαθέσιμο</label>
      <label class="inline"><input type="checkbox" id="s-n2" ${s.notify_target_price ? "checked" : ""}> Όταν φτάσει την τιμή-στόχο</label>
      <label class="inline"><input type="checkbox" id="s-n3" ${s.notify_new_low ? "checked" : ""}> Όταν βρεθεί νέα χαμηλότερη τιμή</label>
      <label class="inline"><input type="checkbox" id="s-n4" ${s.notify_new_shop ? "checked" : ""}> Όταν ένα προϊόν βρεθεί σε νέο κατάστημα (από την ημερήσια αναζήτηση)</label>
      <button id="s-test" style="margin-top:8px">Αποστολή δοκιμαστικού email</button>
    </div>
    <div class="section"><h3>Εφαρμογή</h3>
      <label>Όταν πατάω το Χ</label><select id="s-close"><option value="ask" ${s.close_action === "ask" ? "selected" : ""}>Να ρωτάει</option><option value="tray" ${s.close_action === "tray" ? "selected" : ""}>Να μένει στο system tray</option><option value="exit" ${s.close_action === "exit" ? "selected" : ""}>Να κλείνει</option></select>
      <label class="inline"><input type="checkbox" id="s-auto" ${s.start_with_windows ? "checked" : ""}> Εκκίνηση μαζί με τα Windows (στο system tray)</label>
      <p class="hint">Φάκελος δεδομένων: <code>${esc(state.status.data_dir || "")}</code> <button class="link" id="s-folder">Άνοιγμα</button></p>
      <label class="inline"><input type="checkbox" id="s-autoupd" ${s.auto_update ? "checked" : ""}> Αυτόματη ενημέρωση της εφαρμογής όταν βγαίνει νέα έκδοση</label>
      <p class="hint">Έκδοση <b>${esc(state.status.version || "")}</b> · <span id="s-upd-msg">${esc(state.status.update?.message || "")}</span>
        <button class="link" id="s-upd-check">Έλεγχος τώρα</button> <button class="link" id="s-upd-install" style="display:${state.status.update?.available ? "" : "none"}">Εγκατάσταση τώρα</button></p>
      <p class="hint">Τιμές σε ξένο νόμισμα ($, £ κ.ά.) δείχνονται και σε € με τις ισοτιμίες της Ευρωπαϊκής Κεντρικής Τράπεζας<span id="s-fx">${state.status.fx_date ? ` (της ${new Date(state.status.fx_date + "T00:00:00").toLocaleDateString("el-GR")})` : ""}</span>, που ανανεώνονται σε κάθε έλεγχο. <button class="link" id="s-fx-refresh">Ανανέωση τώρα</button></p>
      <label>Υπενθύμιση πριν λήξει μια εγγύηση (μέρες, 0 = όχι)</label><input id="s-wrem" type="number" min="0" max="365" value="${s.warranty_reminder_days ?? 30}" style="width:120px">
    </div>
    <div class="section"><h3>Καταστήματα</h3>
      <p class="hint">Η «Αναζήτηση» χρησιμοποιεί το URL αναζήτησης κάθε καταστήματος ({q} = το προϊόν). Καταστήματα από links που προσθέτεις μπαίνουν εδώ αυτόματα.</p>
      <div class="row-btns" style="margin-bottom:8px"><button class="danger" id="s-del-sel" disabled>🗑 Διαγραφή επιλεγμένων</button></div>
      <table id="s-shops"><tr><th style="text-align:center;width:36px"><input type="checkbox" id="s-sel-all" title="Επιλογή όλων"></th><th>Όνομα</th><th>Διεύθυνση</th><th>Χώρα</th><th>URL αναζήτησης</th><th>Γενικές κατηγορίες</th><th>Αναζήτηση</th><th></th></tr>${shopRows}</table>
      <h3 style="margin-top:16px">Προσθήκη καταστήματος</h3>
      <div class="two"><div><label>Όνομα</label><input id="ns-name" placeholder="π.χ. Germanos"></div>
        <div><label>Διεύθυνση (site)</label><input id="ns-domain" placeholder="π.χ. www.germanos.gr"></div></div>
      <div class="two"><div><label>Χώρα</label>${countrySel("", 'id="ns-country"', true)}</div>
        <div><label>URL αναζήτησης (προαιρετικό: αν το αφήσεις κενό, το ψάχνω μόνος μου)</label><input id="ns-url" placeholder="https://www.germanos.gr/search?q={q}"></div></div>
      <div class="two"><div><label>Γενικές κατηγορίες</label><div id="ns-secs" data-sectors="[]">${secCell([])}</div></div><div></div></div>
      <p class="hint">Για το URL αναζήτησης: κάνε μια αναζήτηση στο site, αντίγραψε τη διεύθυνση και βάλε {q} στη θέση της λέξης που έψαξες.</p>
      <button id="ns-add">+ Προσθήκη καταστήματος</button>
      <button id="s-shops-xlsx" style="margin-left:8px">⬇ Λίστα καταστημάτων σε Excel</button>
    </div>
    <div class="section"><h3>Γενικές κατηγορίες καταστημάτων</h3>
      <p class="hint">Δήλωσε σε κάθε κατάστημα τι πουλάει (στήλη «Γενικές κατηγορίες» πιο πάνω). Όταν ένα προϊόν έχει γενική κατηγορία, π.χ. μια κουζίνα στις «Ηλεκτρικές συσκευές», η αναζήτηση γίνεται μόνο στα καταστήματα αυτής της κατηγορίας και σε όσα δεν έχουν δηλωμένη καμία («Όλες», π.χ. BestPrice, Amazon). Οι αλλαγές εδώ αποθηκεύονται αμέσως.</p>
      <table id="s-sectors" style="max-width:640px"><tr><th>Όνομα</th><th>Καταστήματα</th><th></th></tr>${(state.sectors || []).map(sectorRow).join("")}</table>
      <div class="row-btns" style="margin-top:8px"><input id="nsec-name" placeholder="Νέα γενική κατηγορία, π.χ. Είδη κατοικιδίων" style="max-width:340px"><button id="nsec-add">+ Προσθήκη</button></div>
    </div>
    <div class="section"><h3>Εξαγωγή και εισαγωγή ρυθμίσεων</h3>
      <p class="hint">Το αρχείο ρυθμίσεων περιέχει όλες τις ρυθμίσεις σου και τη λίστα καταστημάτων. Χρησιμεύει ως αντίγραφο ασφαλείας ή για μεταφορά σε άλλον υπολογιστή, με την «Εισαγωγή». Ο κωδικός email δεν αποθηκεύεται στο αρχείο. Τα αρχεία μπαίνουν στον φάκελο Λήψεις\\PriceTracker. Επίσης, το αρχείο Έγγραφα\\PriceTracker\\Ρυθμίσεις.json ενημερώνεται μόνο του σε κάθε αλλαγή.</p>
      <div class="row-btns">
        <button id="s-exp-json">⬇ Εξαγωγή αρχείου ρυθμίσεων</button>
        <button id="s-exp-xlsx">⬇ Ρυθμίσεις και καταστήματα σε Excel</button>
        <button id="s-imp">⬆ Εισαγωγή αρχείου ρυθμίσεων</button>
        <input type="file" id="s-imp-file" accept=".json,application/json" style="display:none">
      </div>
    </div>
    <div class="btns"><button id="s-cancel2">Άκυρο</button><button class="primary" id="s-save2">Αποθήκευση</button></div></div>`;
  {
    const m = main;
    let dirty = false;
    const leave = () => { state.settingsGuard = null; state.view = "list"; window.scrollTo(0, 0); render(); };
    // Αλλαγές που δεν αποθηκεύτηκαν δεν χάνονται σιωπηλά (Escape, «Πίσω», «Άκυρο»)
    const close = (force) => {
      if (force === true || !dirty) return leave();
      if (document.getElementById("unsaved-dlg")) return;
      modal(`<div id="unsaved-dlg"><h2>Αλλαγές που δεν αποθηκεύτηκαν</h2><p>Έχεις αλλάξει ρυθμίσεις. Να αποθηκευτούν;</p>
        <div class="btns"><button data-close>Επιστροφή στις ρυθμίσεις</button><button class="danger" id="uv-drop">Απόρριψη αλλαγών</button><button class="primary" id="uv-save">Αποθήκευση</button></div></div>`,
        { focus: "#uv-save", onMount: (dm, dclose) => {
            $("#uv-drop", dm).onclick = () => { dclose(); leave(); };
            $("#uv-save", dm).onclick = () => { dclose(); save(); };
          } });
    };
    state.settingsGuard = close;
    {
        const syncBf = () => ($("#s-bfdates", m).style.display = $("#s-bfauto", m).checked ? "none" : "");
        $("#s-bfauto", m).onchange = syncBf; syncBf();
        const presets = { gmail: ["smtp.gmail.com", 587], outlook: ["smtp-mail.outlook.com", 587] };
        const syncProv = () => {
          const pv = $("#s-prov", m).value;
          $("#s-smtp", m).style.display = pv === "custom" ? "" : "none";
          if (presets[pv]) { $("#s-host", m).value = presets[pv][0]; $("#s-port", m).value = presets[pv][1]; }
        };
        $("#s-prov", m).onchange = syncProv; syncProv();
        $("#s-folder", m).onclick = () => api("POST", "/api/open-folder", {});
        const bindDel = (root) => root.querySelectorAll("[data-del-shop]").forEach((b) => b.onclick = () => run(async () => {
          const id = +b.dataset.delShop;
          const sh = (await api("GET", "/api/shops")).find((x) => x.id === id);
          if (!sh) return b.closest("tr").remove();
          const text = sh.used
            ? `Το «${esc(sh.name)}» χρησιμοποιείται σε <b>${sh.used}</b> ${sh.used === 1 ? "προϊόν" : "προϊόντα"}. Αν το διαγράψεις, θα σβηστούν και οι τιμές και το ιστορικό του σε αυτά τα προϊόντα.`
            : `Να διαγραφεί το κατάστημα «${esc(sh.name)}»;`;
          if (!(await confirmBox("Διαγραφή καταστήματος", text, "Διαγραφή", true))) return;
          await api("DELETE", `/api/shops/${id}?listings=${sh.used ? 1 : 0}`);
          b.closest("tr").remove();
          toast("Το κατάστημα διαγράφηκε");
        }));
        bindDel(m);
        // Γενικές κατηγορίες ανά κατάστημα: επιλογή με κουτάκια, αποθηκεύεται με την «Αποθήκευση»
        const refreshSecLabels = () => m.querySelectorAll("[data-sectors]").forEach((el) => {
          const ids = JSON.parse(el.dataset.sectors || "[]").filter((i) => (state.sectors || []).some((x) => x.id === i));
          el.dataset.sectors = JSON.stringify(ids);
          const b = $("[data-secs]", el); if (b) b.textContent = secLabel(ids);
        });
        // Στη σελίδα και όχι στο #main: το #main μένει ίδιο σε κάθε άνοιγμα των Ρυθμίσεων και ο listener θα
        // προστίθετο ξανά και ξανά (πολλά παράθυρα το ένα πάνω στο άλλο)
        $(".settings-page", m).addEventListener("click", (e) => {
          const b = e.target.closest("[data-secs]");
          if (!b) return;
          const holder = b.closest("[data-sectors]");
          const cur = JSON.parse(holder.dataset.sectors || "[]");
          const name = holder.matches("tr") ? $("[data-f=name]", holder).value : ($("#ns-name", m).value || "νέο κατάστημα");
          modal(`<h2>Γενικές κατηγορίες: ${esc(name)}</h2>
            <p class="hint">Τι πουλάει αυτό το κατάστημα; Αν δεν επιλέξεις καμία, θα ψάχνεται για όλα τα προϊόντα.</p>
            <div class="sec-list">${(state.sectors || []).map((x) => `<label class="inline"><input type="checkbox" value="${x.id}" ${cur.includes(x.id) ? "checked" : ""}> ${esc(x.name)}</label>`).join("")}</div>
            <div class="btns"><button data-close>Άκυρο</button><button class="primary" id="sec-ok">OK</button></div>`,
            { onMount: (mm, closeM) => {
                $("#sec-ok", mm).onclick = () => {
                  holder.dataset.sectors = JSON.stringify([...mm.querySelectorAll("input:checked")].map((c) => +c.value));
                  refreshSecLabels(); closeM();
                };
              } });
        });
        const bindSectorRow = (tr) => {
          const id = +tr.dataset.sector, inp = $("[data-sn]", tr);
          inp.onchange = () => run(async () => {
            try { await api("PUT", `/api/sectors/${id}`, { name: inp.value }); }
            catch (e) { inp.value = state.sectors.find((x) => x.id === id).name; throw e; }
            await loadSectors(); refreshSecLabels(); toast("Η γενική κατηγορία μετονομάστηκε");
          });
          $("[data-del-sector]", tr).onclick = () => run(async () => {
            const x = state.sectors.find((y) => y.id === id);
            if (!(await confirmBox("Διαγραφή γενικής κατηγορίας", `Να διαγραφεί η γενική κατηγορία «${esc(x.name)}»; Τα καταστήματα και τα προϊόντα δεν διαγράφονται· απλώς δεν θα έχουν πια αυτή τη γενική κατηγορία.`, "Διαγραφή", true))) return;
            await api("DELETE", `/api/sectors/${id}`);
            tr.remove(); await loadSectors(); refreshSecLabels(); toast("Η γενική κατηγορία διαγράφηκε");
          });
        };
        m.querySelectorAll("tr[data-sector]").forEach(bindSectorRow);
        $("#nsec-add", m).onclick = () => run(async () => {
          const name = $("#nsec-name", m).value.trim();
          if (!name) throw new Error("Γράψε ένα όνομα");
          await api("POST", "/api/sectors", { name });
          await loadSectors();
          const x = state.sectors.find((y) => y.name.toLowerCase() === name.toLowerCase());
          const tmp = document.createElement("tbody"); tmp.innerHTML = sectorRow(x);
          const tr = tmp.firstElementChild; $("#s-sectors", m).appendChild(tr); bindSectorRow(tr);
          $("#nsec-name", m).value = ""; toast(`Προστέθηκε η «${x.name}»`);
        });
        $("#nsec-name", m).onkeydown = (e) => { if (e.key === "Enter") $("#nsec-add", m).click(); };
        // Αυτόματη εύρεση URL αναζήτησης: γίνεται στο παρασκήνιο, εδώ απλώς παρακολουθούμε το αποτέλεσμα
        const watchDetect = (id) => {
          const tr = m.querySelector(`tr[data-shop="${id}"]`);
          if (!tr) return;
          const msg = $("[data-detect-msg]", tr), btn = $("[data-detect]", tr);
          if (btn) btn.style.display = "none";
          msg.textContent = "🔍 Ψάχνω μόνος μου το URL αναζήτησης…";
          let tries = 0;
          const tick = async () => {
            if (!document.body.contains(tr)) return;
            const st = await api("GET", `/api/shops/${id}/detect`).catch(() => null);
            if (st && ["found", "guessed", "notfound"].includes(st.state)) {
              if (st.search_url) { $("[data-f=search_url]", tr).value = st.search_url; $("[data-f=search_enabled]", tr).checked = !!st.search_enabled; }
              msg.textContent = (st.state === "found" ? "✔ " : st.state === "guessed" ? "⚠ " : "✕ ") + st.message;
              if (st.state === "notfound" && btn) btn.style.display = "";
              return;
            }
            if (++tries < 120) setTimeout(tick, 3000);
            else msg.textContent = "Η εύρεση αργεί. Δες ξανά αργότερα.";
          };
          setTimeout(tick, 2000);
        };
        const bindDetect = (root) => root.querySelectorAll("[data-detect]").forEach((b) => b.onclick = () => run(async () => {
          await api("POST", `/api/shops/${b.dataset.detect}/detect`);
          watchDetect(+b.dataset.detect);
        }));
        bindDetect(m);
        const selRows = () => [...m.querySelectorAll("tr[data-shop]")].filter((tr) => $("[data-sel]", tr).checked);
        const syncSel = () => {
          const n = selRows().length, all = m.querySelectorAll("tr[data-shop]").length;
          const b = $("#s-del-sel", m);
          b.disabled = !n;
          b.textContent = n ? `🗑 Διαγραφή επιλεγμένων (${n})` : "🗑 Διαγραφή επιλεγμένων";
          $("#s-sel-all", m).checked = n > 0 && n === all;
        };
        $("#s-shops", m).addEventListener("change", (e) => { if (e.target.matches("[data-sel]")) syncSel(); });
        $("#s-sel-all", m).onchange = (e) => { m.querySelectorAll("[data-sel]").forEach((c) => (c.checked = e.target.checked)); syncSel(); };
        $("#s-del-sel", m).onclick = () => run(async () => {
          const rows = selRows();
          if (!rows.length) return;
          const names = rows.map((tr) => esc($("[data-f=name]", tr).value));
          const used = rows.reduce((a, tr) => a + +tr.dataset.used, 0);
          let text = `Να διαγραφούν ${rows.length === 1 ? "το κατάστημα" : `τα ${rows.length} καταστήματα`}: <b>${names.join(", ")}</b>;`;
          if (used) text += `<br><br>Κάποια χρησιμοποιούνται σε προϊόντα. Θα σβηστούν και οι τιμές, το ιστορικό και τα screenshots τους σε αυτά τα προϊόντα.`;
          if (!(await confirmBox("Διαγραφή καταστημάτων", text, "Διαγραφή", true))) return;
          await api("POST", "/api/shops/delete", { ids: rows.map((tr) => +tr.dataset.shop) });
          rows.forEach((tr) => tr.remove());
          syncSel();
          toast(rows.length === 1 ? "Το κατάστημα διαγράφηκε" : `Διαγράφηκαν ${rows.length} καταστήματα`);
        });
        $("#ns-add", m).onclick = () => run(async () => {
          const body = { name: $("#ns-name", m).value, domain: $("#ns-domain", m).value, search_url: $("#ns-url", m).value,
            sector_ids: JSON.parse($("#ns-secs", m).dataset.sectors || "[]") };
          if ($("#ns-country", m).value !== "auto") body.country = $("#ns-country", m).value;
          const r = await api("POST", "/api/shops", body);
          const sh = (await api("GET", "/api/shops")).find((x) => x.id === r.id);
          const tmp = document.createElement("tbody");
          tmp.innerHTML = shopRow(sh);
          const tr = tmp.firstElementChild;
          $("#s-shops", m).appendChild(tr);
          bindDel(tr);
          bindDetect(tr);
          ["#ns-name", "#ns-domain", "#ns-url"].forEach((x) => ($(x, m).value = ""));
          $("#ns-secs", m).dataset.sectors = "[]"; refreshSecLabels();
          toast(`Προστέθηκε το ${sh.name}` + (r.detecting ? ". Ψάχνω μόνος μου το URL αναζήτησης." : ""));
          if (r.detecting) watchDetect(r.id);
        });
        $("#s-upd-check", m).onclick = () => run(async () => {
          await api("POST", "/api/update/check");
          $("#s-upd-msg", m).textContent = "Έλεγχος…";
          setTimeout(async () => {
            const u = await api("GET", "/api/update");
            $("#s-upd-msg", m).textContent = u.message || "";
            $("#s-upd-install", m).style.display = u.available && u.supported ? "" : "none";
          }, 4000);
        });
        $("#s-fx-refresh", m).onclick = () => run(async () => {
          const r = await api("POST", "/api/fx/refresh");
          $("#s-fx", m).textContent = ` (της ${new Date(r.date + "T00:00:00").toLocaleDateString("el-GR")})`;
          toast(`Ισοτιμίες: 1 € = ${String(r.usd).replace(".", ",")} $ · ${String(r.gbp).replace(".", ",")} £`);
        });
        $("#s-upd-install", m).onclick = () => run(async () => { await api("POST", "/api/update/install"); toast("Η ενημέρωση ξεκίνησε. Η εφαρμογή θα ξανανοίξει μόνη της.", 8000); });
        const collect = () => {
          const body = {
            check_interval_hours: $("#s-int", m).value, bf_interval_hours: $("#s-bfint", m).value,
            bf_window_auto: $("#s-bfauto", m).checked, bf_start: $("#s-bfs", m).value, bf_end: $("#s-bfe", m).value,
            screenshot_mode: $("#s-shot", m).value, browser_mode: $("#s-bmode", m).value,
            email_enabled: $("#s-email", m).checked, smtp_provider: $("#s-prov", m).value, smtp_user: $("#s-user", m).value.trim(),
            email_to: $("#s-to", m).value.trim(), smtp_host: $("#s-host", m).value.trim(), smtp_port: $("#s-port", m).value,
            notify_back_in_stock: $("#s-n1", m).checked, notify_target_price: $("#s-n2", m).checked, notify_new_low: $("#s-n3", m).checked,
            notify_new_shop: $("#s-n4", m).checked, research_daily: $("#s-research", m).checked,
            close_action: $("#s-close", m).value, start_with_windows: $("#s-auto", m).checked,
            auto_update: $("#s-autoupd", m).checked, warranty_reminder_days: +$("#s-wrem", m).value || 0,
          };
          if ($("#s-pass", m).value) body.smtp_password = $("#s-pass", m).value.replace(/\s+/g, "");
          return body;
        };
        const saveAll = async () => {
          await api("PUT", "/api/settings", collect());
          for (const tr of m.querySelectorAll("tr[data-shop]")) {
            await api("PUT", "/api/shops/" + tr.dataset.shop, { name: $("[data-f=name]", tr).value, country: $("[data-f=country]", tr).value,
              search_url: $("[data-f=search_url]", tr).value, search_enabled: $("[data-f=search_enabled]", tr).checked,
              sector_ids: JSON.parse(tr.dataset.sectors || "[]") });
          }
        };
        $("#s-test", m).onclick = () => run(async () => { await saveAll(); await api("POST", "/api/settings/test-email"); toast("Στάλθηκε! Έλεγξε τα εισερχόμενα."); });
        var save = () => run(async () => { await saveAll(); dirty = false; leave(); toast("Οι ρυθμίσεις αποθηκεύτηκαν"); refresh(); });
        m.addEventListener("input", (e) => { if (!e.target.closest("#ns-name,#ns-domain,#ns-url,#ns-country,#ns-secs,#s-imp-file,#nsec-name,#s-sectors")) dirty = true; });
        m.addEventListener("change", (e) => { if (!e.target.closest("#ns-name,#ns-domain,#ns-url,#ns-country,#ns-secs,#s-imp-file,#nsec-name,#s-sectors")) dirty = true; });
        $("#s-save", m).onclick = save; $("#s-save2", m).onclick = save;
        ["#s-back", "#s-cancel", "#s-cancel2"].forEach((x) => ($(x, m).onclick = () => close()));
        const exportSettings = (format) => run(async () => {
          await saveAll();
          const r = await api("POST", "/api/settings/export", { format });
          if (r.opened) toast(`Αποθηκεύτηκε: ${r.name}`, 6000);
          else window.location.href = "/api/settings/export?format=" + format;
        });
        $("#s-exp-json", m).onclick = () => exportSettings("json");
        $("#s-exp-xlsx", m).onclick = () => exportSettings("xlsx");
        $("#s-shops-xlsx", m).onclick = () => exportSettings("xlsx");
        $("#s-imp", m).onclick = () => $("#s-imp-file", m).click();
        $("#s-imp-file", m).onchange = (e) => run(async () => {
          const f = e.target.files[0];
          e.target.value = "";
          if (!f) return;
          if (!(await confirmBox("Εισαγωγή ρυθμίσεων", `Οι ρυθμίσεις θα αντικατασταθούν από αυτές του αρχείου «${esc(f.name)}». Τα καταστήματα του αρχείου προστίθενται ή ενημερώνονται· όσα δεν υπάρχουν στο αρχείο μένουν ως έχουν.`, "Εισαγωγή"))) return;
          const fd = new FormData();
          fd.append("file", f);
          const r = await apiForm("/api/settings/import", fd);
          toast(`Έγινε εισαγωγή. Καταστήματα: ${r.shops_added} νέα, ${r.shops_updated} ενημερώθηκαν.`, 6000);
          await renderSettings();
        });
    }
  }
}

// ---------------------------------------------------------------- export, status, κλείσιμο
async function exportXlsx(productId) {
  await run(async () => {
    const r = await api("POST", "/api/export", productId ? { product_id: productId } : {});
    if (r.opened) toast("Το Excel αποθηκεύτηκε και άνοιξε: " + r.path, 6000);
    else window.location.href = "/api/export.xlsx" + (productId ? "?product=" + productId : "");
  });
}

let lastRunning = false;
let statusFails = 0;
async function pollStatus() {
  try {
    const s = await api("GET", "/api/status");
    state.status = s;
    const parts = [];
    if (s.running) parts.push(`<span class="run">● ${esc(s.task || "Εργασία")} ${s.total ? `${s.progress}/${s.total}` : ""}</span>`);
    parts.push(`Τελευταίος έλεγχος: ${s.last_run ? fmtDate(s.last_run) : "–"}`);
    if (s.next_run) parts.push(`Επόμενος: ${fmtDate(s.next_run)}`);
    const hrs = String(s.interval_hours).replace(".", ",");
    parts.push(`κάθε ${hrs} ${s.interval_hours === 1 ? "ώρα" : "ώρες"}${s.bf_mode ? " (Black Friday)" : ""}`);
    const u = s.update || {};
    if (["downloading", "waiting", "installing"].includes(u.state)) parts.unshift(`<span class="run">⬆ ${esc(u.message)}</span>`);
    else if (u.available && !u.supported) parts.push(`<span class="run">Νέα έκδοση ${esc(u.latest)}</span>`);
    $("#status").innerHTML = parts.join(" · ");
    // «Υγεία»: ό,τι δεν δουλεύει φαίνεται αμέσως, όχι μετά τη Black Friday
    const hb = $("#health");
    const all = (s.health || []).map((x) => (typeof x === "string" ? { text: x } : x));
    const key = all.map((x) => x.text).join("|");
    const issues = key && key === state.healthDismissed ? [] : all;
    if (hb.dataset.key !== key + issues.length) {  // να μη ξαναχτίζεται (και χάνει την εστίαση) κάθε 5"
      hb.dataset.key = key + issues.length;
      hb.innerHTML = issues.map((x) => `<div class="h-item">⚠ ${esc(x.text)}${x.fix === "settings" ? ` <button class="small" data-hfix>Διόρθωση</button>` : ""}</div>`).join("")
        + (issues.length ? `<button class="small h-close" title="Απόκρυψη μέχρι να αλλάξει κάτι" aria-label="Απόκρυψη">✕</button>` : "");
      hb.querySelectorAll("[data-hfix]").forEach((b) => b.onclick = () => run(openSettings));
      const hc = hb.querySelector(".h-close"); if (hc) hc.onclick = () => { state.healthDismissed = key; hb.dataset.key = ""; hb.classList.add("hidden"); };
    }
    hb.classList.toggle("hidden", !issues.length);
    statusFails = 0;
    const bf = new Date(s.black_friday + "T00:00:00"), cm = new Date(s.cyber_monday + "T00:00:00");
    const days = Math.ceil((bf - new Date()) / 86400000);
    $("#bf-box").innerHTML = `<b>Black Friday:</b> ${bf.toLocaleDateString("el-GR")}<br><b>Cyber Monday:</b> ${cm.toLocaleDateString("el-GR")}<br>` +
      (days > 0 ? `σε ${days} μέρες` : days > -4 ? "<b>Τώρα!</b>" : "") + (s.bf_mode ? "<br>Έλεγχοι κάθε " + String(s.interval_hours).replace(".", ",") + (s.interval_hours === 1 ? " ώρα" : " ώρες") : "");
    if (lastRunning && !s.running && state.view !== "settings") refresh();  // όχι στις Ρυθμίσεις: θα χάνονταν οι αλλαγές
    lastRunning = s.running;
  } catch (e) {
    // Μερικές αποτυχίες είναι φυσιολογικές όσο ξεκινά ο server· μετά από λίγες, το λέμε αντί να «παγώνει» η μπάρα
    statusFails += 1;
    if (statusFails >= 3) $("#status").innerHTML = `<span class="warn">⚠ Δεν απαντά η εφαρμογή: ${esc(e.message)}</span>`;
  }
  setTimeout(pollStatus, lastRunning ? 2000 : 5000);
}

// Καλείται από το παράθυρο της εφαρμογής όταν πατηθεί το Χ
window.showCloseDialog = function () {
  if (document.getElementById("close-dlg")) return;
  modal(`<div id="close-dlg"><h2>Κλείσιμο Price Tracker</h2>
    <p>Θέλεις να κλείσει η εφαρμογή ή να μείνει στο <b>system tray</b> (κάτω δεξιά, δίπλα στο ρολόι) για να συνεχίσει τους ελέγχους;</p>
    <p class="hint">Με «Έξοδος» σταματούν οι έλεγχοι τιμών και οι ειδοποιήσεις μέχρι να ξανανοίξεις την εφαρμογή.</p>
    <label class="inline"><input type="checkbox" id="cd-rem"> Να θυμάσαι την επιλογή μου</label>
    <div class="btns"><button data-close>Άκυρο</button><button id="cd-exit" class="danger">Έξοδος</button><button id="cd-tray" class="primary">Παραμονή στο tray</button></div></div>`,
    { focus: "#cd-tray", onMount: (m, close) => {
        const go = (choice) => { const remember = $("#cd-rem", m).checked; close(); api("POST", "/api/window/close-choice", { choice, remember }); };
        $("#cd-exit", m).onclick = () => go("exit");
        $("#cd-tray", m).onclick = () => go("tray");
      } });
};

async function render() {
  await run(async () => {
    document.documentElement.classList.toggle("settings-view", state.view === "settings");
    if (state.view === "settings") await renderSettings();
    else if (state.view === "detail") await renderDetail();
    else if (state.purchased === "warranty") await renderWarranties();
    else await renderList();
  });
}
async function refresh() { await run(loadCategories); await render(); }

$("#btn-add").onclick = addProduct;
$("#btn-check").onclick = () => run(async () => { await api("POST", "/api/check"); toast("Ο έλεγχος όλων ξεκίνησε"); });
$("#btn-export").onclick = () => exportXlsx(null);
$("#btn-settings").onclick = () => { if (state.view !== "settings") run(openSettings); };
$("#btn-add-cat").onclick = addCategory;
document.querySelectorAll("#filter-tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#filter-tabs button").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); state.purchased = b.dataset.f; state.view = "list"; render();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const bgs = document.querySelectorAll(".modal-bg");
  if (bgs.length) { const top = bgs[bgs.length - 1]; top._close ? top._close() : top.remove(); }
  else if (state.view === "settings") { state.settingsGuard ? state.settingsGuard() : (state.view = "list", render()); }
});
// Η μπάρα αποθήκευσης των Ρυθμίσεων «κολλά» ακριβώς κάτω από την πάνω μπάρα, όσο ψηλή κι αν γίνει
(() => {
  const tb = document.querySelector(".topbar");
  const set = () => document.documentElement.style.setProperty("--topbar-h", tb.offsetHeight + "px");
  if (window.ResizeObserver) new ResizeObserver(set).observe(tb);
  set();
})();
refresh();
pollStatus();
