/**
 * <summary>
 * The configuration page: the whole editor for the deck, in one script.
 * </summary>
 * <remarks>
 * Plain JavaScript in one immediately invoked function, no framework and no
 * build step, because the daemon serves this straight from a folder of static
 * files on both platforms and there is nothing on either to build it with.
 *
 * The shape is one way round a loop. The page holds a JSON document of the
 * config, every edit goes through <see cref="change"/> so it can be undone, the
 * document is sent half a second after the last edit, and the daemon answers by
 * redrawing the panels and pushing them back down the event stream. Nothing
 * here draws a panel: the pictures on the grid are JPEGs and GIFs fetched from
 * the daemon, so what this page shows is what the deck itself shows.
 *
 * Two traps for the next person. The document is replaced outright by an undo
 * or by a reload, so an object held across either belongs to a document nothing
 * is looking at: every listener looks its key or action up again when it fires.
 * And a save of our own comes back as a config event just like an edit made in
 * a text editor, which is what <see cref="lastSaved"/> is there to tell apart.
 * </remarks>
 */
(function () {
  "use strict";

  /**
   * <summary>The deck's key grid, and the column the display strip sits in.</summary>
   * <remarks>
   * Fixed to this hardware. STRIP is a column index, not a count: it is one
   * past the last real key, and a cell in it is a strip panel, which can be
   * edited but never pressed.
   * </remarks>
   */
  const ROWS = 3, COLUMNS = 5, STRIP = 5;
  /**
   * <summary>Milliseconds after the last edit before the document is sent.</summary>
   * <remarks>Long enough that dragging a slider is one save rather than fifty,
   * short enough that the deck looks like it followed at once.</remarks>
   */
  const SAVE_DELAY = 500;
  /** <summary>How many steps Undo can go back before the oldest is dropped.</summary> */
  const UNDO_LIMIT = 30;

  /** <summary>querySelector, shortened. Searches within ``root`` when one is given.</summary> */
  const $ = (sel, root) => (root || document).querySelector(sel);
  /**
   * <summary>Build an element from a tag, an attribute object and children.</summary>
   * <param name="tag">Tag name.</param>
   * <param name="attrs">Attributes, with four special keys: class, text and
   * html set the obvious properties, and any key beginning "on" becomes a
   * listener for the event named after it.</param>
   * <param name="children">Nodes or strings to append, in order.</param>
   * <returns>The new element, not yet in the document.</returns>
   * <remarks>
   * false and null leave an attribute off entirely and true sets it empty,
   * which is how a boolean attribute is written. That is the trap: passing
   * ``draggable: false`` does not make an element undraggable, it only omits
   * the attribute. Anything that has to be actively turned off is set as a
   * property afterwards instead, as <see cref="buildGrid"/> does.
   * </remarks>
   */
  const el = (tag, attrs, children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const child of children || []) node.append(child);
    return node;
  };

  // ---- API ----------------------------------------------------------------

  /**
   * <summary>GET a JSON endpoint on the daemon.</summary>
   * <param name="path">Path on the local API, such as "/api/state".</param>
   * <returns>The decoded body.</returns>
   * <exception cref="Error">Any status outside 2xx, carrying the daemon's own
   * message when it sent one and the status text when it did not.</exception>
   */
  async function apiGet(path) {
    const r = await fetch(path);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }
  /**
   * <summary>Send a body to the daemon and decode the JSON answer.</summary>
   * <param name="method">HTTP method, such as "PUT" or "DELETE".</param>
   * <param name="path">Path on the local API.</param>
   * <param name="body">The body, already encoded. Not an object.</param>
   * <param name="type">Content type, JSON when not given.</param>
   * <returns>The decoded body, or an empty object when there was none.</returns>
   * <remarks>The body is taken already encoded because a picture upload sends
   * the chosen file's own bytes through here rather than JSON.</remarks>
   * <exception cref="Error">Any status outside 2xx.</exception>
   */
  async function apiSend(method, path, body, type) {
    const r = await fetch(path, {
      method, body,
      headers: { "Content-Type": type || "application/json" },
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }
  const apiPost = (path, obj) => apiSend("POST", path, JSON.stringify(obj || {}));

  // ---- State ---------------------------------------------------------------

  /**
   * <summary>Everything the page holds between events, kept in one place.</summary>
   * <remarks>
   * ``doc`` is ours to edit and is the only thing ever saved. ``state`` is the
   * daemon's and is read only here; it is null before the first answer and
   * after the deck goes away, so every use has to allow for that. The two are
   * deliberately separate: which page is showing is the daemon's business, what
   * is on the keys is the document's.
   * </remarks>
   */
  let doc = null;          // the config document being edited
  let state = null;        // the daemon's snapshot
  let animated = new Set(); // "row,column" of every panel playing an animation
  let images = [];         // file names in the images folder
  let selected = null;     // {row, column}
  let undoStack = [];
  let saveTimer = null;
  let tileVersion = Date.now();
  let tileScale = 1;       // how many times the deck's image size the page asks panels to be drawn
  let capturing = null;    // hotkey capture callback while active

  /**
   * <summary>Which page is showing, clamped to one that still exists.</summary>
   * <remarks>The daemon's index can outlive the page it pointed at: deleting
   * the last page leaves it pointing past the end until the next config event
   * arrives, and the clamp is what stops that reading undefined.</remarks>
   */
  const pageIndex = () => Math.min(state ? state.page_index : 0, doc.pages.length - 1);
  const currentPage = () => doc.pages[pageIndex()];
  const keyAt = (page, row, column) => page.keys.find(k => k.row === row && k.column === column) || null;
  /**
   * <summary>The key at a position, created empty and added to the page if absent.</summary>
   * <param name="page">The page to look in and, if need be, add to.</param>
   * <param name="row">Row, counting from zero.</param>
   * <param name="column">Column, counting from zero.</param>
   * <returns>The live key object inside the document.</returns>
   * <remarks>
   * Call it inside the mutator passed to <see cref="change"/> rather than
   * holding what it returns. An undo replaces the whole document, so a key kept
   * from before then belongs to a document nothing is showing. Pair it with
   * <see cref="dropEmptyKey"/> so clearing the last field on a key does not
   * leave an empty entry behind in the saved file.
   * </remarks>
   */
  function ensureKey(page, row, column) {
    let key = keyAt(page, row, column);
    if (!key) {
      key = { row, column, image: null, label: null, action: null };
      page.keys.push(key);
    }
    return key;
  }
  /**
   * <summary>Remove a key from its page once nothing is set on it any more.</summary>
   * <param name="page">The page holding the key.</param>
   * <param name="key">The key to drop if it is now blank.</param>
   * <remarks>Keeps the config file down to what was actually configured. Every
   * field a key can carry has to be listed here: one left out means a key that
   * looks empty in the editor is still written out to the file.</remarks>
   */
  function dropEmptyKey(page, key) {
    if (!key.image && !key.label && !key.action && !key.action_long && !key.action_double
        && !key.animation && !key.background) {
      page.keys = page.keys.filter(k => k !== key);
    }
  }

  /** <summary>The navy behind a key with no colour of its own, matching the daemon's own default.</summary> */
  const BUILT_IN_BACKGROUND = "#141c28";
  const deckBackground = () => (doc.deck && doc.deck.background) || BUILT_IN_BACKGROUND;

  /**
   * <summary>A colour well with a Set to default button beside it.</summary>
   * <param name="labelText">The field's label.</param>
   * <param name="get">Returns the stored colour, or null while it is default.</param>
   * <param name="set">Stores a colour, or null to go back to the default.</param>
   * <param name="fallback">Returns the colour null actually means, for the well
   * to show. A function rather than a value, because the deck wide colour it
   * usually falls back to can change while this field is on screen.</param>
   * <param name="help">Optional help line under the field.</param>
   * <returns>The field element.</returns>
   * <remarks>Storing null rather than a copy of the fallback colour is the
   * whole point: a key that never chose a colour writes nothing to the file and
   * goes on following the deck wide setting when that is changed later.</remarks>
   */
  function colourField(labelText, get, set, fallback, help) {
    const current = get();
    const input = el("input", { type: "color", value: current || fallback(), title: "Background colour" });
    const state = el("span", { class: "muted", text: current ? "custom" : "default" });
    const reset = el("button", { type: "button", class: "ghost small", text: "Set to default", disabled: !current });
    input.addEventListener("input", () => {
      set(input.value);
      state.textContent = "custom";
      reset.disabled = false;
    });
    reset.addEventListener("click", () => {
      set(null);
      input.value = fallback();
      state.textContent = "default";
      reset.disabled = true;
    });
    const field = el("div", { class: "field" }, [
      el("label", { text: labelText }),
      el("div", { class: "inline" }, [input, state, reset]),
    ]);
    if (help) field.append(el("div", { class: "help", text: help }));
    return field;
  }

  // ---- The running border ----------------------------------------------------

  const MARK_DEFAULTS = { style: "auto", colour: null, flash_ms: 500 };
  const MARK_STYLES = [["auto", "Automatic"], ["steady", "Always on"],
                       ["blink", "Blinking"], ["none", "No border"]];
  // The green a running key has always used.
  const MARK_DEFAULT_COLOUR = "#4caf7d";

  /**
   * <summary>Style, colour and blink speed of the selected key's running border.</summary>
   * <remarks>
   * Only a hold, a boost or a repeat ever shows it. Kept as null while every
   * part is default, so a key that never touched it writes nothing to the file.
   * </remarks>
   */
  function markFields() {
    const box = el("div");
    const read = () => Object.assign({}, MARK_DEFAULTS,
      (keyAt(currentPage(), selected.row, selected.column) || {}).mark || {});
    const write = patch => change(d => {
      const k = ensureKey(currentPage(), selected.row, selected.column);
      const next = Object.assign({}, MARK_DEFAULTS, k.mark || {}, patch);
      const plain = next.style === MARK_DEFAULTS.style && next.colour === null
        && next.flash_ms === MARK_DEFAULTS.flash_ms;
      k.mark = plain ? null : next;
      dropEmptyKey(currentPage(), k);
    }, { silent: true });

    const style = el("select");
    for (const [value, text] of MARK_STYLES) style.append(el("option", { value, text }));
    style.value = read().style;
    style.addEventListener("change", () => { write({ style: style.value }); renderEditor(); });
    box.append(el("div", { class: "field" }, [
      el("label", { text: "Running border" }),
      style,
      el("div", { class: "help", text: "Shown while a hold, boost or repeat on this key is running. Automatic blinks a repeat, which only fires now and then, and keeps a steady border for a hold or a boost, which hold a key down." }),
    ]));

    box.append(colourField("Border colour", () => read().colour,
      value => write({ colour: value }), () => MARK_DEFAULT_COLOUR,
      "The colour of the border while the key is running."));

    if (read().style === "auto" || read().style === "blink") {
      const start = read().flash_ms;
      const slider = el("input", { type: "range", min: 100, max: 5000, step: 50, value: start });
      const out = el("output", { text: start + " ms" });
      slider.addEventListener("input", () => {
        const ms = parseInt(slider.value, 10) || MARK_DEFAULTS.flash_ms;
        out.textContent = ms + " ms";
        write({ flash_ms: ms });
      });
      box.append(el("div", { class: "field" }, [
        el("label", { text: "Blink speed" }),
        el("div", { class: "inline slider-row" }, [slider, out]),
        el("div", { class: "help", text: "How long each half of the blink lasts. Only used when the border blinks." }),
      ]));
    }
    return box;
  }

  const ANIMATION_KINDS = [["", "None"], ["pulse", "Pulse"], ["spinner", "Spinner"], ["wave", "Wave"],
    ["rainbow", "Rainbow"], ["scroll", "Scrolling label"]];

  /**
   * <summary>Kind, colour and speed for one built in animation.</summary>
   * <param name="get">Returns the live animation object in the document, or null.</param>
   * <param name="set">Stores a new animation object, or null for none.</param>
   * <param name="labelText">The field's label.</param>
   * <param name="help">Optional help line under the field.</param>
   * <returns>The field element.</returns>
   * <remarks>
   * The colour and speed controls are rebuilt whenever the kind changes, and
   * they edit the object ``get`` hands back rather than a copy of it, so an
   * undo takes their edits with it. Rainbow ignores the colour, and its control
   * is disabled rather than hidden so the field does not change height.
   * </remarks>
   */
  function animationField(get, set, labelText, help) {
    const current = get();
    const wrap = el("div", { class: "field" });
    const kind = el("select");
    for (const [value, label] of ANIMATION_KINDS) kind.append(el("option", { value, text: label }));
    kind.value = current ? current.kind : "";
    const controls = el("div", { class: "anim-controls" });
    const build = () => {
      controls.innerHTML = "";
      const a = get();
      if (!a) return;
      const colour = el("input", { type: "color", value: a.colour || "#5c9bde", title: "Colour" });
      colour.addEventListener("input", () => change(d => { get().colour = colour.value; }, { silent: true }));
      const speed = el("input", { type: "range", min: 25, max: 400, step: 5, value: Math.round((a.speed || 1) * 100) });
      const out = el("output", { text: "×" + (a.speed || 1) });
      speed.addEventListener("input", () => {
        const value = parseInt(speed.value, 10) / 100;
        out.textContent = "×" + value;
        change(d => { get().speed = value; }, { silent: true });
      });
      controls.append(
        el("div", { class: "inline" }, [el("span", { class: "muted", text: "Colour" }), colour,
          el("span", { class: "muted", text: "Speed" }), speed, out]),
      );
      if (a.kind === "rainbow") colour.disabled = true;
    };
    kind.addEventListener("change", () => {
      const previous = get();
      set(kind.value ? { kind: kind.value, colour: previous ? previous.colour : "#5c9bde", speed: previous ? previous.speed : 1, fps: 20 } : null);
      build();
    });
    wrap.append(el("label", { text: labelText }), kind, controls);
    if (help) wrap.append(el("div", { class: "help", text: help }));
    build();
    return wrap;
  }

  // ---- Save and undo -------------------------------------------------------

  /** <summary>Put a word in the save area of the header, styled ok or bad.</summary> */
  function setSave(text, cls) {
    const node = $("#saveState");
    node.textContent = text;
    node.className = "save " + (cls || "");
  }

  /**
   * <summary>The only way to edit the document, so every edit can be undone and saved.</summary>
   * <param name="mutator">Called with the document and edits it in place.</param>
   * <param name="options">``{silent: true}`` skips the redraw.</param>
   * <remarks>
   * The snapshot for Undo is taken before the mutator runs and a save is
   * scheduled after it. Nothing may edit the document around this: such an edit
   * cannot be undone and is never saved until some later edit happens to carry
   * it along, which is the sort of bug that only shows up days afterwards.
   *
   * Pass silent from an input the user is still in. A redraw rebuilds the
   * editor pane, taking the focus and the caret with it and abandoning a hotkey
   * capture halfway through. The deck still follows, because the save goes
   * ahead either way.
   * </remarks>
   */
  function change(mutator, options) {
    undoStack.push(JSON.stringify(doc));
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    $("#undoBtn").disabled = false;
    mutator(doc);
    if (!(options && options.silent)) renderAll();
    scheduleSave();
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    setSave("saving", "");
    saveTimer = setTimeout(save, SAVE_DELAY);
  }

  let lastSaved = null;   // what we last sent, so our own reload is not mistaken for an outside edit

  /**
   * <summary>Send the document to the daemon and say in the header how it went.</summary>
   * <remarks>
   * What was sent is remembered in <see cref="lastSaved"/>, because the daemon
   * answers a successful save with a config event and the page has to tell that
   * apart from someone editing the config file in a text editor. Without it
   * every save would reload the document and throw the editor away mid
   * keystroke.
   * </remarks>
   */
  async function save() {
    try {
      const body = JSON.stringify(doc);
      await apiSend("PUT", "/api/document", body);
      lastSaved = body;
      setSave("saved", "ok");
      // The save may have changed which pictures are in use; keep an open
      // settings window honest about it.
      if (settingsRoot) { await loadImages(); renderSettings(); }
    } catch (err) {
      setSave("not saved: " + err.message, "bad");
    }
  }

  /**
   * <summary>Step one edit back and save the result.</summary>
   * <remarks>The whole document is replaced, so anything still holding a key or
   * an action object from before now points at an orphan. That is why the
   * editors look their objects up again on every change instead of keeping
   * them.</remarks>
   */
  function undo() {
    const previous = undoStack.pop();
    if (!previous) return;
    doc = JSON.parse(previous);
    $("#undoBtn").disabled = undoStack.length === 0;
    renderAll();
    scheduleSave();
  }

  // ---- Dialog ----------------------------------------------------------------

  /**
   * <summary>A modal in the page's own style, in place of prompt and confirm.</summary>
   * <param name="opts">title, message, input, confirmText, cancelText, danger,
   * validate, and an optional third button in extra.</param>
   * <returns>A promise of the typed text, or true when there is no input, or
   * null when it was cancelled. The string "extra" when the third button was
   * the one pressed, which is the one answer that is never a name.</returns>
   * <remarks>
   * The browser's own boxes are not used: they are drawn from the desktop theme
   * and come out light on a dark page. Enter confirms, Escape cancels, and a
   * click on the backdrop cancels. The key listener goes on the document in the
   * capture phase and is taken off again on every path out, so a dialog that
   * has resolved never leaves one behind swallowing keys.
   * </remarks>
   */
  function dialog(opts) {
    return new Promise(resolve => {
      const backdrop = el("div", { class: "modal-backdrop" });
      const box = el("div", { class: "modal", role: "dialog", "aria-modal": "true" });
      box.append(el("h2", { text: opts.title }));
      if (opts.message) box.append(el("p", { class: "muted", text: opts.message }));
      let input = null;
      if (opts.input) {
        input = el("input", { type: "text", value: opts.input.value || "", placeholder: opts.input.placeholder || "", maxlength: 40 });
        box.append(el("div", { class: "field" }, [input]));
      }
      const cancel = el("button", { type: "button", class: "ghost", text: opts.cancelText || "Cancel" });
      const ok = el("button", { type: "button", class: opts.danger ? "danger-solid" : "", text: opts.confirmText || "OK" });
      const finish = value => { backdrop.remove(); document.removeEventListener("keydown", onKey, true); resolve(value); };
      const confirm = () => {
        if (input) {
          const value = input.value.trim();
          if (!value) { input.focus(); return; }
          if (opts.validate) { const problem = opts.validate(value); if (problem) { showProblem(problem); return; } }
          finish(value);
        } else finish(true);
      };
      let problem = null;
      const showProblem = text => {
        if (!problem) { problem = el("div", { class: "help bad" }); box.insertBefore(problem, box.lastElementChild); }
        problem.textContent = text;
      };
      cancel.addEventListener("click", () => finish(null));
      ok.addEventListener("click", confirm);
      // An optional third button, such as Delete on the rename dialog. It
      // resolves with the string "extra" so the caller can tell it apart.
      const extra = opts.extra ? el("button", { type: "button", class: opts.extra.danger ? "ghost danger" : "ghost", text: opts.extra.text }) : null;
      if (extra) extra.addEventListener("click", () => finish("extra"));
      const onKey = e => {
        if (e.key === "Escape") { e.preventDefault(); finish(null); }
        else if (e.key === "Enter") { e.preventDefault(); confirm(); }
      };
      document.addEventListener("keydown", onKey, true);
      backdrop.addEventListener("click", e => { if (e.target === backdrop) finish(null); });
      box.append(el("div", { class: "row modal-buttons" }, extra ? [extra, cancel, ok] : [cancel, ok]));
      backdrop.append(box);
      document.body.append(backdrop);
      (input || ok).focus();
      if (input) input.select();
    });
  }

  /**
   * <summary>Why a page name cannot be used, or null when it can.</summary>
   * <param name="name">The proposed name.</param>
   * <param name="ignoreIndex">Index of the page being renamed, so its own
   * current name does not count as a clash. Pass -1 when adding.</param>
   * <returns>A sentence to show the user, or null.</returns>
   * <remarks>"next" and "previous" are refused because a page switching action
   * stores its target by name and uses those two words for a relative move, so
   * a page called either could never be reached.</remarks>
   */
  function pageNameProblem(name, ignoreIndex) {
    if (doc.pages.some((p, i) => i !== ignoreIndex && p.name === name)) return "There is already a page called " + name + ".";
    if (name === "next" || name === "previous") return "That name is reserved for page switching.";
    return null;
  }

  // ---- Rendering: header, tabs, grid --------------------------------------

  function setStatus(text, cls) {
    $("#statusText").textContent = text;
    $("#status").className = "status " + (cls || "");
  }

  /**
   * <summary>Put the daemon's brightness, sleep state and device name in the header.</summary>
   * <remarks>Does nothing until the first state has arrived, so it is safe to
   * call from a redraw that runs before the deck has answered.</remarks>
   */
  function renderHeader() {
    if (!state) return;
    $("#brightness").value = state.brightness;
    $("#brightnessValue").value = state.brightness;
    $("#sleepBtn").textContent = state.asleep ? "Wake" : "Sleep";
    setStatus(state.device ? state.device.name + (state.asleep ? " (asleep)" : "") : "deck", "ok");
  }

  let draggingTab = null;

  /**
   * <summary>Rebuild the row of page tabs, with the showing page marked.</summary>
   * <remarks>
   * A click switches page through the daemon rather than locally, so the deck
   * and this page can never disagree about which one is showing. Double click
   * renames and a drag reorders; reordering is an edit to the document, so Undo
   * puts the order back.
   * </remarks>
   */
  function renderTabs() {
    const tabs = $("#pageTabs");
    tabs.innerHTML = "";
    doc.pages.forEach((page, index) => {
      const tab = el("button", {
        class: "tab" + (index === pageIndex() ? " active" : ""),
        type: "button", role: "tab", text: page.name, draggable: "true",
        title: "Click to show, double click to rename, drag to reorder",
        onclick: () => apiPost("/api/page", { page: page.name }).catch(showError),
        ondblclick: () => renamePage(index),
      });
      tab.addEventListener("dragstart", e => { draggingTab = index; e.dataTransfer.effectAllowed = "move"; tab.classList.add("dragging"); });
      tab.addEventListener("dragend", () => { draggingTab = null; tab.classList.remove("dragging"); });
      tab.addEventListener("dragover", e => { if (draggingTab !== null) { e.preventDefault(); tab.classList.add("dragover"); } });
      tab.addEventListener("dragleave", () => tab.classList.remove("dragover"));
      tab.addEventListener("drop", e => {
        e.preventDefault();
        tab.classList.remove("dragover");
        if (draggingTab === null || draggingTab === index) return;
        const from = draggingTab;
        draggingTab = null;
        change(d => {
          const [moved] = d.pages.splice(from, 1);
          d.pages.splice(index, 0, moved);
        });
      });
      tabs.append(tab);
    });
    tabs.append(el("button", {
      class: "tab add", type: "button", text: "+ page",
      onclick: addPage,
    }));
  }

  /** <summary>Ask for a name and add an empty page at the end.</summary> */
  async function addPage() {
    const name = await dialog({
      title: "New page",
      input: { value: "Page " + (doc.pages.length + 1), placeholder: "Page name" },
      confirmText: "Add page",
      validate: value => pageNameProblem(value, -1),
    });
    if (!name) return;
    change(d => d.pages.push({ name, keys: [] }));
  }

  /**
   * <summary>Rename a page, or delete it from the same dialog.</summary>
   * <param name="index">Which page.</param>
   * <remarks>Every page switching action in the whole document is retargeted in
   * the same edit, both second actions as well as the plain one, so a key that
   * pointed at the old name follows it instead of pointing at a page that no
   * longer exists.</remarks>
   */
  async function renamePage(index) {
    const old = doc.pages[index].name;
    const name = await dialog({
      title: "Rename page",
      message: "Keys that switch to this page follow the new name.",
      input: { value: old, placeholder: "Page name" },
      confirmText: "Rename",
      validate: value => pageNameProblem(value, index),
      extra: doc.pages.length > 1 ? { text: "Delete page", danger: true } : null,
    });
    if (name === "extra") { deletePage(index); return; }
    if (!name || name === old) return;
    change(d => {
      d.pages[index].name = name;
      for (const p of d.pages) for (const k of p.keys) {
        retarget(k.action, old, name);
        retarget(k.action_long, old, name);
        retarget(k.action_double, old, name);
      }
    });
  }

  /**
   * <summary>Confirm, then remove a page and the keys on it.</summary>
   * <param name="index">Which page.</param>
   * <remarks>The keys go with it and only Undo brings them back, which is why
   * the confirmation says how many will be lost.</remarks>
   */
  async function deletePage(index) {
    const page = doc.pages[index];
    const yes = await dialog({
      title: "Delete page " + page.name + "?",
      message: page.keys.length ? "Its " + page.keys.length + " key" + (page.keys.length === 1 ? "" : "s") + " will be lost. Undo brings it back." : "It has no keys.",
      confirmText: "Delete",
      danger: true,
    });
    if (!yes) return;
    change(d => { d.pages.splice(index, 1); });
  }

  /** <summary>Every grid cell by "row,column", so a redraw can reach one without a query.</summary> */
  let cells = {};
  let draggingKey = null;

  /**
   * <summary>Build the whole grid of cells once, listeners and all.</summary>
   * <remarks>
   * Called once at startup and not on every redraw: the pictures are swapped by
   * <see cref="refreshTiles"/> and the outline by <see cref="applySelection"/>,
   * so nothing in here needs to run again, and rebuilding it would drop a drag
   * that was in progress.
   *
   * Each row is five keys, a wider spacer cell, then the strip panel, which is
   * why the inner loop runs to STRIP inclusive. Strip cells are not draggable:
   * they are three fixed panels, not keys that can be moved about.
   * </remarks>
   */
  function buildGrid() {
    const grid = $("#grid");
    grid.innerHTML = "";
    cells = {};
    for (let row = 0; row < ROWS; row++) {
      for (let column = 0; column <= STRIP; column++) {
        if (column === STRIP) grid.append(el("div", { class: "gap" }));
        const isStrip = column === STRIP;
        // A picture is draggable by default and its own drag carries the image
        // rather than the key, which is what stopped a key being dropped on an
        // empty space. Set as a property: el() drops attributes that are false.
        const picture = el("img", { alt: "" });
        picture.draggable = false;
        const cell = el("button", {
          class: "cell" + (isStrip ? " strip" : ""),
          type: "button",
          title: isStrip ? "Strip panel " + (row + 1) : "Row " + (row + 1) + ", column " + (column + 1) + ". Drag to move or swap.",
          onclick: () => select(row, column),
        }, [picture]);
        if (isStrip) {
          cell.append(el("div", { class: "crop" }));
        } else {
          cell.draggable = true;
          cell.addEventListener("dragstart", e => {
            if (!keyAt(currentPage(), row, column)) { e.preventDefault(); return; }
            draggingKey = { row, column };
            e.dataTransfer.effectAllowed = "move";
            // Some engines, WebKitGTK among them, refuse to start a drag at all
            // unless the transfer carries something. The text is never read.
            try { e.dataTransfer.setData("text/plain", row + "," + column); } catch (_) { /* older engines */ }
            cell.classList.add("dragging");
          });
          cell.addEventListener("dragend", () => {
            draggingKey = null;
            // Clear every leftover mark: a drag let go outside the grid never
            // sends dragleave, and the stale outline reads as a second selection.
            for (const other of Object.values(cells)) other.classList.remove("dragover", "dragging");
          });
          cell.addEventListener("dragover", e => { if (draggingKey) { e.preventDefault(); cell.classList.add("dragover"); } });
          cell.addEventListener("dragleave", () => cell.classList.remove("dragover"));
          cell.addEventListener("drop", e => {
            e.preventDefault();
            cell.classList.remove("dragover");
            if (!draggingKey || (draggingKey.row === row && draggingKey.column === column)) return;
            const from = draggingKey;
            draggingKey = null;
            change(d => {
              const page = d.pages[pageIndex()];
              const a = keyAt(page, from.row, from.column);
              const b = keyAt(page, row, column);
              if (a) { a.row = row; a.column = column; }
              if (b) { b.row = from.row; b.column = from.column; }
            });
            select(row, column);
          });
        }
        grid.append(cell);
        cells[row + "," + column] = cell;
      }
    }
    // A rebuilt grid carries no marks, so put the one selection back: the grid
    // and the editor must never disagree about what is selected.
    applySelection();
  }

  /**
   * <summary>Shade the edges of each strip panel that the hardware cuts off.</summary>
   * <remarks>
   * The daemon sends a square picture for a strip panel, but the panel behind
   * the plastic shows less than all of it, so the shaded border is the part
   * that will never be seen. The visible size is a measurement of this deck
   * taken with calibrate-strip, which is why it comes from the config and not
   * from the device. Does nothing until one is known.
   * </remarks>
   */
  function updateCropOverlay() {
    const visible = (doc && doc.strip_visible) || (state && state.strip_visible);
    const size = state && state.device ? state.device.image_size : 95;
    if (!visible) return;
    const x = Math.max(0, (size - visible.width) / 2 / size * 100);
    const y = Math.max(0, (size - visible.height) / 2 / size * 100);
    for (const [pos, cell] of Object.entries(cells)) {
      const crop = cell.querySelector(".crop");
      if (crop) crop.style.inset = y + "% " + x + "% " + y + "% " + x + "%";
    }
  }

  /**
   * <summary>Remember which panels are animating, from the state or a tiles event.</summary>
   * <param name="positions">Array of [row, column] pairs, or undefined to leave as is.</param>
   */
  function noteAnimated(positions) {
    if (!positions) return;
    animated = new Set(positions.map(([row, column]) => row + "," + column));
  }

  /**
   * <summary>Reload the panel pictures from the daemon.</summary>
   * <param name="columns">Only these columns, or all when undefined.</param>
   * <remarks>
   * A still panel is one JPEG. An animated one is asked for as a GIF of the
   * whole loop, so the page moves as the deck does instead of showing the
   * first frame forever. The daemon draws both for the page only.
   * </remarks>
   */
  function refreshTiles(columns) {
    tileVersion = Date.now();
    for (const [pos, cell] of Object.entries(cells)) {
      const [row, column] = pos.split(",").map(Number);
      if (columns && !columns.includes(column)) continue;
      const kind = animated.has(pos) ? ".gif" : ".jpg";
      cell.querySelector("img").src =
        "/api/tiles/" + row + "/" + column + kind + "?v=" + tileVersion + "&scale=" + tileScale;
    }
  }

  // ---- Deck sizing ---------------------------------------------------------

  /**
   * <summary>The smallest a panel is ever drawn, whatever the window does.</summary>
   * <remarks>Below this the deck stops being recognisable, so a very short
   * window gets a scrollbar rather than an unreadable grid.</remarks>
   */
  const MIN_TILE = 44;

  /**
   * <summary>Size the deck preview to the space the window actually has.</summary>
   * <remarks>
   * The panels are square, so one number drives the whole grid: the tile edge.
   * It is whichever is smaller of what the pane can fit across and what is left
   * below the tabs, so the deck grows into a big window and still fits whole in
   * a short one. Everything is measured off the live layout rather than assumed,
   * so gaps and padding can change in the stylesheet without this going stale.
   *
   * The cap matters. Panels are drawn by the daemon at the deck's own image
   * size, so a tile shown much larger than that would be a blown up bitmap. The
   * daemon will draw up to max_preview_scale times that size on request, and
   * this asks for the smallest scale that covers the tile at the screen's pixel
   * density, then stops growing the deck at the largest size it can serve
   * sharply.
   * </remarks>
   */
  function fitDeck() {
    const pane = document.querySelector(".deck-pane");
    const deck = document.querySelector(".deck");
    const grid = $("#grid");
    const spacerCell = document.querySelector(".gap");
    if (!pane || !deck || !grid || !spacerCell) return;

    const deckStyle = getComputedStyle(deck);
    const gridStyle = getComputedStyle(grid);
    const px = (value) => parseFloat(value) || 0;
    const device = (state && state.device) || {};
    const native = device.image_size || 95;
    const maxScale = device.max_preview_scale || 1;
    // With the key pitch measured, the gaps between panels follow the real
    // deck as a share of the tile, so a wallpaper reads through them here as
    // it does on the hardware. Otherwise the stylesheet's gaps apply.
    const pitch = (state && state.key_pitch) || null;
    const ratioX = pitch ? Math.max(0, pitch[0] / native - 1) : null;
    const ratioY = pitch ? Math.max(0, pitch[1] / native - 1) : null;
    if (!pitch) { grid.style.columnGap = ""; grid.style.rowGap = ""; }
    const columnGap = px(gridStyle.columnGap);
    const rowGap = px(gridStyle.rowGap) || columnGap;
    const stripGap = spacerCell.getBoundingClientRect().width;
    const frameX = px(deckStyle.paddingLeft) + px(deckStyle.paddingRight)
      + px(deckStyle.borderLeftWidth) + px(deckStyle.borderRightWidth);
    const frameY = px(deckStyle.paddingTop) + px(deckStyle.paddingBottom)
      + px(deckStyle.borderTopWidth) + px(deckStyle.borderBottomWidth);

    // Seven grid columns: five keys, the wider gap before the strip, the strip.
    const GRID_COLUMNS = COLUMNS + 2;
    const byWidth = pitch
      ? (pane.clientWidth - frameX - stripGap) / ((COLUMNS + 1) + ratioX * (GRID_COLUMNS - 1))
      : (pane.clientWidth - frameX - stripGap - columnGap * (GRID_COLUMNS - 1)) / (COLUMNS + 1);

    const hint = document.querySelector(".hint");
    const hintSpace = hint ? hint.offsetHeight + px(getComputedStyle(hint).marginTop) : 0;
    const main = document.querySelector("main");
    const below = hintSpace + (main ? px(getComputedStyle(main).paddingBottom) : 0);
    const top = deck.getBoundingClientRect().top + document.documentElement.scrollTop;
    const byHeight = pitch
      ? (document.documentElement.clientHeight - top - frameY - below) / (ROWS + ratioY * (ROWS - 1))
      : (document.documentElement.clientHeight - top - frameY - rowGap * (ROWS - 1) - below) / ROWS;

    const tile = Math.max(MIN_TILE, Math.floor(Math.min(byWidth, byHeight, native * maxScale)));
    grid.style.setProperty("--tile", tile + "px");
    if (pitch) {
      grid.style.columnGap = Math.round(ratioX * tile) + "px";
      grid.style.rowGap = Math.round(ratioY * tile) + "px";
    }

    const density = window.devicePixelRatio || 1;
    const wanted = Math.min(maxScale, Math.max(1, Math.ceil(tile * density / native)));
    if (wanted !== tileScale) {
      tileScale = wanted;
      refreshTiles();
    }
  }

  function flash(row, column, pressed) {
    const cell = cells[row + "," + column];
    if (cell) cell.classList.toggle("pressed", pressed);
  }

  /** <summary>Mark the one selected cell, and no other.</summary> */
  function applySelection() {
    const want = selected ? selected.row + "," + selected.column : null;
    for (const [pos, cell] of Object.entries(cells)) {
      cell.classList.toggle("selected", pos === want);
    }
  }

  /**
   * <summary>Select a key for the editor, or deselect it by clicking it again.</summary>
   * <param name="row">Row of the cell that was clicked.</param>
   * <param name="column">Column of the cell that was clicked. STRIP means a
   * display strip panel rather than a key.</param>
   * <remarks>With nothing selected the editor shows the page's own settings,
   * which is where the wallpaper lives.</remarks>
   */
  function select(row, column) {
    const same = selected && selected.row === row && selected.column === column;
    selected = same ? null : { row, column };
    applySelection();
    renderEditor();
  }

  /** <summary>Back to nothing selected, which is where the page settings live.</summary> */
  function deselect() {
    if (!selected) return;
    selected = null;
    applySelection();
    renderEditor();
  }

  // ---- Rendering: editor ---------------------------------------------------

  /**
   * <summary>Redraw everything the document feeds, in an order that holds.</summary>
   * <remarks>The deck is sized last because the tabs above it can wrap onto
   * another line, which moves the deck down and changes how much room is left
   * for it.</remarks>
   */
  function renderAll() {
    renderHeader();
    renderTabs();
    renderEditor();
    renderSettings();
    updateCropOverlay();
    fitDeck();  // the tabs can wrap to another line, which moves the deck down
  }

  /**
   * <summary>Fill the editor pane for whatever is selected.</summary>
   * <remarks>
   * Three different panes come out of here: the page's own settings when
   * nothing is selected, the strip panel editor for a cell in the strip column,
   * and the key editor for anything else.
   *
   * Every listener built here looks its key up again through
   * <see cref="ensureKey"/> when it fires, rather than closing over the key
   * read at build time. An undo or a reload replaces the document underneath,
   * and a captured reference would then write into a document nothing is
   * showing, which looks exactly like an edit being ignored.
   * </remarks>
   */
  function renderEditor() {
    const editor = $("#editor");
    editor.innerHTML = "";
    if (!selected) {
      renderPageEditor(editor);
      return;
    }
    // The way back to the page's own settings, where the wallpaper lives.
    // Clicking the selected key again does it too, and so does Escape, but
    // neither is visible, and the wallpaper was hard to reach without this.
    const back = el("button", { type: "button", class: "ghost small", text: "Page settings" });
    back.addEventListener("click", deselect);
    editor.append(el("div", { class: "editor-back" }, [back]));
    if (selected.column === STRIP) return renderStripEditor(editor);
    const page = currentPage();
    const key = keyAt(page, selected.row, selected.column) || { row: selected.row, column: selected.column, image: null, label: null, action: null };
    const tpl = $("#tpl-editor-key").content.cloneNode(true);
    tpl.querySelector(".pos").textContent = "row " + (selected.row + 1) + ", column " + (selected.column + 1) + " on " + page.name;

    const label = tpl.querySelector("#keyLabel");
    label.value = key.label || "";
    label.addEventListener("input", () => change(d => {
      const k = ensureKey(currentPage(), selected.row, selected.column);
      k.label = label.value.trim() || null;
      dropEmptyKey(currentPage(), k);
    }, { silent: true }));

    tpl.querySelector("#keyPicture").append(picker(key.image, value => change(d => {
      const k = ensureKey(currentPage(), selected.row, selected.column);
      k.image = value;
      dropEmptyKey(currentPage(), k);
    })));

    tpl.querySelector("#keyAnimation").append(animationField(
      () => (keyAt(currentPage(), selected.row, selected.column) || {}).animation || null,
      value => change(d => {
        const k = ensureKey(currentPage(), selected.row, selected.column);
        k.animation = value;
        dropEmptyKey(currentPage(), k);
      }),
      "Animation", "Drawn by the software in place of the picture. The label stays on top."));

    tpl.querySelector("#keyBackground").append(colourField("Background",
      () => (keyAt(currentPage(), selected.row, selected.column) || {}).background || null,
      value => change(d => {
        const k = ensureKey(currentPage(), selected.row, selected.column);
        k.background = value;
        dropEmptyKey(currentPage(), k);
      }, { silent: true }),
      deckBackground,
      "The colour behind the label, around a picture, and under an animation. Default follows the deck wide colour in Settings."));

    tpl.querySelector("#keyMark").append(markFields());

    // The plain action and the two second actions are edited the same way and
    // differ only in which field of the key they write to.
    const type = tpl.querySelector("#actionType");
    const actionSlot = (field, select, fields) => {
      if (!select.options.length) for (const option of type.options) {
        select.append(el("option", { value: option.value, text: option.textContent }));
      }
      select.value = key[field] ? key[field].type : "";
      select.addEventListener("change", () => change(d => {
        const k = ensureKey(currentPage(), selected.row, selected.column);
        k[field] = select.value ? defaultAction(select.value) : null;
        dropEmptyKey(currentPage(), k);
      }));
      if (key[field]) fields.append(actionFields(key[field], () => {
        const k = ensureKey(currentPage(), selected.row, selected.column);
        return k[field];
      }, false));
    };
    actionSlot("action", type, tpl.querySelector("#actionFields"));
    actionSlot("action_long", tpl.querySelector("#actionLongType"), tpl.querySelector("#actionLongFields"));
    actionSlot("action_double", tpl.querySelector("#actionDoubleType"), tpl.querySelector("#actionDoubleFields"));
    if (key.action_long || key.action_double) tpl.querySelector("details.second").open = true;

    // "Press now" runs one of the three actions outright. The daemon cannot
    // time a gesture it never felt, so the page says which one it means.
    const tryPress = (button, gesture, show) => {
      button.hidden = !show;
      button.addEventListener("click", () => apiPost("/api/press",
        { row: selected.row, column: selected.column, gesture }).catch(showError));
    };
    tryPress(tpl.querySelector("#pressBtn"), "press", true);
    tryPress(tpl.querySelector("#pressLongBtn"), "long", !!key.action_long);
    tryPress(tpl.querySelector("#pressDoubleBtn"), "double", !!key.action_double);
    tpl.querySelector("#clearBtn").addEventListener("click", () => change(d => {
      const p = currentPage();
      p.keys = p.keys.filter(k => !(k.row === selected.row && k.column === selected.column));
    }));
    editor.append(tpl);
  }

  /**
   * <summary>The page's own settings, shown while no key is selected.</summary>
   * <param name="editor">The editor pane to fill.</param>
   * <remarks>
   * Today that is the wallpaper: one picture cut across all fifteen keys and
   * drawn under every key that has no picture or animation of its own. The
   * daemon does the cutting at the deck's key pitch, so the slices line up
   * through the gaps once the pitch has been measured with calibrate-grid.
   * </remarks>
   */
  function renderPageEditor(editor) {
    const page = currentPage();
    editor.append(el("h2", { text: "Page " + page.name }));
    editor.append(el("p", { class: "muted", text: "Click a key to edit it. What is below applies to the whole page." }));
    const field = el("div", { class: "field" });
    field.append(el("label", { text: "Wallpaper" }));
    field.append(picker(page.wallpaper || null, value => change(d => { d.pages[pageIndex()].wallpaper = value; }),
                        { themes: false }));
    const pitch = state && state.key_pitch;
    field.append(el("div", { class: "help", text:
      "One picture cut across all fifteen keys. Keys with their own picture or animation keep it, and labels sit in a band on top. "
      + (pitch
        ? "The key pitch is set, so the picture runs straight through the gaps between keys."
        : "Measure the key pitch with  deckplate calibrate-grid  and put it in the config so the picture runs straight through the gaps; until then the keys are treated as touching.") }));
    editor.append(field);
  }

  /**
   * <summary>The editor for one of the three display strip panels.</summary>
   * <param name="editor">The editor pane to fill.</param>
   * <remarks>
   * A panel is stored as a plain string while it is only a kind, and as an
   * object once it carries a colour, a picture or an animation. This moves
   * between the two forms as fields are set and cleared, and goes back to the
   * plain string when nothing but the kind is left, so the config file stays
   * down to what was actually configured.
   * </remarks>
   */
  function renderStripEditor(editor) {
    const tpl = $("#tpl-editor-strip").content.cloneNode(true);
    const row = selected.row;
    tpl.querySelector(".pos").textContent = ["top", "middle", "bottom"][row];
    const entry = doc.strip[row];
    const kind = typeof entry === "string" ? entry : (entry.animation ? "animation" : (entry.image !== undefined ? "image" : entry.kind));
    const stripBg = () => (typeof doc.strip[row] === "object" && doc.strip[row].background) || null;
    const setStripBg = value => change(d => {
      let e = d.strip[row];
      if (typeof e === "string") e = d.strip[row] = { kind: e };
      if (value) e.background = value; else delete e.background;
      if (e.kind && !e.background) d.strip[row] = e.kind;  // back to the plain form
    }, { silent: true });
    const kindSelect = tpl.querySelector("#stripKind");
    kindSelect.value = kind;
    kindSelect.addEventListener("change", () => change(d => {
      const background = stripBg();
      let next;
      if (kindSelect.value === "image") next = { image: null };
      else if (kindSelect.value === "animation") next = { animation: { kind: "pulse", colour: "#5c9bde", speed: 1, fps: 20 } };
      else next = background ? { kind: kindSelect.value } : kindSelect.value;
      if (background && typeof next === "object") next.background = background;
      d.strip[row] = next;
    }));
    tpl.querySelector("#stripBackground").append(colourField("Background", stripBg, setStripBg, deckBackground,
      "Behind the clock, date, weather or picture on this panel."));
    if (kind === "animation") {
      const animField = tpl.querySelector("#stripAnimationField");
      animField.hidden = false;
      animField.append(animationField(
        () => (typeof doc.strip[row] === "object" && doc.strip[row].animation) || null,
        value => change(d => {
          const background = stripBg();
          d.strip[row] = value ? { animation: value } : (background ? { kind: "blank" } : "blank");
          if (background) d.strip[row].background = background;
        }),
        "Animation", "Drawn to fit the visible area of the panel."));
    }
    if (kind === "weather") {
      const weatherField = tpl.querySelector("#stripWeatherField");
      weatherField.hidden = false;
      weatherField.append(weatherFields());
    }
    const pictureField = tpl.querySelector("#stripPictureField");
    if (kind === "image") {
      pictureField.hidden = false;
      tpl.querySelector("#stripPicture").append(picker(entry.image, value => change(d => {
        d.strip[row] = Object.assign({}, d.strip[row], { image: value, fit: !!entry.fit });
      })));
      const fit = el("input", { type: "checkbox", id: "stripFit" });
      fit.checked = !!entry.fit;
      fit.addEventListener("change", () => change(d => { d.strip[row] = Object.assign({}, d.strip[row], { fit: fit.checked }); }, { silent: true }));
      pictureField.append(el("label", { class: "check", for: "stripFit" }, [fit,
        el("span", { text: "Shrink the picture to the visible area" })]));
      pictureField.append(el("div", { class: "help", text: "Off: the picture fills the whole panel and its edges are cut off, which suits some pictures. On: it is scaled to fit inside the part the panel shows, with a border around it." }));
    }
    editor.append(tpl);
  }

  /**
   * <summary>A new action of the given type, with sensible starting values.</summary>
   * <param name="type">One of the types offered in the Action select.</param>
   * <returns>A fresh action object.</returns>
   * <remarks>These values have to stay inside the ranges the sliders in
   * <see cref="actionFields"/> allow, or a brand new action opens with its
   * slider already pinned at one end and no way to see why.</remarks>
   */
  function defaultAction(type) {
    switch (type) {
      case "hotkey": return { type, keys: "" };
      case "sequence": return { type, keys: [], delay_ms: 100 };
      case "chord": return { type, hold: "", keys: [], delay_min_ms: 50, delay_max_ms: 200 };
      case "hold": return { type, keys: "", hold_min_ms: 3000, hold_max_ms: 8000, release_min_ms: 150, release_max_ms: 600 };
      case "boost": return { type, hold: "", keys: "", on_ms: 10000, off_ms: 12000 };
      case "repeat": return { type, keys: "", every_ms: 30000 };
      case "launch": return { type, command: "" };
      case "url": return { type, url: "https://" };
      case "page": return { type, page: "next" };
      case "brightness": return { type, delta: -10 };
      case "sleep": return { type };
      case "multi": return { type, delay_ms: 0, steps: [{ type: "hotkey", keys: "" }] };
    }
    return { type };
  }

  /**
   * <summary>The inputs for one action, whichever type it is.</summary>
   * <param name="action">The action as it is now, read for starting values only.</param>
   * <param name="getAction">Returns the live action object in the document.</param>
   * <param name="nested">True when this is a step inside a multi action.</param>
   * <returns>A box of fields.</returns>
   * <remarks>
   * ``action`` is only ever read; every edit goes through ``getAction`` so it
   * lands in the document that is current when the input fires, not the one
   * that was current when the field was built. An undo between the two is the
   * case that breaks otherwise.
   *
   * A multi action inside a multi action is refused by ignoring it when
   * ``nested`` is set, which is what stops the editor recursing for ever.
   * </remarks>
   */
  function actionFields(action, getAction, nested) {
    const box = el("div");
    const text = (name, labelText, placeholder, help) => {
      const input = el("input", { type: "text", value: action[name] || "", placeholder: placeholder || "" });
      input.addEventListener("input", () => change(d => { getAction()[name] = input.value; }, { silent: true }));
      const field = el("div", { class: "field" }, [el("label", { text: labelText }), input]);
      if (help) field.append(el("div", { class: "help", text: help }));
      return field;
    };
    // A millisecond slider that reads in seconds once it is past a second, so
    // a ten second boost says "10 s" rather than "10000 ms".
    const millis = (name, labelText, opts) => {
      const fallback = opts.fallback ?? 0;
      const show = value => {
        if (!value) return opts.zero || "0 ms";
        if (value < 1000) return value + " ms";
        return (value / 1000).toFixed(value % 1000 ? 1 : 0) + " s";
      };
      const start = action[name] ?? fallback;
      const input = el("input", { type: "range", min: opts.min, max: opts.max, step: opts.step, value: start });
      const out = el("output", { text: show(start) });
      input.addEventListener("input", () => {
        const value = parseInt(input.value, 10) || 0;
        out.textContent = show(value);
        change(d => { getAction()[name] = value; }, { silent: true });
      });
      const field = el("div", { class: "field" }, [
        el("label", { text: labelText }),
        el("div", { class: "inline slider-row" }, [input, out]),
      ]);
      if (opts.help) field.append(el("div", { class: "help", text: opts.help }));
      return field;
    };
    switch (action.type) {
      case "hotkey":
        box.append(keyField({
          label: "Key",
          many: false,
          get: () => (getAction().keys ? [getAction().keys] : []),
          set: list => { getAction().keys = list[0] || ""; },
          help: "Click Capture, then press the key or combination once.",
        }));
        box.append(millis("hold_ms", "Hold it down for", {
          min: 0, max: 10000, step: 100, fallback: 0, zero: "a tap",
          help: "A tap by default. Slide it up to hold the key down for that long, for a game that wants the key held rather than tapped.",
        }));
        break;
      case "sequence": {
        box.append(keyField({
          label: "Keys, in order",
          many: true,
          get: () => {
            const keys = getAction().keys;
            return Array.isArray(keys) ? keys.slice() : String(keys || "").split(/\s+/).filter(Boolean);
          },
          set: list => { getAction().keys = list; },
          help: "Click Capture, then press every key in turn. Escape or Capture again finishes. Click a key to remove it.",
        }));
        const slider = el("input", { type: "range", min: 0, max: 2000, step: 10, value: action.delay_ms ?? 100 });
        const out = el("output", { text: (action.delay_ms ?? 100) + " ms" });
        slider.addEventListener("input", () => {
          out.textContent = slider.value + " ms";
          change(d => { getAction().delay_ms = parseInt(slider.value, 10) || 0; }, { silent: true });
        });
        box.append(el("div", { class: "field" }, [
          el("label", { text: "Pause between keys" }),
          el("div", { class: "inline slider-row" }, [slider, out]),
        ]));
        break;
      }
      case "chord": {
        box.append(keyField({
          label: "Key held down throughout",
          many: false,
          get: () => (getAction().hold ? [getAction().hold] : []),
          set: list => { getAction().hold = list[0] || ""; },
          help: "Pressed first and not let go until the last tap is done. Usually a modifier such as alt: click Capture, then tap alt on its own.",
        }));
        box.append(keyField({
          label: "Keys tapped while it is held",
          many: true,
          get: () => {
            const keys = getAction().keys;
            return Array.isArray(keys) ? keys.slice() : String(keys || "").split(/\s+/).filter(Boolean);
          },
          set: list => { getAction().keys = list; },
          help: "Capture just the keys to tap, without the held key.",
        }));
        box.append(dualSlider("Pause before each tap", 0, 3000, 10, "delay_min_ms", "delay_max_ms", getAction, action,
          "A random time in this range before every tap. The held key is never let go in between."));
        break;
      }
      case "hold": {
        box.append(keyField({
          label: "Key to hold",
          many: false,
          get: () => (getAction().keys ? [getAction().keys] : []),
          set: list => { getAction().keys = list[0] || ""; },
          help: "Press the deck key once to start holding, again to let go. The key shows a green mark while it is held.",
        }));
        box.append(dualSlider("Hold for", 500, 60000, 100, "hold_min_ms", "hold_max_ms", getAction, action,
          "Each time this runs out, the key is let go for a moment."));
        box.append(dualSlider("Let go for", 0, 5000, 10, "release_min_ms", "release_max_ms", getAction, action,
          "A random time in this range, then the key is pressed again. Both at zero means never let go."));
        break;
      }
      case "boost": {
        box.append(keyField({
          label: "Key held down throughout",
          many: false,
          get: () => (getAction().hold ? [getAction().hold] : []),
          set: list => { getAction().hold = list[0] || ""; },
          help: "Held from the moment you press the deck key until you press it again. The forward key, usually.",
        }));
        box.append(keyField({
          label: "Key pulsed for the boost",
          many: false,
          get: () => (getAction().keys ? [getAction().keys] : []),
          set: list => { getAction().keys = list[0] || ""; },
          help: "Pressed and let go over and over while the first key stays down.",
        }));
        box.append(millis("on_ms", "Boost on for", {
          min: 500, max: 120000, step: 500, fallback: 10000,
          help: "How long the boost key is held each time.",
        }));
        box.append(millis("off_ms", "Then off for", {
          min: 0, max: 120000, step: 500, fallback: 12000,
          help: "How long before it boosts again. The cycle repeats until you press the deck key again.",
        }));
        break;
      }
      case "repeat": {
        box.append(keyField({
          label: "Key to press",
          many: false,
          get: () => (getAction().keys ? [getAction().keys] : []),
          set: list => { getAction().keys = list[0] || ""; },
          help: "Press the deck key once to start, again to stop. The key shows a green mark while it is running.",
        }));
        box.append(millis("every_ms", "Press it every", {
          min: 1000, max: 300000, step: 1000, fallback: 30000,
          help: "Tapped, then again after this long, over and over until you press the deck key again.",
        }));
        break;
      }
      case "launch":
        box.append(text("command", "Command", "gedit", "Used on both platforms unless overridden below."));
        box.append(text("command_windows", "Windows override", "notepad.exe"));
        box.append(text("command_linux", "Linux override", "xed"));
        break;
      case "url":
        box.append(text("url", "Web address", "https://"));
        break;
      case "page": {
        const select = el("select");
        for (const name of ["next", "previous", ...doc.pages.map(p => p.name)]) {
          select.append(el("option", { value: name, text: name }));
        }
        select.value = action.page || "next";
        select.addEventListener("change", () => change(d => { getAction().page = select.value; }, { silent: true }));
        box.append(el("div", { class: "field" }, [el("label", { text: "Go to" }), select]));
        break;
      }
      case "brightness": {
        const mode = el("select", {}, [
          el("option", { value: "delta", text: "Change by" }),
          el("option", { value: "value", text: "Set to" }),
        ]);
        mode.value = "value" in action ? "value" : "delta";
        const number = el("input", { type: "number", min: mode.value === "value" ? 0 : -100, max: 100,
          value: "value" in action ? action.value : action.delta });
        const apply = () => change(d => {
          const a = getAction();
          delete a.value; delete a.delta;
          a[mode.value] = parseInt(number.value, 10) || 0;
        }, { silent: true });
        mode.addEventListener("change", () => { number.min = mode.value === "value" ? 0 : -100; apply(); });
        number.addEventListener("input", apply);
        box.append(el("div", { class: "field" }, [el("label", { text: "Brightness" }), el("div", { class: "inline" }, [mode, number])]));
        break;
      }
      case "sleep":
        box.append(el("p", { class: "muted", text: "Turns the screens off until a key is pressed." }));
        break;
      case "multi": {
        if (nested) break;
        const delay = el("input", { type: "number", min: 0, max: 60000, value: action.delay_ms || 0 });
        delay.addEventListener("input", () => change(d => { getAction().delay_ms = parseInt(delay.value, 10) || 0; }, { silent: true }));
        box.append(el("div", { class: "field" }, [el("label", { text: "Pause between steps (ms)" }), delay]));
        const steps = el("div", { class: "steps" });
        (action.steps || []).forEach((step, index) => {
          const head = el("div", { class: "step-head" });
          const typeSelect = el("select");
          // hold is a toggle with its own thread, so it is not offered as a step
          for (const [value, label] of [["hotkey", "Send a hotkey"], ["sequence", "Send several keys"], ["chord", "Hold one key while pressing others"],
            ["launch", "Launch a program"], ["url", "Open a web address"],
            ["page", "Switch page"], ["brightness", "Deck brightness"], ["sleep", "Sleep the deck"]]) {
            typeSelect.append(el("option", { value, text: label }));
          }
          typeSelect.value = step.type;
          typeSelect.addEventListener("change", () => change(d => { getAction().steps[index] = defaultAction(typeSelect.value); }));
          const remove = el("button", { type: "button", class: "ghost small danger", text: "Remove" });
          remove.addEventListener("click", () => change(d => { getAction().steps.splice(index, 1); }));
          head.append(el("span", { class: "muted", text: String(index + 1) }), typeSelect, remove);
          steps.append(el("div", { class: "step" }, [head, actionFields(step, () => getAction().steps[index], true)]));
        });
        const add = el("button", { type: "button", class: "ghost small", text: "+ step" });
        add.addEventListener("click", () => change(d => { getAction().steps.push({ type: "hotkey", keys: "" }); }));
        box.append(el("div", { class: "field" }, [el("label", { text: "Steps" }), steps, el("div", { class: "row" }, [add])]));
        break;
      }
    }
    return box;
  }

  /**
   * <summary>Captured key combinations, shown as chips.</summary>
   * <param name="opts">label, many, get, set and help. ``many`` keeps the
   * capture running for a list of keys instead of stopping after one.</param>
   * <returns>The field element.</returns>
   * <remarks>
   * Filled by pressing keys rather than typing names, because the names the
   * daemon wants are not the ones a keyboard is labelled with. Some keys never
   * reach the page at all: a media key the desktop swallows, or a combination
   * the window manager takes first. The type it instead link is the way round
   * that, and it is why this field cannot simply be made read only.
   * </remarks>
   */
  function keyField(opts) {
    const chips = el("div", { class: "keys" });
    const field = el("div", { class: "field" });
    let textBox = null;

    const commit = list => change(d => opts.set(list), { silent: true });
    const render = () => {
      chips.innerHTML = "";
      const list = opts.get();
      if (!list.length) {
        chips.append(el("span", { class: "muted", text: capturing && capturing.field === field ? "listening" : "nothing captured yet" }));
      }
      list.forEach((combo, index) => {
        const chip = el("button", { type: "button", class: "chip", text: combo,
          title: opts.many ? "Remove this key" : "Remove" });
        chip.addEventListener("click", () => {
          const next = opts.get();
          next.splice(index, 1);
          commit(next);
          render();
        });
        chips.append(chip);
      });
      if (textBox) textBox.value = list.join(" ");
    };

    const capture = el("button", { type: "button", class: "ghost small", text: "Capture" });
    capture.addEventListener("click", () => {
      if (capturing && capturing.field === field) { stopCapture(); render(); return; }
      startCapture(capture, combo => {
        const next = opts.many ? [...opts.get(), combo] : [combo];
        commit(next);
        render();
      }, opts.many);
      capturing.field = field;
      capturing.onStop = render;
      render();
    });
    const clear = el("button", { type: "button", class: "ghost small", text: "Clear" });
    clear.addEventListener("click", () => { commit([]); render(); });
    const typeIt = el("button", { type: "button", class: "link", text: "type it instead" });
    typeIt.addEventListener("click", () => {
      if (textBox) { textBox.parentElement.remove(); textBox = null; typeIt.textContent = "type it instead"; return; }
      textBox = el("input", { type: "text", value: opts.get().join(" "),
        placeholder: opts.many ? "ctrl+l h e l l o enter" : "ctrl+alt+t" });
      textBox.addEventListener("input", () => {
        const list = textBox.value.split(/\s+/).filter(Boolean);
        commit(opts.many ? list : list.slice(0, 1));
        const keep = textBox;
        render();
        textBox = keep;
      });
      field.append(el("div", { class: "typed" }, [textBox,
        el("div", { class: "help", text: "Names: ctrl, alt, shift, cmd, enter, esc, tab, space, up, f1 to f24, media_volume_mute, media_play_pause and so on. Join a combination with +." })]));
      typeIt.textContent = "hide";
      textBox.focus();
    });

    field.append(
      el("label", { text: opts.label }),
      chips,
      el("div", { class: "row" }, [capture, clear, typeIt]),
    );
    if (opts.help) field.append(el("div", { class: "help", text: opts.help }));
    render();
    return field;
  }

  /**
   * <summary>One track with two handles: the low and high end of a random time.</summary>
   * <param name="labelText">The field's label.</param>
   * <param name="min">Lowest value either handle can take.</param>
   * <param name="max">Highest value either handle can take.</param>
   * <param name="step">Slider step, in milliseconds.</param>
   * <param name="lowName">Field of the action holding the low end.</param>
   * <param name="highName">Field of the action holding the high end.</param>
   * <param name="getAction">Returns the live action object in the document.</param>
   * <param name="action">The action as it is now, read for starting values.</param>
   * <param name="help">Optional help line under the field.</param>
   * <returns>The field element.</returns>
   * <remarks>Each handle pushes the other rather than passing it, so the pair
   * can never end up the wrong way round and the daemon is never left to guess
   * what a reversed range was meant to mean.</remarks>
   */
  function dualSlider(labelText, min, max, step, lowName, highName, getAction, action, help) {
    const low = el("input", { type: "range", min, max, step, value: action[lowName] ?? min, "aria-label": labelText + " at least" });
    const high = el("input", { type: "range", min, max, step, value: action[highName] ?? max, "aria-label": labelText + " at most" });
    const out = el("output");
    const fmt = ms => (ms >= 1000 ? (ms / 1000).toFixed(ms % 1000 ? 1 : 0) + " s" : ms + " ms");
    const show = () => { out.textContent = fmt(+low.value) + " to " + fmt(+high.value); };
    const store = () => change(d => {
      const a = getAction();
      a[lowName] = parseInt(low.value, 10);
      a[highName] = parseInt(high.value, 10);
    }, { silent: true });
    low.addEventListener("input", () => { if (+low.value > +high.value) high.value = low.value; show(); store(); });
    high.addEventListener("input", () => { if (+high.value < +low.value) low.value = high.value; show(); store(); });
    show();
    const field = el("div", { class: "field" }, [
      el("label", { text: labelText }),
      el("div", { class: "dual" }, [low, high]),
      el("div", { class: "inline" }, [out]),
    ]);
    if (help) field.append(el("div", { class: "help", text: help }));
    return field;
  }

  // ---- Picture picker --------------------------------------------------------

  /**
   * <summary>The thumbnail URL for a picker value.</summary>
   * <param name="value">A theme icon as "theme:slug/id", an uploaded picture as
   * "images/name", or null.</param>
   * <returns>A URL on the local API, or null when there is no picture.</returns>
   */
  function imageUrl(value) {
    if (!value) return null;
    if (value.startsWith("theme:")) {
      const [slug, id] = value.slice("theme:".length).split("/");
      return "/api/themes/" + encodeURIComponent(slug) + "/" + encodeURIComponent(id) + ".png";
    }
    return "/api/images/" + encodeURIComponent(value.replace(/^images\//, ""));
  }

  let themeData = null;  // the shipped themes, fetched once and reused
  /**
   * <summary>The shipped icon themes, fetched once and kept.</summary>
   * <remarks>They cannot change while the daemon is running, and the gallery is
   * opened often enough that fetching the whole list each time showed.</remarks>
   */
  async function loadThemes() {
    if (themeData) return themeData;
    const data = await apiGet("/api/themes");
    themeData = data.themes || [];
    return themeData;
  }

  /**
   * <summary>The searchable icon gallery, grouped by theme.</summary>
   * <param name="onPick">Called with "theme:slug/id" when an icon is chosen,
   * and not called at all when the gallery is closed without picking.</param>
   * <remarks>
   * Searching hides tiles in place rather than rebuilding the grid, so the
   * lazily loaded thumbnails are not fetched again on every keystroke. A theme
   * with nothing matching hides its heading too, or the modal fills up with
   * headings over empty space.
   * </remarks>
   */
  async function openThemeGallery(onPick) {
    const backdrop = el("div", { class: "modal-backdrop" });
    const box = el("div", { class: "modal gallery-modal", role: "dialog", "aria-modal": "true", "aria-label": "Choose an icon" });
    const close = el("button", { type: "button", class: "ghost icon", "aria-label": "Close", text: "✕" });
    const finish = () => { backdrop.remove(); document.removeEventListener("keydown", onKey, true); };
    const onKey = e => { if (e.key === "Escape") { e.preventDefault(); finish(); } };
    close.addEventListener("click", finish);
    backdrop.addEventListener("click", e => { if (e.target === backdrop) finish(); });
    document.addEventListener("keydown", onKey, true);
    box.append(el("div", { class: "modal-head" }, [el("h2", { text: "Choose an icon" }), close]));
    const search = el("input", { type: "search", class: "gallery-search", placeholder: "Search icons", "aria-label": "Search icons" });
    box.append(search);
    const body = el("div");
    box.append(body);
    backdrop.append(box);
    document.body.append(backdrop);
    try {
      const themes = await loadThemes();
      if (!themes.length) { body.append(el("p", { class: "muted", text: "No icon themes are installed." })); return; }
      const sections = [];  // {heading, grid, tiles: [{el, text}]} so search can filter in place
      for (const theme of themes) {
        const heading = el("h3", { text: theme.name });
        const grid = el("div", { class: "icon-grid" });
        const tiles = [];
        for (const icon of theme.icons) {
          const tile = el("button", { type: "button", class: "icon-tile", title: icon.label });
          tile.append(
            el("img", { src: "/api/themes/" + encodeURIComponent(theme.slug) + "/" + encodeURIComponent(icon.id) + ".png", alt: icon.label, loading: "lazy" }),
            el("span", { text: icon.label }),
          );
          tile.addEventListener("click", () => { onPick("theme:" + theme.slug + "/" + icon.id); finish(); });
          grid.append(tile);
          tiles.push({ el: tile, text: (icon.label + " " + icon.id).toLowerCase() });
        }
        body.append(heading, grid);
        sections.push({ heading, grid, tiles });
      }
      const empty = el("p", { class: "muted", text: "No icon matches that." });
      empty.style.display = "none";
      body.append(empty);
      const applyFilter = () => {
        const query = search.value.trim().toLowerCase();
        let anyAtAll = false;
        for (const section of sections) {
          let visible = 0;
          for (const tile of section.tiles) {
            const match = !query || tile.text.includes(query);
            tile.el.style.display = match ? "" : "none";
            if (match) visible += 1;
          }
          section.heading.style.display = visible ? "" : "none";
          section.grid.style.display = visible ? "" : "none";
          anyAtAll = anyAtAll || visible > 0;
        }
        empty.style.display = anyAtAll ? "none" : "";
      };
      search.addEventListener("input", applyFilter);
      search.focus();
    } catch (err) {
      body.append(el("p", { class: "help bad", text: "Could not load themes: " + err.message }));
    }
  }

  /**
   * <summary>Choose a picture: none, an uploaded one, or a theme icon.</summary>
   * <param name="current">The value now, or null.</param>
   * <param name="onChange">Called with the new value, or null for no picture.</param>
   * <param name="options">``{themes: false}`` leaves the theme gallery out. The
   * wallpaper spans every key and is the user's own picture, not an icon, so
   * the built in themes are offered on key and strip panels only.</param>
   * <returns>The picker element.</returns>
   * <remarks>A value that is neither a theme icon nor a file still in the
   * images folder is added to the select exactly as it stands, so a picture
   * deleted from disk shows as itself rather than quietly becoming No
   * picture.</remarks>
   */
  function picker(current, onChange, options) {
    const wrap = el("div", { class: "picker" });
    const preview = el("img", { class: "preview", alt: "" });
    const src = imageUrl(current);
    if (src) preview.src = src;
    else preview.hidden = true;  // no broken image icon when there is no picture
    const select = el("select");
    select.append(el("option", { value: "", text: "No picture" }));
    for (const file of images) select.append(el("option", { value: "images/" + file, text: file }));
    const name = current ? current.replace(/^images\//, "") : "";
    if (current && current.startsWith("theme:")) select.append(el("option", { value: current, text: "Theme icon" }));
    else if (current && !images.includes(name)) select.append(el("option", { value: current, text: current }));
    select.value = current || "";
    select.addEventListener("change", () => onChange(select.value || null));
    const wantThemes = !(options && options.themes === false);
    const themesBtn = wantThemes ? el("button", { type: "button", class: "ghost small", text: "Choose from a theme" }) : null;
    if (themesBtn) themesBtn.addEventListener("click", () => openThemeGallery(value => { if (value) onChange(value); }));
    const file = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif" });
    const upload = el("button", { type: "button", class: "ghost small", text: "Upload a picture" });
    upload.addEventListener("click", () => file.click());
    file.addEventListener("change", async () => {
      const chosen = file.files[0];
      if (!chosen) return;
      try {
        const r = await fetch("/api/images", {
          method: "POST", body: chosen,
          headers: { "Content-Type": chosen.type || "image/png", "X-Filename": chosen.name },
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || r.statusText);
        await loadImages();
        onChange(data.path);
      } catch (err) {
        showError(err);
      }
    });
    wrap.append(preview, el("div", { class: "controls" },
      themesBtn ? [select, themesBtn, upload, file] : [select, upload, file]));
    return wrap;
  }

  // ---- Hotkey capture ------------------------------------------------------

  /**
   * <summary>Browser key names mapped to the names the daemon understands.</summary>
   * <remarks>Only the keys whose two names differ are listed. A plain letter or
   * digit is lowercased and sent as it is, so it never needs an entry
   * here.</remarks>
   */
  const KEY_NAMES = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Escape: "esc", Enter: "enter", " ": "space", Tab: "tab", Backspace: "backspace",
    Delete: "delete", Home: "home", End: "end", PageUp: "page_up", PageDown: "page_down",
    Insert: "insert", PrintScreen: "print_screen", CapsLock: "caps_lock", "+": "plus",
    AudioVolumeUp: "media_volume_up", AudioVolumeDown: "media_volume_down",
    AudioVolumeMute: "media_volume_mute", MediaPlayPause: "media_play_pause",
    MediaTrackNext: "media_next", MediaTrackPrevious: "media_previous",
  };
  // Older names some engines and automation tools still send for the same keys.
  Object.assign(KEY_NAMES, {
    Return: "enter", Up: "up", Down: "down", Left: "left", Right: "right", Esc: "esc",
    Spacebar: "space", space: "space", Del: "delete", Prior: "page_up", Next: "page_down",
  });
  // By physical key code, as a fallback when the key name is not recognised.
  const CODE_NAMES = {
    Enter: "enter", NumpadEnter: "enter", Space: "space", Escape: "esc", Tab: "tab",
    Backspace: "backspace", Delete: "delete", Insert: "insert", Home: "home", End: "end",
    PageUp: "page_up", PageDown: "page_down", ArrowUp: "up", ArrowDown: "down",
    ArrowLeft: "left", ArrowRight: "right", PrintScreen: "print_screen", CapsLock: "caps_lock",
    NumpadAdd: "plus", Equal: "=", Minus: "-",
  };
  const MODIFIER_KEYS = new Set(["Control", "Alt", "Shift", "Meta", "OS", "AltGraph"]);

  /**
   * <summary>Start listening for a key press to capture.</summary>
   * <param name="button">The Capture button, relabelled while it listens.</param>
   * <param name="done">Called with each captured combination, such as "ctrl+l".</param>
   * <param name="keepGoing">Keep listening after the first key, for a list.</param>
   * <remarks>Only one capture runs at a time: starting another stops the one
   * before it, so a Capture button left listening in a pane that has since been
   * rebuilt cannot go on swallowing keystrokes.</remarks>
   */
  function startCapture(button, done, keepGoing) {
    if (capturing) stopCapture();
    button.textContent = keepGoing ? "Press keys, Esc to finish" : "Press a key";
    button.classList.add("capturing");
    capturing = { button, done, keepGoing };
  }
  /**
   * <summary>Stop listening, put the button back and let the field redraw.</summary>
   * <remarks>Safe to call when nothing is capturing, which is what lets Escape
   * and the teardown of a pane both call it without checking first.</remarks>
   */
  function stopCapture() {
    if (!capturing) return;
    const was = capturing;
    capturing = null;
    pendingModifier = null;
    was.button.textContent = "Capture";
    was.button.classList.remove("capturing");
    if (was.onStop) was.onStop();
  }
  const MODIFIER_NAMES = { Control: "ctrl", Alt: "alt", AltGraph: "alt", Shift: "shift", Meta: "cmd", OS: "cmd" };
  let pendingModifier = null;  // a modifier pressed on its own, captured when released

  /** <summary>Hand one captured combination to the field, stopping unless a list is wanted.</summary> */
  function finishCapture(combo) {
    const done = capturing.done;
    if (!capturing.keepGoing) stopCapture();
    done(combo);
  }

  // A modifier tapped on its own (alt, ctrl, shift, cmd) is a key too: the
  // chord's held key usually is one. It counts when released without any
  // other key having been pressed while it was down.
  document.addEventListener("keyup", event => {
    if (!capturing || !pendingModifier) return;
    if (MODIFIER_NAMES[event.key] === pendingModifier) {
      event.preventDefault();
      pendingModifier = null;
      finishCapture(MODIFIER_NAMES[event.key]);
    }
  }, true);

  document.addEventListener("keydown", event => {
    if (!capturing) return;
    event.preventDefault();
    if (event.key === "Escape") { pendingModifier = null; stopCapture(); return; }
    if (MODIFIER_KEYS.has(event.key)) {
      if (!event.repeat) pendingModifier = MODIFIER_NAMES[event.key] || null;
      return;
    }
    pendingModifier = null;
    let name = KEY_NAMES[event.key];
    if (!name) {
      if (/^F\d{1,2}$/.test(event.key)) name = event.key.toLowerCase();
      else if (event.key.length === 1) name = event.key.toLowerCase();
      else if (CODE_NAMES[event.code]) name = CODE_NAMES[event.code];
      else if (/^Key[A-Z]$/.test(event.code)) name = event.code.slice(3).toLowerCase();
      else if (/^Digit\d$/.test(event.code)) name = event.code.slice(5);
      else if (/^F\d{1,2}$/.test(event.code)) name = event.code.toLowerCase();
    }
    if (!name) return;
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    if (event.metaKey) parts.push("cmd");
    parts.push(name);
    finishCapture(parts.join("+"));
  }, true);

  // ---- Settings ------------------------------------------------------------

  /**
   * <summary>A number input bound to one field of the document.</summary>
   * <param name="section">Top level section of the document, such as "deck".</param>
   * <param name="name">Field within that section.</param>
   * <param name="labelText">The field's label.</param>
   * <param name="min">Lowest value the browser will accept.</param>
   * <param name="max">Highest value the browser will accept.</param>
   * <param name="help">Optional help line under the field.</param>
   * <returns>The field element.</returns>
   * <remarks>Saved silently, so the input keeps the caret while it is being
   * typed in and the deck follows when the save lands. The limits here are the
   * browser's only: the daemon checks the value again when it parses the
   * config, and is the one that decides.</remarks>
   */
  const numberField = (section, name, labelText, min, max, help) => {
    const input = el("input", { type: "number", min, max, value: doc[section][name] });
    input.addEventListener("input", () => change(d => { d[section][name] = parseFloat(input.value) || 0; }, { silent: true }));
    const field = el("div", { class: "field" }, [el("label", { text: labelText }), input]);
    if (help) field.append(el("div", { class: "help", text: help }));
    return field;
  };
  /**
   * <summary>A text input bound to one field of the document, saved silently.</summary>
   * <param name="section">Top level section of the document.</param>
   * <param name="name">Field within that section.</param>
   * <param name="labelText">The field's label.</param>
   * <returns>The field element.</returns>
   */
  const textField = (section, name, labelText) => {
    const input = el("input", { type: "text", value: doc[section][name] || "" });
    input.addEventListener("input", () => change(d => { d[section][name] = input.value; }, { silent: true }));
    return el("div", { class: "field" }, [el("label", { text: labelText }), input]);
  };
  /**
   * <summary>A select bound to one field of the document, saved silently.</summary>
   * <param name="section">Top level section of the document.</param>
   * <param name="name">Field within that section.</param>
   * <param name="labelText">The field's label.</param>
   * <param name="options">Pairs of stored value and label, in the order shown.</param>
   * <returns>The field element.</returns>
   */
  const selectField = (section, name, labelText, options) => {
    const select = el("select");
    for (const [value, label] of options) select.append(el("option", { value, text: label }));
    select.value = doc[section][name];
    select.addEventListener("change", () => change(d => { d[section][name] = select.value; }, { silent: true }));
    return el("div", { class: "field" }, [el("label", { text: labelText }), select]);
  };

  /**
   * <summary>The weather place, the name to show for it, and the units.</summary>
   * <returns>The fields, for the editor of a strip panel showing the weather.</returns>
   * <remarks>
   * One setting shared by every panel that shows the weather rather than a per
   * panel one, which is why the last field says so out loud. The place search
   * goes through the daemon rather than straight to a geocoder, so the page
   * never talks to anything off the machine.
   * </remarks>
   */
  function weatherFields() {
    const search = el("input", { type: "search", placeholder: "Find a place" });
    const results = el("ul", { class: "places" });
    const find = el("button", { type: "button", class: "ghost small", text: "Search" });
    const lookup = async () => {
      results.innerHTML = "";
      if (!search.value.trim()) return;
      try {
        const data = await apiGet("/api/geocode?name=" + encodeURIComponent(search.value.trim()));
        if (!data.places.length) results.append(el("li", { class: "muted", text: "Nothing found" }));
        for (const place of data.places) {
          const label = [place.name, place.region, place.country].filter(Boolean).join(", ");
          results.append(el("li", { text: label, onclick: () => change(d => {
            d.weather.latitude = place.latitude;
            d.weather.longitude = place.longitude;
            d.weather.location_name = place.name;
          }) }));
        }
      } catch (err) { results.append(el("li", { class: "muted", text: err.message })); }
    };
    find.addEventListener("click", lookup);
    search.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); lookup(); } });
    return el("div", { class: "weather-fields" }, [
      el("div", { class: "field" }, [el("label", { text: "Place" }), el("div", { class: "inline" }, [search, find]), results]),
      textField("weather", "location_name", "Shown as"),
      el("div", { class: "row" }, [numberField("weather", "latitude", "Latitude", -90, 90), numberField("weather", "longitude", "Longitude", -180, 180)]),
      selectField("weather", "units", "Units", [["metric", "Celsius"], ["imperial", "Fahrenheit"]]),
      numberField("weather", "refresh_minutes", "Refresh every (minutes)", 1, 1440,
        "The weather is one setting, shared by every panel that shows it."),
    ]);
  }

  /**
   * <summary>The settings window's content while it is open, otherwise null.</summary>
   * <remarks>
   * Doubles as the flag for whether the window is open, which a save checks
   * before refreshing the picture list. The window holds only what applies to
   * the whole deck: anything about one key or one panel is edited by clicking
   * that key or panel.
   * </remarks>
   */
  let settingsRoot = null;

  /**
   * <summary>Open the settings window with a fresh picture list.</summary>
   * <remarks>
   * The picture list says which keys use each picture, and that changes every
   * time a key's picture is set or cleared, so it is fetched again here rather
   * than shown as it was when the page loaded. A stale list left a picture
   * marked as in use, with its Delete button off, after the key had let go of
   * it.
   * </remarks>
   */
  async function openSettings() {
    if (settingsRoot) return;
    await loadImages();
    const backdrop = el("div", { class: "modal-backdrop" });
    const box = el("div", { class: "modal wide", role: "dialog", "aria-modal": "true", "aria-label": "Settings" });
    const close = el("button", { type: "button", class: "ghost small", text: "Close" });
    settingsRoot = el("div", { class: "settings-grid" });
    box.append(el("div", { class: "modal-head" }, [el("h2", { text: "Settings" }), close]), settingsRoot);
    const finish = () => { backdrop.remove(); settingsRoot = null; document.removeEventListener("keydown", onKey, true); };
    const onKey = e => { if (e.key === "Escape" && !capturing) { e.preventDefault(); finish(); } };
    close.addEventListener("click", finish);
    backdrop.addEventListener("click", e => { if (e.target === backdrop) finish(); });
    document.addEventListener("keydown", onKey, true);
    backdrop.append(box);
    document.body.append(backdrop);
    renderSettings();
    close.focus();
  }

  /**
   * <summary>Fill the open settings window, or do nothing while it is shut.</summary>
   * <remarks>
   * Rebuilt whole rather than patched, so it can never drift from the document.
   * Its fields save silently for that very reason: a redraw here would take the
   * caret out of whatever was being typed in. The follow the focused window
   * checkbox is the deliberate exception, because it enables the row of per
   * page match boxes below it and they have to appear at once.
   * </remarks>
   */
  function renderSettings() {
    const root = settingsRoot;
    if (!root) return;
    root.innerHTML = "";

    // Deck
    root.append(el("div", {}, [
      el("h3", { text: "Deck" }),
      numberField("deck", "brightness", "Brightness at start", 0, 100),
      numberField("deck", "sleep_after_minutes", "Screens off after (minutes)", 0, 1440, "0 never sleeps. Any key wakes the deck."),
      selectField("deck", "official_software", "If the official app is running", [
        ["ask", "Ask in the terminal"], ["stop", "Stop it"], ["keep", "Leave it running"]]),
      colourField("Background for the whole deck",
        () => (doc.deck.background && doc.deck.background !== BUILT_IN_BACKGROUND) ? doc.deck.background : null,
        value => change(d => { d.deck.background = value || BUILT_IN_BACKGROUND; }, { silent: true }),
        () => BUILT_IN_BACKGROUND,
        "Used by every key and strip panel without its own colour. Set to default restores the original navy."),
      (() => {
        const flash = el("input", { type: "checkbox", id: "pressFlash" });
        flash.checked = doc.deck.press_flash !== false;
        flash.addEventListener("change", () => change(d => { d.deck.press_flash = flash.checked; }, { silent: true }));
        return el("label", { class: "check", for: "pressFlash" }, [flash, el("span", { text: "Flash a key briefly when it is pressed" })]);
      })(),
    ]));

    // Long press and double press, deck wide. The actions themselves are set
    // on each key; these are only the two thresholds that decide what a
    // gesture was.
    root.append(el("div", {}, [
      el("h3", { text: "Long press and double press" }),
      el("p", { class: "muted", text: "Keys with nothing set for a hold or a double press are unaffected and still act the moment they are pressed." }),
      numberField("deck", "long_press_ms", "A hold counts after (ms)", 120, 5000,
        "The long press action runs as soon as this passes, without waiting for you to let go."),
      numberField("deck", "double_press_ms", "A second press still counts within (ms)", 100, 2000,
        "A key with a double press action waits this long after you let go before doing its plain action."),
    ]));

    // Per application pages
    const focusState = (state && state.focus) || {};
    const followBox = el("input", { type: "checkbox", id: "followFocus" });
    followBox.checked = doc.deck.follow_focus === true;
    followBox.addEventListener("change", () => {
      change(d => { d.deck.follow_focus = followBox.checked; }, { silent: true });
      renderSettings();
    });
    const matchRows = doc.pages.map((page, index) => {
      const input = el("input", { type: "text", value: page.match_window || "",
        placeholder: "e.g. firefox", disabled: !followBox.checked });
      input.addEventListener("input", () => change(d => {
        d.pages[index].match_window = input.value.trim() || null;
      }, { silent: true }));
      return el("div", { class: "field" }, [el("label", { text: page.name }), input]);
    });
    root.append(el("div", { class: "span-all" }, [
      el("h3", { text: "Per application pages" }),
      el("label", { class: "check", for: "followFocus" }, [followBox, el("span", { text: "Follow the window that has focus" })]),
      el("p", { class: "muted", text: "Give a page the name of the program it belongs to and the deck brings it up when that program is in front. It is matched against the window class, the program name and the title, ignoring case, and it is a pattern: firefox|chromium matches either. A page left empty is only reached by hand, and the first empty one is where the deck goes when nothing matches. Switching page yourself sticks until you change window." }),
      el("div", { class: "row wrap" }, matchRows),
      numberField("deck", "focus_poll_ms", "Check the focused window every (ms)", 50, 5000,
        "200 is five times a second, which feels immediate and costs almost nothing."),
      el("p", { class: "muted", text: focusState.window ? "In front now: " + focusState.window
        : "Nothing is being watched. On Linux this needs an X11 session with xprop installed; a Wayland session cannot be asked." }),
    ]));

    // Strip panels: the visible area is a measurement of the deck, shared by all three
    const visible = doc.strip_visible || { width: 78, height: 78 };
    const visibleField = (name, labelText) => {
      const input = el("input", { type: "number", min: 8, max: 200, value: visible[name] });
      input.addEventListener("input", () => change(d => {
        d.strip_visible = d.strip_visible || { width: 78, height: 78 };
        d.strip_visible[name] = parseInt(input.value, 10) || visible[name];
        updateCropOverlay();
      }, { silent: true }));
      return el("div", { class: "field" }, [el("label", { text: labelText }), input]);
    };
    root.append(el("div", {}, [
      el("h3", { text: "Strip panels" }),
      el("p", { class: "muted", text: "The three panels on the right show less than the picture they are sent. Clock, date and weather are drawn to fit this visible area; the shaded edges on the deck view are the part that is cut off." }),
      el("div", { class: "row" }, [visibleField("width", "Visible width (px)"), visibleField("height", "Visible height (px)")]),
      el("div", { class: "help", html: "Measure it with <span class='kbd'>deckplate calibrate-strip</span> while the daemon is stopped." }),
    ]));

    // Pictures
    const gallery = el("div", { class: "gallery" });
    if (!imageDetails.length) gallery.append(el("p", { class: "muted", text: "No pictures uploaded yet. Upload one from a key's picture picker." }));
    for (const detail of imageDetails) {
      const used = detail.used_by.length > 0;
      const remove = el("button", { type: "button", class: "ghost small danger", text: "Delete", disabled: used,
        title: used ? "Remove it from the keys that use it first" : "Delete this picture" });
      remove.addEventListener("click", () => deleteImage(detail));
      gallery.append(el("div", { class: "gallery-item" }, [
        el("img", { src: "/api/images/" + encodeURIComponent(detail.name), alt: "" }),
        el("div", { class: "gallery-text" }, [
          el("div", { class: "gallery-name", text: detail.name, title: detail.name }),
          el("div", { class: "help", text: Math.max(1, Math.round(detail.bytes / 1024)) + " KB" + (used ? " · used by " + detail.used_by.join(", ") : " · not used") }),
        ]),
        remove,
      ]));
    }
    root.append(el("div", { class: "span-all" }, [
      el("h3", { text: "Pictures" }),
      el("p", { class: "muted", html: "Uploaded pictures live in <span class='kbd'>" + (imageFolder || "the images folder next to the config") + "</span>. A picture in use cannot be deleted until no key shows it." }),
      gallery,
    ]));

    // Server, read only
    root.append(el("div", {}, [
      el("h3", { text: "Local API" }),
      el("p", { class: "muted", html: "Listening on <span class='kbd'>" + doc.server.bind + ":" + doc.server.port + "</span>. Change <span class='kbd'>[server]</span> in the config file and restart the daemon to alter it." }),
      el("p", { class: "muted", html: "Config file: <span class='kbd'>" + (state && state.config_path ? state.config_path : "") + "</span>" }),
    ]));
  }

  /**
   * <summary>Point a page switching action at a page's new name.</summary>
   * <param name="action">Any action, or null.</param>
   * <param name="oldName">The name the page had.</param>
   * <param name="newName">The name it has now.</param>
   * <remarks>Recurses into the steps of a multi action, so a rename reaches a
   * page switch buried inside one. Relative switches, next and previous, are
   * left alone: they are not names.</remarks>
   */
  function retarget(action, oldName, newName) {
    if (!action) return;
    if (action.type === "page" && action.page === oldName) action.page = newName;
    if (action.type === "multi") for (const step of action.steps || []) retarget(step, oldName, newName);
  }

  // ---- Events from the daemon ---------------------------------------------

  /**
   * <summary>Open the event stream and keep the page in step with the deck.</summary>
   * <remarks>
   * An EventSource reconnects by itself, so an error is shown and otherwise not
   * acted on. The daemon also holds the stream open with no deck plugged in: a
   * "waiting" event says so, and a "hello" arrives by itself when the deck
   * comes back, which is why nothing here tries to reconnect.
   *
   * A config event is the one that needs care. Our own save comes back as one,
   * and reloading then would throw away the editor pane along with the focus
   * and any capture in progress, so only an edit made outside the page reloads.
   * </remarks>
   */
  function connectEvents() {
    const source = new EventSource("/api/events");
    source.onopen = () => setStatus(state && state.device ? state.device.name : "connected", "ok");
    source.onerror = () => setStatus("reconnecting", "");
    source.onmessage = async event => {
      const data = JSON.parse(event.data);
      switch (data.type) {
        case "hello":
          state = data.state;
          noteAnimated(state.animated);
          fitDeck();
          renderHeader();
          renderTabs();
          refreshTiles();
          updateCropOverlay();
          break;
        case "key":
          flash(data.row, data.column, data.pressed);
          break;
        case "tiles":
          noteAnimated(data.animated);
          refreshTiles(data.columns);
          break;
        case "page":
          state.page = data.page;
          state.page_index = data.page_index;
          renderTabs();
          renderEditor();
          break;
        case "brightness":
          state.brightness = data.value;
          renderHeader();
          break;
        case "sleep":
          state.asleep = data.asleep;
          renderHeader();
          break;
        case "config": {
          // Our own save comes back as a config event too. Rebuilding the
          // editor then would throw away focus and any capture in progress,
          // so only an edit made elsewhere (a text editor on the file) reloads.
          try { state = await apiGet("/api/state"); } catch (e) { /* keep the old state */ }
          noteAnimated(state.animated);
          fitDeck();
          const ours = lastSaved !== null && JSON.stringify(doc) === lastSaved;
          if (ours) {
            renderHeader();
            renderTabs();
          } else {
            await loadDocument();
            renderAll();
          }
          refreshTiles();
          break;
        }
        case "waiting":
          // The stream is held open with no deck connected. It stays open and
          // a "hello" arrives by itself when the deck comes back, so there is
          // nothing to reconnect here.
          setStatus("deck disconnected", "bad");
          break;
        case "closed":
          setStatus("deck disconnected", "bad");
          break;
      }
    };
  }

  // ---- Loading -------------------------------------------------------------

  /**
   * <summary>Show a failure in the header's save area and log it.</summary>
   * <remarks>That area is used because it is the one part of the page that is
   * always visible and already means something went wrong.</remarks>
   */
  function showError(err) {
    setSave(err.message, "bad");
    console.error(err);
  }

  /** <summary>The pictures in the images folder, each with its size and the keys that use it.</summary> */
  let imageDetails = [];
  let imageFolder = "";

  /**
   * <summary>Fetch the picture list from the daemon.</summary>
   * <remarks>Never throws: a failure leaves the lists empty and the pickers
   * offering nothing, rather than stopping whatever was being done.</remarks>
   */
  async function loadImages() {
    try {
      const data = await apiGet("/api/images");
      images = data.images;
      imageDetails = data.details || [];
      imageFolder = data.folder || "";
    } catch (e) { images = []; imageDetails = []; }
  }

  /**
   * <summary>Confirm, then delete a picture from the images folder.</summary>
   * <param name="detail">The entry from the picture list.</param>
   * <remarks>The file itself goes and Undo cannot bring it back, which is why
   * this is one of the few places in the page that asks first. The daemon
   * refuses to delete a picture a key still shows, and the button is disabled
   * for one too.</remarks>
   */
  async function deleteImage(detail) {
    const yes = await dialog({
      title: "Delete " + detail.name + "?",
      message: "The file is removed from the pictures folder. This cannot be undone.",
      confirmText: "Delete",
      danger: true,
    });
    if (!yes) return;
    try {
      await apiSend("DELETE", "/api/images/" + encodeURIComponent(detail.name), "{}");
      await loadImages();
      renderSettings();
      renderEditor();
    } catch (err) {
      showError(err);
    }
  }

  async function loadDocument() {
    doc = await apiGet("/api/document");
  }

  /**
   * <summary>Start the page: build the grid, wire the header, then load and connect.</summary>
   * <remarks>
   * With no deck connected the document cannot be fetched, so the editor says
   * so and this calls itself again a few seconds later rather than leaving a
   * dead page behind. That is the reconnect; there is no other retry loop.
   *
   * The grid is built before anything is loaded so there is something to look
   * at at once. The deck is resized from a ResizeObserver on its pane rather
   * than the window's resize event, because the pane also changes width when
   * the editor appears or the header wraps, and neither of those resizes the
   * window.
   * </remarks>
   */
  async function init() {
    buildGrid();
    $("#undoBtn").addEventListener("click", undo);
    $("#settingsBtn").addEventListener("click", openSettings);

    // Two more ways back to nothing selected, so the page settings and the
    // wallpaper are always one obvious move away: click the empty space around
    // the keys, or press Escape.
    const deckPane = document.querySelector(".deck-pane");
    if (deckPane) {
      deckPane.addEventListener("click", e => { if (!e.target.closest(".cell")) deselect(); });
    }
    document.addEventListener("keydown", e => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      // A dialog, the settings window or a key capture owns Escape while it is
      // open, and each of those marks the event handled before this runs.
      if (document.querySelector(".modal-backdrop")) return;
      deselect();
    });
    $("#sleepBtn").addEventListener("click", () => {
      apiPost(state && state.asleep ? "/api/wake" : "/api/sleep").catch(showError);
    });
    let brightnessTimer = null;
    $("#brightness").addEventListener("input", () => {
      $("#brightnessValue").value = $("#brightness").value;
      clearTimeout(brightnessTimer);
      brightnessTimer = setTimeout(() => apiPost("/api/brightness", { value: parseInt($("#brightness").value, 10) }).catch(showError), 150);
    });
    document.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z" && !capturing) {
        const tag = document.activeElement && document.activeElement.tagName;
        if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") { event.preventDefault(); undo(); }
      }
      if (event.key === "Escape" && capturing) stopCapture();
    });

    try {
      state = await apiGet("/api/state");
      noteAnimated(state.animated);
    } catch (err) {
      setStatus("deck not connected", "bad");
    }
    try {
      await loadDocument();
      await loadImages();
    } catch (err) {
      $("#editor").innerHTML = "";
      $("#editor").append(el("p", { class: "muted", text: "The daemon is running but the deck is not connected. Plug it in; this page will follow." }));
      setStatus("deck not connected", "bad");
      setTimeout(init, 3000);
      return;
    }
    renderAll();
    fitDeck();
    refreshTiles();
    connectEvents();

    // The deck follows the window. A ResizeObserver rather than a resize
    // listener because the pane also changes width when the editor appears or
    // the header wraps, neither of which resizes the window.
    const pane = document.querySelector(".deck-pane");
    if (pane && window.ResizeObserver) new ResizeObserver(() => fitDeck()).observe(pane);
    window.addEventListener("resize", fitDeck);
  }

  init();
})();
