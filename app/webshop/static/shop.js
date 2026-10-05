/* Bondom storefront behaviour. Vanilla, no dependencies, progressive:
   every page works without this file, it only adds convenience. */
(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;
  var reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  root.classList.remove("no-js");

  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $all(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }

  /* ------------------------------------------------ header border on scroll */
  var header = $("#site-header");
  if (header) {
    var onScroll = function () { header.classList.toggle("is-stuck", scrollY > 4); };
    onScroll();
    addEventListener("scroll", onScroll, { passive: true });
  }

  /* --------------------------------------------- "/" focuses product search */
  addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    var field = $all("[data-search-input]").filter(function (el) { return el.offsetParent !== null; })[0];
    if (field) { e.preventDefault(); field.focus(); field.select(); }
  });

  /* ------------------------------------------------------- account menu */
  $all("[data-menu-button]").forEach(function (btn) {
    var menu = doc.getElementById(btn.getAttribute("aria-controls"));
    if (!menu) return;
    function close(focusBack) {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      if (focusBack) btn.focus();
    }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = btn.getAttribute("aria-expanded") === "true";
      if (open) { close(false); return; }
      menu.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      var first = $("a, button", menu);
      if (first) first.focus();
    });
    doc.addEventListener("click", function (e) {
      if (!menu.hidden && !menu.contains(e.target)) close(false);
    });
    menu.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close(true);
    });
  });

  /* ----------------------------------------- Telegram login, mounted late */
  function mountTelegram(box) {
    if (!box || box.getAttribute("data-mounted")) return;
    box.setAttribute("data-mounted", "1");
    var s = doc.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login", box.getAttribute("data-telegram-login"));
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "10");
    s.setAttribute("data-auth-url", box.getAttribute("data-auth-url"));
    s.setAttribute("data-request-access", "write");
    box.appendChild(s);
  }
  $all(".tg-login[data-autoload]").forEach(mountTelegram);

  /* ---------------------------------------------------------- dialogs */
  function openDialog(id) {
    var dlg = doc.getElementById(id);
    if (!dlg) return;
    $all(".tg-login", dlg).forEach(mountTelegram);
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }
  doc.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-open-dialog]");
    if (opener) {
      e.preventDefault();
      openDialog(opener.getAttribute("data-open-dialog"));
      return;
    }
    var closer = e.target.closest("[data-close-dialog]");
    if (closer) {
      var dlg = closer.closest("dialog");
      if (dlg) dlg.close();
      return;
    }
    // Clicking the backdrop (the dialog element itself) closes it.
    if (e.target.tagName === "DIALOG" && e.target.open) e.target.close();
  });

  /* --------------------------------------------- copy to clipboard */
  doc.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    var done = function () {
      var label = $(".copy-label", btn);
      var prev = label ? label.textContent : null;
      btn.classList.add("is-copied");
      if (label) label.textContent = "Copied";
      setTimeout(function () {
        btn.classList.remove("is-copied");
        if (label && prev !== null) label.textContent = prev;
      }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = doc.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:absolute;left:-9999px";
      doc.body.appendChild(ta);
      ta.select();
      try { doc.execCommand("copy"); done(); } catch (err) {}
      doc.body.removeChild(ta);
    }
  });

  /* ------------------------------------------------------------ toasts */
  window.toast = function (message, kind) {
    var region = $("#toast-region");
    if (!region) return;
    var el = doc.createElement("div");
    el.className = "toast";
    el.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-' +
      (kind === "error" ? "alert" : kind === "ok" ? "check" : "info") + '"/></svg><span></span>';
    $("span", el).textContent = message;
    if (kind === "error") el.style.color = "var(--danger)";
    if (kind === "ok") el.style.color = "var(--green-soft)";
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  };

  /* -------------------------------------- forms that must submit once */
  $all("form[data-submit-once]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      var busy = form.getAttribute("data-submit-once") || "Working…";
      $all('button[type="submit"]', form).concat($all('button[form="' + form.id + '"]')).forEach(function (btn) {
        btn.setAttribute("aria-disabled", "true");
        btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> ' + busy;
      });
    });
  });

  /* ------------------------------------------------- quantity stepper */
  $all("[data-stepper]").forEach(function (wrap) {
    var input = $("input", wrap);
    var dec = $('[data-step="-1"]', wrap);
    var inc = $('[data-step="1"]', wrap);
    var min = parseInt(wrap.getAttribute("data-min"), 10) || 1;
    var max = parseInt(wrap.getAttribute("data-max"), 10) || min;
    var unit = parseFloat(wrap.getAttribute("data-unit")) || 0;
    var err = doc.getElementById(wrap.getAttribute("data-error"));
    var errText = err ? $("span", err) : null;
    var totals = $all("[data-total]");
    var qtyLabels = $all("[data-qty-label]");

    function clamp(showError) {
      var raw = parseInt(input.value, 10);
      var v = isNaN(raw) ? min : raw;
      var message = "";
      if (v > max) { v = max; message = "Only " + max + " can be ordered at once."; }
      else if (v < min) { v = min; message = "The minimum order is " + min + "."; }
      if (v !== raw && !(showError && input.value === "")) input.value = v;
      var total = "$" + (unit * v).toFixed(2);
      totals.forEach(function (t) { t.textContent = total; });
      qtyLabels.forEach(function (t) { t.textContent = v + (v === 1 ? " item" : " items"); });
      var invalid = showError && message !== "";
      if (err) err.hidden = !invalid;
      input.setAttribute("aria-invalid", invalid ? "true" : "false");
      if (invalid && errText) errText.textContent = message;
      if (dec) dec.disabled = v <= min;
      if (inc) inc.disabled = v >= max;
    }
    [dec, inc].forEach(function (b) {
      if (!b) return;
      b.addEventListener("click", function () {
        input.value = (parseInt(input.value, 10) || min) + parseInt(b.getAttribute("data-step"), 10);
        clamp(false);
      });
    });
    input.addEventListener("input", function () { clamp(true); });
    input.addEventListener("blur", function () { clamp(false); });
    clamp(false);
  });

  /* ----------- mobile buy bar: hidden while the real button is in view */
  var bar = $("[data-buy-bar]");
  var target = bar && doc.getElementById(bar.getAttribute("data-buy-bar"));
  if (bar && target && "IntersectionObserver" in window) {
    doc.body.classList.add("has-buy-bar");
    new IntersectionObserver(function (entries) {
      bar.classList.toggle("is-hidden", entries[0].isIntersecting);
    }, { threshold: 0.2 }).observe(target);
  }

  /* ---------------------- live filtering on /shop (server does the rest) */
  var liveInput = $("[data-live-filter]");
  if (liveInput) {
    var cards = $all("[data-filter-name]");
    var count = $("[data-result-count]");
    var empty = $("[data-live-empty]");
    var base = cards.length;
    liveInput.addEventListener("input", function () {
      var terms = liveInput.value.toLowerCase().split(/\s+/).filter(Boolean);
      var shown = 0;
      cards.forEach(function (card) {
        var hay = card.getAttribute("data-filter-name");
        var ok = terms.every(function (t) { return hay.indexOf(t) !== -1; });
        card.hidden = !ok;
        if (ok) shown++;
      });
      if (count) count.textContent = shown + (shown === 1 ? " product" : " products");
      if (empty && base > 0) empty.hidden = shown > 0;
    });
  }

  /* -------------------------------------- animated Telegram emoji (lazy) */
  var lottiePromise = null;
  function loadLottie() {
    if (window.lottie) return Promise.resolve(window.lottie);
    if (!lottiePromise) {
      lottiePromise = new Promise(function (resolve, reject) {
        var s = doc.createElement("script");
        s.src = "/web/static/vendor/lottie_light.min.js?v=5.13.0";
        s.onload = function () { window.lottie ? resolve(window.lottie) : reject(); };
        s.onerror = reject;
        doc.head.appendChild(s);
      });
    }
    return lottiePromise;
  }
  var webmOk = (function () {
    var v = doc.createElement("video");
    return !!(v.canPlayType && v.canPlayType('video/webm; codecs="vp9"'));
  })();

  function swap(el, node) {
    el.textContent = "";
    el.appendChild(node);
  }
  function hydrateEmoji(el) {
    var src = el.getAttribute("data-tge-src");
    if (!src) return;
    el.removeAttribute("data-tge-src");
    fetch(src, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) return;
      var type = r.headers.get("content-type") || "";
      if (type.indexOf("image/") === 0) {
        return r.blob().then(function (b) {
          var img = new Image();
          img.alt = "";
          img.decoding = "async";
          img.onload = function () { swap(el, img); };
          img.src = URL.createObjectURL(b);
        });
      }
      // Motion: reduced-motion users keep the still Unicode glyph.
      if (reduceMotion) return;
      if (type.indexOf("video/webm") === 0 && webmOk) {
        return r.blob().then(function (b) {
          var v = doc.createElement("video");
          v.muted = true;
          v.loop = true;
          v.autoplay = true;
          v.playsInline = true;
          v.setAttribute("aria-hidden", "true");
          v.addEventListener("loadeddata", function () { swap(el, v); v.play().catch(function () {}); }, { once: true });
          v.src = URL.createObjectURL(b);
        });
      }
      if (type.indexOf("application/json") === 0) {
        return r.json().then(function (data) {
          return loadLottie().then(function (lottie) {
            var box = doc.createElement("span");
            box.className = "tge-anim";
            swap(el, box);
            lottie.loadAnimation({ container: box, renderer: "svg", loop: true, autoplay: true, animationData: data });
          });
        });
      }
    }).catch(function () { /* keep the Unicode fallback */ });
  }
  var emojis = $all(".tge[data-tge-src]");
  if (emojis.length) {
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          io.unobserve(entry.target);
          hydrateEmoji(entry.target);
        });
      }, { rootMargin: "120px" });
      emojis.forEach(function (el) { io.observe(el); });
    } else {
      emojis.forEach(hydrateEmoji);
    }
  }
})();
