window.answerFreshnessLabel = function (source) {
  if (!source) return "";
  var receipt = source.freshness;
  if (!receipt) return "";
  if (receipt.mode === "live") return "Live data · retrieved at " + (source.retrieved_at || "unknown time");
  return receipt.sources.map(function (item) {
    var label = item.table.split(".").pop().replace(/_/g, " ");
    return label + ": " + (item.kind === "live" ? "live" : item.data_as_of ? "data as of " + item.data_as_of : "freshness unknown");
  }).join("; ") + (receipt.mode === "unknown" ? " · Overall freshness unknown" : "");
};
/* Reference UI: query instructions arrive from Lane5; rows and rendering belong here. */
window.formatAnswerCell = function(value, fmt) {
  if (value == null) return "Not available";
  fmt = fmt || {};
  if (["text", "identifier", "date", "datetime"].includes(fmt.kind) || !fmt.kind) return String(value);
  if (fmt.kind === "boolean") return value === true || value === 1 ? "Yes" : value === false || value === 0 ? "No" : String(value);
  if (typeof value === "boolean" || typeof value === "string" && !value.trim()) return String(value);
  var n = Number(value);
  // Do not lose precision merely for display; Ember should use its decimal-safe formatter.
  if (!Number.isFinite(n) || Math.abs(n) > Number.MAX_SAFE_INTEGER || String(value).replace(/[^0-9]/g, "").length > 15) return String(value);
  if (fmt.kind === "percent" && fmt.scale === "ratio") n *= 100;
  var raw = String(value);
  // Generic numbers and fractional integers preserve the received decimal places.
  var fraction = raw.match(/\.(\d+)(?:[eE]([+-]?\d+))?$/);
  var exponent = raw.match(/[eE]([+-]?\d+)$/);
  var places = Math.max(0, (fraction ? fraction[1].length : 0) - (exponent ? Number(exponent[1]) : 0));
  if (places > 20) return raw + (fmt.unit ? " " + fmt.unit : "");
  var precision = fmt.kind === "integer" && !Number.isInteger(n) ? places :
    fmt.precision == null ? (fmt.kind === "integer" ? 0 : fmt.kind === "number" ? places : 2) : fmt.precision;
  var options = {maximumFractionDigits:precision, minimumFractionDigits:fmt.kind === "currency" ? precision : 0};
  if (fmt.kind === "currency") { options.style="currency"; options.currency=fmt.currency; }
  return n.toLocaleString("en-US", options) + (fmt.kind === "percent" ? "%" : "") + (fmt.unit ? " " + fmt.unit : "");
};
function answerPeriod(value) {
  var text=String(value), quarter=text.match(/^(\d{4})-Q([1-4])$/);
  if (quarter) return quarter[1]+"-"+String((Number(quarter[2])-1)*3+1).padStart(2,"0")+"-01";
  if (/^\d{4}$/.test(text)) return text+"-01-01";
  if (/^\d{4}-\d{2}$/.test(text)) return text+"-01";
  return text;
}

window.renderAnswerDocument = async function (root, doc, table, fetchResult) {
  root.replaceChildren();
  function text(parent, tag, value) {
    var node = document.createElement(tag);
    node.textContent = value == null ? "" : String(value);
    parent.appendChild(node);
    return node;
  }
  // Small DOM-only Markdown renderer for the harness: no HTML or executable URLs.
  function markdown(parent, value) {
    function inline(node, content) {
      var pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\))/g;
      var end = 0, match;
      while ((match = pattern.exec(content))) {
        node.appendChild(document.createTextNode(content.slice(end, match.index)));
        if (match[2]) text(node, "strong", match[2]);
        else if (match[3]) text(node, "code", match[3]);
        else { var link = text(node, "a", match[4]); link.href = match[5]; link.rel = "noopener noreferrer"; }
        end = pattern.lastIndex;
      }
      node.appendChild(document.createTextNode(content.slice(end)));
    }
    var code = null, list = null;
    String(value || "").split("\n").forEach(function (line) {
      if (line.startsWith("```")) { code = code ? null : text(parent, "pre", ""); list = null; return; }
      if (code) { code.appendChild(document.createTextNode(line + "\n")); return; }
      var item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
      if (item) { if (!list) list = text(parent, "ul", ""); inline(text(list, "li", ""), item[1]); return; }
      list = null;
      if (!line.trim()) return;
      var heading = line.match(/^(#{1,6})\s+(.+)/);
      inline(text(parent, heading ? "h" + heading[1].length : "p", ""), heading ? heading[2] : line);
    });
  }
  if (!doc || doc.schema_version !== 2) {
    text(root, "p", "This answer is unavailable.");
    return;
  }
  markdown(root, doc.takeaway);
  var sources = Object.fromEntries(Object.entries(doc.sources || {}).map(([key,value]) => [key,{...value}]));
  var pending = {};
  async function load(rid) {
    var source=sources[rid];
    if (!source || !source.sql || !fetchResult) throw new Error("Missing source SQL");
    if (!pending[rid]) pending[rid]=fetchResult(source).then(function(data) { source.snapshot_id=data.snapshot_id; return data; });
    return pending[rid];
  }
  function grid(parent, selection, title) {
    var source = sources[selection.result_id];
    if (!source || !source.sql) throw new Error("Missing source SQL");
    table(parent, source.sql, title || "View data", source);
  }
  await Promise.all((doc.sections || []).map(async function (section) {
    var card = text(root, "section", "");
    card.className = "answer-section";
    // Primary tables already expose their title in the grid/accordion heading.
    if (section.presentation.kind !== "table") text(card, "h3", section.title);
    if (section.explanation) markdown(card, section.explanation);
    var view = text(card, "div", "");
    var p = {...section.presentation};
    var tableShown = p.kind === "table";
    try {
      if (!["text","table"].includes(p.kind)) {
        var data=await load(p.result_id), cols=data.columns, rows=data.rows;
        var formats=sources[p.result_id].column_formats || {};
        if (data.has_more) throw new Error("Chart exceeds this UI's display limit");
        function index(name) { var i=cols.indexOf(name); if(i<0) throw new Error("Missing column"); return i; }
        function formatted(value,name) { return window.formatAnswerCell(value,formats[name]); }
        if (p.kind === "figure" || p.kind === "facts") {
          if (rows.length !== 1) throw new Error("Expected one row");
          if(p.kind === "figure") p.value=formatted(rows[0][index(p.column)],p.column);
          else p.items=p.columns.map(name => ({label:name.replace(/_/g," "),value:formatted(rows[0][index(name)],name)}));
        } else if (["bars","line"].includes(p.kind)) {
          var xs=(p.x_columns || [p.x_column]).map(index), y=index(p.y_column);
          var points=rows.map(function(row) {
            var raw=row[y], n=raw==null ? null : Number(raw), label=xs.map(i=>String(row[i])).join(" · ");
            if (typeof raw === "boolean" || raw != null && (typeof raw === "object" || !String(raw).trim() || !Number.isFinite(n) || Math.abs(n)>Number.MAX_SAFE_INTEGER)) throw new Error("Unsafe numeric chart value");
            return p.kind === "bars" ? {label,raw:n,value:formatted(raw,p.y_column)} : {label,time:answerPeriod(row[xs[0]]),value:n,formatted_value:raw==null?null:formatted(raw,p.y_column)};
          });
          p.measure_label=p.label;
          if (p.kind === "bars") p.items=points;
          else { p.points=points.sort((a,b)=>Date.parse(a.time)-Date.parse(b.time)); }
        }
      }
      if (p.kind === "text") markdown(view, p.markdown);
      else if (p.kind === "figure") {
        text(view, "strong", p.value);
        text(view, "p", p.label);
      } else if (p.kind === "facts") {
        var list = text(view, "dl", "");
        p.items.forEach(function (item) { text(list, "dt", item.label); text(list, "dd", item.value); });
      } else if (p.kind === "table") grid(view, p, section.title);
      else if (p.kind === "bars" || p.kind === "line") {
        var isBars = p.kind === "bars";
        var points = isBars ? p.items : p.points;
        if (!Array.isArray(points) || !points.length) throw new Error("Empty chart");
        var values = points.map(function (point) { return isBars ? point.raw : point.value; });
        if (values.some(function (v) { return v !== null && (typeof v !== "number" || !Number.isFinite(v)); }) ||
            (isBars && values.includes(null))) throw new Error("Invalid chart values");
        var finite = values.filter(function (v) { return v !== null; });
        if (!finite.length) throw new Error("Missing values");
        var lo = Math.min.apply(null, [0].concat(finite));
        var hi = Math.max.apply(null, [0].concat(finite));
        var span = hi - lo || 1;
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 600 260");
        svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", p.measure_label);
        svg.style.maxWidth = "720px";
        svg.style.width = "100%";
        view.appendChild(svg);
        function shape(tag, attrs) {
          var node = document.createElementNS(svg.namespaceURI, tag);
          Object.keys(attrs).forEach(function (key) { node.setAttribute(key, attrs[key]); });
          svg.appendChild(node);
          return node;
        }
        var previous = null;
        var times = isBars ? [] : points.map(function (point) { return Date.parse(point.time); });
        if (!isBars && (times.some(function (t) { return !Number.isFinite(t); }) || times[times.length - 1] <= times[0])) throw new Error("Invalid time axis");
        shape("line", {x1:40, y1:220, x2:580, y2:220, stroke:"currentColor"});
        points.forEach(function (point, index) {
          var value = values[index];
          if (value === null) { previous = null; return; }
          var x = isBars ? 50 + index * 520 / points.length : 40 + 540 * (times[index] - times[0]) / (times[times.length - 1] - times[0]);
          var y = 220 - 200 * (value - lo) / span;
          var zero = 220 - 200 * (0 - lo) / span;
          if (isBars) shape("rect", {x:x, y:Math.min(y,zero), width:Math.max(1, 480/points.length), height:Math.abs(y-zero), fill:"#5288d8"});
          else {
            if (previous) shape("line", {x1:previous.x, y1:previous.y, x2:x, y2:y, stroke:"#5288d8", "stroke-width":2});
            shape("circle", {cx:x, cy:y, r:4, fill:"#5288d8"});
            previous = {x:x, y:y};
          }
        });
        // Accessible exact labels/values also cover dense labels and missing periods.
        text(view, "p", p.measure_label);
        var legend = text(view, "ul", "");
        points.forEach(function (point) {
          text(legend, "li", (point.rank == null ? "" : point.rank + ". ") + point.label + ": " +
            (isBars ? point.value : point.formatted_value == null ? "Missing" : point.formatted_value));
        });
      } else throw new Error("Unsupported presentation");
    } catch (error) {
      view.replaceChildren();
      try {
        if (!sources[p.result_id]) throw error;
        text(view, "p", "This visualization is unavailable; showing its approved table.");
        grid(view, p);
        tableShown = true;
      } catch (_) { text(view, "p", "This view is unavailable."); }
    }
    if (!tableShown && ["facts","bars","line"].includes(p.kind)) {
      var details = text(card, "details", "");
      text(details, "summary", "View data / CSV");
      try { grid(details, p); } catch (_) { text(details, "p", "This data is unavailable."); }
    }
    if (p.kind !== "table") {
      var freshnessLabel = window.answerFreshnessLabel((doc.sources || {})[p.result_id]);
      if (freshnessLabel) text(card, "p", freshnessLabel);
    }
    (section.caveats || []).forEach(function (caveat) { text(card, "p", caveat); });
    (section.actions || []).forEach(function (action) { text(card, "p", action.label + " — open this prepared action in Ember."); });
  }));
};
