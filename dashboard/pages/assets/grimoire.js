/* ==========================================================================
   GRIMOIRE OS — shell for the five Ghost dashboard pages.
   Presentation only. This file:
     - never calls fetch/XHR, never touches /api, never reads page data;
     - never replaces a page element — it only MOVES existing nodes into window
       frames, so every id, data-role, inline handler and listener survives;
     - stores only layout conveniences (window positions, boot-played flag) in
       this browser's localStorage/sessionStorage.
   If this file fails to load, each page still works with just the CSS skin.
   ========================================================================== */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  var PAGE = root.getAttribute("data-gos") || "page";
  var LAYOUT_KEY = "gos-layout-v1", BOOT_KEY = "gos-booted";

  function ssGet(k) { try { return window.sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { window.sessionStorage.setItem(k, v); } catch (e) {} }
  function lsGet(k) { try { return JSON.parse(window.localStorage.getItem(k)); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  function lsDel(k) { try { window.localStorage.removeItem(k); } catch (e) {} }
  function el(tag, cls, html) { var n = doc.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; }

  root.classList.add("gos");
  var needBoot = ssGet(BOOT_KEY) !== "1";
  if (needBoot) root.classList.add("gos-preboot");
  // safety net: never leave the page hidden, whatever happens below
  setTimeout(function () { root.classList.remove("gos-preboot"); }, 4000);

  /* ---------------- moon ---------------- */
  var SYNODIC = 29.530588853;
  var NEW_MOON_REF = Date.UTC(2000, 0, 6, 18, 14, 0);     // a known new moon
  function moonNow(d) {
    var days = ((d || new Date()).getTime() - NEW_MOON_REF) / 86400000;
    var phase = ((days % SYNODIC) + SYNODIC) % SYNODIC / SYNODIC;   // 0 new, .5 full
    var lit = (1 - Math.cos(2 * Math.PI * phase)) / 2;
    var names = ["New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous", "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent"];
    var idx;
    if (phase < 0.03 || phase > 0.97) idx = 0;
    else if (phase < 0.22) idx = 1; else if (phase < 0.28) idx = 2; else if (phase < 0.47) idx = 3;
    else if (phase < 0.53) idx = 4; else if (phase < 0.72) idx = 5; else if (phase < 0.78) idx = 6; else idx = 7;
    return { phase: phase, lit: lit, name: names[idx], pct: Math.round(lit * 100) };
  }
  function southern() {
    // The lit side of the moon flips south of the equator. Guess from the device time zone only.
    try {
      var tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      return /^(Australia|Antarctica)\//.test(tz) || /^Pacific\/(Auckland|Chatham|Fiji|Tongatapu|Apia|Noumea|Port_Moresby)/.test(tz) ||
        /^Africa\/(Johannesburg|Maputo|Harare|Lusaka|Windhoek|Gaborone|Maseru|Mbabane)/.test(tz) ||
        /^America\/(Sao_Paulo|Argentina|Santiago|Montevideo|Asuncion|La_Paz|Lima)/.test(tz);
    } catch (e) { return false; }
  }
  function moonSVG(m) {
    var r = 9, p = m.phase, waxing = p < 0.5;
    var k = Math.cos(2 * Math.PI * p);                  // 1 new .. -1 full
    var rx = Math.abs(k) * r;
    // lit limb on the right while waxing (northern view); terminator is an ellipse of radius rx
    var sweepOuter = waxing ? 1 : 0;
    var sweepInner = (waxing ? (k > 0 ? 0 : 1) : (k > 0 ? 1 : 0));
    var d = "M0," + (-r) + " A" + r + "," + r + " 0 0 " + sweepOuter + " 0," + r +
            " A" + rx.toFixed(2) + "," + r + " 0 0 " + sweepInner + " 0," + (-r) + " Z";
    var flip = southern() ? ' transform="scale(-1,1)"' : "";
    return '<svg viewBox="-11 -11 22 22" aria-hidden="true"><circle r="9" fill="#1b1622" stroke="rgba(201,204,217,0.45)" stroke-width="0.6"/>' +
           '<path d="' + d + '" fill="#dfe2ee"' + flip + "/></svg>";
  }

  /* ---------------- sigil (wallpaper + boot) ---------------- */
  var VALUES = ["Allure", "Ambition", "Power", "Strength", "Intelligence", "Magic"];
  function sigilSVG(animated) {
    var a = animated ? ' pathLength="1"' : "";
    var s = '<svg class="gos-sigil" viewBox="-300 -300 600 600" aria-hidden="true">';
    s += '<circle r="290" class="draw"' + a + "/>";
    s += '<circle r="272" class="thin" stroke-dasharray="1 7"/>';
    s += '<circle r="226" class="draw d2"' + a + "/>";
    s += '<polygon class="draw d3"' + a + ' points="0,-226 195.7,113 -195.7,113"/>';
    s += '<polygon class="draw d3 blood"' + a + ' points="0,226 195.7,-113 -195.7,-113"/>';
    s += '<circle r="113" class="draw d4"' + a + "/>";
    s += '<circle r="98" class="thin" stroke-dasharray="1 5"/>';
    // crescent at the heart
    s += '<path class="core fillg" d="M0,-52 A52,52 0 1 0 0,52 A72,72 0 0 1 0,-52 Z" opacity="0.85"/>';
    s += '<circle class="core fillg" r="4" cx="14" cy="0"/>';
    for (var i = 0; i < 6; i++) {
      var ang = i * 60;
      s += '<g transform="rotate(' + ang + ')">';
      s += '<path class="fillg" d="M0,-232 l5,6 l-5,6 l-5,-6 Z"/>';
      s += '<text text-anchor="middle" y="-243" style="animation-delay:' + (0.5 + i * 0.16).toFixed(2) + 's">' + VALUES[i].toUpperCase() + "</text>";
      s += "</g>";
      s += '<line class="thin" x1="0" y1="-272" x2="0" y2="-290" transform="rotate(' + (ang + 30) + ')"/>';
    }
    return s + "</svg>";
  }

  /* ---------------- boot sequence ---------------- */
  function runBoot(force) {
    if (!force && !needBoot) return;
    var reduce = false;
    try { reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}
    var m = moonNow();
    var boot = el("div", "gos-boot");
    boot.setAttribute("role", "button");
    boot.setAttribute("aria-label", "Grimoire OS is waking. Tap to skip.");
    boot.innerHTML = sigilSVG(true) +
      '<div class="gos-boot-name">Grimoire OS</div>' +
      '<div class="gos-boot-log"></div>' +
      '<div class="gos-boot-skip">tap to enter</div>';
    doc.body.appendChild(boot);
    root.classList.remove("gos-preboot");
    root.classList.add("gos-booting");
    var log = boot.querySelector(".gos-boot-log");
    var lines = [
      "waking the wards&hellip;",
      "lighting the candles&hellip;",
      "calling the familiar&hellip;",
      "opening the ledger of rituals&hellip;",
      "moon &middot; <b>" + m.name.toLowerCase() + "</b> &middot; " + m.pct + "% lit",
      '<span>' + VALUES.join(" &middot; ").toLowerCase() + "</span>"
    ];
    var timers = [], done = false;
    lines.forEach(function (t, i) {
      timers.push(setTimeout(function () { log.appendChild(el("div", i === lines.length - 1 ? "v" : "", t)); }, reduce ? 0 : 320 + i * 300));
    });
    function finish() {
      if (done) return; done = true;
      timers.forEach(clearTimeout);
      ssSet(BOOT_KEY, "1"); needBoot = false;
      boot.classList.add("gos-out");
      root.classList.remove("gos-booting");
      root.classList.add("gos-entered");
      doc.removeEventListener("keydown", finish, true);
      setTimeout(function () { if (boot.parentNode) boot.parentNode.removeChild(boot); }, 650);
      setTimeout(function () { root.classList.remove("gos-entered"); }, 1800);
    }
    boot.addEventListener("click", finish);
    boot.addEventListener("touchend", function (e) { e.preventDefault(); finish(); }, { passive: false });
    doc.addEventListener("keydown", finish, true);
    timers.push(setTimeout(finish, reduce ? 900 : 3300));
  }

  /* ---------------- taskbar ---------------- */
  var ICONS = {
    dashboard: '<circle cx="12" cy="12" r="6.3"/><circle cx="12" cy="12" r="1.1" fill="currentColor"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22"/>',
    habits:    '<path d="M6 3.5h11a1.5 1.5 0 0 1 1.5 1.5v15.5H7.5A1.5 1.5 0 0 1 6 19z"/><path d="M6 19a1.5 1.5 0 0 1 1.5-1.5h11M9.5 7.5h5.5M9.5 10.5h5.5M9.5 13.5h3"/>',
    bills:     '<ellipse cx="12" cy="7" rx="7" ry="3"/><path d="M5 7v5c0 1.7 3.1 3 7 3s7-1.3 7-3V7M5 12v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5"/>',
    tarot:     '<rect x="6" y="3" width="12" height="18" rx="1.5"/><path d="M12 8l1.2 2.8L16 12l-2.8 1.2L12 16l-1.2-2.8L8 12l2.8-1.2z"/>',
    horoscope: '<path d="M15.5 3.5a8.5 8.5 0 1 0 5 15A9 9 0 0 1 15.5 3.5z"/><path d="M18.5 6.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z"/>'
  };
  var APPS = [
    { id: "dashboard", name: "Command", full: "Command Center", href: "/" },
    { id: "habits", name: "Rituals", full: "Habits & Todos — the ledger of rituals", href: "ghost-habits.html" },
    { id: "bills", name: "Coffers", full: "Bills", href: "ghost-bills.html" },
    { id: "tarot", name: "Tarot", full: "Tarot", href: "ghost-tarot.html" },
    { id: "horoscope", name: "Stars", full: "Horoscope", href: "ghost-horoscope.html" }
  ];
  var WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var bar, tray, menu, startBtn, badge;

  function buildTaskbar() {
    bar = el("nav", "gos-taskbar");
    bar.setAttribute("aria-label", "Grimoire OS taskbar");
    startBtn = el("button", "gos-start", '<svg viewBox="0 0 24 24">' + ICONS.dashboard + '<path d="M12 5.7l5.5 9.5h-11z" stroke-width="0.8"/></svg><span>Grimoire</span><span class="gos-badge"></span>');
    startBtn.type = "button";
    startBtn.setAttribute("aria-expanded", "false");
    startBtn.setAttribute("aria-label", "Grimoire OS menu");
    badge = startBtn.querySelector(".gos-badge");
    bar.appendChild(startBtn);

    var apps = el("div", "gos-apps");
    var demo = /(?:^|[?&])demo=1(?:&|$)/.test(location.search) ? "?demo=1" : "";
    APPS.forEach(function (a) {
      var link = el("a", "gos-app-link" + (a.id === PAGE ? " on" : ""), '<svg viewBox="0 0 24 24">' + ICONS[a.id] + "</svg><span>" + a.name + "</span>");
      link.href = a.href + (a.id === "dashboard" || a.id === "bills" || a.id === "horoscope" ? demo : "");
      link.title = a.full;
      link.setAttribute("data-gos-app", a.id);
      if (a.id === PAGE) link.setAttribute("aria-current", "page");
      apps.appendChild(link);
    });
    bar.appendChild(apps);

    tray = el("div", "gos-tray");
    bar.appendChild(tray);

    var status = el("div", "gos-status", '<span class="gos-moon"></span><span class="gos-clock"><b></b><i></i></span>');
    bar.appendChild(status);
    doc.body.appendChild(bar);

    menu = el("div", "gos-menu");
    menu.setAttribute("role", "menu");
    doc.body.appendChild(menu);
    startBtn.addEventListener("click", function (e) { e.stopPropagation(); toggleMenu(); });
    doc.addEventListener("click", function (e) { if (menu.classList.contains("open") && !menu.contains(e.target)) toggleMenu(false); });
    doc.addEventListener("keydown", function (e) { if (e.key === "Escape" && menu.classList.contains("open")) toggleMenu(false); });

    function tick() {
      var d = new Date(), m = moonNow(d);
      var moon = status.querySelector(".gos-moon");
      moon.innerHTML = moonSVG(m) + "<span>" + m.name + "</span>";
      moon.title = m.name + " · " + m.pct + "% lit";
      var h = d.getHours(), mi = d.getMinutes();
      status.querySelector(".gos-clock b").textContent = (h < 10 ? "0" : "") + h + ":" + (mi < 10 ? "0" : "") + mi;
      status.querySelector(".gos-clock i").textContent = WD[d.getDay()] + " " + d.getDate() + " " + MO[d.getMonth()];
    }
    tick();
    setInterval(tick, 20000);
  }

  function toggleMenu(open) {
    var want = open == null ? !menu.classList.contains("open") : open;
    if (want) renderMenu();
    menu.classList.toggle("open", want);
    startBtn.setAttribute("aria-expanded", want ? "true" : "false");
  }
  function menuBtn(label, fn, cls, st) {
    var b = el("button", cls || "", '<span class="dot"></span><span>' + label + "</span>" + (st ? '<span class="st">' + st + "</span>" : ""));
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", function (e) { e.stopPropagation(); fn(); });
    return b;
  }
  function renderMenu() {
    var m = moonNow();
    menu.innerHTML = "";
    menu.appendChild(el("h4", "", "Grimoire OS"));
    menu.appendChild(el("div", "gos-menu-sub", m.name.toLowerCase() + " · " + m.pct + "% lit"));
    if (wins.length) {
      menu.appendChild(el("div", "gos-menu-label", "Windows"));
      wins.forEach(function (w) {
        var st = w.state.closed ? "closed" : (w.state.min ? "minimised" : "open");
        menu.appendChild(menuBtn(w.name, function () {
          if (w.state.closed || w.state.min) reveal(w); else { w.state.closed = true; applyState(w); save(); }
          renderMenu();
        }, w.state.closed ? "is-closed" : (w.state.min ? "" : "is-open"), st));
      });
      menu.appendChild(el("div", "gos-sep"));
      menu.appendChild(menuBtn("Restore all windows", function () { wins.forEach(function (w) { w.state.closed = false; w.state.min = false; applyState(w); }); save(); renderMenu(); }));
      menu.appendChild(menuBtn("Reset the desk layout", function () { lsDel(LAYOUT_KEY); wins.forEach(function (w) { w.state = defaults(w); w.win.classList.remove("gos-max"); applyState(w); }); placeAll(); renderMenu(); }));
    }
    menu.appendChild(menuBtn("Replay the awakening", function () { toggleMenu(false); runBoot(true); }));
  }

  /* ---------------- window manager (Command Center) ---------------- */
  var wins = [], desk = null, zTop = 20, deskMQ = null, isDesk = false, saveTimer = null;
  // default tiles as fractions of the desk: [x, y, w, h]
  var WIN_DEFS = [
    { id: "habits",       sel: "#cardHabits",    name: "Ledger of Rituals", glyph: "✦", box: [0.00, 0.00, 0.25, 0.58] },
    { id: "todos",        sel: "#cardTodos",     name: "Grimoire",          glyph: "❧", box: [0.25, 0.00, 0.25, 0.58] },
    { id: "timeline",     sel: "#cardTimeline",  name: "Dispatches",        glyph: "✉", box: [0.50, 0.00, 0.24, 0.58] },
    { id: "food",         sel: "#cardFood",      name: "Larder",            glyph: "◐", box: [0.74, 0.00, 0.26, 0.58] },
    { id: "overview",     sel: "#cardOverview",  name: "Wards",             glyph: "◈", box: [0.00, 0.58, 0.16, 0.42] },
    { id: "monitor",      via: "#monGauges",     name: "Scrying Glass",     glyph: "◎", box: [0.16, 0.58, 0.17, 0.19] },
    { id: "spending",     via: "#budgetList",    name: "Coffers",           glyph: "⛁", box: [0.16, 0.77, 0.17, 0.23] },
    { id: "momentum",     sel: "#cardMomentum",  name: "Momentum",          glyph: "➶", box: [0.33, 0.58, 0.21, 0.42] },
    { id: "integrations", via: "#intGrid",       name: "Bindings",          glyph: "⛓", box: [0.54, 0.58, 0.20, 0.42] },
    { id: "decisions",    sel: "#cardDecisions", name: "Crossroads",        glyph: "⚑", box: [0.74, 0.58, 0.26, 0.42] }
  ];

  function defaults(w) { return { x: w.def.box[0], y: w.def.box[1], w: w.def.box[2], h: w.def.box[3], min: false, closed: false }; }

  function buildWindows() {
    desk = doc.querySelector(".content");
    if (!desk) return;
    var saved = lsGet(LAYOUT_KEY) || {};
    WIN_DEFS.forEach(function (def, i) {
      var card = def.sel ? doc.querySelector(def.sel) : null;
      if (!card && def.via) { var inner = doc.querySelector(def.via); card = inner ? inner.closest(".card") : null; }
      if (!card || !card.parentNode) return;
      var win = el("section", "gos-window");
      win.setAttribute("data-gos-win", def.id);
      win.style.setProperty("--gos-i", i);
      var barEl = el("header", "gos-titlebar");
      barEl.appendChild(el("span", "gos-glyph", def.glyph));
      var title = null;
      for (var c = card.firstElementChild; c; c = c.nextElementSibling) { if (c.classList.contains("card-title")) { title = c; break; } }
      if (title) barEl.appendChild(title);                 // the card's own title node, moved (ids inside it survive)
      else barEl.appendChild(el("div", "gos-wintitle", def.name));
      var btns = el("div", "gos-winbtns");
      var bMin = el("button", "gos-winbtn gos-minbtn", "&#x2212;"); bMin.type = "button"; bMin.title = "Minimise"; bMin.setAttribute("aria-label", "Minimise " + def.name);
      var bClose = el("button", "gos-winbtn gos-close", "&#x2715;"); bClose.type = "button"; bClose.title = "Close (reopen from the Grimoire menu)"; bClose.setAttribute("aria-label", "Close " + def.name);
      btns.appendChild(bMin); btns.appendChild(bClose); barEl.appendChild(btns);
      var body = el("div", "gos-winbody");
      card.parentNode.insertBefore(win, card);
      body.appendChild(card);                              // the original card node, moved
      win.appendChild(barEl); win.appendChild(body);

      var w = { def: def, id: def.id, name: def.name, win: win, bar: barEl, state: null };
      var s = saved[def.id];
      w.state = defaults(w);
      if (s && typeof s === "object") {
        ["x", "y", "w", "h"].forEach(function (k) { if (typeof s[k] === "number" && isFinite(s[k])) w.state[k] = s[k]; });
        w.state.min = !!s.min; w.state.closed = !!s.closed;
      }
      wins.push(w);

      bMin.addEventListener("click", function (e) { e.stopPropagation(); w.state.min = !w.state.min; applyState(w); save(); });
      bClose.addEventListener("click", function (e) { e.stopPropagation(); w.state.closed = true; applyState(w); save(); });
      win.addEventListener("pointerdown", function () { focus(w); }, true);
      barEl.addEventListener("dblclick", function (e) { if (e.target.closest(".gos-winbtns")) return; if (isDesk) win.classList.toggle("gos-max"); });
      enableDrag(w);
      if (window.ResizeObserver) {
        new ResizeObserver(function () {
          if (!isDesk || w.dragging || w.applying || win.classList.contains("gos-max") || w.state.min || w.state.closed) return;
          var r = deskRect(); if (!r.width || !r.height) return;
          var nw = win.offsetWidth / r.width, nh = win.offsetHeight / r.height;
          if (Math.abs(nw - w.state.w) > 0.004 || Math.abs(nh - w.state.h) > 0.004) { w.state.w = nw; w.state.h = nh; saveSoon(); }
        }).observe(win);
      }
    });

    try { deskMQ = window.matchMedia("(min-width: 1151px) and (hover: hover) and (pointer: fine)"); } catch (e) { deskMQ = null; }
    function setMode() {
      isDesk = !!(deskMQ && deskMQ.matches);
      root.classList.toggle("gos-desk", isDesk);
      root.classList.toggle("gos-stack", !isDesk);
      placeAll();
    }
    if (deskMQ) { if (deskMQ.addEventListener) deskMQ.addEventListener("change", setMode); else if (deskMQ.addListener) deskMQ.addListener(setMode); }
    window.addEventListener("resize", function () { if (isDesk) placeAll(); });
    setMode();
    wins.forEach(applyState);
    if (wins.length) focus(wins[0]);

    // the index (left nav) also raises the matching window; the page's own handler still runs
    var NAV = { "Timeline": "timeline", "Spending": "spending", "Nutrition": "overview", "Vitals": "monitor", "Integrations": "integrations" };
    Array.prototype.forEach.call(doc.querySelectorAll('[data-role="nav-item"]'), function (item) {
      if (item.tagName === "A") return;
      item.addEventListener("click", function () {
        var label = item.textContent, key = Object.keys(NAV).filter(function (k) { return label.indexOf(k) >= 0; })[0];
        var w = key ? byId(NAV[key]) : null;
        if (w) { reveal(w); if (!isDesk) w.win.scrollIntoView({ behavior: "smooth", block: "start" }); }
      });
    });
  }
  function byId(id) { for (var i = 0; i < wins.length; i++) if (wins[i].id === id) return wins[i]; return null; }
  function deskRect() { return desk.getBoundingClientRect(); }
  function place(w) {
    var s = w.win.style;
    if (!isDesk) { s.left = s.top = s.width = s.height = ""; return; }
    var r = deskRect(), gap = 6;
    w.applying = true;
    s.left = Math.round(w.state.x * r.width) + "px";
    s.top = Math.round(w.state.y * r.height) + "px";
    s.width = Math.max(150, Math.round(w.state.w * r.width) - gap) + "px";
    s.height = Math.max(96, Math.round(w.state.h * r.height) - gap) + "px";
    setTimeout(function () { w.applying = false; }, 60);
  }
  function placeAll() { wins.forEach(place); }
  function applyState(w) {
    w.win.classList.toggle("gos-min", !!w.state.min);
    w.win.classList.toggle("gos-closed", !!w.state.closed);
    var b = w.win.querySelector(".gos-minbtn");
    if (b) b.title = w.state.min ? "Restore" : "Minimise";
    place(w);
    renderTray();
  }
  function focus(w) {
    wins.forEach(function (o) { o.win.classList.toggle("gos-focus", o === w); });
    w.win.style.zIndex = String(++zTop);
  }
  function reveal(w) {
    w.state.min = false; w.state.closed = false;
    applyState(w); focus(w); save();
    w.win.classList.remove("gos-flash"); void w.win.offsetWidth; w.win.classList.add("gos-flash");
  }
  function renderTray() {
    if (!tray) return;
    tray.innerHTML = "";
    var closed = 0;
    wins.forEach(function (w) {
      if (w.state.closed) { closed++; return; }
      if (!w.state.min) return;
      var chip = el("button", "gos-chip", w.name);
      chip.type = "button"; chip.title = "Restore " + w.name;
      chip.addEventListener("click", function () { reveal(w); });
      tray.appendChild(chip);
    });
    if (badge) { badge.textContent = closed ? String(closed) : ""; badge.style.display = closed ? "block" : "none"; badge.title = closed + " closed window(s)"; }
  }
  function save() {
    clearTimeout(saveTimer);
    var out = {};
    wins.forEach(function (w) { out[w.id] = { x: w.state.x, y: w.state.y, w: w.state.w, h: w.state.h, min: w.state.min, closed: w.state.closed }; });
    lsSet(LAYOUT_KEY, out);
  }
  function saveSoon() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 200); }   // resizing fires continuously
  function enableDrag(w) {
    var start = null;
    w.bar.addEventListener("pointerdown", function (e) {
      if (!isDesk || e.button !== 0 || w.win.classList.contains("gos-max")) return;
      if (e.target.closest(".gos-winbtns, input, select, textarea, a, button")) return;
      var r = deskRect();
      start = { px: e.clientX, py: e.clientY, x: w.win.offsetLeft, y: w.win.offsetTop, dw: r.width, dh: r.height };
      w.dragging = true; w.win.classList.add("gos-drag");
      try { w.bar.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    w.bar.addEventListener("pointermove", function (e) {
      if (!start) return;
      var nx = start.x + (e.clientX - start.px), ny = start.y + (e.clientY - start.py);
      nx = Math.min(Math.max(nx, 80 - w.win.offsetWidth), start.dw - 80);      // keep a handle on screen
      ny = Math.min(Math.max(ny, 0), start.dh - 30);
      w.win.style.left = nx + "px"; w.win.style.top = ny + "px";
    });
    function end(e) {
      if (!start) return;
      w.state.x = w.win.offsetLeft / start.dw; w.state.y = w.win.offsetTop / start.dh;
      start = null; w.dragging = false; w.win.classList.remove("gos-drag");
      try { w.bar.releasePointerCapture(e.pointerId); } catch (err) {}
      save();
    }
    w.bar.addEventListener("pointerup", end);
    w.bar.addEventListener("pointercancel", end);
  }

  /* ---------------- app window (Habits / Bills / Tarot / Horoscope) ---------------- */
  var APP_META = {
    habits:    { title: "Ledger of Rituals", plain: "habits & todos", glyph: "✦" },
    bills:     { title: "The Coffers",       plain: "bills",          glyph: "⛁" },
    tarot:     { title: "The Cards",         plain: "tarot",          glyph: "✧" },
    horoscope: { title: "The Stars",         plain: "horoscope",      glyph: "☾" }
  };
  function buildAppWindow() {
    var meta = APP_META[PAGE]; if (!meta) return;
    var movable = [];
    for (var c = doc.body.firstElementChild; c; c = c.nextElementSibling) {
      if (/^(SCRIPT|STYLE|LINK|TEMPLATE|NOSCRIPT)$/.test(c.tagName)) continue;
      if (c.matches(".modal-bg, .modal-backdrop, .chat-overlay, .gos-boot, .gos-taskbar, .gos-menu, .gos-wallpaper")) continue;
      movable.push(c);
    }
    if (!movable.length) return;
    var win = el("main", "gos-window gos-app gos-focus");
    var barEl = el("header", "gos-titlebar");
    barEl.appendChild(el("span", "gos-glyph", meta.glyph));
    barEl.appendChild(el("div", "gos-wintitle", meta.title + ' <small class="gos-plain">' + meta.plain + "</small>"));
    var btns = el("div", "gos-winbtns");
    var back = doc.querySelector("a.backlink, a.back");
    var close = el("a", "gos-winbtn gos-close", "&#x2715;");
    close.href = back ? back.getAttribute("href") : "/";
    close.title = "Close — back to the Command Center";
    close.setAttribute("aria-label", "Close and return to the Command Center");
    btns.appendChild(close); barEl.appendChild(btns);
    var body = el("div", "gos-winbody");
    doc.body.insertBefore(win, movable[0]);
    movable.forEach(function (n) { body.appendChild(n); });     // original nodes, moved
    win.appendChild(barEl); win.appendChild(body);
  }

  /* ---------------- init ---------------- */
  function init() {
    try {
      var wp = el("div", "gos-wallpaper", sigilSVG(false));
      wp.setAttribute("aria-hidden", "true");
      doc.body.insertBefore(wp, doc.body.firstChild);
      if (PAGE === "dashboard") buildWindows(); else buildAppWindow();
      buildTaskbar();
      renderTray();
      runBoot(false);
    } catch (e) {
      if (window.console && console.warn) console.warn("Grimoire OS shell failed; the page still works without it:", e);
    } finally {
      root.classList.remove("gos-preboot");
    }
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init); else init();

  // small surface for tests / the console; nothing on the pages depends on it
  window.GrimoireOS = { moon: moonNow, replayBoot: function () { runBoot(true); }, page: PAGE };
})();
