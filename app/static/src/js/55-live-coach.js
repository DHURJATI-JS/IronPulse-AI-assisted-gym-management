/* Live AI workout companion (WP-K): start screen + live session runtime.
 *   data-component="live-start"    workout picker, voice/mic preferences -> POST /api/live/sessions
 *   data-component="live-session"  coach stage (IP.coachFace), workout HUD, conversation, discomfort flow, safety screen
 * Voice out: POST /api/tts (Kokoro) -> <audio> + Web Audio analyser for lip-sync; 503/429/offline -> speechSynthesis
 *            with a soft voice and boundary/procedural lip-sync, and no new voice requests until Retry-After (or the
 *            page-wide IP.ai "tts" cooldown) has passed. Audio starts only after the member taps Begin.
 * Voice in:  (webkit)SpeechRecognition - continuous + interim results, auto-restart, barge-in, push-to-talk.
 *            The text box always works. All text goes into the DOM with textContent (never innerHTML).
 * AI chat:   one message at a time through IP.ai (01-ai.js) - a newer message replaces the one in flight, a voice
 *            repeat of it is dropped. A refused reply (HTTP 200 + `refused`) is spoken once and handed to
 *            IP.ai.noteRefusal, like a 429: AI chat waits `retry_after` seconds (IP.ai's "live_coach" cooldown is the
 *            only timer), and code "cooldown" pauses every AI widget of the page and the natural voice too. Meanwhile
 *            messages carry "ai": false (the server still runs every safety check and command, without the AI).
 *            Discomfort reports are safety flows: never refused or aborted locally.
 *            Leaving the page aborts what is in flight and pauses the workout (keepalive event) - not on sign-out.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var PREFS_KEY = "ip-live-prefs";
  var SOFT_VOICES = ["Samantha", "Ava", "Allison", "Karen", "Moira", "Tessa", "Google UK English Female",
    "Microsoft Aria Online (Natural)", "Microsoft Jenny Online (Natural)", "Microsoft Zira"];
  var NOOP_FACE = { setEmotion: function () {}, setMouth: function () {}, setSpeaking: function () {},
    setListening: function () {}, setThinking: function () {} };
  var ACTIVE = ["ready", "work", "rest"];
  var RING = 2 * Math.PI * 52;

  function face() {
    return IP.coachFace || NOOP_FACE;
  }
  function now() {
    return window.performance && performance.now ? performance.now() : Date.now();
  }
  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }
  function fmt(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function cap(s) {
    s = String(s || "");
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }
  function readJSON(el, name) {
    try {
      return JSON.parse(el.getAttribute(name) || "null");
    } catch (e) {
      return null;
    }
  }
  function loadPrefs() {
    var p = { voice: true, mic: true };
    try {
      var saved = JSON.parse(window.localStorage.getItem(PREFS_KEY) || "{}") || {};
      if (typeof saved.voice === "boolean") p.voice = saved.voice;
      if (typeof saved.mic === "boolean") p.mic = saved.mic;
    } catch (e) {}
    return p;
  }
  function savePrefs(p) {
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({ voice: !!p.voice, mic: !!p.mic }));
    } catch (e) {}
  }
  function toast(title, body, tone) {
    if (IP.island && IP.island.notify) IP.island.notify({ title: title, body: body || "", tone: tone || "info" });
  }
  function words(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
  }
  /** True when the recogniser most likely heard the coach's own voice through the speakers. */
  function isEcho(heard, said) {
    var h = words(heard);
    if (h.length < 2) return false;
    var bag = {};
    words(said).forEach(function (w) {
      bag[w] = 1;
    });
    var hits = h.filter(function (w) {
      return bag[w];
    }).length;
    return hits / h.length >= 0.6;
  }
  // Microphone / voice instructions are handled right here in the browser (no server round trip).
  var LOCAL_COMMANDS = [
    ["mic_off", /\b(stop listening|(turn|switch|shut) (off|down) (the |your )?(mic|microphone)|(mic|microphone) off|mute (the |my )?(mic|microphone))\b/i],
    ["voice_off", /\b(be quiet|stop talking|shut up|quiet please|voice off|(turn|switch) off (the |your )?voice|mute (yourself|your voice|the voice|voice))\b/i],
    ["voice_on", /\b(voice on|unmute( yourself| the voice| voice)?|(turn|switch) on (the |your )?voice|talk to me again|start talking)\b/i],
  ];
  function localCommand(text) {
    for (var i = 0; i < LOCAL_COMMANDS.length; i++) if (LOCAL_COMMANDS[i][1].test(text || "")) return LOCAL_COMMANDS[i][0];
    return null;
  }
  /** The text as the server's repeat check sees it: case, spacing and surrounding punctuation don't matter. */
  function normText(text) {
    return String(text || "").toLowerCase().replace(/\s+/g, " ").replace(/^[\s.,!?;:'"\u2026-]+|[\s.,!?;:'"\u2026-]+$/g, "");
  }
  // While this network is rate limited (HTTP 429 without a code) only possible health problems still go to the server
  // (deliberately broad - the server's safety layer decides); the core workout commands then run right here.
  var UNSAFE_RX = /pain|hurt|ach(e|ing)|sore|cramp|injur|sprain|strain|pulled|tweak|twist|pop|snap|tear|tore|torn|chest|heart|breath|dizz|faint|pass(ed|ing)? out|black(ed|ing)? out|collaps|sick|nause|vomit|throw(ing)? up|threw up|puk|numb|tingl|weak|head|migraine|vision|blur|seiz|stroke|slur|sugar|hypo|shak|sweat|heat|swell|swoll|bleed|blood|help|emergenc|ambulance|doctor|ouch|unwell|wrong|knee|back|wrist|ankle|neck|shoulder|stomach|\bill\b|not (feeling )?(well|right|good)/i;
  function looksUnsafe(text) {
    return UNSAFE_RX.test(String(text || ""));
  }
  var GATE_COMMANDS = [
    ["next", /^(next|skip|next exercise|skip (it|this|this one)|move on)$/],
    ["pause", /^(pause|hold on|wait|(stop|give me) (for )?a (sec|second|minute))$/],
    ["resume", /^(resume|continue|go|let'?s go|keep going|i'?m ready|start)$/],
    ["done", /^(done|set done|next set|finished|done with (this|the) set)$/],
  ];
  function gateCommand(text) {
    var n = normText(text);
    for (var i = 0; i < GATE_COMMANDS.length; i++) if (GATE_COMMANDS[i][1].test(n)) return GATE_COMMANDS[i][0];
    return null;
  }
  /** A wait the coach can say out loud (IP.ai.formatWait's "2 min" is for the eyes). */
  function spokenWait(seconds) {
    var s = Math.max(1, Math.ceil(Number(seconds) || 0));
    if (s < 60) return s + (s === 1 ? " second" : " seconds");
    var m = Math.ceil(s / 60);
    if (m < 60) return m + (m === 1 ? " minute" : " minutes");
    var h = Math.max(1, Math.round(s / 3600));
    return h === 1 ? "an hour" : h + " hours";
  }
  function formatWait(seconds) {
    return IP.ai && IP.ai.formatWait ? IP.ai.formatWait(seconds) : Math.ceil(seconds) + " s";
  }
  /** AI requests go through IP.ai (per-feature cooldowns, aborted on pagehide); plain IP.api if it is missing. */
  function aiRequest(url, opts) {
    if (IP.ai && IP.ai.request) return IP.ai.request(url, opts);
    return IP.api(url, { method: "POST", json: opts.json, signal: opts.signal });
  }
  function cooldownLeft(feature) {
    return IP.ai && IP.ai.cooldownLeft ? IP.ai.cooldownLeft(feature) : 0;
  }
  function isLogoutForm(form) {
    if (!form || String(form.tagName || "").toUpperCase() !== "FORM") return false;
    var action = form.getAttribute("action") || "";
    try {
      return new URL(action, location.href).pathname === "/auth/logout";
    } catch (e) {
      return /^\/auth\/logout(?:[?#]|$)/.test(action);
    }
  }
  function svgIcon(kind) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "size-4 mt-0.5 shrink-0 text-ok");
    if (kind === "check") {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", "M22 11.08V12a10 10 0 1 1-5.93-9.14");
      var pl = document.createElementNS(ns, "polyline");
      pl.setAttribute("points", "22 4 12 14.01 9 11.01");
      svg.appendChild(p);
      svg.appendChild(pl);
    }
    return svg;
  }

  /* ================================================================ start screen */
  IP.register("live-start", function (root) {
    var form = root.querySelector("[data-live-form]");
    if (!form) return;
    form.setAttribute("data-no-busy", "");
    var startBtn = root.querySelector("[data-start-btn]");
    var label = root.querySelector("[data-selection]");
    var errEl = root.querySelector("[data-start-error]");
    var prefs = loadPrefs();

    root.querySelectorAll("[data-pref]").forEach(function (sw) {
      var key = sw.getAttribute("data-pref");
      if (key === "mic" && !SR) {
        sw.checked = false;
        sw.disabled = true;
        return;
      }
      sw.checked = prefs[key] !== false;
      sw.addEventListener("change", function () {
        prefs[key] = sw.checked;
        savePrefs(prefs);
      });
    });
    if (!SR) {
      root.querySelectorAll("[data-mic-unsupported]").forEach(function (el) {
        el.classList.remove("hidden");
      });
    }

    function picked() {
      return form.querySelector('input[name="pick"]:checked');
    }
    function update() {
      var p = picked();
      var opt = p && p.closest(".live-option");
      if (label) label.textContent = opt ? "Selected: " + (opt.getAttribute("data-title") || "") : "Pick a workout below";
      if (startBtn) startBtn.disabled = !p;
    }
    form.addEventListener("change", update);
    update();

    var dot = root.querySelector("[data-tts-dot]");
    var ttsLabel = root.querySelector("[data-tts-label]");
    IP.api("/api/tts/status?probe=1")
      .then(function (s) {
        if (!s || !dot || !ttsLabel) return;
        var up = s.state === "up";
        dot.setAttribute("data-state", up ? "online" : s.state === "unknown" ? "degraded" : "offline");
        ttsLabel.textContent = up ? "Natural Kokoro voice ready" : "Soft browser voice (Kokoro offline)";
      })
      .catch(function () {});

    function showErr(msg) {
      if (!errEl) return;
      errEl.textContent = msg;
      errEl.classList.remove("hidden");
    }

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var p = picked();
      if (!p) {
        showErr("Pick a workout first.");
        return;
      }
      var body = { voice_enabled: prefs.voice !== false };
      if (p.value.indexOf("quick-") === 0) body.quick = p.value.slice(6);
      else {
        var plan = form.querySelector('input[name="plan_id"]');
        body.plan_id = parseInt(plan && plan.value, 10);
        body.day_index = parseInt(p.value.slice(4), 10);
      }
      var spinner = IP.h("span", { class: "spinner", "aria-hidden": "true" });
      startBtn.disabled = true;
      startBtn.setAttribute("aria-busy", "true");
      startBtn.insertBefore(spinner, startBtn.firstChild);
      if (errEl) errEl.classList.add("hidden");
      try {
        var res = await IP.api("/api/live/sessions", { method: "POST", json: body });
        window.location.assign(res.url || "/me/live/" + res.session.id);
      } catch (err) {
        showErr((err && err.message) || "Couldn't start the session - please try again.");
        startBtn.disabled = false;
        startBtn.removeAttribute("aria-busy");
        spinner.remove();
      }
    });
  });

  /* ================================================================ live session */
  IP.register("live-session", function (root) {
    var session = readJSON(root, "data-session") || { items: [], current_index: 0 };
    var cfg = readJSON(root, "data-config") || {};
    var urls = cfg.urls || {};
    var prefs = loadPrefs();
    function q(sel) {
      return root.querySelector(sel);
    }
    function btn(act) {
      return root.querySelector('[data-act="' + act + '"]');
    }
    var el = {
      status: q("[data-status]"), statusText: q("[data-status-text]"), progress: q("[data-progress]"),
      progressBar: q("[data-progress-bar]"), safety: q("[data-safety]"), safetyReason: q("[data-safety-reason]"),
      safetyText: q("[data-safety-text]"), safetyCall: q("[data-safety-call]"), okNote: q("[data-ok-note]"),
      workout: q("[data-workout]"), voiceChip: q("[data-voice-state]"), voiceText: q("[data-voice-text]"),
      intensity: q("[data-intensity]"), intensityLabel: q("[data-intensity-label]"), faceWrap: q("[data-face-wrap]"),
      thinking: q("[data-thinking]"), caption: q("[data-caption]"), interim: q("[data-interim]"),
      readyOverlay: q("[data-ready]"), doneOverlay: q("[data-done]"), main: q("[data-live-main]"),
      summaryTitle: q("[data-summary-title]"), summaryTime: q("[data-summary-time]"), summaryEx: q("[data-summary-ex]"),
      summarySets: q("[data-summary-sets]"), exPos: q("[data-ex-position]"), exCat: q("[data-ex-category]"),
      exName: q("[data-ex-name]"), exSwap: q("[data-ex-swap]"), exMetric: q("[data-ex-metric]"), exUnit: q("[data-ex-unit]"),
      exSetLabel: q("[data-ex-setlabel]"), exSets: q("[data-ex-sets]"), exLoad: q("[data-ex-load]"),
      exLoadText: q("[data-ex-load-text]"), exCues: q("[data-ex-cues]"), setDoneLabel: q("[data-setdone-label]"),
      ring: q("[data-ring]"), ringValue: q("[data-ring-value]"), timer: q("[data-timer]"), phaseLabel: q("[data-phase-label]"),
      nextName: q("[data-next-name]"), nextDose: q("[data-next-dose]"), panel: q("[data-discomfort-panel]"),
      note: q("[data-discomfort-note]"), warn: q("[data-discomfort-warn]"), log: q("[data-log]"), logEmpty: q("[data-log-empty]"),
      upsell: q("[data-upsell]"), chatForm: q("[data-chat-form]"), chatInput: q("[data-chat-input]"),
    };
    if (!el.caption || !el.timer) return;

    var st = {
      phase: "idle", // idle | ready | work | rest | paused | wait | done | safety
      prev: "work",
      set: 1,
      start: now(),
      acc: 0,
      dur: 0,
      begun: false,
      voiceOn: prefs.voice !== false && session.voice_enabled !== false,
      micOn: false,
      micDenied: false,
      setsDone: 0,
      fired: {},
      lastReply: "",
    };
    var thinkingOn = false;
    var baselineTimer = 0;
    var pending = 0; // requests the coach is "thinking" about
    var msg = null; // the AI message in flight {ctrl, key}: a newer message replaces it - never two replies at once
    var chatHeld = false; // AI chat was paused when last painted (an IP.ai cooldown or the network's limit)
    var noting = false; // true while a refused reply is handed to IP.ai: never the network's rate limit
    var gateUntil = 0; // Date.now() until which this network may not post messages (HTTP 429 without a code)
    var pauseNotedAt = 0;
    var lastIntensity = 0;
    var loggingOut = false;

    function items() {
      return session.items || [];
    }
    function idx() {
      return Math.min(session.current_index || 0, items().length);
    }
    function cur() {
      return items()[idx()] || null;
    }
    function eff(item) {
      return (item && item.effective) || {};
    }
    function setsOf(item) {
      return eff(item).sets || (item && item.sets) || 1;
    }
    function elapsed() {
      if (ACTIVE.indexOf(st.phase) === -1) return st.acc;
      return st.acc + (now() - st.start) / 1000;
    }

    /* ---------------------------------------------------------- emotions */
    function baseline() {
      return { idle: "happy", ready: "encouraging", work: "focused", rest: "calm", paused: "neutral", wait: "focused",
        done: "proud", safety: "concerned" }[st.phase] || "neutral";
    }
    function setEmotion(name) {
      clearTimeout(baselineTimer);
      if (name) face().setEmotion(name);
    }
    function scheduleBaseline(ms) {
      clearTimeout(baselineTimer);
      baselineTimer = setTimeout(function () {
        if (!speaker.speaking && !thinkingOn) face().setEmotion(baseline());
      }, ms == null ? 2600 : ms);
    }

    /* ---------------------------------------------------------- voice out (Kokoro TTS / browser voice) */
    var speaker = (function () {
      var queue = [];
      var speaking = false;
      var token = 0;
      var audio = null;
      var actx = null;
      var analyser = null;
      var tbuf = null;
      var fbuf = null;
      var raf = 0;
      var mode = "procedural";
      var cache = {};
      var order = [];
      var downUntil = 0;
      var unlocked = false;
      var voice = null;
      var lastSaid = "";
      var lastEnd = 0;
      var watchdog = 0;
      var ttsCtrl = null; // the voice line being downloaded (stop() drops it)
      var pulseV = 0;
      var pulseRound = 0;

      if (window.speechSynthesis && window.speechSynthesis.addEventListener) {
        window.speechSynthesis.addEventListener("voiceschanged", function () {
          voice = null;
        });
      }

      function silentWav() {
        var n = 800;
        var buf = new ArrayBuffer(44 + n * 2);
        var v = new DataView(buf);
        function str(o, s) {
          for (var i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i));
        }
        str(0, "RIFF");
        v.setUint32(4, 36 + n * 2, true);
        str(8, "WAVE");
        str(12, "fmt ");
        v.setUint32(16, 16, true);
        v.setUint16(20, 1, true);
        v.setUint16(22, 1, true);
        v.setUint32(24, 8000, true);
        v.setUint32(28, 16000, true);
        v.setUint16(32, 2, true);
        v.setUint16(34, 16, true);
        str(36, "data");
        v.setUint32(40, n * 2, true);
        return new Blob([buf], { type: "audio/wav" });
      }

      function unlock() {
        if (unlocked) return;
        unlocked = true;
        try {
          var AC = window.AudioContext || window.webkitAudioContext;
          if (AC) {
            actx = new AC();
            if (actx.resume) actx.resume();
          }
        } catch (e) {
          actx = null;
        }
        try {
          audio = new Audio();
          audio.preload = "auto";
          audio.setAttribute("playsinline", "");
          if (actx) {
            var src = actx.createMediaElementSource(audio);
            analyser = actx.createAnalyser();
            analyser.fftSize = 1024;
            analyser.smoothingTimeConstant = 0.55;
            src.connect(analyser);
            analyser.connect(actx.destination);
            tbuf = new Uint8Array(analyser.fftSize);
            fbuf = new Uint8Array(analyser.frequencyBinCount);
          }
          audio.src = URL.createObjectURL(silentWav());
          var p = audio.play();
          if (p && p.catch) p.catch(function () {});
        } catch (e) {
          analyser = null;
        }
        if (window.speechSynthesis && typeof SpeechSynthesisUtterance !== "undefined") {
          try {
            var u = new SpeechSynthesisUtterance(" ");
            u.volume = 0;
            window.speechSynthesis.speak(u);
          } catch (e) {}
        }
      }

      function pickVoice() {
        if (voice) return voice;
        var synth = window.speechSynthesis;
        if (!synth || !synth.getVoices) return null;
        var list = synth.getVoices() || [];
        for (var i = 0; i < SOFT_VOICES.length; i++) {
          for (var j = 0; j < list.length; j++) {
            if (list[j].name === SOFT_VOICES[i] || list[j].name.indexOf(SOFT_VOICES[i]) === 0) {
              voice = list[j];
              return voice;
            }
          }
        }
        var en = list.filter(function (v) {
          return /^en([-_]|$)/i.test(v.lang || "");
        });
        voice = en.filter(function (v) {
          return /female|samantha|victoria|serena|fiona|zira|aria|jenny|libby|sonia/i.test(v.name);
        })[0] || en[0] || list[0] || null;
        return voice;
      }

      /** No new voice requests: the engine is down, a refusal's Retry-After runs, or AI is paused page-wide. */
      function voicePaused() {
        return now() < downUntil || cooldownLeft("tts") > 0;
      }
      /** 429 (voice quota / AI paused) or 503 (engine down, voice lane busy): the browser voice until Retry-After. */
      function backOff(err) {
        if (!err || err.name === "AbortError" || (err.status !== 429 && err.status !== 503)) return;
        downUntil = Math.max(downUntil, now() + (err.retryAfter > 0 ? err.retryAfter : 60) * 1000);
      }

      function fetchTts(text) {
        if (cache[text]) return Promise.resolve(cache[text]);
        var ctrl = window.AbortController ? new AbortController() : null;
        var timer = setTimeout(function () {
          if (ctrl) ctrl.abort();
        }, 9000);
        ttsCtrl = ctrl;
        function settle() {
          clearTimeout(timer);
          if (ttsCtrl === ctrl) ttsCtrl = null;
        }
        var headers = { "Content-Type": "application/json", Accept: "audio/mpeg", "X-CSRF-Token": IP.csrf() };
        if (IP.pageId) headers["X-Page-Id"] = IP.pageId; // the server stops this tab's voice work when it closes
        return fetch(urls.tts, {
          method: "POST",
          credentials: "same-origin",
          headers: headers,
          body: JSON.stringify({ text: text.slice(0, 400) }),
          signal: ctrl ? ctrl.signal : undefined,
        })
          .then(function (res) {
            settle();
            var ct = res.headers.get("content-type") || "";
            if (!res.ok || ct.indexOf("audio") === -1) {
              var err = new Error("tts " + res.status);
              err.status = res.status;
              err.retryAfter = IP.parseRetryAfter ? IP.parseRetryAfter(res.headers.get("retry-after")) : null;
              throw err;
            }
            return res.blob();
          }, function (err) {
            settle();
            throw err;
          })
          .then(function (blob) {
            var url = URL.createObjectURL(blob);
            cache[text] = url;
            order.push(text);
            if (order.length > 40) {
              var old = order.shift();
              if (cache[old]) URL.revokeObjectURL(cache[old]);
              delete cache[old];
            }
            return url;
          });
      }

      function setSpeakingUI(on) {
        face().setSpeaking(on);
        if (el.faceWrap) el.faceWrap.setAttribute("data-speaking", on ? "1" : "0");
        voiceState();
      }

      function loop() {
        raf = 0;
        if (!speaking) {
          face().setMouth(0, 0);
          return;
        }
        var v = 0;
        var r = 0;
        if (mode === "audio" && analyser) {
          analyser.getByteTimeDomainData(tbuf);
          var sum = 0;
          for (var i = 0; i < tbuf.length; i++) {
            var x = (tbuf[i] - 128) / 128;
            sum += x * x;
          }
          v = clamp((Math.sqrt(sum / tbuf.length) - 0.012) * 5.5, 0, 1);
          analyser.getByteFrequencyData(fbuf);
          var num = 0;
          var den = 0;
          for (var j = 2; j < fbuf.length / 4; j++) {
            num += j * fbuf[j];
            den += fbuf[j];
          }
          if (den > 0 && v > 0.12) {
            var hz = (num / den) * (actx.sampleRate / analyser.fftSize);
            r = clamp(1 - (hz - 500) / 1300, 0, 1);
          }
        } else {
          var t = now() / 1000;
          pulseV *= 0.86;
          pulseRound *= 0.9;
          v = clamp(0.16 + 0.22 * Math.abs(Math.sin(t * 11.3)) + 0.14 * Math.abs(Math.sin(t * 7.1 + 1.3)) + pulseV, 0, 1);
          r = pulseRound;
        }
        face().setMouth(v, r);
        raf = window.requestAnimationFrame(loop);
      }
      function lipSync(m) {
        mode = m;
        if (!raf) raf = window.requestAnimationFrame(loop);
      }
      function pulse(word) {
        pulseV = 0.42;
        pulseRound = /oo|ou|o|u|w/i.test(word || "") ? 0.85 : 0;
      }

      function finish() {
        clearTimeout(watchdog);
        if (!speaking) return;
        speaking = false;
        lastEnd = now();
        setSpeakingUI(false);
        face().setMouth(0, 0);
        if (queue.length) setTimeout(next, 220);
        else scheduleBaseline();
      }

      function browserSay(text, my) {
        var synth = window.speechSynthesis;
        clearTimeout(watchdog);
        if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
          lipSync("procedural");
          watchdog = setTimeout(function () {
            if (my === token) finish();
          }, Math.min(9000, 600 + text.length * 55));
          return;
        }
        try {
          synth.cancel();
        } catch (e) {}
        var u = new SpeechSynthesisUtterance(text);
        var v = pickVoice();
        if (v) {
          u.voice = v;
          u.lang = v.lang;
        }
        u.rate = 0.95;
        u.pitch = 1.05;
        u.onboundary = function (e) {
          if (my !== token) return;
          var m = text.slice(e.charIndex || 0).match(/^[\w']+/);
          pulse(m ? m[0] : "");
        };
        u.onend = function () {
          if (my === token) finish();
        };
        u.onerror = function () {
          if (my === token) finish();
        };
        lipSync("procedural");
        synth.speak(u);
        watchdog = setTimeout(function () {
          if (my === token) finish();
        }, Math.min(25000, 2000 + text.length * 95));
      }

      function playUrl(url, my, text) {
        if (!audio) {
          browserSay(text, my);
          return;
        }
        audio.onended = function () {
          if (my === token) finish();
        };
        audio.onerror = function () {
          if (my === token) browserSay(text, my);
        };
        audio.src = url;
        if (actx && actx.state === "suspended" && actx.resume) actx.resume();
        var p = audio.play();
        if (p && p.catch) {
          p.catch(function () {
            if (my === token) browserSay(text, my);
          });
        }
        lipSync(analyser ? "audio" : "procedural");
        watchdog = setTimeout(function () {
          if (my === token) finish();
        }, 30000);
      }

      function next() {
        if (speaking) return;
        var item = queue.shift();
        if (!item) return;
        var my = ++token;
        lastSaid = item.text;
        el.caption.textContent = item.text;
        el.caption.setAttribute("data-tone", item.alert ? "alert" : "");
        if (item.emotion) setEmotion(item.emotion);
        if (!st.voiceOn) {
          lastEnd = now();
          scheduleBaseline(3200);
          if (queue.length) setTimeout(next, 900);
          return;
        }
        speaking = true;
        setSpeakingUI(true);
        if (!urls.tts || (!cache[item.text] && voicePaused())) {
          browserSay(item.text, my); // lines downloaded before still play in the natural voice
          return;
        }
        fetchTts(item.text)
          .then(function (url) {
            if (my === token) playUrl(url, my, item.text);
          })
          .catch(function (err) {
            backOff(err);
            if (my === token) browserSay(item.text, my);
          });
      }

      function say(text, emotion, opts) {
        text = String(text || "").trim();
        opts = opts || {};
        if (!text) {
          if (emotion) setEmotion(emotion);
          return;
        }
        if (opts.high) stop();
        else if (opts.low && (speaking || queue.length)) return; // drop optional cues when busy
        queue.push({ text: text, emotion: emotion || null, alert: !!opts.alert });
        if (queue.length > 4) queue.splice(0, queue.length - 4);
        if (!speaking) next();
      }

      function stop() {
        queue.length = 0;
        token++;
        clearTimeout(watchdog);
        if (ttsCtrl) {
          var c = ttsCtrl;
          ttsCtrl = null;
          try {
            c.abort();
          } catch (e) {}
        }
        if (audio) {
          try {
            audio.pause();
          } catch (e) {}
        }
        if (window.speechSynthesis) {
          try {
            window.speechSynthesis.cancel();
          } catch (e) {}
        }
        if (speaking) {
          speaking = false;
          lastEnd = now();
          setSpeakingUI(false);
          face().setMouth(0, 0);
        }
      }

      return {
        say: say,
        stop: stop,
        unlock: unlock,
        get speaking() {
          return speaking;
        },
        lastSaid: function () {
          return lastSaid;
        },
        lastEnd: function () {
          return lastEnd;
        },
        ctx: function () {
          return actx;
        },
      };
    })();

    function say(text, emotion, opts) {
      speaker.say(text, emotion, opts);
    }

    function beep(freq, dur) {
      var ctx = speaker.ctx();
      if (!st.voiceOn || !ctx) return;
      try {
        var t = ctx.currentTime;
        var o = ctx.createOscillator();
        var g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.16, t + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g);
        g.connect(ctx.destination);
        o.start(t);
        o.stop(t + dur + 0.03);
      } catch (e) {}
    }
    function vibrate(ms) {
      try {
        if (navigator.vibrate) navigator.vibrate(ms);
      } catch (e) {}
    }

    /* ---------------------------------------------------------- voice in (speech recognition) */
    var rec = null;
    var recMode = null;
    var recTimer = 0;
    var recFails = 0;

    function startRec(mode) {
      if (!SR || rec || st.micDenied) return;
      var r;
      try {
        r = new SR();
      } catch (e) {
        return;
      }
      r.continuous = mode === "continuous";
      r.interimResults = true;
      r.maxAlternatives = 1;
      r.lang = navigator.language || "en-US";
      rec = r;
      recMode = mode;
      r.onstart = function () {
        recFails = 0;
        voiceState();
      };
      r.onresult = onResult;
      r.onerror = function (e) {
        var err = e && e.error;
        if (err === "not-allowed" || err === "service-not-allowed" || err === "audio-capture") {
          st.micDenied = true;
          setMic(false, true);
          toast(err === "audio-capture" ? "No microphone found" : "Microphone blocked",
            "Allow microphone access to talk to Pulse - or type instead.", "warning");
          addNote("Microphone unavailable - type your messages instead.");
        } else if (err === "network") {
          recFails++;
        }
      };
      r.onend = function () {
        if (rec === r) rec = null;
        el.interim.textContent = "";
        voiceState();
        if (mode === "continuous" && st.micOn && !st.micDenied && st.phase !== "safety" && st.phase !== "done") {
          clearTimeout(recTimer);
          recTimer = setTimeout(function () {
            if (st.micOn && !rec) startRec("continuous");
          }, Math.min(6000, 300 * Math.pow(2, recFails)));
        }
      };
      try {
        r.start();
      } catch (e) {
        rec = null;
      }
      voiceState();
    }

    function stopRec() {
      clearTimeout(recTimer);
      if (rec) {
        var r = rec;
        rec = null;
        r.onend = null;
        try {
          r.abort();
        } catch (e) {}
      }
      el.interim.textContent = "";
      voiceState();
    }

    function onResult(e) {
      var fin = "";
      var interim = "";
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var res = e.results[i];
        var t = (res[0] && res[0].transcript) || "";
        if (res.isFinal) fin += t;
        else interim += t;
      }
      var recent = speaker.speaking || now() - speaker.lastEnd() < 1500;
      if (interim.trim()) {
        if (recent && isEcho(interim, speaker.lastSaid())) return;
        el.interim.textContent = "“" + interim.trim() + "”";
        if (speaker.speaking && interim.trim().length > 2) speaker.stop(); // barge-in
      }
      if (fin.trim()) {
        el.interim.textContent = "";
        if (recent && isEcho(fin, speaker.lastSaid())) return;
        sendMessage(fin.trim(), "voice");
      }
    }

    function setMic(on, silent) {
      on = !!on && !!SR && !st.micDenied;
      st.micOn = on;
      if (!silent) {
        prefs.mic = on;
        savePrefs(prefs);
      }
      var b = btn("mic");
      if (b) {
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.setAttribute("aria-label", on ? "Turn microphone off" : "Turn microphone on");
        var a = b.querySelector("[data-icon-on]");
        var o = b.querySelector("[data-icon-off]");
        if (a) a.hidden = !on;
        if (o) o.hidden = on;
      }
      if (on && st.begun && st.phase !== "safety" && st.phase !== "done") startRec("continuous");
      else if (!on && recMode === "continuous") stopRec();
      if (!silent) addNote(on ? "Microphone on - just talk to Pulse." : "Microphone off - type or hold to talk.");
      voiceState();
    }

    function setVoice(on, silent) {
      st.voiceOn = !!on;
      if (!silent) {
        prefs.voice = st.voiceOn;
        savePrefs(prefs);
      }
      var b = btn("voice");
      if (b) {
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.setAttribute("aria-label", on ? "Mute the AI voice" : "Unmute the AI voice");
        var a = b.querySelector("[data-icon-on]");
        var o = b.querySelector("[data-icon-off]");
        if (a) a.hidden = !on;
        if (o) o.hidden = on;
      }
      if (!on) speaker.stop();
      if (!silent) addNote(on ? "AI voice on." : "AI voice muted - captions stay on.");
    }

    function voiceState() {
      var state = "off";
      var text = st.micOn ? "Mic starting…" : SR ? "Mic off" : "Type to talk";
      if (st.phase === "safety") {
        state = "alert";
        text = "Safety stop";
      } else if (thinkingOn) {
        state = "thinking";
        text = "Thinking…";
      } else if (speaker && speaker.speaking) {
        state = "speaking";
        text = "Speaking";
      } else if (rec) {
        state = "listening";
        text = recMode === "ptt" ? "Listening (hold)" : "Listening";
      } else if (st.phase === "idle") {
        text = "Coach ready";
      }
      if (el.voiceChip) el.voiceChip.setAttribute("data-state", state);
      if (el.voiceText) el.voiceText.textContent = text;
      if (el.faceWrap) el.faceWrap.setAttribute("data-listening", state === "listening" ? "1" : "0");
      face().setListening(state === "listening");
    }

    function thinking(on) {
      thinkingOn = !!on;
      if (el.thinking) el.thinking.hidden = !on;
      face().setThinking(!!on);
      if (on) setEmotion("focused");
      voiceState();
    }

    /* ---------------------------------------------------------- transcript */
    function addMsg(kind, text, meta) {
      if (!el.log || !text) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      var cls = { user: "live-msg live-msg-user", alert: "live-msg live-msg-alert" }[kind] || "live-msg live-msg-coach";
      var node = IP.h("div", { class: cls }, String(text));
      if (meta) node.appendChild(IP.h("span", { class: "live-msg-meta", text: meta }));
      el.log.appendChild(node);
      while (el.log.children.length > 80) el.log.removeChild(el.log.firstChild);
      el.log.scrollTop = el.log.scrollHeight;
    }
    function addNote(text) {
      if (!el.log || !text) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      el.log.appendChild(IP.h("p", { class: "live-msg-note", text: text }));
      el.log.scrollTop = el.log.scrollHeight;
    }
    function metaFor(res) {
      return { ai: "Pulse · AI", rule: "Pulse", safety: "Safety", fallback: "Pulse · offline", upsell: "Pulse" }[
        res && res.source
      ] || "Pulse";
    }

    /* ---------------------------------------------------------- rendering */
    function render() {
      var list = items();
      var n = list.length;
      var i = idx();
      var item = cur();
      if (item) {
        var e = eff(item);
        var sets = setsOf(item);
        el.exPos.textContent = "Exercise " + (i + 1) + " of " + n;
        el.exCat.textContent = cap(item.category || "exercise");
        el.exName.textContent = item.exercise || "";
        if (item.swapped_from) {
          el.exSwap.textContent = "Swapped from " + item.swapped_from + (item.swap_reason ? " · " + item.swap_reason : "");
          el.exSwap.classList.remove("hidden");
        } else el.exSwap.classList.add("hidden");
        el.exMetric.textContent = e.reps ? String(e.reps) : e.duration_s ? String(e.duration_s) : "–";
        el.exUnit.textContent = e.reps ? "reps" : "sec";
        el.exSetLabel.textContent = "Set " + Math.min(st.set, sets) + " of " + sets;
        el.exSets.textContent = "";
        for (var k = 1; k <= sets && k <= 12; k++) {
          var state = k < st.set ? "done" : k === st.set && st.phase !== "idle" ? "current" : "";
          el.exSets.appendChild(IP.h("span", { class: "live-set", "data-state": state }));
        }
        if (el.exLoad) {
          if (e.load_note) {
            if (el.exLoadText) el.exLoadText.textContent = e.load_note;
            el.exLoad.classList.remove("hidden");
          } else el.exLoad.classList.add("hidden");
        }
        el.exCues.textContent = "";
        (item.cues || []).slice(0, 3).forEach(function (c) {
          el.exCues.appendChild(IP.h("li", { class: "live-cue" }, svgIcon("check"), IP.h("span", { text: c })));
        });
      } else {
        el.exPos.textContent = "All done";
        el.exCat.textContent = "Done";
        el.exName.textContent = n ? "Workout finished" : "No exercises";
        el.exSwap.classList.add("hidden");
        el.exMetric.textContent = "✓";
        el.exUnit.textContent = "";
        el.exSetLabel.textContent = "";
        el.exSets.textContent = "";
        el.exCues.textContent = "";
        if (el.exLoad) el.exLoad.classList.add("hidden");
      }
      var nx = list[i + 1];
      el.nextName.textContent = nx ? nx.exercise : item ? "Finish" : "—";
      el.nextDose.textContent = nx ? nx.dose || "" : item ? "Cool down and celebrate" : "";
      var level = session.intensity || 0;
      if (el.intensity) {
        Array.prototype.forEach.call(el.intensity.children, function (bar, j) {
          bar.setAttribute("data-on", j - 3 <= level ? "1" : "0");
        });
      }
      if (el.intensityLabel) el.intensityLabel.textContent = session.intensity_label || "Standard";
      var frac = 1;
      if (n && st.phase !== "done") frac = (i + (item ? (Math.min(st.set, setsOf(item)) - 1) / setsOf(item) : 0)) / n;
      el.progressBar.style.width = (clamp(frac, 0, 1) * 100).toFixed(1) + "%";
      el.progress.setAttribute("aria-valuenow", String(Math.round(clamp(frac, 0, 1) * 100)));
      renderControls();
      renderTimer();
      statusChip();
      voiceState();
    }

    function renderControls() {
      var paused = st.phase === "paused" || st.phase === "idle";
      var t = btn("toggle");
      if (t) {
        t.setAttribute("aria-label", st.phase === "idle" ? "Begin workout" : paused ? "Resume" : "Pause");
        var pi = t.querySelector("[data-icon-pause]");
        var pl = t.querySelector("[data-icon-play]");
        if (pi) pi.hidden = paused;
        if (pl) pl.hidden = !paused;
      }
      var label = { idle: "Begin", ready: "Start now", work: "Set complete", rest: "Skip rest", paused: "Resume",
        wait: "Saving…", done: "Finished", safety: "Stopped" }[st.phase] || "Set complete";
      if (el.setDoneLabel) el.setDoneLabel.textContent = label;
      var over = st.phase === "done" || st.phase === "safety";
      ["toggle", "next", "setdone", "discomfort", "easier", "harder", "ptt", "end"].forEach(function (a) {
        var b = btn(a);
        if (b) b.disabled = over || (st.phase === "wait" && a !== "discomfort");
      });
      var ptt = btn("ptt");
      if (ptt && !SR) ptt.disabled = true;
    }

    function renderTimer() {
      var e = elapsed();
      var phase = st.phase === "paused" ? st.prev : st.phase;
      var txt = "0:00";
      var frac = 0;
      var label = "Ready";
      if (st.phase === "done") {
        txt = "✓";
        frac = 1;
        label = "Done";
      } else if (st.phase === "safety") {
        txt = "—";
        label = "Stopped";
      } else if (st.phase === "wait") {
        txt = "…";
        frac = 1;
        label = "Saving";
      } else if (st.phase !== "idle" && st.dur > 0) {
        var rem = Math.max(0, st.dur - e);
        txt = fmt(rem);
        frac = clamp(rem / st.dur, 0, 1);
        label = { ready: "Get ready", rest: "Rest", work: "Work" }[phase] || "Work";
      } else if (st.phase !== "idle") {
        txt = fmt(e);
        frac = clamp(e / Math.max(20, (eff(cur()).reps || 10) * 3.5), 0, 1);
        label = "Work";
      }
      if (st.phase === "paused") label = "Paused";
      el.timer.textContent = txt;
      el.phaseLabel.textContent = label;
      if (el.ringValue) el.ringValue.setAttribute("stroke-dashoffset", (RING * (1 - frac)).toFixed(2));
      if (el.ring) el.ring.setAttribute("data-phase", st.phase === "paused" ? "paused" : st.phase === "idle" ? "ready" : phase);
    }

    function statusChip() {
      var map = { idle: ["off", "Ready"], ready: ["thinking", "Get ready"], work: ["speaking", "Working"],
        rest: ["listening", "Resting"], paused: ["paused", "Paused"], wait: ["thinking", "Finishing…"],
        done: ["off", session.status === "completed" ? "Completed" : "Finished"], safety: ["alert", "Stopped for safety"] };
      var m = map[st.phase] || map.idle;
      if (el.status) el.status.setAttribute("data-state", m[0]);
      if (el.statusText) el.statusText.textContent = m[1];
    }

    /* ---------------------------------------------------------- workout flow */
    function setPhase(phase, dur) {
      st.phase = phase;
      st.dur = dur || 0;
      st.start = now();
      st.acc = 0;
      st.fired = {};
      render();
      scheduleBaseline(1800);
    }

    function startWork() {
      var item = cur();
      if (!item) {
        st.phase = "wait";
        render();
        return;
      }
      var e = eff(item);
      setPhase("work", item.timed ? e.duration_s || 30 : 0);
      var sets = setsOf(item);
      say(st.set === 1 ? item.exercise + ". Go!" : "Set " + st.set + " of " + sets + ". Go!", "focused", { low: true });
      beep(990, 0.18);
      vibrate(60);
    }

    function completeSet() {
      if (st.phase !== "work") return;
      var item = cur();
      if (!item) return;
      var sets = setsOf(item);
      var done = st.set;
      var index = idx();
      st.setsDone++;
      vibrate(40);
      if (done < sets) {
        st.set = done + 1;
        setPhase("rest", eff(item).rest_s || 45);
        sendEvent("set_done", { index: index, set: done });
        return;
      }
      st.set = 1;
      if (index + 1 >= items().length) {
        st.phase = "wait";
        render();
      } else {
        session.current_index = index + 1;
        setPhase("rest", eff(item).rest_s || 45);
      }
      sendEvent("exercise_done", { index: index, set: done });
    }

    function primaryAction() {
      if (!st.begun) return begin();
      if (st.phase === "work") completeSet();
      else if (st.phase === "ready" || st.phase === "rest") startWork();
      else if (st.phase === "paused") togglePause();
    }

    function pauseLocal() {
      if (ACTIVE.indexOf(st.phase) === -1) return false;
      st.acc = elapsed();
      st.prev = st.phase;
      st.phase = "paused";
      render();
      return true;
    }
    function resumeLocal() {
      if (st.phase !== "paused") return false;
      st.phase = st.prev || "work";
      st.start = now();
      render();
      return true;
    }
    function togglePause() {
      if (st.phase === "paused") {
        if (resumeLocal()) sendEvent("resumed");
      } else if (pauseLocal()) sendEvent("paused");
    }

    function skip() {
      if (!st.begun || (ACTIVE.indexOf(st.phase) === -1 && st.phase !== "paused")) return;
      var i = idx();
      speaker.stop();
      sendEvent("skipped", { index: i, set: st.set });
      st.set = 1;
      if (i + 1 >= items().length) {
        st.phase = "wait";
        render();
        return;
      }
      session.current_index = i + 1;
      setPhase("ready", 6);
    }

    function tick() {
      paintChat();
      if (ACTIVE.indexOf(st.phase) === -1) {
        renderTimer();
        return;
      }
      var item = cur();
      var e = elapsed();
      if (st.dur > 0) {
        var rem = st.dur - e;
        var r = Math.ceil(rem);
        if (r >= 1 && r <= 3 && !st.fired["b" + r]) {
          st.fired["b" + r] = 1;
          beep(r === 1 ? 784 : 660, 0.12);
        }
        if (st.phase === "work" && st.dur >= 40 && rem <= st.dur / 2 && !st.fired.half) {
          st.fired.half = 1;
          say("Halfway there - keep it smooth.", "encouraging", { low: true });
        }
        if (st.phase === "work" && st.dur >= 25 && rem <= 10 && rem > 7 && !st.fired.ten) {
          st.fired.ten = 1;
          say("Ten seconds left!", "encouraging", { low: true });
        }
        if (st.phase === "rest" && st.dur >= 30 && rem <= 10 && rem > 7 && !st.fired.ten && item) {
          st.fired.ten = 1;
          say("Ten seconds. Get ready for " + item.exercise + ".", "calm", { low: true });
        }
        if (rem <= 0) {
          if (st.phase === "work") completeSet();
          else startWork();
          return;
        }
      }
      renderTimer();
    }

    function begin() {
      if (st.begun || st.phase === "safety" || st.phase === "done") return;
      st.begun = true;
      speaker.unlock();
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.main) el.main.hidden = false;
      requestWakeLock();
      st.set = 1;
      setPhase("ready", idx() === 0 ? 8 : 6);
      setMic(st.micOn, true);
      sendEvent("started", { index: idx(), set: 1 });
      var t = btn("toggle");
      if (t) t.focus();
    }

    function enterSafety(a) {
      a = a || {};
      var em = a.emergency || cfg.emergency || {};
      if (el.safetyReason && a.label) el.safetyReason.textContent = a.suspended ? a.label + "." : "You reported " + a.label + ".";
      if (el.safetyText && st.lastReply) el.safetyText.textContent = st.lastReply;
      if (el.safetyCall && em.number) {
        el.safetyCall.setAttribute("href", "tel:" + em.number);
        var span = el.safetyCall.lastChild;
        if (span && span.nodeType === 3) span.textContent = " Call " + em.number;
      }
      if (st.phase === "safety") return;
      st.phase = "safety";
      stopRec();
      releaseWakeLock();
      closePanel();
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.doneOverlay) el.doneOverlay.hidden = true;
      if (el.main) el.main.hidden = false;
      if (el.safety) el.safety.hidden = false;
      if (el.workout) el.workout.setAttribute("data-mode", "safety");
      setEmotion("concerned");
      render();
      if (el.safety && el.safety.scrollIntoView) el.safety.scrollIntoView({ behavior: "smooth", block: "start" });
      if (el.safetyCall) el.safetyCall.focus({ preventScroll: true });
    }

    function enterDone() {
      if (st.phase === "done" || st.phase === "safety") return;
      st.phase = "done";
      stopRec();
      releaseWakeLock();
      closePanel();
      var completed = session.status === "completed";
      var started = Date.parse(session.started_at || "") || Date.now();
      var ended = Date.parse(session.ended_at || "") || Date.now();
      if (el.summaryTitle) el.summaryTitle.textContent = completed ? "Workout complete!" : "Session ended";
      if (el.summaryTime) el.summaryTime.textContent = String(Math.max(1, Math.round((ended - started) / 60000)));
      if (el.summaryEx) el.summaryEx.textContent = String(completed ? items().length : Math.min(session.current_index || 0, items().length));
      if (el.summarySets) el.summarySets.textContent = String(st.setsDone);
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.main) el.main.hidden = true;
      if (el.doneOverlay) el.doneOverlay.hidden = false;
      setEmotion("proud");
      render();
    }

    /* ---------------------------------------------------------- server round trips */
    function stateFor(extra) {
      var phase = st.phase === "paused" ? "paused" : st.phase;
      var s = { index: idx(), set: st.set, phase: ["work", "rest", "paused", "ready", "done"].indexOf(phase) >= 0 ? phase : "work",
        elapsed_s: Math.round(elapsed()), voice: st.voiceOn };
      if (extra) {
        Object.keys(extra).forEach(function (k) {
          s[k] = extra[k];
        });
      }
      return s;
    }

    function localCue(type) {
      var item = cur();
      var name = item ? item.exercise : "the next one";
      return { started: "Let's go! First up: " + name + ".", set_done: "Nice work! Take your rest.",
        exercise_done: item ? "Great job! Next up: " + name + "." : "Great job!", skipped: "Okay, skipping. Next up: " + name + ".",
        paused: "Paused.", resumed: "Let's go!" }[type] || "";
    }

    async function sendEvent(type, extra, opts) {
      opts = opts || {};
      try {
        var res = await IP.api(urls.event, { method: "POST", json: { type: type, state: stateFor(extra) } });
        applyServer(res, { event: type, silent: opts.silent });
        return res;
      } catch (err) {
        handleError(err, type);
        if (!opts.silent && (!err || !err.status || err.status >= 500 || err.status === 429)) say(localCue(type), "encouraging");
        if (type === "exercise_done" && st.phase === "wait") enterDone();
        return null;
      }
    }

    /* ---------------------------------------------------------- AI chat pacing (docs/ai-guard-and-queue.md) */
    function gateLeft() {
      return Math.max(0, Math.ceil((gateUntil - Date.now()) / 1000));
    }
    /** Seconds AI chat stays paused: IP.ai's "live_coach" cooldown - a refused reply here, a 429, or AI paused for the
     * whole account by any AI widget of this page. */
    function chatPauseLeft() {
      return cooldownLeft("live_coach");
    }
    /** A refused reply: IP.ai keeps the wait (one timer for the whole page), as if the server had answered 429. */
    function holdChat(refused) {
      if (!IP.ai || !IP.ai.noteRefusal) return;
      noting = true;
      try {
        IP.ai.noteRefusal("live_coach", refused.code, refused.retry_after);
      } finally {
        noting = false;
      }
      paintChat();
    }
    var placeholder = el.chatInput ? el.chatInput.getAttribute("placeholder") || "" : "";
    var painted = placeholder;
    /** The chat box's countdown; says once that chat is back when a pause (whoever started it) is over. */
    function paintChat() {
      var left = Math.max(chatPauseLeft(), gateLeft());
      if (left > 0) chatHeld = true;
      else if (chatHeld) {
        chatHeld = false;
        if (st.phase !== "safety" && st.phase !== "done") addNote("Pulse can chat again.");
      }
      if (!el.chatInput) return;
      var text = left > 0 ? "Chat is back in " + formatWait(left) + " - commands still work" : placeholder;
      if (text === painted) return;
      painted = text;
      el.chatInput.setAttribute("placeholder", text);
    }
    function notePaused(seconds, gate) {
      if (Date.now() - pauseNotedAt < 20000) return; // one note per 20 s, however much is said meanwhile
      pauseNotedAt = Date.now();
      var wait = formatWait(seconds) || "a moment";
      addNote(gate ? "Lots of messages from this network - chat is back in " + wait + ". The workout buttons still work."
        : "Chat with Pulse is back in " + wait + " - say next, pause or easier any time.");
    }
    function busy(delta) {
      pending = Math.max(0, pending + delta);
      if (thinkingOn !== pending > 0) thinking(pending > 0);
    }
    function abortMessage() {
      var m = msg;
      msg = null;
      if (m && m.ctrl) {
        try {
          m.ctrl.abort();
        } catch (e) {}
      }
    }
    /** Commands this page can run without the message endpoint (it is rate limited for this network). */
    function runLocal(cmd) {
      if (!cmd || !st.begun || st.phase === "safety" || st.phase === "done") return false;
      if (cmd === "next") skip();
      else if (cmd === "pause") {
        if (pauseLocal()) sendEvent("paused");
      } else if (cmd === "resume") {
        if (resumeLocal()) sendEvent("resumed");
      } else primaryAction();
      return true;
    }
    /** The reply to an AI message: a refusal is spoken once (nothing for a repeat, which pauses nothing - the first
     * answer is on its way) and pauses AI chat through IP.ai - before the line is spoken, so an account-wide pause
     * already keeps the natural voice from asking. */
    function applyReply(res) {
      var refused = (res && res.refused) || null;
      if (refused && refused.code === "duplicate") {
        if (res.session) {
          session = res.session; // the first answer is (or was) on its way - say nothing
          render();
        }
        return;
      }
      if (refused && refused.retry_after > 0) holdChat(refused);
      applyServer(res, { chat: true });
    }
    /** AI chat is paused: the server still runs every safety check and command on these words - without the AI. */
    async function sendPlain(text, via, unsafe) {
      busy(1);
      try {
        var res = await aiRequest(urls.message, {
          json: { text: text, via: via === "voice" ? "voice" : "text", state: stateFor(), ai: false },
          feature: "live_coach",
          safety: true,
          timeout: 12000,
        });
        busy(-1);
        if (res && res.chat_paused && !res.reply) {
          if (res.session) {
            session = res.session;
            render();
          }
          showMemory(res);
          notePaused(Math.max(chatPauseLeft(), gateLeft()), gateLeft() > 0);
        } else applyServer(res, { chat: true });
      } catch (err) {
        busy(-1);
        var status = err && err.status;
        if (unsafe && status !== 401 && status !== 403 && status !== 404 && st.phase !== "safety" && st.phase !== "done") {
          // these words may describe a health problem and could not be checked: straight to the discomfort report
          addNote("Couldn't reach Pulse - if something hurts or feels wrong, tell me where.");
          openPanel();
        } else handleError(err, "message");
      }
    }

    async function sendMessage(text, via, opts) {
      text = String(text || "").trim().slice(0, 500);
      opts = opts || {};
      if (!text) return;
      var local = localCommand(text);
      if (local) {
        if (!opts.quiet) addMsg("user", text);
        if (local === "mic_off") setMic(false);
        else if (local === "voice_off") setVoice(false);
        else {
          setVoice(true);
          say("I'm back! I'll talk you through it.", "happy");
        }
        return;
      }
      var key = normText(text);
      if (msg && key && key === msg.key) return; // the recogniser repeated what Pulse is still answering
      var unsafe = looksUnsafe(text);
      var gate = gateLeft();
      if (opts.quiet) addNote(opts.note || text);
      else addMsg("user", text, via === "voice" ? "You · voice" : null);
      if (via === "voice") {
        // instant "I heard you" acknowledgement while the reply is on its way
        beep(880, 0.06);
        setTimeout(function () {
          beep(1320, 0.07);
        }, 75);
      }
      if (gate > 0 && !unsafe) {
        // every refused request counts against this network's IP: run the core commands here, hold the rest
        if (!runLocal(gateCommand(text))) notePaused(gate, true);
        return;
      }
      if (speaker.speaking) speaker.stop();
      if (gate > 0 || chatPauseLeft() > 0) {
        await sendPlain(text, via, unsafe);
        return;
      }
      abortMessage(); // one AI message at a time: the newest wins ...
      var mine = { ctrl: window.AbortController ? new AbortController() : null, key: key };
      if (!unsafe) msg = mine; // ... but words that may describe a health problem are never cut short by a newer one
      busy(1);
      var plain = false;
      try {
        var res = await aiRequest(urls.message, {
          json: { text: text, via: via === "voice" ? "voice" : "text", state: stateFor() },
          feature: "live_coach",
          timeout: 12000,
          signal: mine.ctrl ? mine.ctrl.signal : undefined,
          safety: unsafe,
        });
        if (msg === mine) msg = null;
        busy(-1);
        applyReply(res);
      } catch (err) {
        if (msg === mine) msg = null;
        busy(-1);
        if (err && err.code === "cooldown_local") plain = true; // AI got paused page-wide meanwhile
        else if (!(err && err.name === "AbortError" && err.reason !== "timeout")) handleError(err, "message");
        // (a newer message or the page closing aborted this one: nothing to say)
      }
      if (plain) await sendPlain(text, via, unsafe);
    }

    function intensity(word, note) {
      var t = Date.now();
      if (t - lastIntensity < 1500) return; // one step per 1.5 s, however fast the button is tapped
      lastIntensity = t;
      sendMessage(word, "text", { quiet: true, note: note });
    }

    function applyServer(res, ctx) {
      if (!res) return;
      ctx = ctx || {};
      var acts = res.actions || [];
      var safetyAct = null;
      acts.forEach(function (a) {
        if (a && (a.type === "safety_stop" || a.type === "suspended")) safetyAct = a;
      });
      var reply = res.reply || res.cue || "";
      if (reply) st.lastReply = reply;
      if (res.session) session = res.session;
      if (reply && !ctx.silent && (ctx.chat || safetyAct || ctx.event)) addMsg(safetyAct ? "alert" : "coach", reply, ctx.event ? null : metaFor(res));
      if (reply && !ctx.silent) say(reply, res.emotion, { high: !!(ctx.chat || safetyAct), alert: !!safetyAct });
      else if (res.emotion && !ctx.silent) setEmotion(res.emotion);
      acts.forEach(function (a) {
        handleAction(a || {});
      });
      if (!ctx.silent) showMemory(res);
      if (res.source === "upsell" && el.upsell) el.upsell.classList.remove("hidden");
      if (res.session) {
        if (res.session.status === "stopped_for_safety") enterSafety(safetyAct || {});
        else if (res.session.ended) enterDone();
      }
      render();
    }

    /** "Remember this?" / "Forgotten - Undo" cards (54-memory.js) under the transcript: never saved without a tap. */
    function showMemory(res) {
      if (!IP.memory || !el.log || !res || !res.memory || !res.memory.length) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      IP.memory.render(el.log, res.memory);
      while (el.log.children.length > 80) el.log.removeChild(el.log.firstChild);
      el.log.scrollTop = el.log.scrollHeight;
    }

    function handleAction(a) {
      switch (a.type) {
        case "pause":
          pauseLocal();
          break;
        case "resume":
          resumeLocal();
          break;
        case "next":
          st.set = 1;
          if (st.begun) setPhase("ready", 6);
          break;
        case "replace_exercise":
          st.set = 1;
          if (st.phase === "paused") {
            st.prev = "ready";
            st.acc = 0;
            st.dur = 6;
          } else if (st.begun) setPhase("ready", 6);
          break;
        case "set_done":
          if (st.phase === "work") completeSet();
          else if (st.phase === "ready" || st.phase === "rest") startWork();
          break;
        case "quit":
        case "completed":
        case "ended":
          enterDone();
          break;
        case "safety_stop":
          enterSafety(a);
          break;
        case "suspended":
          enterSafety({ label: "Your gym activities are paused for your safety", suspended: true, emergency: a.emergency });
          break;
        case "mic_off":
          setMic(false);
          break;
        case "voice_off":
          setVoice(false);
          break;
        case "voice_on":
          setVoice(true);
          break;
        case "upsell":
          if (el.upsell) el.upsell.classList.remove("hidden");
          break;
      }
    }

    function handleError(err, ctx) {
      var status = err && err.status;
      var data = (err && err.data) || {};
      if (status === 401) {
        toast("Signed out", "Please sign in again to keep training.", "warning");
        return;
      }
      if (status === 403) {
        if (data.suspended) enterSafety({ label: "Your gym activities are paused for your safety", suspended: true });
        toast("Not available", data.detail || (err && err.message) || "", "warning");
        return;
      }
      if (status === 404) {
        toast("Session not found", "This session no longer exists.", "warning");
        return;
      }
      if (ctx !== "message" && ctx !== "discomfort") return;
      var wait = err && err.retryAfter > 0 ? err.retryAfter : 0;
      var text = status === 429
        ? "One moment - I'm catching my breath. Try again in " + (wait ? spokenWait(wait) : "a few seconds") + "."
        : "Sorry, I didn't catch that in time - say next, pause or easier.";
      addMsg("coach", text, "Pulse · offline");
      say(text, "calm", { high: true });
    }

    /* ---------------------------------------------------------- discomfort panel */
    var disc = { part: null, sev: "moderate" };
    function openPanel() {
      if (!el.panel || st.phase === "safety" || st.phase === "done") return;
      el.panel.hidden = false;
      var b = btn("discomfort");
      if (b) b.setAttribute("aria-expanded", "true");
      speaker.stop();
      if (pauseLocal()) sendEvent("paused", null, { silent: true });
      say("Okay - tell me where you feel it.", "empathetic", { high: true });
      var first = el.panel.querySelector("[data-part]");
      if (first) first.focus({ preventScroll: true });
      if (el.panel.scrollIntoView) el.panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    function closePanel() {
      if (!el.panel) return;
      el.panel.hidden = true;
      var b = btn("discomfort");
      if (b) b.setAttribute("aria-expanded", "false");
    }
    function paintDisc() {
      root.querySelectorAll("[data-part]").forEach(function (c) {
        c.setAttribute("aria-pressed", c.getAttribute("data-part") === disc.part ? "true" : "false");
      });
      root.querySelectorAll("[data-sev]").forEach(function (c) {
        c.setAttribute("aria-pressed", c.getAttribute("data-sev") === disc.sev ? "true" : "false");
      });
      var serious = disc.sev === "severe" || disc.part === "chest" || disc.part === "head";
      if (el.warn) el.warn.classList.toggle("hidden", !serious);
      var r = btn("report");
      if (r) r.disabled = !disc.part;
    }
    async function reportDiscomfort() {
      if (!disc.part) return;
      var r = btn("report");
      if (r) r.disabled = true;
      var note = el.note ? el.note.value.trim().slice(0, 300) : "";
      addMsg("user", "I feel " + disc.sev + " discomfort in my " + (disc.part === "other" ? "body" : disc.part) + (note ? " - " + note : ""));
      busy(1);
      try {
        // a safety flow: never refused by a local AI cooldown, never aborted when the page hides (IP.ai safety)
        var res = await aiRequest(urls.discomfort, {
          json: { body_part: disc.part, severity: disc.sev, note: note || null, state: stateFor() },
          feature: "live_coach",
          safety: true,
        });
        busy(-1);
        closePanel();
        applyServer(res, { chat: true });
        disc.part = null;
        disc.sev = "moderate";
        if (el.note) el.note.value = "";
        paintDisc();
      } catch (err) {
        busy(-1);
        if (r) r.disabled = false;
        var serious = disc.sev === "severe" || disc.part === "chest" || disc.part === "head";
        if (serious) {
          var num = (cfg.emergency && cfg.emergency.number) || "112";
          st.lastReply = "Please stop now and rest. If it's severe or getting worse, call " + num + " right away.";
          say(st.lastReply, "concerned", { high: true, alert: true });
          enterSafety({ label: disc.part + " discomfort", emergency: cfg.emergency });
        } else handleError(err, "discomfort");
      }
    }

    /* ---------------------------------------------------------- end + wake lock */
    async function endSession() {
      if (st.phase === "done" || st.phase === "safety") return;
      if (!window.confirm("End this workout now?")) return;
      speaker.stop();
      try {
        var res = await IP.api(urls.end, { method: "POST", json: { reason: "user_ended" } });
        applyServer(res, { chat: true });
      } catch (err) {
        handleError(err, "end");
      }
      enterDone();
    }

    var wakeLock = null;
    function requestWakeLock() {
      try {
        if (!navigator.wakeLock || !navigator.wakeLock.request || wakeLock) return;
        navigator.wakeLock.request("screen").then(function (l) {
          wakeLock = l;
          l.addEventListener("release", function () {
            wakeLock = null;
          });
        }).catch(function () {});
      } catch (e) {}
    }
    function releaseWakeLock() {
      if (wakeLock) {
        try {
          wakeLock.release();
        } catch (e) {}
        wakeLock = null;
      }
    }

    /* ---------------------------------------------------------- wiring */
    root.addEventListener("click", function (e) {
      var chip = e.target.closest && e.target.closest("[data-part],[data-sev]");
      if (chip && root.contains(chip)) {
        if (chip.hasAttribute("data-part")) disc.part = chip.getAttribute("data-part");
        else disc.sev = chip.getAttribute("data-sev");
        paintDisc();
        return;
      }
      var b = e.target.closest && e.target.closest("[data-act]");
      if (!b || !root.contains(b) || b.disabled) return;
      switch (b.getAttribute("data-act")) {
        case "begin":
          begin();
          break;
        case "toggle":
          if (!st.begun) begin();
          else togglePause();
          break;
        case "next":
          skip();
          break;
        case "end":
          endSession();
          break;
        case "mic":
          setMic(!st.micOn);
          break;
        case "voice":
          setVoice(!st.voiceOn);
          break;
        case "setdone":
          primaryAction();
          break;
        case "discomfort":
          if (el.panel && !el.panel.hidden) closePanel();
          else openPanel();
          break;
        case "cancel-discomfort":
          closePanel();
          break;
        case "report":
          reportDiscomfort();
          break;
        case "easier":
          intensity("easier", "Intensity ↓ easier");
          break;
        case "harder":
          intensity("harder", "Intensity ↑ harder");
          break;
        case "im-ok":
          if (el.okNote) el.okNote.classList.remove("hidden");
          b.disabled = true;
          say("I'm glad you feel better. Please keep resting - your gym team will check in before you train again.", "empathetic", { high: true });
          break;
      }
    });

    var ptt = btn("ptt");
    if (ptt) {
      var pttDown = function (e) {
        if (e && e.button > 0) return;
        if (e && e.preventDefault) e.preventDefault();
        if (ptt.disabled) return;
        speaker.unlock();
        speaker.stop();
        ptt.setAttribute("data-active", "1");
        if (!rec) startRec("ptt");
      };
      var pttUp = function () {
        if (ptt.getAttribute("data-active") !== "1") return;
        ptt.removeAttribute("data-active");
        if (rec && recMode === "ptt") {
          try {
            rec.stop();
          } catch (x) {}
        }
      };
      ptt.addEventListener("pointerdown", pttDown);
      ptt.addEventListener("pointerup", pttUp);
      ptt.addEventListener("pointerleave", pttUp);
      ptt.addEventListener("pointercancel", pttUp);
      ptt.addEventListener("contextmenu", function (e) {
        e.preventDefault();
      });
      ptt.addEventListener("keydown", function (e) {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          pttDown();
        }
      });
      ptt.addEventListener("keyup", function (e) {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          pttUp();
        }
      });
    }

    if (el.chatForm) {
      el.chatForm.setAttribute("data-no-busy", "");
      el.chatForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var t = el.chatInput.value.trim();
        if (!t) return;
        el.chatInput.value = "";
        speaker.unlock();
        sendMessage(t, "text");
      });
    }

    document.addEventListener("keydown", function (e) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target;
      if (t && t.closest && t.closest("input, textarea, select, button, a, summary, [contenteditable]")) return;
      var k = (e.key || "").toLowerCase();
      if (k === " " || k === "spacebar") {
        e.preventDefault();
        if (!st.begun) begin();
        else togglePause();
      } else if (k === "m") setMic(!st.micOn);
      else if (k === "n") skip();
      else if (k === "v") setVoice(!st.voiceOn);
    });

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && st.begun && (ACTIVE.indexOf(st.phase) >= 0 || st.phase === "paused")) requestWakeLock();
    });
    if (IP.ai && IP.ai.onCooldown) {
      IP.ai.onCooldown(function (info) {
        // HTTP 429 without a code on the message endpoint: the network's rate limit, not an AI refusal (a refused
        // reply handed to IP.ai never is, whatever its code)
        if (info && !noting && !info.ended && !info.code && info.feature === "live_coach" && info.seconds > 0) {
          gateUntil = Math.max(gateUntil, Date.now() + info.seconds * 1000);
        }
        paintChat(); // a pause started or ended - here or in any other AI widget of this page
      });
    }
    document.addEventListener("submit", function (e) {
      if (!e.defaultPrevented && isLogoutForm(e.target)) loggingOut = true; // after data-confirm had its say
    });
    window.addEventListener("pageshow", function (e) {
      if (e && e.persisted) loggingOut = false;
    });
    window.addEventListener("pagehide", function () {
      stopRec();
      speaker.stop(); // also drops the voice line being downloaded
      abortMessage(); // IP.ai (01-ai.js) has already aborted this page's AI requests - safety reports excepted
      // a workout left mid-set is paused on the server too - unless the page goes because of a sign-out: the session
      // cookie is gone by then, and a request failing CSRF counts against the gym's IP
      if (!loggingOut && st.begun && pauseLocal()) {
        IP.api(urls.event, { method: "POST", json: { type: "paused", state: stateFor() }, keepalive: true }).catch(function () {});
      }
    });

    if (!SR) {
      root.querySelectorAll("[data-mic-unsupported]").forEach(function (n) {
        n.classList.remove("hidden");
      });
      var note = root.querySelector("[data-mic-note]");
      if (note) note.textContent = "Voice input isn't supported in this browser - type instead. Pulse can still talk to you.";
    }

    // initial state (before Begin the mic toggle only records the preference - recognition starts on Begin)
    setVoice(st.voiceOn, true);
    setMic(prefs.mic !== false && !!SR, true);
    if (session.stopped_for_safety) {
      st.lastReply = el.safetyText ? el.safetyText.textContent : "";
      enterSafety({});
    } else if (session.ended) {
      enterDone();
    } else {
      render();
      face().setEmotion("happy");
    }
    setInterval(tick, 250);

    // diagnostics for tests
    IP.liveCoach = {
      state: st,
      session: function () {
        return session;
      },
      chat: function () {
        return { paused: chatPauseLeft(), gate: gateLeft(), inFlight: !!msg, pending: pending };
      },
    };
  });

  IP.liveCoachUtil = { isEcho: isEcho, localCommand: localCommand, fmt: fmt, normText: normText, looksUnsafe: looksUnsafe,
    gateCommand: gateCommand, spokenWait: spokenWait, isLogoutForm: isLogoutForm };
})();
