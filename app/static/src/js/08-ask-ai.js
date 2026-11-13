/* "Ask AI" box with streamed answers (server-sent events through IP.ai.stream). Markup: partials/ask_ai.html
 *   <div data-component="ask-ai" data-endpoint="/api/plans/ask" data-extra='{"gym_slug":"x"}'> ... </div>
 * Events: {"type":"meta","provider","cached","busy"} | {"type":"chunk","text"} |
 *         {"type":"blocked","code","detail","retry_after"} | {"type":"done","fallback","note","disclaimer","blocked"}
 * A refusal of the AI guard (quota, cooldown, duplicate, spam ...) arrives as a "blocked" event: its detail replaces
 * the answer, and the ask button and suggestions stay disabled while IP.ai.cooldownLeft("plans") > 0, with a
 * countdown under the form (outside the live region, so screen readers are not told every second). Leaving the page
 * aborts the stream (IP.ai, reason "page-hide") and nothing is shown. Text is rendered with textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP || !IP.ai) return;
  var FEATURE = "plans"; // the server's AI policy: the plan advisor's quota and cooldown bucket

  IP.register("ask-ai", function (root) {
    var endpoint = root.getAttribute("data-endpoint");
    var extra = {};
    try {
      extra = JSON.parse(root.getAttribute("data-extra") || "{}") || {};
    } catch (e) {
      extra = {};
    }
    var form = root.querySelector("[data-ask-form]");
    var input = root.querySelector("[data-ask-input]");
    var box = root.querySelector("[data-ask-answer]");
    var text = root.querySelector("[data-ask-text]");
    var typing = root.querySelector("[data-ask-typing]");
    var note = root.querySelector("[data-ask-note]");
    if (!endpoint || !form || !input || !box || !text || !typing || !note) return;
    var waitEl = root.querySelector("[data-ask-wait]");
    var button = form.querySelector("button");
    var suggestions = Array.prototype.slice.call(root.querySelectorAll("[data-ask-suggest]"));
    var section = root.closest ? root.closest("section") : null;
    var providerEl = section ? section.querySelector("[data-ask-provider]") : null;
    var busy = false;
    var ticker = null;

    function setNote(msg) {
      note.textContent = msg || "";
      note.hidden = !msg;
    }

    /** The ask button and the suggestions are off while an answer streams and while the AI cooldown runs. */
    function sync() {
      var wait = IP.ai.cooldownLeft(FEATURE);
      var off = busy || wait > 0;
      if (button) button.disabled = off;
      suggestions.forEach(function (b) {
        b.disabled = off;
      });
      if (waitEl) {
        waitEl.textContent = wait > 0 ? "You can ask again in " + IP.ai.formatWait(wait) + "." : "";
        waitEl.hidden = !(wait > 0);
      }
      if (wait > 0 && !ticker) {
        ticker = setInterval(sync, 1000);
      } else if (!(wait > 0) && ticker) {
        clearInterval(ticker);
        ticker = null;
      }
    }

    function handle(evt) {
      if (evt.type === "chunk") {
        typing.hidden = true;
        text.textContent += evt.text || "";
      } else if (evt.type === "meta") {
        if (providerEl) {
          providerEl.textContent = evt.provider
            ? evt.provider + (evt.cached ? " (instant answer)" : "")
            : evt.busy
              ? "AI busy - automatic answer"
              : "AI unavailable - automatic answer";
        }
      } else if (evt.type === "blocked") {
        typing.hidden = true;
        text.textContent = evt.detail || IP.ai.describeError({ code: evt.code, retryAfter: evt.retry_after });
      } else if (evt.type === "done" && evt.note) {
        setNote(evt.note);
      }
    }

    async function ask(question) {
      question = (question || "").trim();
      if (busy || question.length < 2) return;
      busy = true;
      box.classList.remove("hidden");
      text.textContent = "";
      setNote("");
      typing.hidden = false;
      sync();
      var result = null;
      try {
        result = await IP.ai.stream(endpoint, {
          json: Object.assign({ question: question }, extra),
          feature: FEATURE,
          timeout: 45000,
          onEvent: handle,
        });
      } catch (err) {
        if (!(err && err.name === "AbortError" && err.reason === "page-hide")) {
          // a refusal, a cooldown, a timeout or a network failure: say what happened (keep any words already shown)
          if (text.textContent) setNote(IP.ai.describeError(err));
          else text.textContent = IP.ai.describeError(err);
        }
      } finally {
        busy = false;
        typing.hidden = true;
      }
      if (result && !result.done && !result.blocked) {
        // the server stopped the answer (this tab or session was closed elsewhere)
        if (text.textContent) setNote("The answer was interrupted - ask again for the full answer.");
        else text.textContent = "The answer was interrupted - please try again.";
      }
      sync();
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      ask(input.value);
    });
    suggestions.forEach(function (b) {
      b.addEventListener("click", function () {
        input.value = b.getAttribute("data-ask-suggest");
        ask(input.value);
      });
    });
    IP.ai.onCooldown(sync); // a cooldown started by another AI widget of this page (code "cooldown" pauses them all)
    sync();
  });
})();
