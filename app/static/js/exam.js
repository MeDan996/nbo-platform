/*
 * Exam player.
 *
 * Design rules, in priority order:
 *  1. Never lose an answer. Every change is queued and retried until the server
 *     confirms it; the queue survives a dropped connection and is flushed again
 *     on reconnect and on page hide.
 *  2. Never trust the client clock. The countdown ticks locally for smoothness
 *     but is re-synchronised against the server's authoritative deadline, so a
 *     changed system clock or a sleeping laptop cannot buy extra time.
 *  3. Navigation must work offline. Every question is already in the DOM; moving
 *     between them is a class toggle, not a network request.
 */
(function () {
  "use strict";

  var root = document.getElementById("exam-root");
  if (!root) return;

  var ATTEMPT = root.dataset.attemptId;
  var BASE = "/survival/attempt/" + ATTEMPT;
  var STRINGS = JSON.parse(root.dataset.strings || "{}");

  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-question-card]"));
  var gridButtons = Array.prototype.slice.call(document.querySelectorAll("[data-grid-btn]"));
  var timerEl = document.getElementById("timer");
  var saveEl = document.getElementById("save-state");
  var saveLabel = document.getElementById("save-label");
  var current = 0;
  var remaining = parseInt(root.dataset.remaining || "0", 10);
  var submitted = false;
  var questionEnteredAt = Date.now();

  /* ---------------------------------------------------------------- utils */
  function t(key, fallback) { return STRINGS[key] || fallback || key; }

  function toast(message, isError) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast" + (isError ? " err" : "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  function post(path, body) {
    return fetch(BASE + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-NBO-Fingerprint": window.NBO_FINGERPRINT || ""
      },
      body: JSON.stringify(body),
      credentials: "same-origin",
      keepalive: true
    });
  }

  /* ------------------------------------------------------------ save queue */
  /* Keyed by question so repeated edits to one answer collapse into one write. */
  var pending = {};
  var inFlight = false;
  var failures = 0;

  function setSaveState(state) {
    if (!saveEl) return;
    saveEl.classList.remove("is-saving", "is-error");
    if (state === "saving") {
      saveEl.classList.add("is-saving");
      if (saveLabel) saveLabel.textContent = t("saving", "Saving…");
    } else if (state === "error") {
      saveEl.classList.add("is-error");
      if (saveLabel) saveLabel.textContent = t("save_failed", "Retrying…");
    } else if (saveLabel) {
      saveLabel.textContent = t("saved", "Saved");
    }
  }

  function queue(examQuestionId, payload) {
    pending[examQuestionId] = Object.assign(pending[examQuestionId] || {}, payload, {
      exam_question_id: parseInt(examQuestionId, 10)
    });
    setSaveState("saving");
    flush();
  }

  function flush() {
    if (inFlight || submitted) return;
    var keys = Object.keys(pending);
    if (!keys.length) { setSaveState("saved"); return; }

    var key = keys[0];
    var payload = pending[key];
    inFlight = true;

    post("/answer", payload).then(function (res) {
      if (res.status === 409) {
        return res.json().then(function (data) {
          submitted = true;
          window.location.href = data.redirect || BASE + "/result";
        });
      }
      if (!res.ok) throw new Error("save failed: " + res.status);
      return res.json().then(function (data) {
        /* Only drop the entry if it was not edited again while in flight. */
        if (pending[key] === payload) delete pending[key];
        failures = 0;
        if (data.progress) updateProgress(data.progress);
        if (typeof data.seconds_remaining === "number") remaining = data.seconds_remaining;
      });
    }).catch(function () {
      failures++;
      setSaveState("error");
    }).finally(function () {
      inFlight = false;
      if (!Object.keys(pending).length) {
        setSaveState("saved");
        return;
      }
      /* Drain the rest of the queue immediately while things are working - a
         participant answering quickly must not outrun the save loop. Back off
         only once writes are actually failing, and keep retrying either way:
         an answer is not safe until the server has it. */
      var delay = failures === 0
        ? 0
        : Math.min(15000, 400 * Math.pow(2, Math.min(failures, 5)));
      setTimeout(flush, delay);
    });
  }

  setInterval(flush, 10000);
  window.addEventListener("online", flush);

  /* ------------------------------------------------------------- answering */
  function responseFor(card) {
    var response = {};
    var type = card.dataset.questionType;

    if (type === "numeric") {
      var num = card.querySelector("[data-numeric]");
      response.value = num && num.value !== "" ? parseFloat(num.value) : null;
    } else if (type === "short_text" || type === "open_response") {
      var text = card.querySelector("[data-text]");
      response.text = text ? text.value : "";
    } else if (type === "mcq_single" || type === "mcq_multi") {
      card.querySelectorAll("[data-choice]").forEach(function (input) {
        response[input.dataset.statementId] = input.checked;
      });
    } else {
      card.querySelectorAll("[data-statement]").forEach(function (row) {
        var pressed = row.querySelector('.tf-btn[aria-pressed="true"]');
        response[row.dataset.statementId] = pressed ? pressed.dataset.value === "true" : null;
      });
    }
    return response;
  }

  function isAnswered(card) {
    var r = responseFor(card);
    if ("text" in r) return String(r.text || "").trim() !== "";
    if ("value" in r) return r.value !== null && !isNaN(r.value);
    return Object.keys(r).some(function (k) { return r[k] !== null; });
  }

  function syncCard(card) {
    var index = parseInt(card.dataset.index, 10) - 1;
    var button = gridButtons[index];
    if (button) button.classList.toggle("answered", isAnswered(card));

    card.querySelectorAll("[data-statement]").forEach(function (row) {
      row.classList.toggle("is-answered", !!row.querySelector('.tf-btn[aria-pressed="true"]'));
    });
    card.querySelectorAll("[data-choice]").forEach(function (input) {
      var label = input.closest(".choice");
      if (label) label.classList.toggle("is-selected", input.checked);
    });
  }

  function persist(card) {
    syncCard(card);
    queue(card.dataset.examQuestionId, {
      response: responseFor(card),
      seconds_spent: Math.round((Date.now() - questionEnteredAt) / 1000),
      flagged: card.dataset.flagged === "1"
    });
  }

  /* True/false buttons */
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".tf-btn");
    if (button) {
      var row = button.closest("[data-statement]");
      var wasPressed = button.getAttribute("aria-pressed") === "true";
      row.querySelectorAll(".tf-btn").forEach(function (b) {
        b.setAttribute("aria-pressed", "false");
      });
      button.setAttribute("aria-pressed", wasPressed ? "false" : "true");
      persist(button.closest("[data-question-card]"));
      return;
    }

    var clear = event.target.closest(".tf-clear");
    if (clear) {
      var clearRow = clear.closest("[data-statement]");
      clearRow.querySelectorAll(".tf-btn").forEach(function (b) {
        b.setAttribute("aria-pressed", "false");
      });
      persist(clear.closest("[data-question-card]"));
      return;
    }

    var flagBtn = event.target.closest("[data-flag]");
    if (flagBtn) {
      var card = flagBtn.closest("[data-question-card]");
      var next = card.dataset.flagged === "1" ? "0" : "1";
      card.dataset.flagged = next;
      flagBtn.setAttribute("aria-pressed", next === "1" ? "true" : "false");
      var idx = parseInt(card.dataset.index, 10) - 1;
      if (gridButtons[idx]) gridButtons[idx].classList.toggle("flagged", next === "1");
      persist(card);
    }
  });

  document.addEventListener("change", function (event) {
    var card = event.target.closest("[data-question-card]");
    if (card && event.target.matches("[data-choice]")) persist(card);
  });

  var textTimer = null;
  document.addEventListener("input", function (event) {
    var card = event.target.closest("[data-question-card]");
    if (!card) return;
    if (!event.target.matches("[data-text], [data-numeric]")) return;
    clearTimeout(textTimer);
    textTimer = setTimeout(function () { persist(card); }, 700);
  });

  /* ------------------------------------------------------------ navigation */
  function show(index) {
    if (index < 0 || index >= cards.length) return;
    var leaving = cards[current];
    if (leaving) {
      /* Credit the time spent before moving on, without a network round trip
         unless something actually changed. */
      var spent = Math.round((Date.now() - questionEnteredAt) / 1000);
      if (spent > 1) {
        queue(leaving.dataset.examQuestionId, {
          response: responseFor(leaving),
          seconds_spent: spent,
          flagged: leaving.dataset.flagged === "1"
        });
      }
    }

    cards.forEach(function (card, i) { card.hidden = i !== index; });
    gridButtons.forEach(function (button, i) {
      button.classList.toggle("current", i === index);
      button.setAttribute("aria-current", i === index ? "true" : "false");
    });
    current = index;
    questionEnteredAt = Date.now();
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });

    var prev = document.getElementById("nav-prev");
    var next = document.getElementById("nav-next");
    if (prev) prev.disabled = index === 0;
    if (next) next.disabled = index === cards.length - 1;
  }

  gridButtons.forEach(function (button, index) {
    button.addEventListener("click", function () { show(index); });
  });
  var prevBtn = document.getElementById("nav-prev");
  var nextBtn = document.getElementById("nav-next");
  if (prevBtn) prevBtn.addEventListener("click", function () { show(current - 1); });
  if (nextBtn) nextBtn.addEventListener("click", function () { show(current + 1); });

  document.addEventListener("keydown", function (event) {
    if (event.target.matches("input, textarea, select")) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowLeft") show(current - 1);
    if (event.key === "ArrowRight") show(current + 1);
  });

  function updateProgress(progress) {
    var answered = document.getElementById("count-answered");
    var total = document.getElementById("count-total");
    if (answered) answered.textContent = progress.answered;
    if (total) total.textContent = progress.total;
  }

  /* ----------------------------------------------------------------- clock */
  function renderTimer() {
    if (!timerEl) return;
    var value = Math.max(0, remaining);
    var h = Math.floor(value / 3600);
    var m = Math.floor((value % 3600) / 60);
    var s = value % 60;
    timerEl.textContent =
      (h > 0 ? h + ":" + String(m).padStart(2, "0") : String(m)) +
      ":" + String(s).padStart(2, "0");

    timerEl.classList.toggle("warn", value <= 900 && value > 300);
    timerEl.classList.toggle("danger", value <= 300);
  }

  var warned = {};
  function maybeWarn() {
    [900, 300, 60].forEach(function (mark) {
      if (remaining <= mark && !warned[mark]) {
        warned[mark] = true;
        toast(t("time_warning_" + mark, Math.round(mark / 60) + " min remaining"));
      }
    });
  }

  setInterval(function () {
    if (submitted) return;
    remaining -= 1;
    renderTimer();
    maybeWarn();
    if (remaining <= 0) autoSubmit();
  }, 1000);
  renderTimer();

  /* Re-sync against the server, which owns the deadline. */
  function sync() {
    if (submitted) return;
    fetch(BASE + "/state", { credentials: "same-origin" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data) return;
        remaining = data.seconds_remaining;
        if (data.progress) updateProgress(data.progress);
        renderTimer();
        if (data.status !== "in_progress") {
          submitted = true;
          window.location.href = BASE + "/result";
        }
      })
      .catch(function () { /* offline: keep ticking locally */ });
  }
  setInterval(sync, 30000);

  function autoSubmit() {
    if (submitted) return;
    submitted = true;
    toast(t("time_up", "Time is up."), true);
    var form = document.getElementById("submit-form");
    if (form) form.submit();
  }

  /* ------------------------------------------------------------- telemetry */
  function sendEvent(kind, data) {
    if (submitted) return;
    post("/event", { kind: kind, data: data || {} }).catch(function () {});
  }

  window.addEventListener("blur", function () { sendEvent("window_blur"); });
  window.addEventListener("focus", function () { sendEvent("window_focus"); });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      sendEvent("tab_hidden");
      flush();
    }
  });

  document.addEventListener("paste", function (event) {
    var text = (event.clipboardData && event.clipboardData.getData("text")) || "";
    sendEvent("paste", { length: text.length });
  });
  document.addEventListener("copy", function () { sendEvent("copy"); });
  document.addEventListener("contextmenu", function () { sendEvent("context_menu"); });

  document.addEventListener("fullscreenchange", function () {
    if (!document.fullscreenElement) sendEvent("fullscreen_exit");
  });

  window.addEventListener("offline", function () { sendEvent("network_drop"); });

  window.addEventListener("pagehide", flush);
  window.addEventListener("beforeunload", function (event) {
    if (submitted) return;
    flush();
    if (Object.keys(pending).length) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* ---------------------------------------------------------------- submit */
  var submitBtn = document.getElementById("submit-btn");
  var modal = document.getElementById("submit-modal");
  if (submitBtn && modal) {
    submitBtn.addEventListener("click", function () {
      var unanswered = cards.filter(function (c) { return !isAnswered(c); }).length;
      var note = document.getElementById("submit-unanswered");
      if (note) {
        note.textContent = note.dataset.template.replace("{unanswered}", unanswered);
        note.hidden = unanswered === 0;
      }
      modal.hidden = false;
    });
    modal.addEventListener("click", function (event) {
      if (event.target === modal || event.target.closest("[data-modal-close]")) {
        modal.hidden = true;
      }
    });
    var confirmBtn = document.getElementById("submit-confirm");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", function () {
        submitted = true;
        document.getElementById("submit-form").submit();
      });
    }
  }

  /* ------------------------------------------------------------------ init */
  cards.forEach(syncCard);
  show(0);
  setSaveState("saved");
})();
