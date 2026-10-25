// Budget app front end. Plain JS, no build step.

import {
  barList, categoryColors, columnChart, legend, lineChart, meter, money, stateBadge, tableView,
} from "./charts.js";

const main = document.getElementById("main");

// ---- Small helpers ---------------------------------------------------------

// Build an element. Children that are strings become text, never HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

async function api(method, path, body, headers = {}) {
  const options = { method, headers: { "X-Budget": "1", ...headers } };
  if (typeof body === "string") {
    options.body = body;
  } else if (body !== undefined) {
    options.body = JSON.stringify(body);
    options.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function amountCell(cents) {
  const text = cents > 0 ? `+${currency.format(cents / 100)}` : currency.format(cents / 100);
  return h("td", { class: `num amount ${cents > 0 ? "in" : "out"}` }, text);
}

function formatDate(iso) {
  const [year, month, day] = iso.split("-");
  return `${month}/${day}/${year}`;
}

// SQLite stores UTC as "YYYY-MM-DD HH:MM:SS".
function formatTimestamp(utc) {
  return new Date(utc.replace(" ", "T") + "Z").toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// ---- Messages shown at the top of the page --------------------------------

let flash = null;

function setFlash(kind, text) {
  flash = { kind, text };
}

function flashElement() {
  if (!flash) return null;
  // text can be a string or a list of lines.
  const lines = Array.isArray(flash.text) ? flash.text : [flash.text];
  return h(
    "div",
    { class: `flash ${flash.kind}`, role: flash.kind === "error" ? "alert" : "status" },
    lines.map((line) => h("div", {}, line)),
  );
}

async function act(action) {
  try {
    await action();
  } catch (error) {
    setFlash("error", error.message);
  }
  await render();
}

// ---- Transactions table with category editing ----------------------------

// ---- Category pickers ------------------------------------------------------

const NEW_CATEGORY = "__new__";
const categoryLabel = (c) => c.full_name ?? c.name;

// A <select> of categories in tree order ("Food", "Food › Groceries", ...) ending
// with "+ New category…", which creates one on the spot. onPick(id) runs on change.
function categoryPicker({ categories, selectedId = null, label, blank = null, filter = () => true, onPick = null }) {
  const select = h(
    "select",
    { "aria-label": label },
    blank ? h("option", { value: "", selected: selectedId == null }, blank) : null,
    categories.filter(filter).map((c) =>
      h("option", { value: c.id, selected: c.id === selectedId, class: c.parent_id ? "sub" : null }, categoryLabel(c)),
    ),
    h("option", { value: NEW_CATEGORY }, "+ New category…"),
  );
  let current = select.value;
  select.addEventListener("change", async () => {
    if (select.value === NEW_CATEGORY) {
      const created = await newCategoryDialog(categories);
      if (!created) {
        select.value = current;
        return;
      }
      categories.push(created);
      select.lastElementChild.before(h("option", { value: created.id }, categoryLabel(created)));
      select.value = String(created.id);
    }
    current = select.value;
    onPick?.(select.value ? Number(select.value) : null);
  });
  return select;
}

// Whose spending: Household (shared) or a person. onPick(personId or null).
function personPicker({ people, selectedId = null, label, onPick = null, emptyLabel = "Household" }) {
  const select = h(
    "select",
    { "aria-label": label, class: "person-select" },
    h("option", { value: "", selected: selectedId == null }, emptyLabel),
    people.map((p) => h("option", { value: p.id, selected: p.id === selectedId }, p.name)),
  );
  if (onPick) select.addEventListener("change", () => onPick(select.value ? Number(select.value) : null));
  return select;
}

// Resolves to the new category, or null if cancelled.
function newCategoryDialog(categories, parentId = null) {
  const groups = categories.filter((c) => c.parent_id == null && (c.kind === "expense" || c.kind === "income"));
  const name = h("input", { type: "text", maxlength: 60, required: true, "aria-label": "Category name", placeholder: "e.g. Pets" });
  const parent = h(
    "select",
    { "aria-label": "Where it goes" },
    h("option", { value: "" }, "Its own group"),
    groups.map((g) => h("option", { value: g.id, selected: g.id === parentId }, `In ${g.name}`)),
  );
  const error = h("div", { class: "flash error", role: "alert", hidden: true });
  return openDialog("New category", (close) => [
    h("h2", {}, "New category"),
    h(
      "form",
      {
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            close(await api("POST", "/api/categories", { name: name.value, parent_id: parent.value ? Number(parent.value) : null }));
          } catch (e) {
            error.textContent = e.message;
            error.hidden = false;
          }
        },
      },
      h(
        "div",
        { class: "field-row" },
        h("label", { class: "field" }, h("span", {}, "Name"), name),
        h("label", { class: "field" }, h("span", {}, "Where"), parent),
      ),
      error,
      h(
        "div",
        { class: "dialog-buttons" },
        h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { type: "submit", class: "primary" }, "Create"),
      ),
    ),
  ]);
}

// ---- Transactions table ------------------------------------------------------

// Rows are entries: a split transaction shows one row per part.
// `sorting` is optional: { sort, dir, onSort(column) } makes headers clickable.
// `showRecent` adds how the same store was categorized before (rows need `recent`).
// `people` adds a Person column (Household or a person) when there are any.
function transactionTable(transactions, categories, { showAccount = true, sorting = null, showRecent = false, people = [] } = {}) {
  const showPerson = people.length > 0;
  const columns = (showAccount ? 6 : 5) + (showPerson ? 1 : 0);
  const header = (column, label, attrs = {}) => {
    if (!sorting) return h("th", attrs, label);
    const active = sorting.sort === column;
    const arrow = active ? (sorting.dir === "asc" ? " ▲" : " ▼") : "";
    return h(
      "th",
      { ...attrs, "aria-sort": active ? (sorting.dir === "asc" ? "ascending" : "descending") : "none" },
      h("button", { class: "sort", onclick: () => sorting.onSort(column) }, label, arrow),
    );
  };
  const rows = transactions.map((txn) => {
    const isPart = txn.part_id != null;
    let row;
    const picker = categoryPicker({
      categories,
      selectedId: txn.category_id,
      label: `Category for ${txn.description}`,
      onPick: async (categoryId) => {
        try {
          if (isPart) {
            await api("PATCH", `/api/parts/${txn.part_id}`, { category_id: categoryId });
          } else {
            const result = await api("PATCH", `/api/transactions/${txn.id}`, { category_id: categoryId });
            showRulePrompt(row, result, categories, columns);
          }
        } catch (error) {
          setFlash("error", error.message);
          await render();
        }
      },
    });
    const person = showPerson
      ? personPicker({
          people,
          selectedId: txn.person_id,
          label: `Whose spending is ${txn.description}`,
          onPick: async (personId) => {
            try {
              const path = isPart ? `/api/parts/${txn.part_id}` : `/api/transactions/${txn.id}`;
              await api("PATCH", path, { person_id: personId });
            } catch (error) {
              setFlash("error", error.message);
              await render();
            }
          },
        })
      : null;
    const splitButton = h(
      "button",
      {
        type: "button",
        class: "small",
        title: isPart ? "Change how this transaction is split" : "Split into several categories",
        onclick: () =>
          act(async () => {
            const outcome = await splitDialog(txn.id, categories, people);
            if (outcome === "saved") setFlash("ok", `Split saved for ${txn.description}.`);
            if (outcome === "unsplit") setFlash("ok", `${txn.description} is no longer split.`);
          }),
      },
      isPart ? "Edit split" : "Split",
    );
    row = h(
      "tr",
      { class: isPart ? "part-row" : null },
      h("td", { class: "date" }, formatDate(txn.date)),
      showAccount ? h("td", { class: "hide-narrow nowrap" }, txn.account_name) : null,
      h(
        "td",
        { class: "description" },
        txn.description,
        isPart ? h("span", { class: "tag" }, `part ${txn.part_number} of ${txn.parts_count}`) : null,
        showRecent && txn.recent?.length ? h("div", { class: "muted small recent" }, `Before: ${txn.recent.join(" · ")}`) : null,
      ),
      h("td", {}, picker),
      showPerson ? h("td", {}, person) : null,
      amountCell(txn.amount_cents),
      h("td", { class: "num" }, splitButton),
    );
    return row;
  });

  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          header("date", "Date"),
          showAccount ? header("account", "Account", { class: "hide-narrow" }) : null,
          header("description", "Description"),
          header("category", "Category"),
          showPerson ? h("th", {}, "Person") : null,
          header("amount", "Amount", { class: "num" }),
          h("th", { class: "num" }, h("span", { class: "visually-hidden" }, "Split")),
        ),
      ),
      h("tbody", {}, rows),
    ),
  );
}

// Split one transaction into parts. Resolves to "saved", "unsplit" or null.
async function splitDialog(transactionId, categories, people = []) {
  const txn = await api("GET", `/api/transactions/${transactionId}`);
  const sign = txn.amount_cents < 0 ? -1 : 1;
  const total = Math.abs(txn.amount_cents);
  const parts = [];
  const body = h("tbody");
  const remaining = h("strong");
  const error = h("div", { class: "flash error", role: "alert", hidden: true });
  // The first part soaks up whatever the others don't, until you edit it yourself.
  let firstFollows = true;

  const cents = (part) => {
    try {
      return parseDollars(part.amount.value) ?? 0;
    } catch {
      return NaN;
    }
  };
  function update() {
    if (firstFollows && parts.length > 1) {
      const others = parts.slice(1).reduce((sum, p) => sum + cents(p), 0);
      if (!Number.isNaN(others)) parts[0].amount.value = dollarsInput(Math.max(0, total - others));
    }
    const left = total - parts.reduce((sum, p) => sum + cents(p), 0);
    remaining.textContent = Number.isNaN(left) ? "–" : currency.format(left / 100);
    remaining.className = left === 0 ? "balanced" : "unbalanced";
  }
  function addPart(amountCents, categoryId, personId = txn.person_id) {
    const part = {};
    const number = parts.length + 1;
    part.amount = h("input", {
      type: "text", inputmode: "decimal", class: "money", value: amountCents ? dollarsInput(amountCents) : "",
      "aria-label": `Amount of part ${number}`,
      oninput: () => {
        if (part === parts[0]) firstFollows = false;
        update();
      },
    });
    part.picker = categoryPicker({ categories, selectedId: categoryId, label: `Category of part ${number}`, blank: "Pick a category…" });
    part.person = people.length ? personPicker({ people, selectedId: personId, label: `Person for part ${number}` }) : null;
    part.row = h(
      "tr",
      {},
      h("td", { class: "num nowrap" }, h("span", { class: "dollar" }, "$"), part.amount),
      h("td", {}, part.picker),
      part.person ? h("td", {}, part.person) : null,
      h(
        "td",
        { class: "num" },
        h("button", {
          type: "button", class: "small", "aria-label": `Remove part ${number}`,
          onclick: () => {
            parts.splice(parts.indexOf(part), 1);
            part.row.remove();
            update();
          },
        }, "Remove"),
      ),
    );
    parts.push(part);
    body.append(part.row);
    update();
  }

  if (txn.parts.length) {
    firstFollows = false;
    txn.parts.forEach((p) => addPart(Math.abs(p.amount_cents), p.category_id, p.person_id));
  } else {
    addPart(total, txn.category_id);
    addPart(0, null);
  }

  return openDialog("Split transaction", (close) => [
    h("h2", {}, "Split transaction"),
    h("p", {}, `${formatDate(txn.date)} · ${txn.description} · `, h("strong", {}, currency.format(total / 100))),
    h("p", { class: "muted small" }, "Give each part an amount and a category. The parts must add up to the total."),
    h(
      "form",
      {
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            const payload = parts.map((p, i) => {
              const amount = cents(p);
              if (!amount || Number.isNaN(amount)) throw new Error(`Part ${i + 1} needs an amount.`);
              if (!p.picker.value || p.picker.value === NEW_CATEGORY) throw new Error(`Part ${i + 1} needs a category.`);
              return {
                amount_cents: sign * amount,
                category_id: Number(p.picker.value),
                person_id: p.person?.value ? Number(p.person.value) : null,
              };
            });
            await api("PUT", `/api/transactions/${transactionId}/split`, { parts: payload });
            close("saved");
          } catch (e) {
            error.textContent = e.message;
            error.hidden = false;
          }
        },
      },
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", { class: "num" }, "Amount"), h("th", {}, "Category"), people.length ? h("th", {}, "Person") : null, h("th", {}))),
          body,
        ),
      ),
      h(
        "div",
        { class: "split-footer" },
        h("button", { type: "button", onclick: () => addPart(0, null) }, "+ Add part"),
        h("span", {}, "Left to assign: ", remaining),
      ),
      error,
      h(
        "div",
        { class: "dialog-buttons" },
        txn.parts.length
          ? h("button", {
              type: "button",
              class: "danger",
              onclick: async () => {
                await api("DELETE", `/api/transactions/${transactionId}/split`);
                close("unsplit");
              },
            }, "Unsplit")
          : null,
        h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { type: "submit", class: "primary" }, "Save split"),
      ),
    ),
  ]);
}

// After a category change, offer to save a rule for similar descriptions.
function showRulePrompt(row, result, categories, columns) {
  row.nextElementSibling?.classList.contains("rule-prompt") && row.nextElementSibling.remove();
  if (!result.suggested_pattern) return;

  const category = categories.find((c) => c.id === result.category_id);
  const categoryName = categoryLabel(category);
  const input = h("input", {
    type: "text",
    value: result.suggested_pattern,
    "aria-label": "Text the description must contain",
  });
  const prompt = h(
    "tr",
    { class: "rule-prompt" },
    h(
      "td",
      { colspan: columns },
      h(
        "div",
        { class: "row" },
        "Always put descriptions containing",
        input,
        `in ${categoryName}?`,
        h(
          "button",
          {
            class: "primary",
            onclick: () =>
              act(async () => {
                const rule = await api("POST", "/api/rules", {
                  pattern: input.value,
                  category_id: result.category_id,
                });
                setFlash(
                  "ok",
                  `Rule saved: “${rule.pattern}” → ${categoryName}. ` +
                    `${plural(rule.updated, "other transaction")} updated.`,
                );
              }),
          },
          "Save rule",
        ),
        h("button", { onclick: () => prompt.remove() }, "No thanks"),
      ),
    ),
  );
  row.after(prompt);
  input.focus();
  input.select();
}

// ---- Pages -----------------------------------------------------------------

async function importPage() {
  const [accounts, batches, review, categories, people] = await Promise.all([
    api("GET", "/api/accounts"),
    api("GET", "/api/batches"),
    api("GET", "/api/transactions?needs_review=1&with_recent=1"),
    api("GET", "/api/categories"),
    api("GET", "/api/people"),
  ]);

  return h(
    "div",
    {},
    h("h1", {}, "Import"),

    h("h2", {}, "Accounts"),
    accounts.length
      ? h("div", { class: "accounts" }, accounts.map(accountCard))
      : h("p", { class: "muted" }, "Add an account to start importing."),
    addAccountForm(),

    h("h2", {}, people.length ? "Needs a category or person " : "Needs a category ", h("span", { class: "count" }, `(${review.count})`)),
    review.count
      ? [
          h("p", { class: "muted" }, "Pick a category, or Split an order that covers several. After picking, you'll be offered a rule so similar purchases are sorted automatically next time. “Before” shows how you sorted the same store last time."),
          people.length
            ? h("p", { class: "muted" }, "Also listed: transactions in categories you budget per person that don't have a person yet.")
            : null,
          transactionTable(review.rows, categories, { showRecent: true, people }),
        ]
      : h("div", { class: "card empty" }, "Nothing to review."),

    h("h2", {}, "Import history"),
    batches.length ? historyTable(batches) : h("div", { class: "card empty" }, "No imports yet."),
  );
}

function accountCard(account) {
  const input = h("input", {
    type: "file",
    accept: ".csv,.CSV,text/csv",
    hidden: true,
    onchange: () => {
      const file = input.files[0];
      input.value = ""; // so picking the same file again still triggers an import
      if (file) act(() => importFile(account, file));
    },
  });
  const details = [
    plural(account.transaction_count, "transaction"),
    account.last_import ? `last import ${formatTimestamp(account.last_import)}` : "never imported",
  ].join(" · ");

  return h(
    "div",
    { class: "card account" },
    h("div", {}, h("strong", {}, account.name), h("span", { class: "muted" }, details)),
    input,
    h("button", { class: "primary", onclick: () => input.click() }, "Import CSV"),
  );
}

async function importFile(account, file) {
  await runImport(account, file.name, await file.text(), {});
}

// The server may stop and ask something first ("needs"); answer, then retry.
async function runImport(account, filename, text, options) {
  const query = new URLSearchParams(options).toString();
  const result = await api("POST", `/api/accounts/${account.id}/import${query ? "?" + query : ""}`, text, {
    "Content-Type": "text/csv",
    "X-Filename": encodeURIComponent(filename),
  });
  const cancelled = () => setFlash("ok", `Import of ${filename} cancelled. Nothing was imported.`);

  if (result.needs === "new_columns") {
    if (!confirm(`${result.message}\n\nImport ${filename} into ${account.name} anyway?`)) return cancelled();
    return runImport(account, filename, text, { ...options, allow_new_columns: 1 });
  }
  if (result.needs === "direction") {
    const sign = await askDirection(account, result);
    if (sign == null) return cancelled();
    return runImport(account, filename, text, { ...options, amount_sign: sign });
  }
  if (result.needs === "columns") {
    if (!(await askColumns(filename, result))) return cancelled();
    return runImport(account, filename, text, options);
  }

  const parts = [
    `${filename} → ${account.name}: ${result.rows_added} new`,
    `${plural(result.rows_skipped, "duplicate")} skipped`,
  ];
  if (result.rows_pending) parts.push(`${result.rows_pending} pending left out until they clear`);
  let summary = parts.join(", ") + ".";
  if (result.uncategorized) {
    const n = result.uncategorized;
    summary += ` ${plural(n, "transaction")} ${n === 1 ? "needs" : "need"} a category below.`;
  }
  setFlash("ok", [summary, ...(result.budget_alerts ?? []).map((alert) => `⚠ ${alert}`)]);
}

// A modal dialog. build(close) returns its contents; close(value) resolves the promise.
function openDialog(label, build) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "modal", "aria-label": label });
    const close = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close(null);
    });
    dialog.append(...build(close));
    document.body.append(dialog);
    dialog.showModal();
  });
}

// Which way do purchases go in this account's files? Resolves to 1, -1 or null.
function askDirection(account, question) {
  const guess = question.guess;
  const sign = (n) => (n < 0 ? "negative" : "positive");
  const purchasesAre = guess === 1 ? "negative" : "positive";
  const intro =
    question.reason === "first"
      ? `This file has one amount column, so it's not obvious which way the money went. ` +
        `Judging by rows like payments, refunds and credits, purchases in ${account.name} probably show as ${purchasesAre} numbers.`
      : `Purchases in ${account.name} usually show as ${guess === 1 ? "positive" : "negative"} numbers, ` +
        `but this file looks like its purchases are ${purchasesAre}. It may be from a different account, or the bank changed its format.`;

  return openDialog("Which way do amounts go?", (close) => [
    h("h2", {}, question.reason === "first" ? "Which way do amounts go?" : "This file looks reversed"),
    h("p", {}, intro),
    h("p", { class: "muted" }, "If purchases are " + purchasesAre + ", these rows would be counted like this:"),
    simpleTable(
      [["Description"], ["In the file", "num"], ["Counted as", "num"]],
      question.examples.map((e) =>
        h(
          "tr",
          {},
          h("td", { class: "description" }, e.description),
          h("td", { class: "num" }, (e.amount_cents < 0 ? "-" : "") + currency.format(Math.abs(e.amount_cents) / 100)),
          h("td", { class: "num" }, (e.amount_cents * guess < 0 ? "Spending " : "Money in ") + currency.format(Math.abs(e.amount_cents) / 100)),
        ),
      ),
    ),
    h(
      "div",
      { class: "dialog-buttons" },
      h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { type: "button", onclick: () => close(-guess) }, `No, purchases are ${sign(guess)}`),
      h("button", { type: "button", class: "primary", autofocus: true, onclick: () => close(guess) }, `Yes, purchases are ${purchasesAre}`),
    ),
    h("p", { class: "muted small" }, `Your answer is saved for ${account.name}. You can change it later in Settings.`),
  ]);
}

// Which column is which, for a file the app couldn't recognize. Resolves true once saved.
function askColumns(filename, question) {
  const { headers, rows } = question;
  const columnSelect = (label, optional) =>
    h(
      "select",
      { "aria-label": label },
      h("option", { value: "" }, optional ? "— none —" : "Pick a column…"),
      headers.map((name) => h("option", { value: name }, name)),
    );
  const fields = {
    date: columnSelect("Date column"),
    description: columnSelect("Description column"),
    amount: columnSelect("Amount column"),
    debit: columnSelect("Money out column"),
    credit: columnSelect("Money in column"),
    category: columnSelect("Category column", true),
    type: columnSelect("Type column", true),
    status: columnSelect("Status column", true),
  };
  const single = h("input", { type: "radio", name: "amount-style", checked: true });
  const split = h("input", { type: "radio", name: "amount-style" });
  const singleRow = h("label", { class: "field" }, h("span", {}, "Amount"), fields.amount);
  const splitRows = h(
    "div",
    { class: "field-row" },
    h("label", { class: "field" }, h("span", {}, "Money out"), fields.debit),
    h("label", { class: "field" }, h("span", {}, "Money in"), fields.credit),
  );
  const syncStyle = () => {
    singleRow.hidden = !single.checked;
    splitRows.hidden = single.checked;
  };
  single.addEventListener("change", syncStyle);
  split.addEventListener("change", syncStyle);
  syncStyle();
  const error = h("div", { class: "flash error", role: "alert", hidden: true });

  return openDialog("Which column is which?", (close) => [
    h("h2", {}, "Which column is which?"),
    h("p", {}, `The app doesn't recognize the columns in ${filename}. Match them once and it will remember for files like this.`),
    h(
      "div",
      { class: "table-wrap preview" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, headers.map((name) => h("th", {}, name)))),
        h("tbody", {}, rows.map((row) => h("tr", {}, headers.map((_, i) => h("td", {}, row[i] ?? ""))))),
      ),
    ),
    h(
      "form",
      {
        onsubmit: async (event) => {
          event.preventDefault();
          const layout = Object.fromEntries(Object.entries(fields).map(([key, select]) => [key, select.value || null]));
          if (single.checked) layout.debit = layout.credit = null;
          else layout.amount = null;
          try {
            await api("POST", "/api/layouts", { headers, layout });
            close(true);
          } catch (e) {
            error.textContent = e.message;
            error.hidden = false;
          }
        },
      },
      h(
        "div",
        { class: "field-row" },
        h("label", { class: "field" }, h("span", {}, "Date"), fields.date),
        h("label", { class: "field" }, h("span", {}, "Description"), fields.description),
      ),
      h(
        "div",
        { class: "radio-row" },
        h("label", {}, single, " One amount column"),
        h("label", {}, split, " Separate money-out and money-in columns"),
      ),
      singleRow,
      splitRows,
      h(
        "details",
        {},
        h("summary", {}, "Optional columns"),
        h(
          "div",
          { class: "field-row" },
          h("label", { class: "field" }, h("span", {}, "Category"), fields.category),
          h("label", { class: "field" }, h("span", {}, "Type"), fields.type),
          h("label", { class: "field" }, h("span", {}, "Status (pending rows are skipped)"), fields.status),
        ),
      ),
      error,
      h(
        "div",
        { class: "dialog-buttons" },
        h("button", { type: "button", onclick: () => close(false) }, "Cancel"),
        h("button", { type: "submit", class: "primary" }, "Save and import"),
      ),
    ),
  ]);
}

function addAccountForm() {
  const name = h("input", { type: "text", placeholder: "Account name, e.g. Chase Freedom", "aria-label": "Account name", required: true, maxlength: 60 });
  return h(
    "form",
    {
      class: "inline",
      onsubmit: (event) => {
        event.preventDefault();
        act(async () => {
          const account = await api("POST", "/api/accounts", { name: name.value });
          setFlash("ok", `Added ${account.name}. Use its Import CSV button to bring in transactions.`);
        });
      },
    },
    name,
    h("button", { type: "submit" }, "Add account"),
  );
}

function historyTable(batches) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", {}, "Imported"),
          h("th", {}, "Account"),
          h("th", { class: "hide-narrow" }, "File"),
          h("th", { class: "num" }, "Added"),
          h("th", { class: "num" }, "Skipped"),
          h("th", {}, ""),
        ),
      ),
      h(
        "tbody",
        {},
        batches.map((batch) =>
          h(
            "tr",
            {},
            h("td", { class: "date" }, formatTimestamp(batch.imported_at)),
            h("td", {}, batch.account_name),
            h("td", { class: "hide-narrow description" }, batch.filename),
            h("td", { class: "num" }, batch.rows_added),
            h("td", { class: "num" }, batch.rows_skipped),
            h(
              "td",
              { class: "num" },
              h(
                "button",
                {
                  onclick: () => {
                    const message =
                      `Undo the ${batch.filename} import? This deletes the ` +
                      `${plural(batch.rows_added, "transaction")} it added, including any category changes you made to them.`;
                    if (!confirm(message)) return;
                    act(async () => {
                      const result = await api("DELETE", `/api/batches/${batch.id}`);
                      setFlash("ok", `Import undone. ${plural(result.removed, "transaction")} removed.`);
                    });
                  },
                },
                "Undo",
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

// ---- Transactions page -----------------------------------------------------
// Its filters live in the page address (#/transactions?period=this-month&…),
// so any view can be bookmarked or linked to.

const PERIODS = [
  ["all", "All time"],
  ["this-week", "This week"],
  ["this-month", "This month"],
  ["last-month", "Last month"],
  ["this-year", "This year"],
  ["custom", "Custom dates"],
];

const GROUPINGS = [
  ["", "No grouping"],
  ["week", "Week"],
  ["month", "Month"],
  ["year", "Year"],
  ["category", "Category group"],
  ["subcategory", "Subcategory"],
  ["merchant", "Merchant"],
  ["person", "Person"],
];

const FILTER_DEFAULTS = {
  period: "all", start: "", end: "", account: "", category: "", exact: "", person: "", q: "", group: "", sort: "date", dir: "desc",
};

// Sorting a text column starts A→Z; dates and amounts start largest first.
const TEXT_COLUMNS = new Set(["description", "category", "account"]);

function hashParams() {
  return new URLSearchParams(location.hash.split("?")[1] ?? "");
}

function readFilters() {
  const params = hashParams();
  const filters = { ...FILTER_DEFAULTS };
  for (const key of Object.keys(filters)) {
    if (params.has(key)) filters[key] = params.get(key);
  }
  return filters;
}

function writeFilters(filters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== FILTER_DEFAULTS[key]) params.set(key, value);
  }
  const query = params.toString();
  // replaceState doesn't fire hashchange, so the page isn't rebuilt while typing.
  history.replaceState(null, "", `#/transactions${query ? "?" + query : ""}`);
}

const isoDate = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

// [start, end] as yyyy-mm-dd for a period, in your local time zone. Weeks run Monday–Sunday.
function periodRange(filters, today = new Date()) {
  const y = today.getFullYear();
  const m = today.getMonth();
  switch (filters.period) {
    case "this-week": {
      const monday = new Date(y, m, today.getDate() - ((today.getDay() + 6) % 7));
      const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
      return [isoDate(monday), isoDate(sunday)];
    }
    case "this-month":
      return [isoDate(new Date(y, m, 1)), isoDate(new Date(y, m + 1, 0))];
    case "last-month":
      return [isoDate(new Date(y, m - 1, 1)), isoDate(new Date(y, m, 0))];
    case "this-year":
      return [`${y}-01-01`, `${y}-12-31`];
    case "custom":
      return [filters.start, filters.end];
    default:
      return ["", ""];
  }
}

function apiQuery(filters) {
  const [start, end] = periodRange(filters);
  const params = new URLSearchParams();
  const set = (key, value) => value && params.set(key, value);
  set("start", start);
  set("end", end);
  set("account_id", filters.account);
  set("category_id", filters.category);
  // "exact": only what's put directly in a group, not its subcategories (e.g. "Food (other)").
  if (filters.category && filters.exact) set("category_exact", "1");
  set("person", filters.person);
  set("q", filters.q.trim());
  return params;
}

async function transactionsPage() {
  const [accounts, categories, people] = await Promise.all([
    api("GET", "/api/accounts"),
    api("GET", "/api/categories"),
    api("GET", "/api/people"),
  ]);
  if (!accounts.length) {
    return h(
      "div",
      {},
      h("h1", {}, "Transactions"),
      h("div", { class: "card empty" }, "No transactions yet. ", h("a", { href: "#/import" }, "Import a CSV"), " to get started."),
    );
  }

  let filters = readFilters();
  const results = h("div", { "aria-live": "polite" });
  let latestRequest = 0;

  async function update(changes = {}) {
    filters = { ...filters, ...changes };
    writeFilters(filters);
    syncControls();
    const request = ++latestRequest;
    try {
      const query = apiQuery(filters);
      const listQuery = new URLSearchParams(query);
      listQuery.set("sort", filters.sort);
      listQuery.set("dir", filters.dir);
      const totalsQuery = new URLSearchParams(query);
      totalsQuery.set("group", filters.group);
      const [list, totals] = await Promise.all([
        api("GET", `/api/transactions?${listQuery}`),
        filters.group ? api("GET", `/api/totals?${totalsQuery}`) : null,
      ]);
      if (request !== latestRequest) return; // a newer change is already loading
      results.replaceChildren(...renderResults(list, totals));
    } catch (error) {
      if (request === latestRequest) {
        results.replaceChildren(h("div", { class: "flash error", role: "alert" }, error.message));
      }
    }
  }

  function renderResults(list, totals) {
    const summary = [
      plural(list.count, "transaction"),
      `Spent ${currency.format(list.spent_cents / 100)}`,
      `Money in ${currency.format(list.income_cents / 100)}`,
    ].join(" · ");
    const parts = [
      h("p", { class: "summary" }, summary),
      h("p", { class: "muted small" }, "Totals leave out transfers between your accounts. Refunds are subtracted from spending."),
    ];
    if (totals) parts.push(groupTable(totals));
    if (list.rows.length < list.count) {
      parts.push(h("p", { class: "muted" }, `Showing the first ${list.rows.length}. Narrow the filters to see the rest.`));
    }
    parts.push(
      list.count
        ? transactionTable(list.rows, categories, {
            people,
            sorting: {
              sort: filters.sort,
              dir: filters.dir,
              onSort: (column) => {
                const dir =
                  column === filters.sort
                    ? filters.dir === "asc" ? "desc" : "asc"
                    : TEXT_COLUMNS.has(column) ? "asc" : "desc";
                update({ sort: column, dir });
              },
            },
          })
        : h("div", { class: "card empty" }, "No transactions match these filters."),
    );
    return parts;
  }

  function groupTable(groups) {
    const groupName = GROUPINGS.find(([key]) => key === filters.group)[1];
    if (!groups.length) return h("div", { class: "card empty" }, "Nothing to total.");
    return h(
      "div",
      { class: "table-wrap groups" },
      h(
        "table",
        {},
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, groupName),
            h("th", { class: "num" }, "Transactions"),
            h("th", { class: "num" }, "Spent"),
            h("th", { class: "num" }, "Money in"),
          ),
        ),
        h(
          "tbody",
          {},
          groups.map((group) =>
            h(
              "tr",
              {},
              h(
                "td",
                {},
                h(
                  "button",
                  {
                    class: "link",
                    title: "Show these transactions",
                    onclick: () => drillInto(group.filter),
                  },
                  group.label,
                ),
              ),
              h("td", { class: "num" }, group.count),
              h("td", { class: "num" }, currency.format(group.spent_cents / 100)),
              h("td", { class: "num" }, group.income_cents ? currency.format(group.income_cents / 100) : "–"),
            ),
          ),
        ),
      ),
    );
  }

  // Narrow the list to one group's transactions.
  function drillInto(filter) {
    const changes = { group: "" };
    if (filter.start) Object.assign(changes, { period: "custom", start: filter.start, end: filter.end });
    if (filter.category_id) Object.assign(changes, { category: String(filter.category_id), exact: filter.category_exact ? "1" : "" });
    if (filter.text) changes.q = filter.text;
    if (filter.person != null) changes.person = String(filter.person);
    update(changes);
  }

  // ---- Controls ----
  const field = (label, control) => h("label", { class: "field" }, h("span", {}, label), control);
  const options = (pairs, selected) =>
    pairs.map(([value, label]) => h("option", { value, selected: value === selected }, label));

  const period = h("select", { onchange: () => update({ period: period.value }) });
  const start = h("input", { type: "date", "aria-label": "From", onchange: () => update({ start: start.value }) });
  const end = h("input", { type: "date", "aria-label": "To", onchange: () => update({ end: end.value }) });
  const dateRange = field("From – to", h("div", { class: "custom-dates" }, start, h("span", { class: "muted" }, "to"), end));
  const account = h("select", { onchange: () => update({ account: account.value }) });
  const category = h("select", { onchange: () => update({ category: category.value, exact: "" }) });
  const person = h("select", { onchange: () => update({ person: person.value }) });
  let typing;
  const search = h("input", {
    type: "search",
    placeholder: "Search descriptions",
    oninput: () => {
      clearTimeout(typing);
      typing = setTimeout(() => update({ q: search.value }), 250);
    },
  });
  const group = h("select", { onchange: () => update({ group: group.value }) });
  const clear = h("button", { type: "button", onclick: () => update({ ...FILTER_DEFAULTS, sort: filters.sort, dir: filters.dir }) }, "Clear filters");

  function syncControls() {
    period.replaceChildren(...options(PERIODS, filters.period));
    account.replaceChildren(
      ...options([["", "All accounts"], ...accounts.map((a) => [String(a.id), a.name])], filters.account),
    );
    category.replaceChildren(
      ...options(
        [["", "All categories"], ...categories.map((c) => [String(c.id), c.has_children && filters.exact && String(c.id) === filters.category ? `${c.name} (other)` : categoryLabel(c)])],
        filters.category,
      ),
    );
    person.replaceChildren(
      ...options([["", "Everyone"], ["household", "Household"], ...people.map((p) => [String(p.id), p.name])], filters.person),
    );
    group.replaceChildren(...options(GROUPINGS.filter(([key]) => key !== "person" || people.length), filters.group));
    dateRange.hidden = filters.period !== "custom";
    start.value = filters.start;
    end.value = filters.end;
    if (document.activeElement !== search) search.value = filters.q;
    const defaults = Object.keys(FILTER_DEFAULTS).filter((k) => k !== "sort" && k !== "dir");
    clear.hidden = defaults.every((k) => filters[k] === FILTER_DEFAULTS[k]);
  }

  const page = h(
    "div",
    {},
    h("h1", {}, "Transactions"),
    h(
      "div",
      { class: "card filters" },
      field("Dates", period),
      dateRange,
      field("Account", account),
      field("Category", category),
      people.length ? field("Person", person) : null,
      field("Search", search),
      field("Group totals by", group),
      clear,
    ),
    results,
  );
  await update();
  return page;
}

// ---- Settings page ---------------------------------------------------------

// "600", "$1,200.50" -> cents. Blank -> null.
function parseDollars(text) {
  const cleaned = text.replace(/[$,\s]/g, "");
  if (!cleaned) return null;
  if (!/^\d+(\.\d{0,2})?$/.test(cleaned)) throw new Error(`“${text}” isn't a dollar amount.`);
  return Math.round(Number(cleaned) * 100);
}

const dollarsInput = (cents) => (cents == null ? "" : cents % 100 ? (cents / 100).toFixed(2) : String(cents / 100));

function simpleTable(headers, rows) {
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, headers.map(([label, cls]) => h("th", { class: cls }, label)))),
      h("tbody", {}, rows),
    ),
  );
}

const confirmed = (message, action) => () => {
  if (confirm(message)) act(action);
};

async function settingsPage() {
  const thisMonth = periodRange({ period: "this-month" });
  const lastMonth = periodRange({ period: "last-month" });
  const [people, budgetData, categories, rules, bankCategories, accounts, layouts, thisMonthTotals, lastMonthTotals] = await Promise.all([
    api("GET", "/api/people"),
    api("GET", "/api/budgets"),
    api("GET", "/api/categories"),
    api("GET", "/api/rules"),
    api("GET", "/api/bank-categories"),
    api("GET", "/api/accounts"),
    api("GET", "/api/layouts"),
    api("GET", `/api/totals?group=subcategory&start=${thisMonth[0]}&end=${thisMonth[1]}`),
    api("GET", `/api/totals?group=subcategory&start=${lastMonth[0]}&end=${lastMonth[1]}`),
  ]);
  const ruleTargets = (c) => c.kind !== "uncategorized";

  return h(
    "div",
    {},
    h("h1", {}, "Settings"),
    peopleSection(people),
    budgetsSection(budgetData, categories, people, thisMonthTotals, lastMonthTotals),
    rulesSection(rules, categories, ruleTargets, people),
    categoriesSection(categories),
    bankCategoriesSection(bankCategories, categories, ruleTargets),
    accountsSection(accounts),
    layoutsSection(layouts),
  );
}

// Spending per category id from "subcategory" totals, with each group's total
// including its subcategories.
function peopleSection(people) {
  if (!people.length) {
    // First time: ask for the two people up front.
    const first = h("input", { type: "text", maxlength: 60, placeholder: "First person", "aria-label": "First person's name" });
    const second = h("input", { type: "text", maxlength: 60, placeholder: "Second person", "aria-label": "Second person's name" });
    return h(
      "section",
      {},
      h("h2", {}, "People"),
      h(
        "div",
        { class: "card" },
        h("p", {}, "Who's in your household? Transactions are shared (Household) unless a rule or you assign them to someone, and budgets can be set per person."),
        h(
          "form",
          {
            class: "inline",
            onsubmit: (event) => {
              event.preventDefault();
              act(async () => {
                const names = [first.value, second.value].filter((n) => n.trim());
                if (!names.length) throw new Error("Enter at least one name.");
                for (const name of names) await api("POST", "/api/people", { name });
                setFlash("ok", `Added ${names.join(" and ")}.`);
              });
            },
          },
          first,
          second,
          h("button", { type: "submit", class: "primary" }, "Save"),
        ),
      ),
    );
  }

  const rows = people.map((person) => {
    const name = h("input", {
      type: "text",
      value: person.name,
      maxlength: 60,
      "aria-label": `Name of ${person.name}`,
      onchange: () =>
        act(async () => {
          const result = await api("PATCH", `/api/people/${person.id}`, { name: name.value });
          setFlash("ok", `Renamed ${person.name} to ${result.name}.`);
        }),
    });
    const message =
      `Remove ${person.name}? Their transactions go back to Household, rules stop assigning them, and their budgets are deleted.`;
    return h(
      "tr",
      {},
      h("td", {}, name),
      h(
        "td",
        { class: "num" },
        h(
          "button",
          {
            type: "button",
            class: "small",
            onclick: confirmed(message, async () => {
              const result = await api("DELETE", `/api/people/${person.id}`);
              setFlash("ok", `Removed ${person.name}. ${plural(result.moved, "transaction")} moved to Household.`);
            }),
          },
          "Remove",
        ),
      ),
    );
  });
  const newName = h("input", { type: "text", maxlength: 60, required: true, placeholder: "Name", "aria-label": "New person's name" });
  return h(
    "section",
    {},
    h("h2", {}, "People"),
    h("p", { class: "muted" }, "Transactions are Household (shared) unless a rule or you assign them to a person. Rename by editing a name."),
    simpleTable([["Name"], ["", "num"]], rows),
    h(
      "form",
      {
        class: "inline",
        onsubmit: (event) => {
          event.preventDefault();
          act(async () => {
            const created = await api("POST", "/api/people", { name: newName.value });
            setFlash("ok", `Added ${created.name}.`);
          });
        },
      },
      newName,
      h("button", { type: "submit" }, "Add person"),
    ),
  );
}

function spentByCategory(totals, categories) {
  const parentOf = Object.fromEntries(categories.map((c) => [c.id, c.parent_id]));
  const spent = {};
  for (const g of totals) {
    const id = g.filter.category_id;
    spent[id] = (spent[id] ?? 0) + g.spent_cents;
    const parent = parentOf[id];
    if (parent != null) spent[parent] = (spent[parent] ?? 0) + g.spent_cents;
  }
  return spent;
}

function budgetsSection(budgetData, categories, people, thisMonthTotals, lastMonthTotals) {
  const thisMonth = spentByCategory(thisMonthTotals, categories);
  const lastMonth = spentByCategory(lastMonthTotals, categories);
  const moneyOrDash = (cents) => (cents ? currency.format(cents / 100) : "–");

  // One input per category and "who": everyone (null), then each person.
  const who = [{ id: null, name: "Everyone" }, ...people];
  const existing = new Map(budgetData.limits.map((l) => [`${l.category_id}:${l.person_id ?? ""}`, l.monthly_limit_cents]));
  const inputs = [];
  const limitInput = (cents, label) =>
    h("input", { type: "text", inputmode: "decimal", class: "money", value: dollarsInput(cents), placeholder: "–", "aria-label": label });

  const rows = categories
    .filter((c) => c.kind === "expense")
    .map((c) => {
      const cells = who.map((w) => {
        const input = limitInput(existing.get(`${c.id}:${w.id ?? ""}`), `Monthly budget for ${categoryLabel(c)}, ${w.name}`);
        inputs.push({ category_id: c.id, person_id: w.id, input });
        return h("td", { class: "num nowrap" }, h("span", { class: "dollar" }, "$"), input);
      });
      return h(
        "tr",
        { class: c.parent_id ? "sub-row" : "group-row" },
        h("td", {}, c.parent_id ? h("span", { class: "indent" }, c.name) : h("strong", {}, c.name)),
        h("td", { class: "num hide-narrow" }, moneyOrDash(lastMonth[c.id])),
        h("td", { class: "num" }, moneyOrDash(thisMonth[c.id])),
        ...cells,
      );
    });

  const overall = limitInput(budgetData.overall_cents, "Monthly budget for all spending");
  const allThisMonth = thisMonthTotals.reduce((sum, g) => sum + g.spent_cents, 0);
  const allLastMonth = lastMonthTotals.reduce((sum, g) => sum + g.spent_cents, 0);
  rows.push(
    h(
      "tr",
      { class: "total-row" },
      h("td", {}, h("strong", {}, "All spending"), h("div", { class: "muted small" }, "Optional overall limit")),
      h("td", { class: "num hide-narrow" }, moneyOrDash(allLastMonth)),
      h("td", { class: "num" }, moneyOrDash(allThisMonth)),
      h("td", { class: "num nowrap" }, h("span", { class: "dollar" }, "$"), overall),
      ...people.map(() => h("td", {})),
    ),
  );

  const warn = h("input", { type: "number", min: 1, max: 99, step: 1, value: budgetData.warn_pct, class: "pct", "aria-label": "Warning level in percent" });

  const save = () =>
    act(async () => {
      const limits = inputs
        .map(({ category_id, person_id, input }) => ({ category_id, person_id, monthly_limit_cents: parseDollars(input.value) }))
        .filter((l) => l.monthly_limit_cents != null);
      const warnPct = Number(warn.value);
      if (!Number.isInteger(warnPct)) throw new Error("The warning level must be a whole percentage.");
      await api("PUT", "/api/budgets", { limits, overall_cents: parseDollars(overall.value), warn_pct: warnPct });
      setFlash("ok", "Budgets saved.");
    });

  return h(
    "section",
    {},
    h("h2", {}, "Monthly budgets"),
    h(
      "p",
      { class: "muted" },
      "Leave a box blank for no budget. A group's budget covers everything in it, including its subcategories, which can have budgets of their own. " +
        (people.length
          ? "“Everyone” counts all spending in a category; a person's column counts only spending assigned to them, so you can budget, say, Personal per person without a Personal total. "
          : "") +
        "Spending is money out minus refunds; transfers don't count.",
    ),
    h(
      "form",
      { onsubmit: (event) => { event.preventDefault(); save(); } },
      simpleTable(
        [
          ["Category"],
          ["Last month", "num hide-narrow"],
          ["This month so far", "num"],
          ...who.map((w) => [people.length ? `${w.name} limit` : "Monthly limit", "num"]),
        ],
        rows,
      ),
      h(
        "div",
        { class: "form-row" },
        h("label", {}, "Warn me at ", warn, " % of a budget"),
        h("button", { type: "submit", class: "primary" }, "Save budgets"),
      ),
    ),
  );
}

function rulesSection(rules, categories, isTarget, people) {
  const pattern = h("input", { type: "text", placeholder: "e.g. TRADER JOE", "aria-label": "Text the description contains", required: true });
  const category = categoryPicker({ categories, label: "Category", blank: "Category…", filter: isTarget });
  const person = people.length ? personPicker({ people, label: "Person", emptyLabel: "Anyone (no person)" }) : null;
  const add = h(
    "form",
    {
      class: "inline",
      onsubmit: (event) => {
        event.preventDefault();
        act(async () => {
          if (!category.value || category.value === NEW_CATEGORY) throw new Error("Pick a category for the rule.");
          const rule = await api("POST", "/api/rules", {
            pattern: pattern.value,
            category_id: Number(category.value),
            person_id: person?.value ? Number(person.value) : null,
          });
          setFlash("ok", `Rule added: “${rule.pattern}”. ${plural(rule.updated, "transaction")} re-sorted.`);
        });
      },
    },
    "Descriptions containing",
    pattern,
    "go to",
    category,
    person ? [" for ", person] : null,
    h("button", { type: "submit" }, "Add rule"),
  );

  const rows = rules.map((rule) =>
    h(
      "tr",
      {},
      h("td", { class: "description" }, h("code", {}, rule.pattern)),
      h(
        "td",
        {},
        categoryPicker({
          categories,
          selectedId: rule.category_id,
          label: `Category for rule ${rule.pattern}`,
          filter: isTarget,
          onPick: (categoryId) =>
            act(async () => {
              const result = await api("PATCH", `/api/rules/${rule.id}`, { category_id: categoryId });
              setFlash("ok", `Rule “${rule.pattern}” updated. ${plural(result.updated, "transaction")} re-sorted.`);
            }),
        }),
      ),
      people.length
        ? h(
            "td",
            {},
            personPicker({
              people,
              selectedId: rule.person_id,
              label: `Person for rule ${rule.pattern}`,
              emptyLabel: "Anyone",
              onPick: (personId) =>
                act(async () => {
                  await api("PATCH", `/api/rules/${rule.id}`, { person_id: personId });
                  setFlash("ok", `Rule “${rule.pattern}” updated.`);
                }),
            }),
          )
        : null,
      h(
        "td",
        { class: "num" },
        h(
          "button",
          {
            onclick: () =>
              act(async () => {
                const result = await api("DELETE", `/api/rules/${rule.id}`);
                setFlash("ok", `Rule “${rule.pattern}” deleted. ${plural(result.updated, "transaction")} re-sorted.`);
              }),
          },
          "Delete",
        ),
      ),
    ),
  );

  return h(
    "section",
    {},
    h("h2", {}, "Rules ", h("span", { class: "count" }, `(${rules.length})`)),
    h("p", { class: "muted" }, "Rules match whole words anywhere in the description, ignoring case. When several match, the longest wins. Categories you set by hand, and split transactions, are never changed by rules."),
    add,
    rules.length
      ? simpleTable([["Contains"], ["Goes to"], ...(people.length ? [["Person"]] : []), ["", "num"]], rows)
      : h("div", { class: "card empty" }, "No rules yet."),
  );
}

const KIND_LABELS = { expense: "Spending", income: "Income", transfer: "Built in", uncategorized: "Built in" };

function categoriesSection(categories) {
  const groups = categories.filter((c) => c.parent_id == null && (c.kind === "expense" || c.kind === "income"));
  const childrenOf = (id) => categories.filter((c) => c.parent_id === id);

  const rows = categories.map((c) => {
    const builtIn = c.kind === "transfer" || c.kind === "uncategorized";
    const name = h("input", {
      type: "text",
      value: c.name,
      maxlength: 60,
      class: c.parent_id ? "indent-input" : "group-input",
      "aria-label": `Name of ${categoryLabel(c)}`,
      onchange: () =>
        act(async () => {
          const result = await api("PATCH", `/api/categories/${c.id}`, { name: name.value });
          setFlash("ok", `Renamed “${categoryLabel(c)}” to “${result.full_name}”.`);
        }),
    });

    // Subcategories, and groups without subcategories, can move.
    let move = null;
    if (!builtIn && !c.has_children) {
      move = h(
        "select",
        {
          "aria-label": `Move ${categoryLabel(c)}`,
          onchange: () =>
            act(async () => {
              const parentId = move.value === "top" ? null : Number(move.value);
              const result = await api("PATCH", `/api/categories/${c.id}`, { parent_id: parentId });
              setFlash("ok", `Moved to “${result.full_name}”.`);
            }),
        },
        h("option", { value: "", selected: true, disabled: true }, "Move to…"),
        c.parent_id != null ? h("option", { value: "top" }, "Its own group") : null,
        groups
          .filter((g) => g.id !== c.id && g.id !== c.parent_id && g.kind === c.kind)
          .map((g) => h("option", { value: g.id }, `In ${g.name}`)),
      );
    }

    const addSub =
      !builtIn && c.parent_id == null
        ? h(
            "button",
            {
              type: "button",
              class: "small",
              onclick: () =>
                act(async () => {
                  const created = await newCategoryDialog(categories, c.id);
                  if (created) setFlash("ok", `Added “${created.full_name}”.`);
                }),
            },
            "+ Subcategory",
          )
        : null;

    let message;
    if (c.parent_id != null) {
      message = `Delete “${categoryLabel(c)}”? Its ${plural(c.transaction_count, "transaction")} and ${plural(c.rule_count, "rule")} move up to ${c.group_name}.`;
    } else {
      const family = [c, ...childrenOf(c.id)];
      const count = family.reduce((sum, x) => sum + x.transaction_count, 0);
      const ruleCount = family.reduce((sum, x) => sum + x.rule_count, 0);
      const subs = family.length - 1;
      message =
        `Delete “${c.name}”${subs ? ` and its ${plural(subs, "subcategory").replace("subcategorys", "subcategories")}` : ""}? ` +
        `${plural(count, "transaction")} will be re-sorted by your other rules or go back to “Needs a category”` +
        (ruleCount ? `, and ${plural(ruleCount, "rule")} will be deleted.` : ".");
    }

    return h(
      "tr",
      { class: c.parent_id ? "sub-row" : "group-row" },
      h("td", {}, name),
      h("td", { class: "hide-narrow" }, KIND_LABELS[c.kind]),
      h("td", { class: "num" }, c.transaction_count),
      h(
        "td",
        { class: "num actions" },
        addSub,
        move,
        builtIn
          ? h("span", { class: "muted small" }, "Built in")
          : h(
              "button",
              {
                type: "button",
                class: "small",
                onclick: confirmed(message, async () => {
                  const result = await api("DELETE", `/api/categories/${c.id}`);
                  setFlash(
                    "ok",
                    result.moved_to
                      ? `Deleted “${categoryLabel(c)}”. ${plural(result.moved, "transaction")} moved to ${result.moved_to}.`
                      : `Deleted “${c.name}”. ${plural(result.moved, "transaction")} re-sorted.`,
                  );
                }),
              },
              "Delete",
            ),
      ),
    );
  });

  const newGroup = h(
    "button",
    {
      type: "button",
      onclick: () =>
        act(async () => {
          const created = await newCategoryDialog(categories);
          if (created) setFlash("ok", `Added “${created.full_name}”.`);
        }),
    },
    "+ New category",
  );

  return h(
    "section",
    {},
    h("h2", {}, "Categories"),
    h(
      "p",
      { class: "muted" },
      "Groups can have subcategories. Rename by editing a name. Deleting a subcategory moves its transactions up to its group. Transfer and Uncategorized are built in.",
    ),
    simpleTable([["Name"], ["Type", "hide-narrow"], ["Transactions", "num"], ["", "num"]], rows),
    h("div", { class: "form-row" }, newGroup),
  );
}

function bankCategoriesSection(bankCategories, categories, isTarget) {
  const rows = bankCategories.map((b) =>
    h(
      "tr",
      {},
      h("td", {}, b.bank_category),
      h(
        "td",
        {},
        categoryPicker({
          categories,
          selectedId: b.category_id,
          label: `Your category for ${b.bank_category}`,
          blank: "Don't use (Needs a category)",
          filter: isTarget,
          onPick: (categoryId) =>
            act(async () => {
              const result = await api("PUT", "/api/bank-categories", { bank_category: b.bank_category, category_id: categoryId });
              setFlash("ok", `“${b.bank_category}” updated. ${plural(result.updated, "transaction")} re-sorted.`);
            }),
        }),
      ),
      h("td", { class: "num" }, b.transaction_count),
    ),
  );
  return h(
    "section",
    {},
    h("h2", {}, "Bank categories"),
    h("p", { class: "muted" }, "Some files include the bank's own category (like “Food & Drink”). When none of your rules match, it's translated to your category here. New bank categories appear after you import a file that uses them."),
    simpleTable([["Bank's category"], ["Your category"], ["Transactions", "num"]], rows),
  );
}

function accountsSection(accounts) {
  if (!accounts.length) {
    return h("section", {}, h("h2", {}, "Accounts"), h("div", { class: "card empty" }, "No accounts yet. ", h("a", { href: "#/import" }, "Add one on the Import page"), "."));
  }
  const rows = accounts.map((a) => {
    const name = h("input", {
      type: "text",
      value: a.name,
      maxlength: 60,
      "aria-label": `Name of ${a.name}`,
      onchange: () =>
        act(async () => {
          const result = await api("PATCH", `/api/accounts/${a.id}`, { name: name.value });
          setFlash("ok", `Renamed “${a.name}” to “${result.name}”.`);
        }),
    });
    const message =
      `Delete “${a.name}”? This permanently deletes the account, its import history and its ` +
      `${plural(a.transaction_count, "transaction")}. This can't be undone.`;
    const direction = h(
      "select",
      {
        "aria-label": `Which way amounts go for ${a.name}`,
        onchange: () =>
          act(async () => {
            await api("PATCH", `/api/accounts/${a.id}`, { amount_sign: Number(direction.value) });
            setFlash("ok", `Saved for ${a.name}. This applies to future imports; to fix earlier ones, undo them on the Import page and import again.`);
          }),
      },
      a.amount_sign == null ? h("option", { value: "", selected: true, disabled: true }, "Asks on next import") : null,
      h("option", { value: "1", selected: a.amount_sign === 1 }, "Purchases are negative"),
      h("option", { value: "-1", selected: a.amount_sign === -1 }, "Purchases are positive"),
    );
    return h(
      "tr",
      {},
      h("td", {}, name),
      h("td", {}, direction),
      h("td", { class: "num" }, a.transaction_count),
      h(
        "td",
        { class: "num" },
        h(
          "button",
          {
            class: "danger",
            onclick: confirmed(message, async () => {
              const result = await api("DELETE", `/api/accounts/${a.id}`);
              setFlash("ok", `Deleted “${a.name}” and ${plural(result.removed, "transaction")}.`);
            }),
          },
          "Delete",
        ),
      ),
    );
  });
  return h(
    "section",
    {},
    h("h2", {}, "Accounts"),
    h("p", { class: "muted" }, "Add accounts and import files on the Import page. “Amounts” says which way purchases go in files with a single amount column; files with separate money-out and money-in columns don't need it."),
    simpleTable([["Name"], ["Amounts"], ["Transactions", "num"], ["", "num"]], rows),
  );
}

const LAYOUT_LABELS = {
  date: "Date", description: "Description", amount: "Amount", debit: "Money out",
  credit: "Money in", category: "Category", type: "Type", status: "Status",
};

function layoutsSection(layouts) {
  if (!layouts.length) return null;
  return h(
    "section",
    {},
    h("h2", {}, "Saved column layouts"),
    h("p", { class: "muted" }, "Column choices you made for files the app didn't recognize. Delete one to be asked again."),
    simpleTable(
      [["File columns"], ["Used as"], ["", "num"]],
      layouts.map((saved) =>
        h(
          "tr",
          {},
          h("td", { class: "description" }, saved.headers.join(", ")),
          h(
            "td",
            {},
            Object.entries(saved.layout)
              .filter(([, column]) => column)
              .map(([field, column]) => `${LAYOUT_LABELS[field]}: ${column}`)
              .join(" · "),
          ),
          h(
            "td",
            { class: "num" },
            h(
              "button",
              {
                onclick: () =>
                  act(async () => {
                    await api("DELETE", `/api/layouts/${saved.id}`);
                    setFlash("ok", "Saved layout deleted.");
                  }),
              },
              "Delete",
            ),
          ),
        ),
      ),
    ),
  );
}

// ---- Dashboard -------------------------------------------------------------

const DASH_PERIODS = [
  ["last-6", "Last 6 months"],
  ["last-12", "Last 12 months"],
  ["this-year", "This year"],
  ["all", "All time"],
];

function dashboardRange(period, today = new Date()) {
  const y = today.getFullYear();
  const m = today.getMonth();
  const end = isoDate(new Date(y, m + 1, 0));
  if (period === "last-6") return [isoDate(new Date(y, m - 5, 1)), end];
  if (period === "last-12") return [isoDate(new Date(y, m - 11, 1)), end];
  if (period === "this-year") return [`${y}-01-01`, end];
  return ["", ""];
}

// Link to the Transactions page showing one slice.
function showTransactions({ start, end, category, person }) {
  const params = new URLSearchParams({ period: "custom", start, end });
  if (category != null) params.set("category", category);
  if (person != null) params.set("person", person);
  location.hash = `#/transactions?${params}`;
}

const STATE_ORDER = { over: 0, warn: 1, pace: 2, ok: 3 };

async function dashboardPage() {
  const params = hashParams();
  const view = {
    period: DASH_PERIODS.some(([k]) => k === params.get("period")) ? params.get("period") : "last-12",
    split: params.get("split") === "1",
  };
  const saveView = () => {
    const query = new URLSearchParams();
    if (view.period !== "last-12") query.set("period", view.period);
    if (view.split) query.set("split", "1");
    history.replaceState(null, "", `#/dashboard${query.toString() ? "?" + query : ""}`);
  };

  const [range0Start, range0End] = dashboardRange(view.period);
  const [accounts, categories, first] = await Promise.all([
    api("GET", "/api/accounts"),
    api("GET", "/api/categories"),
    api("GET", `/api/dashboard?start=${range0Start}&end=${range0End}`),
  ]);
  if (!accounts.some((a) => a.transaction_count)) {
    return h(
      "div",
      {},
      h("h1", {}, "Dashboard"),
      h("div", { class: "card empty" }, "No transactions yet. ", h("a", { href: "#/import" }, "Import a CSV"), " to see your spending here."),
    );
  }
  const names = Object.fromEntries(categories.map((c) => [c.id, c.name]));

  const charts = h("div", { class: "dash-charts" });
  async function loadCharts(data) {
    charts.classList.add("loading");
    try {
      if (!data) {
        const [start, end] = dashboardRange(view.period);
        data = await api("GET", `/api/dashboard?start=${start}&end=${end}`);
      }
      charts.replaceChildren(...chartCards(data, names, view, () => { saveView(); loadCharts(data); }));
    } catch (error) {
      charts.replaceChildren(h("div", { class: "flash error", role: "alert" }, error.message));
    } finally {
      charts.classList.remove("loading");
    }
  }

  const period = h(
    "select",
    { "aria-label": "Date range for the charts", onchange: () => { view.period = period.value; saveView(); loadCharts(); } },
    DASH_PERIODS.map(([value, label]) => h("option", { value, selected: value === view.period }, label)),
  );

  const page = h(
    "div",
    {},
    h("h1", {}, "Dashboard"),
    thisMonthCard(first.budget),
    budgetCard(first.budget),
    h("div", { class: "filters-row" }, h("label", { class: "field" }, h("span", {}, "Charts show"), period)),
    charts,
  );
  await loadCharts(first);
  return page;
}

function thisMonthCard(budget) {
  const overall = budget.overall;
  const flagged = [overall, ...budget.categories]
    .filter((s) => s && s.state !== "ok")
    .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state]);
  const daysLeft = budget.days_left === 0 ? "last day of the month" : plural(budget.days_left, "day") + " left";

  return h(
    "div",
    { class: "card this-month" },
    h("div", { class: "hero-label" }, `Spent in ${budget.label}`),
    h("div", { class: "hero" }, money(budget.spent_cents)),
    overall
      ? [
          h("div", { class: "hero-sub" }, `of ${money(overall.limit_cents)} budget · ${daysLeft} `, stateBadge(overall.state)),
          meter({ spent: overall.spent_cents, limit: overall.limit_cents, state: overall.state, elapsedPct: budget.elapsed_pct }),
        ]
      : h("div", { class: "hero-sub" }, `${daysLeft} · `, h("a", { href: "#/settings" }, "Set a monthly budget")),
    flagged.length
      ? h(
          "ul",
          { class: "alerts", "aria-label": "Budget warnings" },
          flagged.map((s) => h("li", {}, stateBadge(s.state), h("span", {}, s.message))),
        )
      : budget.categories.length || overall
        ? h("p", { class: "muted ok-note" }, "Everything is within budget so far.")
        : null,
  );
}

function budgetCard(budget) {
  if (!budget.categories.length) {
    return h(
      "div",
      { class: "card" },
      h("h2", { class: "card-title" }, "Budgets"),
      h("p", { class: "muted" }, "No category budgets yet. ", h("a", { href: "#/settings" }, "Set them in Settings"), "."),
    );
  }
  const rows = [...budget.categories].sort(
    (a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.pct - a.pct,
  );
  return h(
    "div",
    { class: "card" },
    h("h2", { class: "card-title" }, `Budgets for ${budget.label}`),
    h("p", { class: "muted small" }, "The thin line on each bar shows how far through the month we are. Click a budget to see its transactions."),
    h(
      "div",
      { class: "budget-rows" },
      rows.map((b) =>
        h(
          "button",
          {
            class: "budget-row",
            type: "button",
            onclick: () => showTransactions({ start: budget.start, end: budget.end, category: b.category_id, person: b.person_id }),
          },
          h("span", { class: "budget-name" }, b.name),
          stateBadge(b.state),
          h("span", { class: "budget-amount" }, `${money(b.spent_cents)} of ${money(b.limit_cents)}`),
          meter({ spent: b.spent_cents, limit: b.limit_cents, state: b.state, elapsedPct: budget.elapsed_pct }),
        ),
      ),
    ),
  );
}

function chartCards(data, names, view, onToggleSplit) {
  const months = data.monthly;
  const colorOf = categoryColors(data.category_rank);
  const top = data.category_rank.slice(0, 7).map(String);

  // Monthly spending: one series, or top 7 categories + Other.
  let series;
  if (view.split) {
    const present = top.filter((id) => months.some((m) => m.by_category[id]));
    series = present.map((id) => ({ key: id, label: names[id], color: colorOf(id), value: (m) => m.by_category[id] ?? 0 }));
    const other = (m) =>
      Object.entries(m.by_category).reduce((sum, [id, cents]) => (top.includes(id) ? sum : sum + cents), 0);
    if (months.some((m) => other(m) > 0)) series.push({ key: "other", label: "Other", color: colorOf("other"), value: other });
  } else {
    series = [{ key: "spent", label: "Spent", color: "var(--series-1)", value: (m) => m.spent_cents }];
  }
  const split = h("input", { type: "checkbox", checked: view.split, onchange: () => { view.split = split.checked; onToggleSplit(); } });

  const monthlyCard = h(
    "div",
    { class: "card chart-card wide" },
    h(
      "div",
      { class: "card-head" },
      h("h2", { class: "card-title" }, "Monthly spending"),
      h("label", { class: "toggle" }, split, " Split by category"),
    ),
    series.length > 1 ? legend(series.map((s) => ({ label: s.label, color: s.color }))) : null,
    columnChart({
      months,
      series,
      ariaLabel: "Spending per month",
      onSelect: (m) => showTransactions({ start: m.start, end: m.end }),
    }),
    tableView(
      ["Month", ...series.map((s) => s.label), ...(series.length > 1 ? ["Total"] : [])],
      months.map((m) => [m.label, ...series.map((s) => money(s.value(m))), ...(series.length > 1 ? [money(m.spent_cents)] : [])]),
    ),
  );

  const { start, end } = data.range;
  const categoryCard = h(
    "div",
    { class: "card chart-card" },
    h("h2", { class: "card-title" }, "Spending by category"),
    data.categories.length
      ? [
          barList({
            items: data.categories.map((g) => ({
              label: g.label,
              cents: g.spent_cents,
              onSelect: () => showTransactions({ start, end, category: g.filter.category_id }),
            })),
          }),
          tableView(["Category", "Spent", "Transactions"], data.categories.map((g) => [g.label, money(g.spent_cents), g.count])),
        ]
      : h("p", { class: "muted" }, "No spending in this range."),
  );

  const lines = [
    { label: "Money in", color: "var(--series-1)", value: (m) => m.income_cents },
    { label: "Spent", color: "var(--series-2)", value: (m) => m.spent_cents },
  ];
  const incomeCard = h(
    "div",
    { class: "card chart-card" },
    h("h2", { class: "card-title" }, "Money in vs spent"),
    legend(lines.map((l) => ({ label: l.label, color: l.color, key: "line" }))),
    lineChart({
      months,
      series: lines,
      ariaLabel: "Money in and spending per month",
      extraRows: (m) => [{ value: money(m.income_cents - m.spent_cents), label: m.income_cents >= m.spent_cents ? "saved" : "overspent" }],
      onSelect: (m) => showTransactions({ start: m.start, end: m.end }),
    }),
    tableView(
      ["Month", "Money in", "Spent", "Difference"],
      months.map((m) => [m.label, money(m.income_cents), money(m.spent_cents), money(m.income_cents - m.spent_cents)]),
    ),
  );

  return [monthlyCard, h("div", { class: "chart-grid" }, categoryCard, incomeCard)];
}

// ---- Ask -------------------------------------------------------------------
// Fill-in-the-blank questions answered from your data (no AI). Answers stay
// in the feed until the page is reloaded.

const askFeed = [];
const askChoices = {};

// An answer's link -> the Transactions page address for that slice.
function transactionsHash(link) {
  const params = new URLSearchParams();
  if (link.start || link.end) {
    params.set("period", "custom");
    if (link.start) params.set("start", link.start);
    if (link.end) params.set("end", link.end);
  }
  if (link.category_id != null) params.set("category", link.category_id);
  if (link.text) params.set("q", link.text);
  if (link.person != null) params.set("person", link.person);
  if (link.sort) params.set("sort", link.sort);
  if (link.dir) params.set("dir", link.dir);
  const query = params.toString();
  return `#/transactions${query ? "?" + query : ""}`;
}

async function askPage() {
  const [data, accounts] = await Promise.all([api("GET", "/api/ask"), api("GET", "/api/accounts")]);
  const feed = h("div", { class: "ask-feed", "aria-live": "polite" });

  function renderFeed() {
    feed.replaceChildren(
      ...(askFeed.length
        ? [...askFeed].reverse().map(answerCard)
        : [h("div", { class: "card empty" }, "Pick a question and press Ask. Answers appear here.")]),
    );
  }

  function questionRow(question) {
    const choices = (askChoices[question.type] ??= {});
    const controls = {};
    const parts = question.parts.map((part) => {
      if (typeof part === "string") return part;
      const slot = part.slot;
      const value = choices[slot] ?? data.defaults[slot];
      if (slot === "merchant") {
        const input = h("input", {
          type: "text", class: "inline-input", placeholder: "Amazon", value, "aria-label": "Merchant name",
          oninput: () => { choices[slot] = input.value; },
        });
        controls[slot] = input;
        return input;
      }
      const kind = slot.startsWith("period") ? "period" : slot;
      const labelKey = part.kind === "noun" ? "noun" : kind === "period" ? "phrase" : "label";
      const select = h(
        "select",
        { class: "inline-select", "aria-label": slot.replace("_", " "), onchange: () => { choices[slot] = select.value; } },
        data.options[kind].map((o) => h("option", { value: o.value, selected: o.value === value }, o[labelKey])),
      );
      controls[slot] = select;
      return select;
    });

    const button = h("button", { type: "submit", class: "primary" }, "Ask");
    return h(
      "form",
      {
        class: "question",
        onsubmit: async (event) => {
          event.preventDefault();
          const params = Object.fromEntries(Object.entries(controls).map(([slot, c]) => [slot, c.value]));
          button.disabled = true;
          try {
            askFeed.push(await api("POST", "/api/ask", { type: question.type, params }));
          } catch (error) {
            askFeed.push({ question: "", text: error.message, error: true });
          } finally {
            button.disabled = false;
          }
          renderFeed();
          feed.firstElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        },
      },
      h("span", { class: "question-text" }, parts),
      button,
    );
  }

  renderFeed();
  const noData = !accounts.some((a) => a.transaction_count);
  return h(
    "div",
    {},
    h("h1", {}, "Ask"),
    noData ? h("div", { class: "flash error" }, "There's no data yet. ", h("a", { href: "#/import" }, "Import a CSV"), " first.") : null,
    h(
      "div",
      { class: "ask-layout" },
      h("div", { class: "card ask-questions" }, h("h2", { class: "card-title" }, "Questions"), data.questions.map(questionRow)),
      h(
        "div",
        {},
        h(
          "div",
          { class: "card-head" },
          h("h2", { class: "card-title" }, "Answers"),
          h("button", { type: "button", onclick: () => { askFeed.length = 0; renderFeed(); } }, "Clear"),
        ),
        feed,
      ),
    ),
  );
}

function answerCard(entry) {
  if (entry.error) return h("div", { class: "card answer flash error", role: "alert" }, entry.text);
  return h(
    "div",
    { class: "card answer" },
    h("div", { class: "asked" }, entry.question),
    h("p", { class: "answer-text" }, entry.text),
    entry.bars?.length
      ? barList({
          items: entry.bars.map((b) => ({
            label: b.label,
            cents: b.cents,
            onSelect: () => { location.hash = transactionsHash(b.link); },
          })),
        })
      : null,
    entry.table
      ? tableView(entry.table.headers, entry.table.rows.map((row) => row.map(String)))
      : null,
    entry.link ? h("a", { class: "answer-link", href: transactionsHash(entry.link) }, "Show these transactions →") : null,
  );
}

const pages = {
  dashboard: dashboardPage,
  transactions: transactionsPage,
  import: importPage,
  ask: askPage,
  settings: settingsPage,
};

// ---- Routing ----------------------------------------------------------------

function currentPage() {
  const name = location.hash.replace(/^#\//, "").split("?")[0];
  return name in pages ? name : "dashboard";
}

async function render() {
  const name = currentPage();
  for (const link of document.querySelectorAll(".sidebar a")) {
    link.classList.toggle("active", link.dataset.page === name);
    link.toggleAttribute("aria-current", link.dataset.page === name);
  }
  let content;
  try {
    content = await pages[name]();
  } catch (error) {
    content = h("div", { class: "flash error", role: "alert" }, `Couldn't load this page: ${error.message}`);
  }
  main.replaceChildren(...[flashElement(), content].filter(Boolean));
}

window.addEventListener("hashchange", () => {
  flash = null;
  render();
  main.focus();
});

render();
