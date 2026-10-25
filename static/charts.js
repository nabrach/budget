// Hand-drawn SVG/HTML charts. No libraries.
//
// Conventions (from the data-viz guide): thin marks, 4px rounded data ends,
// a 2px surface gap between stacked segments, hairline grid, a legend for 2+
// series, text in ink colors (never the series color), a hover/focus tooltip
// on every mark, and a table view for every chart.

const SVG = "http://www.w3.org/2000/svg";

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usdCompact = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1,
});
export const money = (cents) => usd.format(cents / 100);
const moneyCompact = (cents) => usdCompact.format(cents / 100);
const usdWhole = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const moneyWhole = (cents) => usdWhole.format(cents / 100);

// Category colors: the reference palette's 8 slots, read from CSS so dark mode
// gets its own steps. Slots go to categories by all-time spending rank, so a
// category keeps its color whatever date range is shown. Past 7, "Other".
export const OTHER = "other";
export function categoryColors(rank) {
  const colors = new Map();
  rank.slice(0, 7).forEach((id, i) => colors.set(String(id), `var(--series-${i + 1})`));
  return (id) => colors.get(String(id)) ?? "var(--series-other)";
}

// ---- Tooltip (one shared element) ---------------------------------------------

let tip;
function tooltipEl() {
  if (!tip) {
    tip = el("div", { class: "chart-tooltip", role: "tooltip", hidden: true });
    document.body.append(tip);
  }
  return tip;
}

// rows: [{ value, label, color, key: "line" | "rect" }]. Values lead, labels follow.
export function showTooltip(anchor, title, rows) {
  const t = tooltipEl();
  t.replaceChildren(
    el("div", { class: "tt-title" }, title),
    rows.map((row) =>
      el(
        "div",
        { class: "tt-row" },
        row.color ? el("span", { class: `tt-key ${row.key ?? "line"}`, style: { background: row.color } }) : null,
        el("strong", {}, row.value),
        el("span", { class: "tt-label" }, row.label),
      ),
    ),
  );
  t.hidden = false;
  const box = anchor.getBoundingClientRect();
  const width = t.offsetWidth;
  const height = t.offsetHeight;
  let left = box.left + box.width / 2 - width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  let top = box.top - height - 8;
  if (top < 8) top = box.bottom + 8;
  t.style.left = `${left}px`;
  t.style.top = `${top}px`;
}

export function hideTooltip() {
  if (tip) tip.hidden = true;
}
window.addEventListener("scroll", hideTooltip, { passive: true });
window.addEventListener("hashchange", hideTooltip);

// ---- Shared pieces -----------------------------------------------------------

function niceScale(max, ticks = 4) {
  if (max <= 0) return { max: 10000, step: 2500 }; // $100 in cents
  const raw = max / ticks;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((c) => c >= raw);
  return { max: step * Math.ceil(max / step), step };
}

// Rounded at the top (the data end), square at the baseline.
function columnPath(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y + r}V${y + h}Z`;
}

// Draws when the container has a width, and again on resize.
function responsive(draw, minHeight) {
  const box = el("div", { class: "chart-box", style: { minHeight: `${minHeight}px` } });
  let lastWidth = 0;
  new ResizeObserver(([entry]) => {
    const width = Math.floor(entry.contentRect.width);
    if (width && width !== lastWidth) {
      lastWidth = width;
      box.replaceChildren(draw(width));
    }
  }).observe(box);
  return box;
}

export function legend(items) {
  return el(
    "ul",
    { class: "legend" },
    items.map((item) =>
      el("li", {}, el("span", { class: `swatch ${item.key ?? "rect"}`, style: { background: item.color } }), item.label),
    ),
  );
}

export function tableView(headers, rows) {
  return el(
    "details",
    { class: "table-view" },
    el("summary", {}, "Show as table"),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, headers.map((label, i) => el("th", { class: i ? "num" : null }, label)))),
        el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((cell, i) => el("td", { class: i ? "num" : null }, cell))))),
      ),
    ),
  );
}

// Show every Nth month label so they never collide.
function labelEvery(count, width) {
  const fits = Math.max(1, Math.floor(width / 56));
  return Math.ceil(count / fits);
}

function monthTick(month, index, previous) {
  const [year, m] = month.month.split("-").map(Number);
  const name = new Date(year, m - 1, 1).toLocaleString("en-US", { month: "short" });
  const showYear = index === 0 || m === 1 || (previous && previous.month.slice(0, 4) !== month.month.slice(0, 4));
  return showYear ? `${name} ${String(year).slice(2)}` : name;
}

const PLOT = { top: 24, right: 16, bottom: 28, left: 56, height: 220 };

function yAxis(svg, width, scale, y) {
  for (let v = 0; v <= scale.max; v += scale.step) {
    svg.append(
      s("line", { x1: PLOT.left, x2: width - PLOT.right, y1: y(v), y2: y(v), class: v ? "grid" : "baseline" }),
      s("text", { x: PLOT.left - 8, y: y(v), class: "tick", "text-anchor": "end", "dominant-baseline": "middle" }, moneyCompact(v)),
    );
  }
}

// ---- Column chart: one column per month, optionally stacked ----------------------
//
// months: [{ month, label, ... }]
// series: [{ key, label, color, value(month) -> cents }]
// onSelect(month): called when a column is clicked or activated by keyboard.

export function columnChart({ months, series, onSelect, ariaLabel }) {
  const totals = months.map((m) => series.reduce((sum, ser) => sum + Math.max(0, ser.value(m)), 0));
  const scale = niceScale(Math.max(...totals, 0));
  const max = Math.max(...totals);
  const lastWithData = totals.findLastIndex((t) => t > 0);
  const stacked = series.length > 1;

  const draw = (width) => {
    const height = PLOT.top + PLOT.height + PLOT.bottom;
    const plotWidth = width - PLOT.left - PLOT.right;
    const band = plotWidth / months.length;
    const barWidth = Math.min(24, band * 0.6);
    const y = (v) => PLOT.top + PLOT.height - (v / scale.max) * PLOT.height;
    const svg = s("svg", { width, height, class: "chart", role: "img", "aria-label": ariaLabel });
    yAxis(svg, width, scale, y);
    const every = labelEvery(months.length, plotWidth);

    months.forEach((month, i) => {
      const x = PLOT.left + band * i + (band - barWidth) / 2;
      const group = s("g", { class: "column" });
      // Stack from the baseline up, leaving a 2px surface gap between segments.
      let base = 0;
      const parts = series.map((ser) => ({ ser, value: Math.max(0, ser.value(month)) })).filter((p) => p.value > 0);
      parts.forEach((part, j) => {
        const top = y(base + part.value);
        const bottom = y(base);
        const gap = j > 0 ? 2 : 0;
        const isTop = j === parts.length - 1;
        const h = bottom - top - gap;
        if (h > 0) {
          group.append(
            isTop
              ? s("path", { d: columnPath(x, top, barWidth, h), fill: part.ser.color, class: "mark" })
              : s("rect", { x, y: top, width: barWidth, height: h, fill: part.ser.color, class: "mark" }),
          );
        }
        base += part.value;
      });

      // Label the latest and the highest month only.
      if (totals[i] > 0 && (i === lastWithData || totals[i] === max)) {
        group.append(s("text", { x: x + barWidth / 2, y: y(totals[i]) - 6, class: "value", "text-anchor": "middle" }, moneyWhole(totals[i])));
      }
      if (i % every === 0) {
        group.append(s("text", { x: x + barWidth / 2, y: PLOT.top + PLOT.height + 18, class: "tick", "text-anchor": "middle" }, monthTick(month, i, months[i - 1])));
      }

      // Hit target: the whole band, taller than the mark.
      const rows = [
        { value: money(totals[i]), label: "spent" },
        ...(stacked
          ? series
              .map((ser) => ({ value: money(ser.value(month)), label: ser.label, color: ser.color, key: "rect", cents: ser.value(month) }))
              .filter((row) => row.cents !== 0)
              .reverse()
          : []),
      ];
      const hit = s("rect", {
        x: PLOT.left + band * i, y: PLOT.top, width: band, height: PLOT.height,
        class: "hit", tabindex: 0, role: "button",
        "aria-label": `${month.label}: ${money(totals[i])} spent. Show transactions.`,
      });
      const show = () => { group.classList.add("hover"); showTooltip(hit, month.label, rows); };
      const hide = () => { group.classList.remove("hover"); hideTooltip(); };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
      hit.addEventListener("click", () => { hideTooltip(); onSelect?.(month); });
      hit.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); hideTooltip(); onSelect?.(month); } });
      group.append(hit);
      svg.append(group);
    });
    return svg;
  };
  return responsive(draw, PLOT.top + PLOT.height + PLOT.bottom);
}

// ---- Line chart with a crosshair -----------------------------------------------
//
// series: [{ label, color, value(month) -> cents }] on one dollar axis.
// extraRows(month): extra tooltip rows, e.g. the difference between the series.

export function lineChart({ months, series, onSelect, extraRows, ariaLabel }) {
  const all = months.flatMap((m) => series.map((ser) => ser.value(m)));
  const scale = niceScale(Math.max(...all, 0));

  const draw = (width) => {
    const height = PLOT.top + PLOT.height + PLOT.bottom;
    const plotWidth = width - PLOT.left - PLOT.right - 64; // room for end labels
    const step = months.length > 1 ? plotWidth / (months.length - 1) : 0;
    const x = (i) => PLOT.left + (months.length > 1 ? step * i : plotWidth / 2);
    const y = (v) => PLOT.top + PLOT.height - (Math.max(0, v) / scale.max) * PLOT.height;
    const svg = s("svg", { width, height, class: "chart", role: "img", "aria-label": ariaLabel });
    yAxis(svg, width - 64, scale, y);

    const every = labelEvery(months.length, plotWidth);
    months.forEach((month, i) => {
      if (i % every === 0) {
        svg.append(s("text", { x: x(i), y: PLOT.top + PLOT.height + 18, class: "tick", "text-anchor": "middle" }, monthTick(month, i, months[i - 1])));
      }
    });

    // End labels, nudged apart only if they would overlap.
    const ends = series.map((ser) => ({ ser, y: y(ser.value(months[months.length - 1])) }));
    if (ends.length === 2 && Math.abs(ends[0].y - ends[1].y) < 14) {
      const [hi, lo] = ends[0].y < ends[1].y ? ends : [ends[1], ends[0]];
      const mid = (hi.y + lo.y) / 2;
      hi.y = mid - 7;
      lo.y = mid + 7;
    }

    series.forEach((ser, k) => {
      const points = months.map((m, i) => `${x(i)},${y(ser.value(m))}`);
      svg.append(s("polyline", { points: points.join(" "), fill: "none", stroke: ser.color, class: "line" }));
      const last = months.length - 1;
      svg.append(
        s("circle", { cx: x(last), cy: y(ser.value(months[last])), r: 4, fill: ser.color, class: "dot" }),
        s("text", { x: x(last) + 10, y: ends[k].y, class: "value", "dominant-baseline": "middle" }, ser.label),
      );
    });

    // Crosshair: snaps to the nearest month.
    const cross = s("g", { class: "crosshair", visibility: "hidden" });
    const hair = s("line", { y1: PLOT.top, y2: PLOT.top + PLOT.height, class: "hair" });
    const dots = series.map((ser) => s("circle", { r: 4, fill: ser.color, class: "dot" }));
    cross.append(hair, ...dots);
    svg.append(cross);

    const overlay = s("rect", {
      x: PLOT.left - step / 2, y: PLOT.top, width: plotWidth + step, height: PLOT.height,
      class: "hit", tabindex: 0, role: "slider",
      "aria-label": `${ariaLabel}. Use the arrow keys to move between months and Enter to show transactions.`,
    });
    let index = months.length - 1;
    const point = (i) => {
      index = Math.max(0, Math.min(months.length - 1, i));
      const month = months[index];
      hair.setAttribute("x1", x(index));
      hair.setAttribute("x2", x(index));
      series.forEach((ser, k) => {
        dots[k].setAttribute("cx", x(index));
        dots[k].setAttribute("cy", y(ser.value(month)));
      });
      cross.setAttribute("visibility", "visible");
      overlay.setAttribute("aria-valuetext", month.label);
      showTooltip(hair, month.label, [
        ...series.map((ser) => ({ value: money(ser.value(month)), label: ser.label, color: ser.color })),
        ...(extraRows ? extraRows(month) : []),
      ]);
    };
    const nearest = (event) => {
      const box = svg.getBoundingClientRect();
      const px = event.clientX - box.left;
      return months.length > 1 ? Math.round((px - PLOT.left) / step) : 0;
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); hideTooltip(); };
    overlay.addEventListener("pointermove", (e) => point(nearest(e)));
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", () => point(index));
    overlay.addEventListener("blur", hide);
    overlay.addEventListener("click", (e) => { hideTooltip(); onSelect?.(months[nearest(e)]); });
    overlay.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); point(index - 1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); point(index + 1); }
      else if (e.key === "Enter") { e.preventDefault(); hideTooltip(); onSelect?.(months[index]); }
    });
    svg.append(overlay);
    return svg;
  };
  return responsive(draw, PLOT.top + PLOT.height + PLOT.bottom);
}

// ---- Horizontal bars (HTML) ---------------------------------------------------
// items: [{ label, cents, onSelect }]; one series, so one color.

export function barList({ items, color = "var(--series-1)" }) {
  const max = Math.max(...items.map((i) => i.cents), 1);
  return el(
    "div",
    { class: "bar-list" },
    items.map((item) => {
      const button = el(
        "button",
        {
          class: "bar-row",
          type: "button",
          "aria-label": `${item.label}: ${money(item.cents)}. Show transactions.`,
          onclick: () => { hideTooltip(); item.onSelect?.(); },
        },
        el("span", { class: "bar-label" }, item.label),
        el(
          "span",
          { class: "bar-track" },
          // Leave 84px after the longest bar for its value label.
          el("span", { class: "bar", style: { width: `calc((100% - 84px) * ${item.cents / max})`, background: color } }),
          el("span", { class: "bar-value" }, money(item.cents)),
        ),
      );
      return button;
    }),
  );
}

// ---- Budget meter (HTML) --------------------------------------------------------
// Fill color carries the state; a hairline marks how far through the month it is.

const STATES = {
  ok: { icon: "✓", label: "On track" },
  pace: { icon: "↗", label: "Ahead of pace" },
  warn: { icon: "!", label: "Near limit" },
  over: { icon: "✕", label: "Over budget" },
};

export function meter({ spent, limit, state, elapsedPct }) {
  const pct = limit ? Math.min(100, (Math.max(0, spent) / limit) * 100) : 0;
  return el(
    "div",
    {
      class: `meter state-${state}`,
      role: "meter",
      "aria-valuemin": 0,
      "aria-valuemax": limit,
      "aria-valuenow": Math.max(0, spent),
      "aria-valuetext": `${money(spent)} of ${money(limit)}`,
    },
    el("span", { class: "meter-fill", style: { width: `${pct}%` } }),
    elapsedPct != null ? el("span", { class: "meter-today", style: { left: `${elapsedPct}%` }, title: "How far through the month we are" }) : null,
  );
}

export function stateBadge(state) {
  const { icon, label } = STATES[state];
  return el("span", { class: `badge state-${state}` }, el("span", { class: "badge-icon", "aria-hidden": "true" }, icon), label);
}
