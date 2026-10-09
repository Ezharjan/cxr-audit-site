/* Copyright (C) 2026 Goh Shu Wen. AGPL-3.0-or-later. See LICENSE. */
/* Visual layout editor: the canvas is a projection of the current YAML textarea. */
(function () {
  "use strict";
  var TYPES = { heading: ["Heading", "H"], value: ["Value", "#"], text: ["Static text", "T"], divider: ["Divider", "—"],
    table: ["Table", "▦"], line: ["Line chart", "⌁"], bar: ["Bar chart", "▥"], pie: ["Pie chart", "◔"],
    confusion_matrix: ["Confusion matrix", "⊞"], boxplot: ["Box plot", "⊟"] };
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

  window.PrimerVisualEditor = function (root, api) {
    var canvas = root.querySelector('[data-role="visual-canvas"]');
    var textarea = root.querySelector('[data-role="yaml-textarea"]');
    if (!canvas || !textarea) { return; }
    var status = root.querySelector('[data-role="visual-status"]');
    var dialog = root.querySelector('[data-role="card-options"]');
    var form = root.querySelector('[data-role="card-options-form"]');
    var currentCard;
    var addCard = root.querySelector('[data-visual-action="add-card"]');
    var undo = root.querySelector('[data-visual-action="undo"]');
    var formError = root.querySelector('[data-role="card-form-error"]');
    var layout, catalog, selected, history = [], historyText = textarea.value, typingTimer, syncTimer;
    var sequence = 0, busy = false, valid = false, writing = false, gesture;
    function field(name) { return form.elements.namedItem(name); }
    function el(tag, text, cls) {
      var node = document.createElement(tag);
      if (text !== undefined) { node.textContent = text; }
      if (cls) { node.className = cls; }
      return node;
    }
    function button(text, action, cls) {
      var node = el("button", text, cls || "btn btn-outline");
      node.type = "button"; node.dataset.visualAction = action; return node;
    }
    function remember(text) {
      if (history[history.length - 1] !== text) { history.push(text); }
      if (history.length > 50) { history.shift(); }
      undo.disabled = !history.length || busy;
    }
    function finishTyping() {
      clearTimeout(typingTimer);
      if (textarea.value !== historyText) { remember(historyText); historyText = textarea.value; }
    }
    function state(message) {
      status.textContent = message || "";
      canvas.inert = busy || !valid;
      addCard.disabled = busy || !valid;
      form.inert = busy;
      canvas.classList.toggle("visual-paused", busy || !valid);
      undo.disabled = !history.length || busy;
    }
    function write(text) {
      textarea.value = text;
      historyText = text;
      writing = true;
      try { textarea.dispatchEvent(new Event("input", { bubbles: true })); }
      finally { writing = false; }
    }
    function sync() {
      var text = textarea.value, request = ++sequence;
      valid = false; state("Checking layout…");
      return api.post(root.dataset.visualUrl).then(function (result) {
        if (request !== sequence || text !== textarea.value) { return; }
        if (result.status >= 400) {
          state("Visual editing paused. Correct the YAML: " + (result.data.errors || ["Layout could not load."]).join(" "));
          return;
        }
        layout = result.data.layout; catalog = result.data.catalog; valid = true;
        render(); state();
      }).catch(function () { if (request === sequence) { state("Layout could not load. Try switching to Visual editor again."); } });
    }
    function mutate(operation, previewOnly) {
      if (busy || !valid) { return Promise.resolve(false); }
      finishTyping();
      var before = textarea.value, request = ++sequence;
      busy = true; state("Updating card…"); formError.textContent = "";
      return api.post(root.dataset.visualUrl, { operation: JSON.stringify(operation) }).then(function (result) {
        if (request !== sequence || before !== textarea.value) { return false; }
        if (result.status >= 400) {
          formError.textContent = (result.data.errors || ["Card could not be updated."]).join(" ");
          state(formError.textContent); return false;
        }
        if (previewOnly) {
          var cards = result.data.layout.sections.find(function (s) { return s.id === operation.section_id; }).widgets;
          var previousIds = layout.sections.flatMap(function (section) { return section.widgets.map(function (card) { return card.id; }); });
          var id = operation.action === "add" ? cards.find(function (card) { return previousIds.indexOf(card.id) < 0; }).id : operation.widget_id;
          api.preview({ yaml_text: result.data.yaml_text, widget_id: id });
        } else {
          if (before !== result.data.yaml_text) { remember(before); write(result.data.yaml_text); }
          layout = result.data.layout; catalog = result.data.catalog; valid = true; render();
        }
        return true;
      }).catch(function () { formError.textContent = "Card could not be updated. Try again."; state(formError.textContent); return false; })
        .finally(function () { busy = false; state(formError.textContent); });
    }
    function render() {
      canvas.replaceChildren();
      layout.sections.forEach(function (section) {
        var container = el("section", undefined, "visual-section"); container.dataset.sectionId = section.id;
        var grid = el("div", undefined, "visual-grid");
        grid.style.setProperty("--visual-row-height", layout.grid.row_height_px + "px");
        section.widgets.forEach(function (card) {
          var node = el("article", undefined, "visual-card"); node.tabIndex = 0; node.dataset.widgetId = card.id; node.dataset.type = card.type;
          node.style.gridColumn = "span " + card.layout.width;
          node.style.gridRow = "span " + card.layout.height;
          var heading = el("header", undefined, "visual-card-header");
          var handle = button("⠿", "move", "visual-move"); handle.setAttribute("aria-label", "Move " + card.title);
          heading.append(handle, el("h4", card.title)); node.appendChild(heading);
          if (card.type === "text") { node.appendChild(el("p", card.text, "visual-text")); }
          else if (card.type === "divider") { node.appendChild(el("hr")); }
          else if (card.type !== "heading") { node.appendChild(el("span", TYPES[card.type][1], "visual-icon")); node.appendChild(el("p", TYPES[card.type][0], "editor-help")); }
          var actions = el("div", undefined, "visual-card-actions");
          actions.append(button("Edit", "edit"), button("Preview", "preview")); node.appendChild(actions);
          var resize = button("◢", "resize", "visual-resize"); resize.setAttribute("aria-label", "Resize " + card.title); node.appendChild(resize);
          node.addEventListener("click", function (event) {
            if (busy || !valid) { return; }
            currentCard = card.id;
            canvas.querySelectorAll(".visual-selected").forEach(function (item) { item.classList.remove("visual-selected"); });
            node.classList.toggle("visual-selected", true);
            var action = event.target.closest("[data-visual-action]");
            if (!action) { return; }
            if (action.dataset.visualAction === "edit") { openOptions(section, card); }
            if (action.dataset.visualAction === "preview") { api.preview({ widget_id: card.id }); }
          });
          handle.addEventListener("pointerdown", function (event) { startGesture(event, section, card, node, grid, "move"); });
          resize.addEventListener("pointerdown", function (event) { startGesture(event, section, card, node, grid, "resize"); });
          grid.appendChild(node);
        });
        container.appendChild(grid); canvas.appendChild(container);
      });
    }
    function choices(select, values, value, empty) {
      select.replaceChildren(); if (empty !== undefined) { select.appendChild(new Option(empty, "")); }
      values.forEach(function (item) { select.appendChild(new Option(item.label, item.id)); });
      select.value = value || "";
      if (select.selectedIndex < 0 && select.options.length) { select.selectedIndex = 0; }
    }
    function checks(role, values, chosen, prefix) {
      var box = form.querySelector('[data-role="' + role + '"]');
      box.querySelectorAll("label").forEach(function (label) { label.remove(); });
      values.forEach(function (item) {
        var label = el("label", undefined, "card-check"), input = el("input"); input.type = "checkbox";
        input.name = prefix; input.value = item.id; input.checked = chosen.indexOf(item.id) >= 0;
        label.append(input, document.createTextNode(item.label)); box.appendChild(label);
      });
    }
    function checked(name) { return Array.from(form.querySelectorAll('input[name="' + name + '"]:checked'), function (node) { return node.value; }); }
    function measurement() { return catalog.measurements.find(function (m) { return m.id === field("measurement").value; }); }
    function displayFields(selector, shown) { form.querySelectorAll(selector).forEach(function (node) { node.hidden = !shown; }); }
    function refreshGuide(reset) {
      var type = field("type").value, original = selected.card || {}, query = original.query || {}, m;
      var dynamic = type !== "text" && type !== "divider" && type !== "heading";
      displayFields("[data-dynamic]", dynamic); displayFields("[data-text]", type === "text");
      displayFields("[data-line]", type === "line"); displayFields("[data-pie]", type === "pie");
      displayFields("[data-box]", type === "boxplot"); displayFields("[data-matrix]", type === "confusion_matrix");
      form.querySelectorAll('[data-role="source-options"] select').forEach(function (node) { node.disabled = !dynamic; });
      if (!dynamic) { return; }
      if (reset) {
        choices(field("measurement"), catalog.measurements.filter(function (item) { return item.displays.indexOf(type) >= 0; }), query.measurement);
      }
      m = measurement();
      form.querySelector('[data-role="measure-description"]').textContent = m ? m.description : "No compatible measurement.";
      var sources = form.querySelector('[data-role="source-options"]');
      var previous = {};
      sources.querySelectorAll("select").forEach(function (node) { previous[node.dataset.role] = node.value; });
      sources.replaceChildren();
      if (!m) { return; }
      Object.keys(Object.assign({}, m.inputs, m.optional_inputs)).forEach(function (role) {
        var kind = m.inputs[role] || m.optional_inputs[role], label = el("label", role.replace(/_/g, " "));
        var select = el("select"); select.dataset.role = role;
        choices(select, catalog.sources.filter(function (source) { return source.kind === kind || (role === "prediction" && source.kind === "score"); }),
          previous[role] || (query.inputs || {})[role], m.inputs[role] ? "Choose a data source" : "None");
        select.required = !!m.inputs[role]; label.appendChild(select); sources.appendChild(label);
      });
      var controls = original.controls || {};
      checks("filter-options", catalog.dimensions, controls.filters || [], "filters");
      checks("comparison-options", m.comparison ? catalog.dimensions : [], controls.compare_by || [], "comparison");
      choices(field("default_group"), catalog.dimensions.filter(function (d) { return checked("comparison").indexOf(d.id) >= 0; }), original.default_compare_by, "None");
      field("ci").disabled = ["accuracy", "sensitivity", "specificity"].indexOf(m.id) < 0;
      if (field("ci").disabled) { field("ci").checked = false; }
      checks("column-options", (m.columns || []).map(function (id) { return { id: id, label: id.replace(/_/g, " ") }; }), original.columns || [], "columns");
      form.querySelector('[data-role="column-options"]').hidden = type !== "table";
      form.querySelector('[data-role="benchmark-options"]').hidden = ["value", "line", "bar", "boxplot"].indexOf(type) < 0;
    }
    function thresholdGuide(settings) {
      var box = form.querySelector('[data-role="lunit-threshold-options"]');
      var usesLunit = Array.from(form.querySelectorAll('[data-role="source-options"] select')).some(function (node) { return node.value === "lunit_findings"; });
      var policy = catalog.policies.find(function (p) { return p.id === field("policy").value; });
      box.hidden = !usesLunit || !policy;
      box.querySelectorAll("input").forEach(function (node) {
        node.disabled = box.hidden || (node.dataset.finding && checked("lunit_findings").indexOf(node.dataset.finding) < 0);
      });
      if (settings === undefined) { return; }
      settings = settings || {};
      field("lunit_default").value = settings.default === undefined ? "" : settings.default;
      var list = form.querySelector('[data-role="lunit-finding-options"]'); list.replaceChildren();
      if (!policy) { return; }
      Object.keys(policy.findings).forEach(function (finding) {
        var row = el("div", undefined, "card-size-fields"), label = el("label", undefined, "card-check");
        var include = el("input"); include.type = "checkbox"; include.name = "lunit_findings"; include.value = finding;
        include.checked = !settings.findings || settings.findings.indexOf(finding) >= 0;
        label.append(include, document.createTextNode(finding.replace(/_/g, " ")));
        var overrideLabel = el("label", "Cutoff override"), cutoff = el("input"); cutoff.type = "number";
        cutoff.min = policy.score_scale[0]; cutoff.max = policy.score_scale[1]; cutoff.step = "any";
        cutoff.dataset.finding = finding; cutoff.placeholder = "Use default cutoff or policy (" + policy.findings[finding] + ")";
        cutoff.value = (settings.overrides || {})[finding] === undefined ? "" : settings.overrides[finding];
        cutoff.disabled = box.hidden || !include.checked;
        include.addEventListener("change", function () { cutoff.disabled = !include.checked; });
        overrideLabel.appendChild(cutoff); row.append(label, overrideLabel); list.appendChild(row);
      });
    }
    function addTarget(target) {
      var row = el("div", undefined, "card-target-row");
      var label = el("input"), value = el("input"), remove = button("Remove", "remove-target");
      label.placeholder = "Target name"; label.setAttribute("aria-label", "Target name"); label.value = target.label || "";
      value.type = "number"; value.step = "any"; value.setAttribute("aria-label", "Target value"); value.value = target.value === undefined ? "" : target.value;
      row.append(label, value, remove); row.dataset.unit = target.unit || "";
      remove.addEventListener("click", function () { row.remove(); });
      form.querySelector('[data-role="benchmark-list"]').appendChild(row);
    }
    function openOptions(section, card, type) {
      selected = { section: section, card: card ? clone(card) : null, text: textarea.value };
      form.reset(); formError.textContent = "";
      var current = card || {}, query = current.query || {}, size = current.layout || { width: catalog.presets[type][0], height: catalog.presets[type][1] };
      choices(field("type"), Object.keys(TYPES).map(function (id) { return { id: id, label: TYPES[id][0] }; }), current.type || type);
      field("title").value = current.title || TYPES[type][0]; field("text").value = current.text || "";
      field("width").value = size.width; field("height").value = size.height;
      field("start").value = (current.window || {}).start || "M"; field("end").value = (current.window || {}).end || "D";
      field("bucket").value = current.bucket || "week"; field("export").value = current.export || "summary";
      field("ci").checked = !!(current.ci || {}).enabled;
      field("donut").checked = !!(current.options || {}).donut;
      field("whiskers").value = (current.options || {}).whiskers || "tukey";
      field("normalization").value = (current.options || {}).normalization || "count";
      choices(field("cohort"), catalog.cohorts, query.cohort, "All eligible records");
      choices(field("policy"), catalog.policies, query.threshold_policy, "None (label predictions)");
      refreshGuide(true);
      thresholdGuide(query.lunit_thresholds || {});
      form.querySelector('[data-role="benchmark-list"]').replaceChildren(); (current.benchmarks || []).forEach(addTarget);
      var deleting = form.querySelector('[data-visual-action="delete-card"]'); deleting.hidden = !card; deleting.disabled = section.widgets.length === 1;
      root.querySelector("#cardOptionsTitle").textContent = card ? "Edit card" : "Add card";
      dialog.showModal();
    }
    function candidate() {
      var card = clone(selected.card || {}), type = field("type").value;
      card.title = field("title").value.trim(); card.type = type;
      card.layout = { width: Number(field("width").value), height: Number(field("height").value) };
      if (type === "text" || type === "divider" || type === "heading") {
        Object.keys(card).forEach(function (key) { if (["id", "title", "type", "layout"].indexOf(key) < 0) { delete card[key]; } });
        if (type === "text") { card.text = field("text").value; }
        return card;
      }
      delete card.text;
      var m = measurement(), oldQuery = card.query || {};
      card.query = Object.assign({}, oldQuery, { measurement: m.id, inputs: {} });
      form.querySelectorAll('[data-role="source-options"] select').forEach(function (node) { if (node.value) { card.query.inputs[node.dataset.role] = node.value; } });
      [ ["cohort", "cohort"], ["threshold_policy", "policy"] ].forEach(function (pair) { if (field(pair[1]).value) { card.query[pair[0]] = field(pair[1]).value; } else { delete card.query[pair[0]]; } });
      delete card.query.lunit_thresholds;
      if (!form.querySelector('[data-role="lunit-threshold-options"]').hidden) {
        var findings = checked("lunit_findings"), settings = {}, overrides = {};
        if (!findings.length) { throw new Error("Include at least one Lunit finding."); }
        var policy = catalog.policies.find(function (p) { return p.id === field("policy").value; });
        if (findings.length !== Object.keys(policy.findings).length) { settings.findings = findings; }
        if (field("lunit_default").value !== "") { settings.default = Number(field("lunit_default").value); }
        form.querySelectorAll('[data-finding]').forEach(function (node) {
          if (!node.disabled && node.value !== "") { overrides[node.dataset.finding] = Number(node.value); }
        });
        if (Object.keys(overrides).length) { settings.overrides = overrides; }
        if (Object.keys(settings).length) { card.query.lunit_thresholds = settings; }
      }
      card.window = { start: field("start").value.trim(), end: field("end").value.trim() };
      card.controls = { date_range: true, filters: checked("filters"), compare_by: checked("comparison") };
      delete card.default_compare_by; if (field("default_group").value) { card.default_compare_by = field("default_group").value; }
      card.ci = field("ci").checked ? { enabled: true, method: "wilson" } : { enabled: false };
      card.export = field("export").value;
      delete card.bucket; if (type === "line") { card.bucket = field("bucket").value; }
      delete card.columns; if (type === "table" && checked("columns").length) { card.columns = checked("columns"); }
      delete card.options;
      if (type === "pie") { card.options = { donut: field("donut").checked }; }
      if (type === "boxplot") { card.options = { whiskers: field("whiskers").value }; }
      if (type === "confusion_matrix") { card.options = { normalization: field("normalization").value }; }
      delete card.benchmarks;
      if (["value", "line", "bar", "boxplot"].indexOf(type) >= 0) {
        var targets = Array.from(form.querySelectorAll(".card-target-row"), function (row) {
          var inputs = row.querySelectorAll("input");
          if (!inputs[0].value.trim() || !inputs[1].value || !Number.isFinite(Number(inputs[1].value))) { throw new Error("Give each target a name and finite value."); }
          return { label: inputs[0].value.trim(), value: Number(inputs[1].value), unit: row.dataset.unit || Object.values(m.units)[0] };
        });
        if (targets.length) { card.benchmarks = targets; }
      }
      return card;
    }
    function submit(previewOnly) {
      if (busy || !valid) { formError.textContent = "Wait for the layout to finish updating, then try again."; return; }
      if (!form.reportValidity()) { return; }
      if (selected.text !== textarea.value) { formError.textContent = "The draft changed. Cancel and reopen this card to use its latest settings."; return; }
      var card;
      try { card = candidate(); } catch (error) { formError.textContent = error.message; return; }
      if (selected.card && selected.card.type !== card.type && !window.confirm("Changing type may remove incompatible chart, column, and data settings. Apply this change?")) { return; }
      return mutate({ action: selected.card ? "edit" : "add", section_id: selected.section.id,
        widget_id: selected.card ? selected.card.id : "", index: selected.index, card: card }, previewOnly).then(function (success) { if (success && !previewOnly) { dialog.close(); } });
    }
    function startGesture(event, section, card, node, grid, action) {
      if (event.button !== 0 || busy || !valid || window.matchMedia("(max-width: 700px)").matches) { return; }
      event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
      gesture = { section: section, card: card, node: node, grid: grid, action: action,
        x: event.clientX, y: event.clientY, index: section.widgets.indexOf(card), size: clone(card.layout), changed: false };
      node.classList.add("visual-dragging");
    }
    root.addEventListener("pointermove", function (event) {
      if (!gesture) { return; }
      var g = gesture;
      if (g.action === "resize") {
        var gap = parseFloat(window.getComputedStyle(g.grid).columnGap) || 12;
        var column = (g.grid.getBoundingClientRect().width + gap) / 12;
        g.size = { width: clamp(g.card.layout.width + Math.round((event.clientX - g.x) / column), 1, 12),
          height: clamp(g.card.layout.height + Math.round((event.clientY - g.y) / (layout.grid.row_height_px + gap)), 1, 30) };
        g.node.style.gridColumn = "span " + g.size.width; g.node.style.gridRow = "span " + g.size.height;
        g.changed = g.size.width !== g.card.layout.width || g.size.height !== g.card.layout.height;
      } else {
        var viewport = canvas.closest(".editor-visual"), bounds = viewport.getBoundingClientRect();
        if (event.clientY > bounds.bottom - 40) { viewport.scrollTop += 20; }
        else if (event.clientY < bounds.top + 40) { viewport.scrollTop -= 20; }
        g.grid.querySelectorAll(".visual-drop-before,.visual-drop-after").forEach(function (node) { node.classList.remove("visual-drop-before", "visual-drop-after"); });
        var target = document.elementFromPoint(event.clientX, event.clientY);
        target = target && target.closest(".visual-card");
        if (!target || target === g.node || target.parentNode !== g.grid) { g.index = g.section.widgets.indexOf(g.card); g.changed = false; return; }
        var box = target.getBoundingClientRect();
        var after = event.clientX >= box.left + box.width / 2;
        var ordered = g.section.widgets.filter(function (card) { return card.id !== g.card.id; });
        g.index = ordered.findIndex(function (card) { return card.id === target.dataset.widgetId; }) + (after ? 1 : 0);
        g.changed = g.index !== g.section.widgets.indexOf(g.card);
        target.classList.add(after ? "visual-drop-after" : "visual-drop-before");
      }
    });
    function endGesture(cancel) {
      if (!gesture) { return; }
      var g = gesture; gesture = null; render();
      if (!cancel && g.changed) { mutate({ action: g.action, section_id: g.section.id, widget_id: g.card.id, layout: g.size, index: g.index }); }
    }
    root.addEventListener("pointerup", function () { endGesture(false); });
    root.addEventListener("pointercancel", function () { endGesture(true); });
    root.addEventListener("keydown", function (event) { if (event.key === "Escape") { endGesture(true); } });
    root.addEventListener("click", function (event) {
      if (event.target.closest('[data-action="editor-tab"]')) { sync(); }
      var node = event.target.closest("[data-visual-action]"); if (!node) { return; }
      var action = node.dataset.visualAction;
      if (action === "undo" && !busy) { finishTyping(); if (history.length) { write(history.pop()); sync(); } }
      if (action === "add-card" && valid && !busy) {
        var nodes = Array.from(canvas.querySelectorAll(".visual-card"));
        var current = nodes.find(function (item) { return item.dataset.widgetId === currentCard; }) || nodes[nodes.length - 1];
        var section = current ? layout.sections.find(function (item) { return item.id === current.closest(".visual-section").dataset.sectionId; }) : layout.sections[0];
        var row = current ? Array.from(current.parentNode.children).filter(function (item) { return item.offsetTop === current.offsetTop; }) : [];
        var last = row[row.length - 1];
        var index = last ? section.widgets.findIndex(function (card) { return card.id === last.dataset.widgetId; }) + 1 : section.widgets.length;
        var used = row.reduce(function (sum, item) { return sum + section.widgets.find(function (card) { return card.id === item.dataset.widgetId; }).layout.width; }, 0);
        openOptions(section, null, "value");
        selected.index = index;
        field("width").value = used < 12 ? Math.min(catalog.presets.value[0], 12 - used) : catalog.presets.value[0];
      }
      if (action === "cancel") { dialog.close(); }
      if (action === "preset") { var preset = catalog.presets[field("type").value]; field("width").value = preset[0]; field("height").value = preset[1]; }
      if (action === "add-target") { addTarget({}); }
      if (action === "preview-options") { submit(true); }
      if (action === "delete-card" && selected.card && selected.text === textarea.value) {
        if (!window.confirm('Are you sure you want to delete "' + selected.card.title + '"?')) { return; }
        mutate({ action: "delete", section_id: selected.section.id, widget_id: selected.card.id }).then(function (success) { if (success) { dialog.close(); } });
      }

    });
    form.addEventListener("submit", function (event) { event.preventDefault(); submit(false); });
    field("type").addEventListener("change", function () {
      refreshGuide(true); thresholdGuide();
      if (!selected.card) {
        if (Object.values(TYPES).some(function (type) { return type[0] === field("title").value; })) { field("title").value = TYPES[field("type").value][0]; }
        var preset = catalog.presets[field("type").value];
        field("width").value = preset[0]; field("height").value = preset[1];
      }
    });
    field("measurement").addEventListener("change", function () { refreshGuide(false); thresholdGuide(); });
    field("policy").addEventListener("change", function () { thresholdGuide({}); });
    form.querySelector('[data-role="source-options"]').addEventListener("change", function () {
      if (!form.querySelector('[data-role="lunit-finding-options"]').children.length) { thresholdGuide({}); }
      else { thresholdGuide(); }
    });
    form.querySelector('[data-visual-action="reset-thresholds"]').addEventListener("click", function () { thresholdGuide({}); });
    form.querySelector('[data-role="comparison-options"]').addEventListener("change", function () {
      choices(field("default_group"), catalog.dimensions.filter(function (d) { return checked("comparison").indexOf(d.id) >= 0; }), field("default_group").value, "None");
    });
    textarea.addEventListener("input", function () {
      if (writing) { return; }
      ++sequence; valid = false; state("Checking layout…");
      clearTimeout(syncTimer); syncTimer = setTimeout(sync, 400);
      clearTimeout(typingTimer); typingTimer = setTimeout(finishTyping, 600);
    });
    sync();
  };
})();
