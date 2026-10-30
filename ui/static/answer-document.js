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
/* Reference renderer: values and ordering come only from the approved document. */
window.renderAnswerDocument = function (root, doc, table) {
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
  if (!doc || doc.schema_version !== 1) {
    text(root, "p", "This answer is unavailable.");
    return;
  }
  markdown(root, doc.takeaway);
  function grid(parent, selection, title) {
    var source = (doc.sources || {})[selection.result_id];
    if (!source || !source.table_ref) throw new Error("Missing table reference");
    table(parent, source.table_ref, title || "Supporting data", source);
  }
  (doc.sections || []).forEach(function (section) {
    var card = text(root, "section", "");
    card.className = "answer-section";
    text(card, "h3", section.title);
    if (section.explanation) markdown(card, section.explanation);
    var view = text(card, "div", "");
    var p = section.presentation || {};
    try {
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
        if (!section.fallback_table) throw error;
        text(view, "p", "This visualization is unavailable; showing its approved table.");
        grid(view, section.fallback_table);
      } catch (_) { text(view, "p", "This view is unavailable."); }
    }
    if (section.supporting_table) {
      var details = text(card, "details", "");
      text(details, "summary", "Supporting data");
      try { grid(details, section.supporting_table); } catch (_) { text(details, "p", "This data is unavailable."); }
    }
    if (p.kind !== "table") {
      var freshnessLabel = window.answerFreshnessLabel((doc.sources || {})[p.result_id]);
      if (freshnessLabel) text(card, "p", freshnessLabel);
    }
    (section.caveats || []).forEach(function (caveat) { text(card, "p", caveat); });
    (section.actions || []).forEach(function (action) { text(card, "p", action.label + " — open this prepared action in Ember."); });
  });
};
