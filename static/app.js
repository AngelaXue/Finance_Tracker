// Every save in the app: POST a JSON body, resolve to the fetch Response.
function postJSON(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Date.toISOString() converts to UTC first, so it silently reports
// tomorrow's date once local time has passed midnight UTC (e.g. any
// evening in a US timezone) — never use it for "today" in the user's own
// calendar. This reads the local year/month/day directly instead.
function localISODate(date) {
  const d = date || new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

// Shown right after a save that flagged one or more rows as possible
// duplicates. A native confirm() only offers two buttons, but there are
// three genuinely different answers here, so this builds a small custom
// modal instead. Resolves to "continue" (keep it, not a duplicate),
// "discard" (delete it, it is one), or "cancel" (undecided — leave it
// exactly as saved and flagged, decide later).
function showDuplicateModal(count) {
  return new Promise((resolve) => {
    const isSingle = count === 1;
    const noun = isSingle ? "entry" : "entries";
    const verb = isSingle ? "matches" : "match";
    const pronoun = isSingle ? "it" : "them";

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal-box">
        <h3>Possible duplicate</h3>
        <p>The ${count} ${noun} you just saved ${verb} an existing transaction's account, date, and amount —
          it might be a real duplicate, or it might just be a second transaction that happens to look the same.</p>
        <p class="caption">Already saved either way. <strong>Continue</strong> confirms ${pronoun} — not a
          duplicate, clears the highlight. <strong>Discard</strong> deletes ${pronoun} now. <strong>Cancel</strong>
          leaves ${pronoun} exactly as saved (still highlighted pink) so you can look ${pronoun} over or edit
          ${isSingle ? "it" : "them"} yourself before deciding.</p>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary" data-modal-cancel>Cancel</button>
          <button type="button" class="btn btn-danger" data-modal-discard>Discard</button>
          <button type="button" class="btn btn-primary" data-modal-continue>Continue</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    function close(result) {
      overlay.remove();
      resolve(result);
    }
    overlay.querySelector("[data-modal-continue]").addEventListener("click", () => close("continue"));
    overlay.querySelector("[data-modal-discard]").addEventListener("click", () => close("discard"));
    overlay.querySelector("[data-modal-cancel]").addEventListener("click", () => close("cancel"));
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close("cancel"); // clicking the backdrop = undecided, same as Cancel
    });
  });
}

// Transactions page: inline edits are staged locally and committed via
// an explicit "Save changes" button — the spreadsheet mental model.
// Autocomplete for the Account, Description and Category cells. Account and
// Description are textareas (so long text wraps), which a native
// <datalist> can't attach to, and a datalist ignores Tab — so this is a
// small hand-rolled dropdown instead: one shared box,
// repositioned under whichever cell is being typed in. Suggestions come
// from the page itself (account names + past descriptions), nothing is
// fetched or stored. Filling a cell dispatches "input" so the normal
// dirty-tracking/auto-grow handlers run exactly as if it were typed.
function initSuggestions(table) {
  const dataEl = document.getElementById("suggestion-data");
  if (!dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  // tabTakesOnly: Tab fills the suggestion when it's the only one. Not for
  // descriptions — a new one often starts like an old one ("Uber" vs
  // "Uber Eats"), and Tab would silently swap it; there, Tab only takes a
  // suggestion highlighted with the arrow keys.
  const sources = {
    account_name: { options: data.accounts, minChars: 0, tabTakesOnly: true },
    description: { options: data.descriptions, minChars: 2, tabTakesOnly: false },
    category: { options: data.categories || [], minChars: 1, tabTakesOnly: true },
  };
  const MAX_SHOWN = 8;
  const box = document.createElement("div");
  box.className = "suggest-box";
  box.hidden = true;
  document.body.appendChild(box);
  let target = null;
  let matches = [];
  let active = -1;
  let suppress = false; // ignore the "input" event we dispatch ourselves

  const fieldOf = (el) => el && el.dataset && (el.dataset.field || el.dataset.newField);

  // Starts-with matches first, then contains; options are already ordered
  // most-used first, so each group keeps that order.
  function find(field, text) {
    const q = text.trim().toLowerCase();
    const { options, minChars } = sources[field];
    if (q.length < minChars) return [];
    const prefix = [];
    const contains = [];
    for (const opt of options) {
      const lower = opt.toLowerCase();
      if (lower === q) continue;
      if (lower.startsWith(q)) prefix.push(opt);
      else if (lower.includes(q)) contains.push(opt);
      if (prefix.length >= MAX_SHOWN) break;
    }
    return prefix.concat(contains).slice(0, MAX_SHOWN);
  }

  function hide() {
    box.hidden = true;
    matches = [];
    active = -1;
  }

  function fill(el, value) {
    el.value = value;
    suppress = true;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    suppress = false;
  }

  // Picking a past description also fills the row's Category/Method with
  // what's usually been used for it — only cells that are still empty, so
  // nothing already typed is overwritten. Type is only filled on a new row
  // whose Type hasn't been changed by hand; saved rows keep theirs.
  function choose(value) {
    const field = fieldOf(target);
    fill(target, value);
    const defaults = field === "description" && (data.defaults || {})[value];
    if (!defaults) return;
    const row = target.closest("tr");
    ["category", "method"].forEach((name) => {
      const cell = row.querySelector(`[data-field='${name}'], [data-new-field='${name}']`);
      if (cell && defaults[name] && !cell.value.trim()) fill(cell, defaults[name]);
    });
    const typeSelect = row.querySelector("select[data-new-field='transaction_type']");
    if (typeSelect && defaults.transaction_type && !typeSelect.dataset.setByHand) {
      typeSelect.value = defaults.transaction_type;
    }
  }

  function render() {
    box.replaceChildren(...matches.map((m, i) => {
      const item = document.createElement("div");
      item.className = "suggest-item" + (i === active ? " active" : "");
      item.textContent = m;
      // mousedown + preventDefault keeps focus in the cell (a click would
      // blur it first and close the box before the click lands).
      item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        choose(m);
        hide();
      });
      return item;
    }));
    const rect = target.getBoundingClientRect();
    box.style.left = `${rect.left}px`;
    box.style.top = `${rect.bottom}px`;
    box.style.minWidth = `${rect.width}px`;
    box.hidden = false;
  }

  function open(el) {
    const field = fieldOf(el);
    if (!sources[field]) return;
    target = el;
    matches = find(field, el.value);
    active = -1;
    if (matches.length) render();
    else hide();
  }

  table.addEventListener("input", (e) => { if (!suppress) open(e.target); });
  table.addEventListener("focusin", (e) => { if (fieldOf(e.target) === "account_name") open(e.target); });

  // Leaving an Account cell with a partial name that matches exactly one
  // account ("amex" → "Amex Gold") completes it, so Save doesn't
  // reject it as an unrecognized account.
  table.addEventListener("focusout", (e) => {
    hide();
    const el = e.target;
    if (fieldOf(el) !== "account_name") return;
    const q = el.value.trim().toLowerCase();
    if (!q || data.accounts.some((a) => a.toLowerCase() === q)) return;
    const hits = data.accounts.filter((a) => a.toLowerCase().includes(q));
    if (hits.length === 1) fill(el, hits[0]);
  });

  table.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      active = (active + step + matches.length) % matches.length;
      render();
    } else if ((e.key === "Enter" || e.key === "Tab") && active >= 0) {
      if (e.key === "Enter") e.preventDefault();
      choose(matches[active]);
      hide();
    } else if (e.key === "Tab" && matches.length === 1 && sources[fieldOf(target)].tabTakesOnly) {
      choose(matches[0]);
      hide();
    } else if (e.key === "Escape") {
      hide();
    }
  });

  // The box is positioned against the viewport, so it would drift away
  // from its cell on scroll — just close it instead.
  document.addEventListener("scroll", hide, true);
}

function initTransactionsPage() {
  const table = document.querySelector("[data-transactions-table]");
  if (!table) return;

  const body = document.querySelector("[data-transactions-body]");
  const saveBtn = document.querySelector("[data-save-btn]");
  const discardBtn = document.querySelector("[data-discard-btn]");
  const suggestBtn = document.querySelector("[data-suggest-btn]");
  const deleteBtn = document.querySelector("[data-delete-btn]");
  const setAttributableBtns = document.querySelectorAll("[data-set-attributable-btn]");
  const iouSelect = document.querySelector("[data-iou-select]");
  const iouPerson = document.querySelector("[data-iou-person]");
  const addRowBtn = document.querySelector("[data-add-row-btn]");
  const newRowTemplate = document.getElementById("new-row-template");
  const pendingEdits = {};
  // One selection set serves two purposes: Save treats a selected row as
  // "confirmed" (clears its needs-review flag, harmless no-op for a row
  // that was never flagged), and the Delete button treats it as "marked
  // for deletion" — same checkboxes, two independent actions you choose
  // between, not two different meanings baked into the checkbox itself.
  const selectedIds = new Set();
  let newRowCount = 0;

  const selectedTotalEl = document.querySelector("[data-selected-total]");
  const selectedCountEl = document.querySelector("[data-selected-count]");

  // Lets you build an ad hoc sum over any hand-picked set of rows (not just
  // whatever the filter dropdowns can express) by checking them — reads
  // live input values, so it reflects any not-yet-saved amount edits too.
  function updateSelectedTotal() {
    if (!selectedTotalEl) return;
    let total = 0;
    selectedIds.forEach((id) => {
      const input = table.querySelector(`input[data-field='amount'][data-id='${id}']`);
      if (input) total += parseFloat(input.value.replace(/,/g, "")) || 0;
    });
    selectedTotalEl.textContent = total.toLocaleString(undefined, MONEY_DIGITS);
    selectedTotalEl.classList.toggle("amount-positive", total >= 0);
    selectedTotalEl.classList.toggle("amount-negative", total < 0);
    if (selectedCountEl) selectedCountEl.textContent = `(${selectedIds.size} selected)`;
  }

  function updateSaveState() {
    const hasNewRows = body.querySelectorAll("tr.new-row").length > 0;
    const dirty = Object.keys(pendingEdits).length > 0 || selectedIds.size > 0 || hasNewRows;
    saveBtn.disabled = !dirty;
    discardBtn.disabled = !dirty;
    if (deleteBtn) deleteBtn.disabled = selectedIds.size === 0;
    setAttributableBtns.forEach((btn) => { btn.disabled = selectedIds.size === 0; });
    if (iouSelect) iouSelect.disabled = selectedIds.size === 0;
    if (iouPerson) iouPerson.disabled = selectedIds.size === 0;
  }

  function markDirty(id, field, value, originalValue) {
    if (value === originalValue) {
      if (pendingEdits[id]) {
        delete pendingEdits[id][field];
        if (Object.keys(pendingEdits[id]).length === 0) delete pendingEdits[id];
      }
    } else {
      pendingEdits[id] = pendingEdits[id] || {};
      pendingEdits[id][field] = value;
    }
    updateSaveState();
  }

  const rowCheckboxes = table.querySelectorAll("[data-row-id]");
  rowCheckboxes.forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = Number(cb.dataset.rowId);
      if (cb.checked) selectedIds.add(id);
      else selectedIds.delete(id);
      updateSaveState();
      updateSelectedTotal();
    });
  });

  const selectAll = table.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      rowCheckboxes.forEach((cb) => {
        cb.checked = selectAll.checked;
        cb.dispatchEvent(new Event("change"));
      });
    });
  }

  if (suggestBtn) {
    suggestBtn.addEventListener("click", async () => {
      suggestBtn.disabled = true;
      suggestBtn.textContent = "Working...";
      const res = await fetch("/api/transactions/suggest-categories", { method: "POST" });
      if (res.ok) {
        window.location.reload();
      } else {
        alert("Couldn't generate suggestions — please try again.");
        suggestBtn.disabled = false;
      }
    });
  }

  // Delete: its own explicit action with a confirm dialog, not bundled
  // into the batch Save flow — deletion is irreversible, so it shouldn't
  // ride along with edits you haven't explicitly submitted yet.
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      const ids = [...selectedIds];
      if (ids.length === 0) return;
      const noun = ids.length === 1 ? "transaction" : "transactions";
      if (!confirm(`Delete ${ids.length} selected ${noun} permanently? This cannot be undone.`)) return;
      const res = await postJSON("/api/transactions/delete", { ids });
      if (res.ok) {
        window.location.reload();
      } else {
        alert("Couldn't delete — please try again.");
      }
    });
  }

  // Mark Mine / Mark Not Mine and the IOU… dropdown: applied right away
  // to the selected rows, outside the batch Save flow. All reversible (mark
  // the rows the other way, or "Reset to automatic"), so no confirm dialog,
  // unlike Delete.
  async function applyToSelected(url, value, extra = {}) {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    // The page reloads afterward, which would silently drop unsaved edits.
    const hasUnsaved = Object.keys(pendingEdits).length > 0 || body.querySelectorAll("tr.new-row").length > 0;
    if (hasUnsaved && !confirm("You have unsaved edits that will be lost. Continue anyway?")) return;
    const res = await postJSON(url, { ids, value, ...extra });
    if (res.ok) {
      window.location.reload();
    } else {
      const data = await res.json().catch(() => ({}));
      alert(data.error || "Couldn't update — please try again.");
    }
  }

  setAttributableBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      applyToSelected("/api/transactions/set-attributable", Number(btn.dataset.value));
    });
  });

  // They paid / I paid need a person: the box next to the menu
  if (iouSelect) {
    iouSelect.addEventListener("change", () => {
      const value = iouSelect.value;
      iouSelect.value = "";
      if (!value) return;
      const person = iouPerson ? iouPerson.value.trim() : "";
      if ((value === "they_paid" || value === "i_paid") && !person) {
        alert("Type the person's name in the Person box first.");
        if (iouPerson) iouPerson.focus();
        return;
      }
      applyToSelected("/api/transactions/set-iou", value, { person });
    });
  }

  function autoGrow(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }

  // Reading scrollHeight forces a synchronous layout reflow, so calling
  // autoGrow() for one textarea at a time in a loop over every row (up to
  // 3 auto-growing fields per row) forces one reflow per element —
  // hundreds to thousands on a large table. Batching all the "auto"
  // writes first, then all the scrollHeight reads, then all the final
  // writes collapses that down to effectively one reflow for the whole
  // initial-load pass, same end result per element.
  function batchAutoGrow(elements) {
    elements.forEach((el) => { el.style.height = "auto"; });
    const heights = elements.map((el) => el.scrollHeight);
    elements.forEach((el, i) => { el.style.height = heights[i] + "px"; });
  }

  const initialTextareas = [];
  ["date", "account_name", "description", "category", "method", "amount", "note"].forEach((field) => {
    table.querySelectorAll(`input[data-field='${field}'], textarea[data-field='${field}']`).forEach((el) => {
      const original = el.value;
      if (el.tagName === "TEXTAREA") initialTextareas.push(el);
      el.addEventListener("input", () => {
        markDirty(el.dataset.id, field, el.value, original);
        if (el.tagName === "TEXTAREA") autoGrow(el);
        if (field === "amount") updateSelectedTotal();
      });
    });
  });
  batchAutoGrow(initialTextareas);

  table.querySelectorAll("select[data-field='transaction_type']").forEach((el) => {
    const original = el.value;
    el.addEventListener("change", () => markDirty(el.dataset.id, "transaction_type", el.value, original));
  });

  // Keeps every "+ Add row" entry pinned right below the sticky header
  // (stacked, if there's more than one) so you can add a row while
  // scrolled deep into your history without scrolling back up to fill it
  // in or back down to keep checking past entries. Same technique as the
  // header itself: sticky is set on each <td>, not the <tr> (unreliable
  // across browsers on table rows) — a CSS custom property set once per
  // row lets every cell in it share the same computed offset.
  function updateNewRowStickyOffsets() {
    const headerHeight = table.querySelector("thead").offsetHeight;
    let cumulative = headerHeight;
    body.querySelectorAll("tr.new-row").forEach((row) => {
      row.style.setProperty("--new-row-sticky-top", `${cumulative}px`);
      cumulative += row.offsetHeight;
    });
  }

  if (addRowBtn && newRowTemplate) {
    addRowBtn.addEventListener("click", () => {
      const fragment = newRowTemplate.content.cloneNode(true);
      const row = fragment.querySelector("tr");
      newRowCount += 1;
      row.dataset.newId = `new-${newRowCount}`;
      const dateInput = row.querySelector("[data-new-field='date']");
      if (dateInput) dateInput.value = localISODate();
      const typeSelect = row.querySelector("select[data-new-field='transaction_type']");
      if (typeSelect) typeSelect.addEventListener("change", () => { typeSelect.dataset.setByHand = "1"; });
      row.querySelector("[data-remove-new]").addEventListener("click", () => {
        row.remove();
        updateSaveState();
        updateNewRowStickyOffsets();
      });
      row.querySelectorAll("textarea[data-new-field]").forEach((el) => {
        el.addEventListener("input", () => {
          autoGrow(el);
          updateNewRowStickyOffsets(); // growing a textarea can shift rows below it
        });
      });
      body.prepend(row);
      updateSaveState();
      updateNewRowStickyOffsets();
    });
  }

  initSuggestions(table);

  discardBtn.addEventListener("click", () => {
    window.location.reload();
  });

  saveBtn.addEventListener("click", async () => {
    const edits = Object.entries(pendingEdits).map(([id, fields]) => ({ id: Number(id), ...fields }));
    const newRows = [...body.querySelectorAll("tr.new-row")].map((row) => {
      const entry = {};
      row.querySelectorAll("[data-new-field]").forEach((el) => {
        entry[el.dataset.newField] = el.value;
      });
      return entry;
    });
    if (edits.length === 0 && selectedIds.size === 0 && newRows.length === 0) return;
    const res = await postJSON("/api/transactions/save", { edits, confirmed_ids: [...selectedIds], new_rows: newRows });
    if (!res.ok) {
      alert("Save failed — please try again.");
      return;
    }
    const responseBody = await res.json().catch(() => ({}));
    // A 200 response can still contain per-row failures (e.g. a new
    // row's account name didn't exactly match an existing account) —
    // those must never pass silently, or a row can vanish with no
    // indication anything went wrong.
    if (responseBody.skipped && responseBody.skipped.length > 0) {
      const lines = responseBody.skipped.map((s) => `Row ${s.id ?? "(new)"}: ${s.reason}`);
      alert(`${responseBody.skipped.length} row(s) were NOT saved:\n\n${lines.join("\n")}`);
    }
    // Nothing is ever blocked for looking like a duplicate — it's always
    // saved — but ask right here what to do about it instead of just
    // leaving it pink and silent. Three genuinely different outcomes,
    // which a native confirm() can't offer (only two buttons):
    //   Continue — confirms it's not a duplicate, clears the flag
    //   Discard  — it really is a duplicate, delete it now
    //   Cancel   — undecided; leave it exactly as saved (still flagged
    //              pink) so it can be looked at / edited later, without
    //              forcing a decision right now
    const flaggedIds = responseBody.newly_flagged || [];
    if (flaggedIds.length > 0) {
      const action = await showDuplicateModal(flaggedIds.length);
      if (action === "discard") {
        await postJSON("/api/transactions/delete", { ids: flaggedIds });
      } else if (action === "continue") {
        await postJSON("/api/transactions/save", { edits: [], confirmed_ids: flaggedIds, new_rows: [] });
      }
      // action === "cancel": do nothing — leave it saved and flagged.
    }
    window.location.reload();
  });
}

// Period selector's "Custom Range" control: one dropdown, a year of months
// in a 3x4 grid, click a month to start the range and click another to end
// it (everything between highlights automatically) — Expedia-style, instead
// of two separate native date-picker inputs.
function initRangePickers() {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  document.querySelectorAll("[data-range-picker]").forEach((root) => {
    const trigger = root.querySelector("[data-range-trigger]");
    const panel = root.querySelector("[data-range-panel]");
    const monthsEl = root.querySelector("[data-range-months]");
    const yearLabel = root.querySelector("[data-range-year-label]");
    const prevYearBtn = root.querySelector("[data-range-prev-year]");
    const nextYearBtn = root.querySelector("[data-range-next-year]");
    const doneBtn = root.querySelector("[data-range-done]");
    const clearBtn = root.querySelector("[data-range-clear]");

    const periodParam = root.dataset.periodParam || "period";
    const startParam = root.dataset.startParam || "start";
    const endParam = root.dataset.endParam || "end";

    const today = new Date();
    const currentYear = today.getFullYear();
    const currentMonth = today.getMonth(); // 0-indexed

    const minDateStr = root.dataset.minDate;
    let minYear = null, minMonth = null;
    if (minDateStr) {
      const [my, mm] = minDateStr.split("-").map(Number);
      minYear = my;
      minMonth = mm - 1;
    }

    let start = null; // { year, month } — pending selection, not yet applied
    let end = null;
    let viewYear = currentYear;

    const initialStart = root.dataset.initialStart;
    const initialEnd = root.dataset.initialEnd;
    if (initialStart) {
      const [y, m] = initialStart.split("-").map(Number);
      start = { year: y, month: m - 1 };
      viewYear = y;
    }
    if (initialEnd) {
      const [y, m] = initialEnd.split("-").map(Number);
      end = { year: y, month: m - 1 };
    }

    function isFuture(year, month) {
      return year > currentYear || (year === currentYear && month > currentMonth);
    }

    function isBeforeMin(year, month) {
      if (minYear === null) return false;
      return year < minYear || (year === minYear && month < minMonth);
    }

    function monthValue(year, month) {
      return year * 12 + month;
    }

    function isEndpoint(year, month) {
      const val = monthValue(year, month);
      if (start && val === monthValue(start.year, start.month)) return true;
      if (end && val === monthValue(end.year, end.month)) return true;
      return false;
    }

    function inRange(year, month) {
      if (!start || !end) return false;
      const val = monthValue(year, month);
      const a = monthValue(start.year, start.month);
      const b = monthValue(end.year, end.month);
      const lo = Math.min(a, b), hi = Math.max(a, b);
      return val > lo && val < hi;
    }

    function pad(n) {
      return String(n).padStart(2, "0");
    }

    function lastDayOfMonth(year, month) {
      return new Date(year, month + 1, 0).getDate();
    }

    // Merge into the CURRENT page's query string so this picker's params
    // don't clobber a second, independent period control on the same page.
    function applyRange() {
      if (!start) return;
      let a = start, b = end || start;
      if (monthValue(a.year, a.month) > monthValue(b.year, b.month)) [a, b] = [b, a];
      const startDate = `${a.year}-${pad(a.month + 1)}-01`;
      const endDate = (b.year === currentYear && b.month === currentMonth)
        ? localISODate(today)
        : `${b.year}-${pad(b.month + 1)}-${pad(lastDayOfMonth(b.year, b.month))}`;
      const url = new URL(window.location.href);
      url.searchParams.set(periodParam, "custom");
      url.searchParams.set(startParam, startDate);
      url.searchParams.set(endParam, endDate);
      saveScrollPosition();
      window.location.href = url.toString();
    }

    function selectMonth(year, month) {
      if (!start || end) {
        start = { year, month };
        end = null;
      } else {
        end = { year, month };
      }
      render();
    }

    function render() {
      yearLabel.textContent = viewYear;
      nextYearBtn.disabled = viewYear >= currentYear;
      prevYearBtn.disabled = minYear !== null && viewYear <= minYear;
      doneBtn.disabled = !start;
      monthsEl.innerHTML = "";
      MONTHS.forEach((name, idx) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = name;
        btn.className = "range-month";
        if (isFuture(viewYear, idx) || isBeforeMin(viewYear, idx)) {
          btn.disabled = true;
          btn.classList.add("range-month-disabled");
        } else {
          if (isEndpoint(viewYear, idx)) btn.classList.add("range-month-endpoint");
          else if (inRange(viewYear, idx)) btn.classList.add("range-month-inrange");
          btn.addEventListener("click", () => selectMonth(viewYear, idx));
        }
        monthsEl.appendChild(btn);
      });
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      panel.hidden = !panel.hidden;
      if (!panel.hidden) render();
    });

    prevYearBtn.addEventListener("click", () => {
      if (minYear === null || viewYear > minYear) { viewYear -= 1; render(); }
    });
    nextYearBtn.addEventListener("click", () => {
      if (viewYear < currentYear) { viewYear += 1; render(); }
    });

    clearBtn.addEventListener("click", () => { start = null; end = null; render(); });
    doneBtn.addEventListener("click", applyRange);

    panel.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => { panel.hidden = true; });
  });
}

// Any period-control navigation (preset links or the range-picker's Done
// button) reloads the page — without this, the browser resets scroll to
// the top on every reload. Save the exact pixel position right before
// navigating, then restore it right after the reload. Scrolling happens
// inside .content (see style.css — the sidebar is fixed-height and
// scrolls independently), not on window/body, so that's what's saved and
// restored, not window.scrollY/scrollTo.
const SCROLL_POS_KEY = "ft-scroll-pos";

function saveScrollPosition() {
  const content = document.querySelector(".content");
  sessionStorage.setItem(SCROLL_POS_KEY, String(content ? content.scrollTop : 0));
}

function restoreScrollPosition() {
  const saved = sessionStorage.getItem(SCROLL_POS_KEY);
  if (saved === null) return;
  sessionStorage.removeItem(SCROLL_POS_KEY);
  const content = document.querySelector(".content");
  if (content) content.scrollTop = parseInt(saved, 10);
}

// IOUs page: two side-by-side panels (They paid / I paid) for the selected
// person, each with its own checkbox selection. Settled/Pending/Clear set a
// reminder mark on any selected entry; Delete only removes manual entries,
// with a confirm — transaction-derived entries are changed from the
// Transactions page instead.
function initIousPage() {
  const panels = document.querySelectorAll("[data-ledger-panel]");
  const addForm = document.querySelector("[data-ledger-add-form]");

  async function post(url, payload) {
    const res = await postJSON(url, payload);
    if (res.ok) {
      window.location.reload();
    } else {
      alert("Couldn't update — please try again.");
    }
  }

  panels.forEach((panel) => {
    const checkboxes = panel.querySelectorAll("[data-ledger-item]");
    const selectAll = panel.querySelector("[data-ledger-select-all]");
    const settleBtns = panel.querySelectorAll("[data-ledger-settle]");
    const deleteBtn = panel.querySelector("[data-ledger-delete-btn]");

    function selected() {
      return [...checkboxes]
        .filter((cb) => cb.checked)
        .map((cb) => ({ source: cb.dataset.source, id: Number(cb.dataset.id) }));
    }

    function updateButtons() {
      const items = selected();
      settleBtns.forEach((btn) => { btn.disabled = items.length === 0; });
      if (deleteBtn) deleteBtn.disabled = !items.some((i) => i.source === "manual");
    }

    checkboxes.forEach((cb) => cb.addEventListener("change", updateButtons));

    if (selectAll) {
      selectAll.addEventListener("change", () => {
        checkboxes.forEach((cb) => { cb.checked = selectAll.checked; });
        updateButtons();
      });
    }

    settleBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        post("/api/ious/set-settlement", { items: selected(), value: btn.dataset.ledgerSettle || null });
      });
    });

    if (deleteBtn) {
      deleteBtn.addEventListener("click", () => {
        const items = selected();
        const ids = items.filter((i) => i.source === "manual").map((i) => i.id);
        if (ids.length === 0) return;
        const skipped = items.length - ids.length;
        const noun = ids.length === 1 ? "manual entry" : "manual entries";
        let message = `Delete ${ids.length} ${noun} permanently? This cannot be undone.`;
        if (skipped) {
          message += `\n\n${skipped} selected transaction(s) will be left alone — change those from the Transactions page.`;
        }
        if (!confirm(message)) return;
        post("/api/ious/delete", { ids });
      });
    }
  });

  if (addForm) {
    const dateInput = addForm.querySelector("[name='date']");
    if (dateInput && !dateInput.value) dateInput.value = localISODate();
    addForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = Object.fromEntries(new FormData(addForm).entries());
      const res = await postJSON("/api/ious/add", payload);
      if (res.ok) {
        window.location.reload();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(body.error || "Couldn't add entry — please check the fields and try again.");
      }
    });
  }
}

// Import page: upload statements into an account's folder, Preview (a
// rolled-back dry run with sample rows), then Import. Each file's "Amount
// signs" choice is sent with both. The optional history section works the
// same way with its own endpoints.
function initImportPage() {
  const page = document.querySelector("[data-import-page]");
  if (!page) return;

  function escapeHTML(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }
  const money = (x) => x.toLocaleString(undefined, MONEY_DIGITS);

  async function upload(form, url) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = form.querySelector("button[type='submit']");
      button.disabled = true;
      const res = await fetch(url, { method: "POST", body: new FormData(form) });
      if (res.ok) {
        window.location.reload();
      } else {
        const data = await res.json().catch(() => ({}));
        alert(data.error || "Couldn't add the files — please try again.");
        button.disabled = false;
      }
    });
  }
  const uploadForm = document.querySelector("[data-import-upload-form]");
  if (uploadForm) upload(uploadForm, "/api/import/upload");
  const historyForm = document.querySelector("[data-history-upload-form]");
  if (historyForm) upload(historyForm, "/api/import/history/upload");

  document.querySelectorAll("[data-import-discard]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Delete ${btn.dataset.name} without importing it?`)) return;
      const res = await postJSON("/api/import/discard", { kind: btn.dataset.kind, name: btn.dataset.name });
      if (res.ok) window.location.reload();
      else alert("Couldn't remove it — reload the page and try again.");
    });
  });

  function signs() {
    const out = {};
    document.querySelectorAll("[data-import-file]").forEach((row) => {
      const select = row.querySelector("[data-import-sign]");
      if (select) out[row.dataset.importFile] = select.value;
    });
    return out;
  }

  function renderFiles(data, imported) {
    const parts = data.files.map((f) => {
      if (f.error) {
        return `<div class="import-file-result import-error"><strong>${escapeHTML(f.name)}</strong> — not imported: ${escapeHTML(f.error)}</div>`;
      }
      const span = f.rows ? ` · ${f.first_date} → ${f.last_date}` : "";
      const dups = f.flagged ? ` · <span class="amount-negative-text">${f.flagged} possible duplicate(s)</span>` : "";
      const note = f.sign_note ? `<div class="caption">${escapeHTML(f.sign_note)}</div>` : "";
      const sample = (f.sample || []).map((r) =>
        `<tr><td>${r.date}</td><td>${escapeHTML(r.description)}</td>` +
        `<td class="num ${r.amount < 0 ? "amount-negative" : "amount-positive"}">${money(r.amount)}</td>` +
        `<td>${r.transaction_type}</td></tr>`).join("");
      const table = sample ? `<table class="nw-table import-sample"><tr><th>Date</th><th>Description</th>` +
        `<th class="num">Amount</th><th>Type</th></tr>${sample}</table>` +
        `<div class="caption">First ${f.sample.length} rows. Money going out should be negative (red).</div>` : "";
      return `<div class="import-file-result"><strong>${escapeHTML(f.name)}</strong> — ${f.rows} rows` +
        ` (${escapeHTML(f.format)})${span}${dups}${note}${table}</div>`;
    });
    const verb = imported ? "Imported" : "Would import";
    let summary = `<p><strong>${verb} ${data.inserted} transaction(s).</strong>`;
    if (data.flagged) summary += ` ${data.flagged} look like rows already here — they're added and highlighted pink on Transactions for you to check.`;
    if (imported) summary += ` ${data.suggested} category suggestion(s) to review (yellow on Transactions).`;
    summary += "</p>";
    return summary + parts.join("");
  }

  const result = document.querySelector("[data-import-result]");
  const previewBtn = document.querySelector("[data-import-preview]");
  const runBtn = document.querySelector("[data-import-run]");
  if (previewBtn) {
    previewBtn.addEventListener("click", async () => {
      previewBtn.disabled = true;
      result.innerHTML = '<p class="caption">Reading files…</p>';
      const res = await postJSON("/api/import/preview", { signs: signs() });
      const data = await res.json().catch(() => null);
      result.innerHTML = res.ok && data ? renderFiles(data, false) : '<p class="amount-negative-text">Preview failed.</p>';
      previewBtn.disabled = false;
    });
  }
  if (runBtn) {
    runBtn.addEventListener("click", async () => {
      if (!confirm("Import these files? Files that go in cleanly are deleted afterwards.")) return;
      runBtn.disabled = true;
      result.innerHTML = '<p class="caption">Importing…</p>';
      const res = await postJSON("/api/import/run", { signs: signs() });
      const data = await res.json().catch(() => null);
      if (res.ok && data) {
        result.innerHTML = renderFiles(data, true) +
          `<p><a href="/transactions?period=all&review=needs_review">Review new categories →</a> · ` +
          `<a href="/import">Refresh this page</a></p>`;
      } else {
        result.innerHTML = '<p class="amount-negative-text">Import failed — nothing was saved.</p>';
        runBtn.disabled = false;
      }
    });
  }

  function renderHistory(data, imported) {
    const verb = imported ? "Did" : "Would";
    let html = `<p><strong>${verb} create ${data.accounts_created} account(s), add ${data.inserted} transaction(s)` +
      ` and ${data.balances} balance(s).</strong> ${data.already_present} already here (skipped).</p>`;
    if (data.categories.length) {
      html += `<p class="caption">${data.categories.length} categories — check for near-duplicates (e.g. "Groceries" vs "grocery") before importing:</p>` +
        `<p class="caption">${data.categories.map(([c, n]) => `${escapeHTML(c)} (${n})`).join(" · ")}</p>`;
    }
    if (data.errors.length) {
      html += `<p class="amount-negative-text">${data.errors.length} row(s) skipped:</p><ul class="caption ledger-tips">` +
        data.errors.slice(0, 50).map((e) => `<li>${escapeHTML(e)}</li>`).join("") + "</ul>";
    }
    if (imported && !data.errors.length) html += '<p class="caption">Done — the files were deleted.</p>';
    return html;
  }
  const historyResult = document.querySelector("[data-history-result]");
  [["[data-history-preview]", "/api/import/history/preview", false],
   ["[data-history-run]", "/api/import/history/run", true]].forEach(([selector, url, imported]) => {
    const btn = document.querySelector(selector);
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (imported && !confirm("Import this history now?")) return;
      btn.disabled = true;
      historyResult.innerHTML = '<p class="caption">Working…</p>';
      const res = await postJSON(url, {});
      const data = await res.json().catch(() => null);
      historyResult.innerHTML = res.ok && data ? renderHistory(data, imported) : '<p class="amount-negative-text">That didn\'t work.</p>';
      btn.disabled = false;
    });
  });
}

// Staged inline edits (CDs & Bonds tables, Investments' All entries): each input
// remembers its original value; changing it records edits[key][field] and
// highlights its cell, changing it back removes the entry. Nothing is
// written until the caller's Save sends `edits`.
function trackStagedEdits(inputs, edits, keyOf, fieldOf, onChange) {
  inputs.forEach((el) => {
    const original = el.value;
    const eventName = el.tagName === "SELECT" || el.type === "date" ? "change" : "input";
    el.addEventListener(eventName, () => {
      const key = keyOf(el);
      const field = fieldOf(el);
      if (el.value === original) {
        if (edits[key]) {
          delete edits[key][field];
          if (Object.keys(edits[key]).length === 0) delete edits[key];
        }
      } else {
        edits[key] = edits[key] || {};
        edits[key][field] = el.value;
      }
      el.closest("td").classList.toggle("cd-edited", el.value !== original);
      onChange();
    });
  });
}

// CDs & Bonds tables (Net Worth: all of them; Investments: each account's own) — inline
// edits + bulk delete, mirroring the IOUs page's checkbox-select
// + "Delete selected" + confirm pattern. Each [data-cd-section] is wired
// on its own, so several tables on one page don't share buttons.
function initCdsSection() {
  document.querySelectorAll("[data-cd-section]").forEach(initCdTable);
  initCdAddForm();
}

function initCdTable(section) {
  const deleteBtn = section.querySelector("[data-cd-delete-btn]");
  const selectAll = section.querySelector("[data-cd-select-all]");
  const selectedIds = new Set();

  function rowCheckboxes() {
    return section.querySelectorAll("[data-cd-row-id]");
  }

  rowCheckboxes().forEach((cb) => {
    cb.addEventListener("change", () => {
      const id = Number(cb.dataset.cdRowId);
      if (cb.checked) selectedIds.add(id);
      else selectedIds.delete(id);
      if (deleteBtn) deleteBtn.disabled = selectedIds.size === 0;
    });
  });

  if (selectAll) {
    selectAll.addEventListener("change", () => {
      rowCheckboxes().forEach((cb) => {
        cb.checked = selectAll.checked;
        cb.dispatchEvent(new Event("change"));
      });
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      const ids = [...selectedIds];
      if (ids.length === 0) return;
      const noun = ids.length === 1 ? "row" : "rows";
      if (!confirm(`Delete ${ids.length} selected ${noun} permanently? This cannot be undone.`)) return;
      const res = await postJSON("/api/cds/delete", { ids });
      if (res.ok) {
        window.location.reload();
      } else {
        alert("Couldn't delete — please try again.");
      }
    });
  }

  // Inline edits: same staged model as Transactions — changes collect
  // locally and nothing is written until Save changes.
  const saveBtn = section.querySelector("[data-cd-save-btn]");
  const discardBtn = section.querySelector("[data-cd-discard-btn]");
  const pendingEdits = {};

  function updateEditButtons() {
    const dirty = Object.keys(pendingEdits).length > 0;
    if (saveBtn) saveBtn.disabled = !dirty;
    if (discardBtn) discardBtn.disabled = !dirty;
  }

  trackStagedEdits(section.querySelectorAll("[data-cd-field]"), pendingEdits,
    (el) => el.dataset.cdId, (el) => el.dataset.cdField, updateEditButtons);

  if (discardBtn) discardBtn.addEventListener("click", () => window.location.reload());

  const unsavedOk = () => Object.keys(pendingEdits).length === 0
    || confirm("You have unsaved edits in this table — they'll be discarded. Continue?");

  // Add interest: one entry for one CD/bond; its Current Balance becomes
  // principal + all its interest
  const interestForm = section.querySelector("[data-cd-interest-form]");
  if (interestForm) {
    interestForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!unsavedOk()) return;
      const data = new FormData(interestForm);
      const res = await postJSON("/api/cds/interest/add", {
        cd_id: Number(data.get("cd_id")), date: data.get("date"),
        amount: data.get("amount"), note: data.get("note"),
      });
      if (res.ok) {
        window.location.reload();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(body.error || "Couldn't add it — please check the fields and try again.");
      }
    });
  }

  const interestBoxes = [...section.querySelectorAll("[data-cd-interest-id]")];
  const interestDelete = section.querySelector("[data-cd-interest-delete]");
  interestBoxes.forEach((cb) => cb.addEventListener("change", () => {
    if (interestDelete) interestDelete.disabled = !interestBoxes.some((b) => b.checked);
  }));
  if (interestDelete) {
    interestDelete.addEventListener("click", async () => {
      const ids = interestBoxes.filter((b) => b.checked).map((b) => Number(b.dataset.cdInterestId));
      if (ids.length === 0 || !unsavedOk()) return;
      if (!confirm(`Delete ${ids.length} interest entr${ids.length === 1 ? "y" : "ies"}?`)) return;
      const res = await postJSON("/api/cds/interest/delete", { ids });
      if (res.ok) window.location.reload();
      else alert("Couldn't delete — please try again.");
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const edits = Object.entries(pendingEdits).map(([id, fields]) => ({ id: Number(id), ...fields }));
      if (edits.length === 0) return;
      const res = await postJSON("/api/cds/update", { edits });
      if (res.ok) {
        window.location.reload();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(`${body.error || "Couldn't save"} — nothing was saved, your edits are still on the page.`);
      }
    });
  }
}

function initCdAddForm() {
  const addForm = document.querySelector("[data-cd-add-form]");
  if (addForm) {
    addForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const formData = new FormData(addForm);
      const payload = {
        account_name: formData.get("account_name"),
        institution: formData.get("institution"),
        principal: formData.get("principal"),
        apy: formData.get("apy"),
        kind: formData.get("kind"),
        opened_date: formData.get("opened_date"),
        maturity_date: formData.get("maturity_date"),
        note: formData.get("note"),
      };
      const res = await postJSON("/api/cds/add", payload);
      if (res.ok) {
        window.location.reload();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(body.error || "Couldn't add it — please check the fields and try again.");
      }
    });
  }
}

// Investments page: "which account did this go to" dropdowns (needs-a-
// look list + All entries). Staged like Transactions: a change only
// highlights the row; that table's Save changes commits them all, Discard
// clears them. In the needs-a-look list, coming back to the tab (e.g.
// after editing a note on Transactions) re-checks each row's guess and
// fills in any dropdown that hasn't been picked by hand.
function initInvestmentAssign() {
  document.querySelectorAll("[data-inv-assign-group]").forEach((group) => {
    const selects = [...group.querySelectorAll("[data-inv-assign]")];
    const saveBtn = group.querySelector("[data-inv-assign-save]");
    const discardBtn = group.querySelector("[data-inv-assign-discard]");
    const countLabel = group.querySelector("[data-inv-assign-count]");
    if (!selects.length || !saveBtn) return;

    function update() {
      const staged = selects.filter((s) => s.value);
      selects.forEach((s) => s.closest("td").classList.toggle("cd-edited", Boolean(s.value)));
      saveBtn.disabled = discardBtn.disabled = staged.length === 0;
      if (countLabel) countLabel.textContent = staged.length ? `${staged.length} unsaved` : "";
    }

    selects.forEach((s) => s.addEventListener("change", () => {
      s.dataset.pickedByHand = s.value ? "1" : "";
      update();
    }));

    discardBtn.addEventListener("click", () => {
      selects.forEach((s) => { s.value = ""; s.dataset.pickedByHand = ""; });
      update();
    });

    saveBtn.addEventListener("click", async () => {
      const assignments = selects.filter((s) => s.value)
        .map((s) => ({ id: Number(s.dataset.invAssign), target: s.value }));
      if (!assignments.length) return;
      saveBtn.disabled = true;
      const res = await postJSON("/api/investments/assign", { assignments });
      if (res.ok) {
        window.location.reload();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(`${body.error || "Couldn't save"} — nothing was saved, your choices are still on the page.`);
        update();
      }
    });

    if (group.hasAttribute("data-inv-guess-refresh")) {
      const refreshGuesses = async () => {
        const ids = selects.map((s) => s.dataset.invAssign).join(",");
        const res = await fetch(`/api/investments/guesses?ids=${ids}`).catch(() => null);
        if (!res || !res.ok) return;
        const guesses = await res.json();
        selects.forEach((s) => {
          const g = guesses[s.dataset.invAssign];
          if (!g || s.dataset.pickedByHand) return; // never overwrite a hand-picked choice
          s.value = g.guess ? String(g.guess) : "";
        });
        update();
      };
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshGuesses();
      });
      window.addEventListener("focus", refreshGuesses);
    }

    update();
  });
}

// Investments page, All entries: current-value ("snap:<id>") and
// principal-added ("dep:<id>") rows edit in place, and contribution rows'
// account dropdowns are staged; one Save changes commits all of them,
// Discard reverts, Delete selected removes checked rows after a confirm.
function initInvestmentEntries() {
  const root = document.querySelector("[data-inv-entries]");
  if (!root) return;
  const saveBtn = root.querySelector("[data-entries-save]");
  const discardBtn = root.querySelector("[data-entries-discard]");
  const deleteBtn = root.querySelector("[data-entries-delete]");
  const countLabel = root.querySelector("[data-entries-count]");
  if (!saveBtn) return;
  const edits = {}; // "snap:12" / "dep:3" -> { field: value }
  const assignSelects = [...root.querySelectorAll("[data-inv-assign]")];
  const checkboxes = [...root.querySelectorAll("[data-entry-select]")];

  function update() {
    const editCount = Object.keys(edits).length;
    const assignCount = assignSelects.filter((s) => s.value).length;
    assignSelects.forEach((s) => s.closest(".inv-entry-assign").classList.toggle("cd-edited", Boolean(s.value)));
    saveBtn.disabled = discardBtn.disabled = editCount + assignCount === 0;
    const parts = [];
    if (editCount) parts.push(`${editCount} entr${editCount === 1 ? "y" : "ies"} edited`);
    if (assignCount) parts.push(`${assignCount} account change${assignCount === 1 ? "" : "s"}`);
    countLabel.textContent = parts.join(" · ");
    deleteBtn.disabled = !checkboxes.some((cb) => cb.checked);
  }

  trackStagedEdits(root.querySelectorAll("[data-entry-field]"), edits,
    (el) => el.dataset.entryKey, (el) => el.dataset.entryField, update);

  // "snap:12" -> { kind: "snap", id: 12 }
  const parseKey = (key) => ({ kind: key.split(":")[0], id: Number(key.split(":")[1]) });
  function editsOf(kind) {
    return Object.entries(edits)
      .filter(([key]) => parseKey(key).kind === kind)
      .map(([key, fields]) => ({ id: parseKey(key).id, ...fields }));
  }
  assignSelects.forEach((s) => s.addEventListener("change", update));
  checkboxes.forEach((cb) => cb.addEventListener("change", update));

  discardBtn.addEventListener("click", () => window.location.reload());

  async function post(url, body) {
    const res = await postJSON(url, body);
    if (res.ok) return null;
    const data = await res.json().catch(() => ({}));
    return data.error || "Couldn't save";
  }

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    // Each request is all-or-nothing on its own; stop at the first failure
    const steps = [
      ["/api/balances/update", { edits: editsOf("snap") }, "edits"],
      ["/api/investments/deposits/update", { edits: editsOf("dep") }, "edits"],
      ["/api/investments/assign", {
        assignments: assignSelects.filter((s) => s.value)
          .map((s) => ({ id: Number(s.dataset.invAssign), target: s.value })),
      }, "assignments"],
    ];
    let savedSomething = false;
    for (const [url, body, listKey] of steps) {
      if (!body[listKey].length) continue;
      const error = await post(url, body);
      if (error) {
        alert(savedSomething
          ? `${error} — some changes were saved; the rest weren't.`
          : `${error} — nothing was saved, your edits are still on the page.`);
        if (savedSomething) window.location.reload();
        else update();
        return;
      }
      savedSomething = true;
    }
    window.location.reload();
  });

  deleteBtn.addEventListener("click", async () => {
    const picked = checkboxes.filter((cb) => cb.checked).map((cb) => parseKey(cb.dataset.entrySelect));
    if (!picked.length) return;
    const noun = picked.length === 1 ? "entry" : "entries";
    if (!confirm(`Delete ${picked.length} ${noun} permanently? This cannot be undone.`)) return;
    const snapIds = picked.filter((p) => p.kind === "snap").map((p) => p.id);
    const depIds = picked.filter((p) => p.kind === "dep").map((p) => p.id);
    const error = (snapIds.length && await post("/api/balances/delete", { ids: snapIds }))
      || (depIds.length && await post("/api/investments/deposits/delete", { ids: depIds }));
    if (error) alert(error);
    window.location.reload();
  });

  update();
}

// Help page: sections are folded <details>. Any in-page link (the menu,
// or a "see X" link inside a section) and arriving with a #hash (e.g. the
// (i) next to Type on Transactions) opens the target section and scrolls
// to it. Expand all / Collapse all do what they say.
function initHelpPage() {
  const sections = [...document.querySelectorAll("details.help-section")];
  if (!sections.length) return;

  function openSection(id, scroll) {
    const target = document.getElementById(id);
    if (!target) return;
    const section = target.closest("details.help-section");
    if (!section) return;
    section.open = true;
    if (scroll) target.scrollIntoView({ behavior: "smooth", block: "start" });
    section.classList.add("help-flash");
    setTimeout(() => section.classList.remove("help-flash"), 900);
  }

  document.querySelectorAll('a[href^="#"]').forEach((link) => {
    link.addEventListener("click", (e) => {
      const id = link.getAttribute("href").slice(1);
      if (!document.getElementById(id)) return;
      e.preventDefault();
      history.replaceState(null, "", `#${id}`);
      openSection(id, true);
    });
  });

  const expand = document.querySelector("[data-help-expand]");
  const collapse = document.querySelector("[data-help-collapse]");
  if (expand) expand.addEventListener("click", () => sections.forEach((s) => { s.open = true; }));
  if (collapse) collapse.addEventListener("click", () => sections.forEach((s) => { s.open = false; }));

  if (location.hash) openSection(decodeURIComponent(location.hash.slice(1)), true);
  window.addEventListener("hashchange", () => openSection(decodeURIComponent(location.hash.slice(1)), true));

  initHelpSearch(sections, openSection);
}

// Help search — runs entirely in the browser over the page's own text (no
// server, nothing sent anywhere). "Smarter" than a plain word match, but
// not AI: each topic (an h3 and what follows it, or a section's intro) is
// scored on
//   - word stems: "reimbursed" / "reimbursement" / "reimburse" all match
//   - related words (HELP_RELATED): "owe" also finds IOU/debt/loan
//   - typos and half-typed words: "duplciate", "reimb"
//   - where it hit: topic title > section title > body text
//   - rarer words count for more
// and a topic matching every query word beats one matching only some.
// Stopwords drop out, so a question ("how do I fix a wrong category")
// works too. "/" focuses the box; arrows + Enter pick; Esc clears;
// /help?q=... opens with a search filled in.
const HELP_STOPWORDS = new Set((
  "a an and are as at be by can could do does did for from get got has have how i if in into is it its " +
  "me my of on or should so that the their them then there these they this to was what when where which " +
  "who why will with would you your not no about up out just also any all go goes going happen happens"
).split(" "));

// Groups of words that mean roughly the same thing here. Extend when a new
// concept gets its own vocabulary (e.g. a feature you add for your user).
const HELP_RELATED = [
  "work employer company business job",
  "reimburse reimbursement payback repay claim",
  "passthrough forward pass",
  "duplicate dupe double twice repeat copy pink",
  "iou owe owed debt lend loan borrow friend family person someone",
  "notmine attributable gift",
  "delete remove erase",
  "import csv upload download statement file parse format",
  "networth balance asset liability",
  "invest investment brokerage ira retirement stock snapshot principal",
  "cd bond treasury certificate interest",
  "category label classify categorize classifier",
  "type kind",
  "transfer move payoff",
  "venmo paypal wallet cash zelle",
  "refund credit return",
  "date period range month year",
  "spending spend expense purchase",
  "income salary paycheck deposit inflow earn",
  "savings save residual",
  "fix wrong mistake error undo correct edit change",
  "paid pay payment settle settled",
  "review check yellow predict guess suggest",
  "chart graph trend diagram sankey",
  "cashflow sankey inflow outflow",
  "currency exchange rate fx convert",
  "demo sample fake",
  "account bank checking",
];

function helpNormalize(text) {
  return text.toLowerCase()
    .replace(/pass[\s-]?through/g, "passthrough")
    .replace(/not mine/g, "notmine")
    .replace(/net worth/g, "networth")
    .replace(/cash flow/g, "cashflow");
}

function helpStem(word) {
  let w = word;
  if (w.length <= 3) return w;
  w = w.replace(/ies$/, "y");
  const strip = (re) => {
    const m = w.match(re);
    if (!m || w.length - m[0].length < 3) return false;
    w = w.slice(0, -m[0].length);
    return true;
  };
  // One ending only — "reimbursed" → "reimburs", not on to "reimbur"
  if (!strip(/(ments?|ings?|ed)$/) && !w.endsWith("ss")) strip(/s$/);
  strip(/e$/);
  return w;
}

function helpTokens(text) {
  return helpNormalize(text).split(/[^a-z0-9]+/)
    .filter((w) => w && !HELP_STOPWORDS.has(w))
    .map(helpStem);
}

// Edit distance, giving up (returns max + 1) once it can't be <= max
function helpEditDistance(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prevPrev = null;
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let rowMin = i;
    for (let j = 1; j <= b.length; j++) {
      let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      // a swapped pair of letters counts as one typo
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) v = Math.min(v, prevPrev[j - 2] + 1);
      cur.push(v);
      rowMin = Math.min(rowMin, v);
    }
    if (rowMin > max) return max + 1;
    prevPrev = prev;
    prev = cur;
  }
  return prev[b.length];
}

function initHelpSearch(sections, openSection) {
  const input = document.querySelector("[data-help-search]");
  const list = document.querySelector("[data-help-results]");
  if (!input || !list) return;

  // related[stem] → stems in the same group(s)
  const related = new Map();
  HELP_RELATED.forEach((group) => {
    const stems = group.split(" ").map(helpStem);
    stems.forEach((s) => {
      if (!related.has(s)) related.set(s, new Set());
      stems.forEach((o) => { if (o !== s) related.get(s).add(o); });
    });
  });

  // One topic per h3 (plus each section's intro), with its stems by field
  const topics = [];
  let autoId = 0;
  sections.forEach((section) => {
    const sectionTitle = section.querySelector(".help-summary h2").textContent.trim();
    const caption = (section.querySelector(".help-summary .caption") || {}).textContent || "";
    let current = { section, sectionTitle, title: sectionTitle, anchorId: section.id, intro: true, parts: [caption] };
    topics.push(current);
    const body = section.querySelector(".help-body");
    [...(body ? body.children : [])].forEach((el) => {
      if (el.tagName === "H3") {
        if (!el.id) el.id = `help-topic-${++autoId}`;
        current = { section, sectionTitle, title: el.textContent.trim(), anchorId: el.id, intro: false, parts: [] };
        topics.push(current);
      } else {
        current.parts.push(el.textContent);
      }
    });
  });
  const vocab = new Set();
  topics.forEach((t) => {
    t.text = t.parts.join(" ").replace(/\s+/g, " ").trim();
    t.norm = helpNormalize(`${t.title} ${t.text}`);
    t.titleStems = new Set(helpTokens(t.title));
    t.sectionStems = new Set(helpTokens(t.sectionTitle));
    t.bodyStems = new Map();
    helpTokens(t.text).forEach((s) => t.bodyStems.set(s, (t.bodyStems.get(s) || 0) + 1));
    [...t.titleStems, ...t.sectionStems, ...t.bodyStems.keys()].forEach((s) => vocab.add(s));
  });
  // Rarer words count for more: "passthrough" says more than "mark"
  const docFreq = new Map();
  topics.forEach((t) => {
    new Set([...t.titleStems, ...t.sectionStems, ...t.bodyStems.keys()])
      .forEach((s) => docFreq.set(s, (docFreq.get(s) || 0) + 1));
  });
  const rarity = (s) => Math.log(1 + topics.length / (docFreq.get(s) || 1));

  // A query word → every stem it should match, with a weight (1 = exact)
  function expand(q, isLast) {
    const m = new Map();
    const add = (s, w) => { if ((m.get(s) || 0) < w) m.set(s, w); };
    add(q, 1);
    (related.get(q) || []).forEach((s) => add(s, 0.55));
    vocab.forEach((s) => {
      if (s === q) return;
      if (s.startsWith(q) && (q.length >= 4 || (isLast && q.length >= 3))) add(s, 0.75);
      else if (q.length >= 5) {
        const d = helpEditDistance(q, s, 2);
        if (d === 1) add(s, 0.6);
        else if (d === 2 && q.length >= 8) add(s, 0.45);
      }
    });
    // A typo'd or half-typed word still brings its related words along
    [...m].forEach(([s, w]) => {
      if (s !== q && w >= 0.6) (related.get(s) || []).forEach((r) => add(r, 0.4));
    });
    return m;
  }

  function search(query) {
    const raw = helpNormalize(query).split(/[^a-z0-9]+/).filter((w) => w && !HELP_STOPWORDS.has(w));
    if (!raw.length) return { results: [], terms: [] };
    const endsMidWord = /[a-z0-9]$/i.test(query);
    const terms = raw.map((w, i) => expand(helpStem(w), endsMidWord && i === raw.length - 1));
    const phrase = raw.length > 1 ? raw.join(" ") : null;
    const results = [];
    topics.forEach((t) => {
      let score = 0;
      let matched = 0;
      let exact = 0;
      terms.forEach((exp) => {
        let best = 0;
        let bestWeight = 0;
        exp.forEach((w, s) => {
          let v = 0;
          if (t.titleStems.has(s)) v = Math.max(v, 3 * w);
          if (t.sectionStems.has(s)) v = Math.max(v, 2 * w);
          const c = t.bodyStems.get(s);
          if (c) v = Math.max(v, w * (1 + 0.5 * Math.min(Math.log2(c), 2)));
          v *= rarity(s);
          if (v > best) { best = v; bestWeight = w; }
        });
        if (best > 0) { matched++; score += best; if (bestWeight === 1) exact++; }
      });
      if (!matched) return;
      score *= (matched / terms.length) ** 2;
      if (phrase && t.norm.includes(phrase)) score += 3;
      results.push({ topic: t, score, exact });
    });
    results.sort((a, b) => b.score - a.score);
    return { results: results.slice(0, 8), terms };
  }

  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // The sentence with the most hits, trimmed around the first one, with
  // every matching word highlighted
  function snippet(topic, terms) {
    const hit = (word) => {
      const s = helpStem(helpNormalize(word));
      return terms.some((exp) => exp.has(s));
    };
    const sentences = (topic.text || "").split(/(?<=[.!?:])\s+/).filter(Boolean);
    let best = sentences[0] || "";
    let bestHits = -1;
    sentences.forEach((sentence) => {
      const n = (sentence.match(/[A-Za-z0-9][A-Za-z0-9']*/g) || []).filter(hit).length;
      if (n > bestHits) { best = sentence; bestHits = n; }
    });
    let text = best;
    if (text.length > 200) {
      const words = [...text.matchAll(/[A-Za-z0-9][A-Za-z0-9']*/g)];
      const first = words.find((m) => hit(m[0]));
      const start = Math.max(0, (first ? first.index : 0) - 60);
      text = (start > 0 ? "…" : "") + text.slice(start, start + 200).trim() + "…";
    }
    let out = "";
    let last = 0;
    text.replace(/[A-Za-z0-9][A-Za-z0-9']*/g, (word, idx) => {
      out += esc(text.slice(last, idx)) + (hit(word) ? `<mark>${esc(word)}</mark>` : esc(word));
      last = idx + word.length;
      return word;
    });
    return out + esc(text.slice(last));
  }

  let current = [];
  let active = 0;

  function go(topic) {
    history.replaceState(null, "", `#${topic.anchorId}`);
    openSection(topic.anchorId, true);
    if (!topic.intro) {
      const h3 = document.getElementById(topic.anchorId);
      h3.classList.add("help-flash-topic");
      setTimeout(() => h3.classList.remove("help-flash-topic"), 1500);
    }
    list.hidden = true;
  }

  function render() {
    const query = input.value.trim();
    if (!query) { list.hidden = true; current = []; return; }
    const { results, terms } = search(query);
    current = results;
    active = 0;
    if (!results.length) {
      list.innerHTML = '<li class="help-search-note">No matches. Try other words, or Expand all and use the browser\'s Find (⌘F / Ctrl+F).</li>';
      list.hidden = false;
      return;
    }
    const onlyRelated = results.every((r) => r.exact === 0);
    list.innerHTML = (onlyRelated ? '<li class="help-search-note">No exact match — showing close and related topics.</li>' : "") +
      results.map((r, i) => {
        const t = r.topic;
        const where = t.intro ? " — overview" : ` — ${esc(t.sectionTitle)}`;
        return `<li data-i="${i}" class="${i === 0 ? "active" : ""}">` +
          `<div class="help-result-title">${esc(t.title)}<span class="help-result-where">${where}</span></div>` +
          `<div class="help-result-snippet">${snippet(t, terms)}</div></li>`;
      }).join("");
    list.hidden = false;
  }

  function setActive(i) {
    if (!current.length) return;
    active = (i + current.length) % current.length;
    list.querySelectorAll("li[data-i]").forEach((li) => li.classList.toggle("active", Number(li.dataset.i) === active));
    const li = list.querySelector(`li[data-i="${active}"]`);
    if (li) li.scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("input", render);
  input.addEventListener("focus", () => { if (input.value.trim()) render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); if (current[active]) go(current[active].topic); }
    else if (e.key === "Escape") { input.value = ""; list.hidden = true; }
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (!li) return;
    e.preventDefault();  // keep focus so the list doesn't vanish before the click lands
    go(current[Number(li.dataset.i)].topic);
  });
  input.addEventListener("blur", () => { list.hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey) return;
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    e.preventDefault();
    input.focus();
  });

  const preset = new URLSearchParams(location.search).get("q");
  if (preset) { input.value = preset; input.focus(); render(); }
}

// Collapsible panels (<details data-collapse-key>): remember which ones
// were closed, per browser. Only a convenience — the page works without it.
function initCollapsiblePanels() {
  const STORAGE_KEY = "collapsed-panels";
  let collapsed = [];
  try { collapsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); } catch (e) { collapsed = []; }
  document.querySelectorAll("details[data-collapse-key]").forEach((panel) => {
    const key = panel.dataset.collapseKey;
    if (collapsed.includes(key)) panel.open = false;
    panel.addEventListener("toggle", () => {
      collapsed = collapsed.filter((k) => k !== key);
      if (!panel.open) collapsed.push(key);
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(collapsed)); } catch (e) { /* storage blocked */ }
      // A chart drawn while hidden has no size; redraw it once visible
      if (panel.open && window.Chart) {
        panel.querySelectorAll("canvas").forEach((canvas) => {
          const chart = Chart.getChart(canvas);
          if (chart) chart.resize();
        });
      }
    });
  });
}

function initInvestmentSnapshotForm() {
  const form = document.querySelector("[data-snapshot-add-form]");
  if (!form) return;
  const dateInput = form.querySelector("[name='date']");
  if (dateInput && !dateInput.value) dateInput.value = localISODate();

  // Current value and/or principal added — at least one
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const formData = new FormData(form);
    const payload = {
      account_name: formData.get("account_name"),
      date: formData.get("date"),
      balance: (formData.get("balance") || "").trim(),
      principal_added: (formData.get("principal_added") || "").trim(),
      note: (formData.get("note") || "").trim(),
    };
    if (!payload.balance && !payload.principal_added) {
      alert("Enter a current value, principal added, or both.");
      return;
    }
    const send = () => postJSON("/api/investments/entry", payload);
    let res = await send();
    let body = res.ok ? {} : await res.json().catch(() => ({}));
    // Looks like a deposit already logged on Transactions — only add on a yes
    if (!res.ok && body.needs_confirm) {
      if (!confirm(`${body.error}\n\nAdd it anyway?`)) return;
      payload.confirm_duplicate = true;
      res = await send();
      body = res.ok ? {} : await res.json().catch(() => ({}));
    }
    if (res.ok) {
      window.location.reload();
    } else {
      alert(body.error || "Couldn't save — please check the fields and try again.");
    }
  });
}

// Net Worth page: Add Account form — the no-terminal way to create an
// account (same validation as maintenance.py's create-account).
function initAccountAddForm() {
  const form = document.querySelector("[data-account-add-form]");
  if (!form) return;
  const dateInput = form.querySelector("[name='balance_date']");
  if (dateInput && !dateInput.value) dateInput.value = localISODate();

  // One currency in use: the currency picker waits behind "Other currency?"
  const currencySelect = form.querySelector("[data-currency-select]");
  const currencyReveal = form.querySelector("[data-currency-reveal]");
  if (currencySelect && currencyReveal) {
    currencyReveal.addEventListener("click", () => {
      currencySelect.hidden = false;
      currencyReveal.hidden = true;
      currencySelect.focus();
    });
  }

  // "Someone else pays it" asks who
  const ownerSelect = form.querySelector("[data-owner-select]");
  const personInput = form.querySelector("[data-owner-person]");
  if (ownerSelect && personInput) {
    const sync = () => {
      personInput.hidden = ownerSelect.value !== "other";
      personInput.required = ownerSelect.value === "other";
    };
    ownerSelect.addEventListener("change", sync);
    sync();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    // The date is pre-filled for convenience; only send it with a balance.
    if (!String(payload.starting_balance || "").trim()) {
      delete payload.starting_balance;
      delete payload.balance_date;
    }
    const res = await postJSON("/api/accounts/add", payload);
    if (res.ok) {
      window.location.reload();
    } else {
      const body = await res.json().catch(() => ({}));
      alert(body.error || "Couldn't create the account — please check the fields and try again.");
    }
  });
}

// Net Worth page: Set Balance — records a real balance for an account so
// it stops being summed from all of its transactions.
function initSetBalanceForm() {
  const form = document.querySelector("[data-set-balance-form]");
  if (!form) return;
  const dateInput = form.querySelector("[name='date']");
  if (dateInput && !dateInput.value) dateInput.value = localISODate();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = Object.fromEntries(new FormData(form).entries());
    const res = await postJSON("/api/accounts/set-balance", payload);
    if (res.ok) {
      window.location.reload();
    } else {
      const body = await res.json().catch(() => ({}));
      alert(body.error || "Couldn't set the balance — please check the fields and try again.");
    }
  });
}

// (i) tooltips (<span class="info-tip" data-tip="...">): shown as one
// floating box on <body>, positioned next to the icon and kept inside
// the window — so it's never hidden under the sidebar or clipped by a
// scrolling table. Without JS the CSS ::after version still works.
function initInfoTips() {
  const tips = document.querySelectorAll(".info-tip[data-tip]");
  if (!tips.length) return;
  document.documentElement.classList.add("js-info-tips");
  const box = document.createElement("div");
  box.className = "info-tip-float";
  box.hidden = true;
  box.setAttribute("role", "tooltip");
  document.body.appendChild(box);
  const GAP = 8;

  function show(el) {
    box.textContent = el.dataset.tip;
    box.classList.toggle("info-tip-float-wide", el.classList.contains("info-tip-wide"));
    box.hidden = false;
    const icon = el.getBoundingClientRect();
    const tip = box.getBoundingClientRect();
    const maxLeft = window.innerWidth - tip.width - GAP;
    const left = Math.max(GAP, Math.min(icon.left + icon.width / 2 - tip.width / 2, maxLeft));
    let top = icon.bottom + GAP;
    if (top + tip.height > window.innerHeight - GAP) top = Math.max(GAP, icon.top - GAP - tip.height);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  }
  const hide = () => { box.hidden = true; };

  tips.forEach((el) => {
    el.addEventListener("mouseenter", () => show(el));
    el.addEventListener("focus", () => show(el));
    el.addEventListener("mouseleave", hide);
    el.addEventListener("blur", hide);
  });
  // The icon moves when anything scrolls; just hide rather than chase it
  document.addEventListener("scroll", hide, true);
}

function initScrollPreservation() {
  document.querySelectorAll(".period-selector a.period-btn").forEach((a) => {
    a.addEventListener("click", saveScrollPosition);
  });
  restoreScrollPosition();
}

document.addEventListener("DOMContentLoaded", () => {
  initTransactionsPage();
  initIousPage();
  initImportPage();
  initInfoTips();
  initCdsSection();
  initInvestmentSnapshotForm();
  initInvestmentAssign();
  initInvestmentEntries();
  initCollapsiblePanels();
  initHelpPage();
  initAccountAddForm();
  initSetBalanceForm();
  initRangePickers();
  initScrollPreservation();
});
