/* Live coach face (WP-K): a Memoji-style animated avatar drawn on <canvas data-component="coach-face">.
 * Emotion presets drive brows, eyes, cheeks, mouth and head tilt through critically-damped springs; idle
 * blinking, breathing and glances keep it alive; the mouth follows a lip-sync value (0..1) with rounder shapes
 * for O/U vowels. Respects prefers-reduced-motion and pauses while hidden.
 *   IP.coachFace.setEmotion(name)       neutral|happy|excited|encouraging|calm|concerned|empathetic|proud|focused
 *   IP.coachFace.setMouth(value, round) lip-sync 0..1 (+ optional roundness 0..1)
 *   IP.coachFace.setSpeaking(bool)      IP.coachFace.setListening(bool)      IP.coachFace.setThinking(bool)
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var EMOTIONS = {
    neutral: { browY: 0, browTilt: 0, eyeOpen: 1, squint: 0.06, smile: 0.32, mouthW: 1, mouthOpen: 0, blush: 0.25, tilt: 0 },
    happy: { browY: -2, browTilt: -0.05, eyeOpen: 0.96, squint: 0.36, smile: 0.88, mouthW: 1.08, mouthOpen: 0.12, blush: 0.5, tilt: -0.035 },
    excited: { browY: -6, browTilt: -0.1, eyeOpen: 1.12, squint: 0.16, smile: 1, mouthW: 1.14, mouthOpen: 0.32, blush: 0.62, tilt: 0.045 },
    encouraging: { browY: -3, browTilt: -0.06, eyeOpen: 1.02, squint: 0.26, smile: 0.72, mouthW: 1.05, mouthOpen: 0.07, blush: 0.42, tilt: 0.05 },
    calm: { browY: 1, browTilt: 0.03, eyeOpen: 0.8, squint: 0.14, smile: 0.42, mouthW: 0.96, mouthOpen: 0, blush: 0.3, tilt: -0.02 },
    concerned: { browY: -3, browTilt: 0.4, eyeOpen: 1.06, squint: 0, smile: -0.38, mouthW: 0.88, mouthOpen: 0.05, blush: 0.06, tilt: 0.065 },
    empathetic: { browY: -2, browTilt: 0.28, eyeOpen: 0.9, squint: 0.12, smile: 0.12, mouthW: 0.94, mouthOpen: 0, blush: 0.34, tilt: 0.085 },
    proud: { browY: -4, browTilt: -0.08, eyeOpen: 0.9, squint: 0.44, smile: 0.96, mouthW: 1.12, mouthOpen: 0.16, blush: 0.56, tilt: -0.06 },
    focused: { browY: 3, browTilt: -0.22, eyeOpen: 0.9, squint: 0.2, smile: 0.12, mouthW: 0.94, mouthOpen: 0, blush: 0.2, tilt: 0 },
  };
  var KEYS = Object.keys(EMOTIONS.neutral);
  var shared = { emotion: "neutral", mouth: 0, round: 0, speaking: false, listening: false, thinking: false, poke: 0 };
  var faces = [];

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  IP.coachFace = {
    setEmotion: function (name) {
      shared.emotion = EMOTIONS[name] ? name : "neutral";
    },
    setMouth: function (value, round) {
      shared.mouth = clamp(+value || 0, 0, 1);
      if (round != null) shared.round = clamp(+round || 0, 0, 1);
    },
    setSpeaking: function (on) {
      shared.speaking = !!on;
      if (!on) {
        shared.mouth = 0;
        shared.round = 0;
      }
    },
    setListening: function (on) {
      shared.listening = !!on;
    },
    setThinking: function (on) {
      shared.thinking = !!on;
    },
    emotion: function () {
      return shared.emotion;
    },
    emotions: KEYS.length ? Object.keys(EMOTIONS) : [],
  };

  /* ---------------------------------------------------------------- drawing (virtual 300 x 300 canvas) */
  var SKIN_HI = "#ffe3cc";
  var SKIN = "#f7c49e";
  var SKIN_LO = "#e3a079";
  var HAIR_HI = "#5a3b26";
  var HAIR = "#36231a";
  var HAIR_LO = "#1f140e";
  var LIP = "#c85a6a";

  function headPath(ctx) {
    ctx.beginPath();
    ctx.moveTo(150, 44);
    ctx.bezierCurveTo(206, 44, 236, 86, 236, 138);
    ctx.bezierCurveTo(236, 190, 204, 232, 150, 234);
    ctx.bezierCurveTo(96, 232, 64, 190, 64, 138);
    ctx.bezierCurveTo(64, 86, 94, 44, 150, 44);
    ctx.closePath();
  }

  function radial(ctx, x0, y0, r0, x1, y1, r1, stops) {
    var g = ctx.createRadialGradient(x0, y0, r0, x1, y1, r1);
    for (var i = 0; i < stops.length; i++) g.addColorStop(stops[i][0], stops[i][1]);
    return g;
  }

  function linear(ctx, x0, y0, x1, y1, stops) {
    var g = ctx.createLinearGradient(x0, y0, x1, y1);
    for (var i = 0; i < stops.length; i++) g.addColorStop(stops[i][0], stops[i][1]);
    return g;
  }

  function drawTorso(ctx, p) {
    ctx.save();
    ctx.translate(0, p.bob * 0.45);
    ctx.beginPath();
    ctx.moveTo(26, 304);
    ctx.bezierCurveTo(32, 270, 78, 252, 122, 248);
    ctx.lineTo(178, 248);
    ctx.bezierCurveTo(222, 252, 268, 270, 274, 304);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 40, 250, 260, 304, [[0, "#3d9bff"], [0.55, "#0a84ff"], [1, "#5e5ce6"]]);
    ctx.fill();
    // shoulder highlight + zip line
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = radial(ctx, 96, 262, 4, 96, 262, 70, [[0, "#ffffff"], [1, "rgba(255,255,255,0)"]]);
    ctx.fill();
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(150, 262);
    ctx.lineTo(150, 304);
    ctx.stroke();
    ctx.globalAlpha = 1;
    // neck
    ctx.beginPath();
    ctx.moveTo(124, 206);
    ctx.lineTo(176, 206);
    ctx.lineTo(178, 252);
    ctx.quadraticCurveTo(150, 266, 122, 252);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 0, 206, 0, 262, [[0, "#d9906a"], [0.45, SKIN_LO], [1, SKIN]]);
    ctx.fill();
    // collar
    ctx.beginPath();
    ctx.moveTo(116, 250);
    ctx.quadraticCurveTo(150, 272, 184, 250);
    ctx.lineWidth = 7;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#0866d8";
    ctx.stroke();
    ctx.restore();
  }

  function drawEar(ctx, x, flip) {
    ctx.save();
    ctx.translate(x, 146);
    ctx.scale(flip, 1);
    ctx.beginPath();
    ctx.ellipse(0, 0, 13, 20, -0.12, 0, Math.PI * 2);
    ctx.fillStyle = radial(ctx, -3, -4, 2, 0, 0, 22, [[0, SKIN_HI], [0.6, SKIN], [1, SKIN_LO]]);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(2, 1, 6, 11, -0.12, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(196, 116, 84, 0.35)";
    ctx.fill();
    ctx.restore();
  }

  function drawHair(ctx) {
    ctx.beginPath();
    ctx.moveTo(63, 136);
    ctx.bezierCurveTo(54, 74, 96, 30, 152, 30);
    ctx.bezierCurveTo(208, 30, 248, 70, 237, 136);
    ctx.bezierCurveTo(231, 114, 222, 104, 208, 99);
    ctx.bezierCurveTo(178, 108, 132, 108, 104, 97);
    ctx.bezierCurveTo(90, 104, 74, 114, 63, 136);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 80, 30, 220, 130, [[0, HAIR_HI], [0.5, HAIR], [1, HAIR_LO]]);
    ctx.fill();
    // glossy streak
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.beginPath();
    ctx.moveTo(108, 48);
    ctx.quadraticCurveTo(150, 34, 196, 52);
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#a07850";
    ctx.stroke();
    ctx.restore();
  }

  function drawHeadband(ctx) {
    ctx.save();
    headPath(ctx);
    ctx.clip();
    ctx.beginPath();
    ctx.moveTo(52, 104);
    ctx.quadraticCurveTo(150, 60, 248, 104);
    ctx.lineTo(248, 121);
    ctx.quadraticCurveTo(150, 77, 52, 121);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 60, 0, 240, 0, [[0, "#0a84ff"], [0.6, "#5e5ce6"], [1, "#bf5af2"]]);
    ctx.fill();
    ctx.globalAlpha = 0.35;
    ctx.beginPath();
    ctx.moveTo(60, 106);
    ctx.quadraticCurveTo(150, 65, 240, 106);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
    ctx.restore();
  }

  function drawBrow(ctx, cx, y, tilt, side, lift) {
    var inner = cx - side * 17;
    var outer = cx + side * 17;
    var yi = y - tilt * 11 - lift;
    var yo = y + 2 - lift * 0.4;
    ctx.beginPath();
    ctx.moveTo(inner, yi);
    ctx.quadraticCurveTo(cx, Math.min(yi, yo) - 6, outer, yo);
    ctx.lineWidth = 7.5;
    ctx.lineCap = "round";
    ctx.strokeStyle = HAIR;
    ctx.stroke();
  }

  function drawEye(ctx, cx, cy, open, squint, lookX, lookY, happyArc) {
    var rx = 15;
    var top = 17 * open;
    var bottom = 15 * open * (1 - squint * 0.9);
    if (open < 0.14) {
      // closed: a soft lid line (curves up when smiling)
      ctx.beginPath();
      ctx.moveTo(cx - rx, cy);
      ctx.quadraticCurveTo(cx, cy + (happyArc ? -7 : 4), cx + rx, cy);
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.strokeStyle = "#2a1a12";
      ctx.stroke();
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(cx - rx, cy);
    ctx.bezierCurveTo(cx - rx, cy - top * 1.32, cx + rx, cy - top * 1.32, cx + rx, cy);
    ctx.bezierCurveTo(cx + rx, cy + bottom * 1.3, cx - rx, cy + bottom * 1.3, cx - rx, cy);
    ctx.closePath();
    ctx.fillStyle = radial(ctx, cx, cy - 4, 2, cx, cy, 20, [[0, "#ffffff"], [1, "#e6e9ef"]]);
    ctx.fill();
    ctx.clip();
    var ix = cx + lookX;
    var iy = cy + lookY + 1;
    ctx.beginPath();
    ctx.arc(ix, iy, 10.5, 0, Math.PI * 2);
    ctx.fillStyle = radial(ctx, ix, iy, 1, ix, iy, 11, [[0, "#9a6234"], [0.55, "#6a3f1f"], [0.9, "#3a2010"], [1, "#24130a"]]);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix, iy, 5.2, 0, Math.PI * 2);
    ctx.fillStyle = "#0c0806";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix - 3.6, iy - 4.2, 3.3, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.95)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix + 3.4, iy + 3.2, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.fill();
    // upper-lid shadow inside the eye
    ctx.fillStyle = "rgba(120, 70, 50, 0.18)";
    ctx.fillRect(cx - rx, cy - top * 1.4, rx * 2, 5);
    ctx.restore();
    // smiling eyes: the cheeks push the lower lid up
    if (squint > 0.2) {
      ctx.beginPath();
      ctx.moveTo(cx - rx + 2, cy + bottom * 1.02);
      ctx.quadraticCurveTo(cx, cy + bottom * 1.02 - 2 - squint * 4, cx + rx - 2, cy + bottom * 1.02);
      ctx.lineWidth = 2;
      ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(170, 92, 62, " + (0.55 * squint).toFixed(3) + ")";
      ctx.stroke();
    }
    // lash line
    ctx.beginPath();
    ctx.moveTo(cx - rx - 1, cy + 0.5);
    ctx.bezierCurveTo(cx - rx, cy - top * 1.34, cx + rx, cy - top * 1.34, cx + rx + 1, cy + 0.5);
    ctx.lineWidth = 2.8;
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(34, 20, 12, 0.92)";
    ctx.stroke();
  }

  function drawMouth(ctx, p) {
    var cx = 150;
    var cy = 198;
    var round = p.round;
    var open = clamp(p.mouthOpen + p.mouth * 0.95, 0, 1);
    var w = 46 * p.mouthW * (1 - 0.38 * round * Math.min(1, open * 3));
    var h = (1.5 + open * 25) * (1 + 0.45 * round);
    var corner = -p.smile * 7;
    var lx = cx - w / 2;
    var rx = cx + w / 2;
    var upY = cy - 2.5 - p.smile * 1.2 - h * 0.18;
    var dnY = cy + 3 + p.smile * 9 + h;
    if (h < 4.2) {
      ctx.beginPath();
      ctx.moveTo(lx, cy + corner);
      ctx.quadraticCurveTo(cx, cy + p.smile * 11, rx, cy + corner);
      ctx.lineWidth = 4.2;
      ctx.lineCap = "round";
      ctx.strokeStyle = LIP;
      ctx.stroke();
      // tiny dimples
      ctx.globalAlpha = 0.35 * clamp(p.smile, 0, 1);
      ctx.beginPath();
      ctx.arc(lx - 3, cy + corner - 1.5, 2, 0, Math.PI * 2);
      ctx.arc(rx + 3, cy + corner - 1.5, 2, 0, Math.PI * 2);
      ctx.fillStyle = "#c46a52";
      ctx.fill();
      ctx.globalAlpha = 1;
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(lx, cy + corner);
    ctx.quadraticCurveTo(cx, upY, rx, cy + corner);
    ctx.quadraticCurveTo(cx, dnY, lx, cy + corner);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 0, upY, 0, dnY, [[0, "#3a0d14"], [1, "#6b1d2a"]]);
    ctx.fill();
    ctx.clip();
    // teeth
    ctx.fillStyle = "#fbfbfd";
    ctx.fillRect(lx, upY - 6, w, Math.min(9, h * 0.4) + 6);
    // tongue
    ctx.beginPath();
    ctx.ellipse(cx, dnY - 3, w * 0.3, Math.max(3, h * 0.32), 0, 0, Math.PI * 2);
    ctx.fillStyle = "#e46d7f";
    ctx.fill();
    ctx.restore();
    ctx.beginPath();
    ctx.moveTo(lx, cy + corner);
    ctx.quadraticCurveTo(cx, upY, rx, cy + corner);
    ctx.quadraticCurveTo(cx, dnY, lx, cy + corner);
    ctx.closePath();
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.strokeStyle = LIP;
    ctx.stroke();
  }

  function draw(ctx, p, t) {
    ctx.clearRect(0, 0, 300, 300);
    drawTorso(ctx, p);
    ctx.save();
    // head group: bob + tilt around the neck
    ctx.translate(150 + p.swayX, 226 + p.bob);
    ctx.rotate(p.tilt + p.sway + p.nod);
    ctx.translate(-150, -226);
    drawEar(ctx, 64, -1);
    drawEar(ctx, 236, 1);
    // head with a soft drop shadow
    ctx.save();
    ctx.shadowColor = "rgba(40, 30, 90, 0.28)";
    ctx.shadowBlur = 26;
    ctx.shadowOffsetY = 10;
    headPath(ctx);
    ctx.fillStyle = radial(ctx, 122, 104, 8, 150, 140, 170, [[0, SKIN_HI], [0.42, SKIN], [1, SKIN_LO]]);
    ctx.fill();
    ctx.restore();
    // rim light + chin shade
    ctx.save();
    headPath(ctx);
    ctx.clip();
    ctx.fillStyle = radial(ctx, 98, 82, 4, 98, 82, 96, [[0, "rgba(255,255,255,0.32)"], [1, "rgba(255,255,255,0)"]]);
    ctx.fillRect(40, 40, 220, 200);
    ctx.fillStyle = radial(ctx, 150, 250, 20, 150, 250, 92, [[0, "rgba(160, 80, 50, 0.22)"], [1, "rgba(160, 80, 50, 0)"]]);
    ctx.fillRect(40, 140, 220, 100);
    ctx.restore();
    drawHair(ctx);
    drawHeadband(ctx);
    // cheeks
    var blush = clamp(p.blush, 0, 1);
    if (blush > 0.01) {
      ctx.fillStyle = radial(ctx, 98, 180, 2, 98, 180, 24, [[0, "rgba(255, 105, 120, " + (0.42 * blush).toFixed(3) + ")"], [1, "rgba(255,105,120,0)"]]);
      ctx.fillRect(70, 152, 56, 56);
      ctx.fillStyle = radial(ctx, 202, 180, 2, 202, 180, 24, [[0, "rgba(255, 105, 120, " + (0.42 * blush).toFixed(3) + ")"], [1, "rgba(255,105,120,0)"]]);
      ctx.fillRect(174, 152, 56, 56);
    }
    var lift = p.speakLift;
    drawBrow(ctx, 112, 113 + p.browY, p.browTilt, 1, lift);
    drawBrow(ctx, 188, 113 + p.browY, p.browTilt, -1, lift);
    var open = clamp(p.eyeOpen * (1 - p.blink), 0, 1.25);
    var happyArc = p.smile > 0.6;
    drawEye(ctx, 112, 143, open, p.squint, p.lookX, p.lookY, happyArc);
    drawEye(ctx, 188, 143, open, p.squint, p.lookX, p.lookY, happyArc);
    // nose
    ctx.beginPath();
    ctx.moveTo(142, 174);
    ctx.quadraticCurveTo(150, 181, 158, 174);
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(170, 92, 62, 0.42)";
    ctx.stroke();
    ctx.beginPath();
    ctx.ellipse(149, 164, 5, 7, 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255, 255, 255, 0.18)";
    ctx.fill();
    drawMouth(ctx, p);
    ctx.restore();
    // let the shoulders fade into the stage instead of ending in a hard edge
    ctx.save();
    ctx.globalCompositeOperation = "destination-out";
    ctx.fillStyle = linear(ctx, 0, 258, 0, 300, [[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,1)"]]);
    ctx.fillRect(0, 258, 300, 44);
    ctx.restore();
  }

  /* ---------------------------------------------------------------- animation */
  function makeState(emotion) {
    var e = EMOTIONS[emotion] || EMOTIONS.neutral;
    var s = { vel: {}, blink: 0, lookX: 0, lookY: 0, mouth: 0, round: 0, speakLift: 0 };
    KEYS.forEach(function (k) {
      s[k] = e[k];
      s.vel[k] = 0;
    });
    return s;
  }

  function step(face, now) {
    var s = face.state;
    var dt = Math.min(0.05, Math.max(0.001, (now - face.last) / 1000));
    face.last = now;
    var reduced = face.reduced;
    var target = EMOTIONS[shared.emotion] || EMOTIONS.neutral;
    var k = 70;
    var c = 2 * Math.sqrt(k);
    KEYS.forEach(function (key) {
      var goal = target[key];
      if (key === "eyeOpen" && shared.listening) goal += 0.06;
      if (key === "tilt" && shared.listening) goal += 0.04;
      if (key === "browY" && shared.thinking) goal -= 2;
      var a = k * (goal - s[key]) - c * s.vel[key];
      s.vel[key] += a * dt;
      s[key] += s.vel[key] * dt;
    });
    // lip-sync (fast attack, softer release)
    var m = shared.speaking ? shared.mouth : 0;
    s.mouth += (m - s.mouth) * (m > s.mouth ? 0.55 : 0.22);
    s.round += ((shared.speaking ? shared.round : 0) - s.round) * 0.25;
    s.speakLift += ((shared.speaking ? Math.max(0, shared.mouth - 0.55) * 6 : 0) - s.speakLift) * 0.12;
    // blinking every 3-6 s (sometimes a double blink)
    if (now >= face.nextBlink) {
      face.blinkStart = now;
      face.nextBlink = now + (reduced ? 5000 : 3000) + Math.random() * 3000;
      if (Math.random() < 0.18) face.doubleAt = now + 260;
    }
    if (face.doubleAt && now >= face.doubleAt) {
      face.blinkStart = now;
      face.doubleAt = 0;
    }
    var bp = (now - face.blinkStart) / 150;
    s.blink = bp >= 0 && bp <= 1 ? Math.sin(bp * Math.PI) : 0;
    // glances
    if (!reduced && now >= face.nextGlance) {
      face.nextGlance = now + 2000 + Math.random() * 3200;
      face.lookTarget = [Math.random() * 7 - 3.5, Math.random() * 4 - 2];
    }
    var lt = face.lookTarget;
    if (shared.listening || reduced) lt = [0, 0.5];
    if (shared.thinking) lt = [-4.5, -4];
    s.lookX += (lt[0] - s.lookX) * 0.08;
    s.lookY += (lt[1] - s.lookY) * 0.08;
    var t = now / 1000;
    var p = {
      bob: reduced ? 0 : Math.sin(t * 1.55) * 2.2,
      sway: reduced ? 0 : Math.sin(t * 0.63) * 0.014,
      swayX: reduced ? 0 : Math.sin(t * 0.41) * 1.2,
      nod: reduced || !shared.speaking ? 0 : Math.sin(t * 7.3) * s.mouth * 0.022,
      blink: s.blink,
      lookX: s.lookX,
      lookY: s.lookY,
      mouth: s.mouth,
      round: s.round,
      speakLift: s.speakLift,
    };
    KEYS.forEach(function (key) {
      p[key] = s[key];
    });
    if (face.poke && now - face.poke < 900) {
      p.smile = Math.max(p.smile, 0.95);
      p.squint = Math.max(p.squint, 0.4);
      p.blush = Math.max(p.blush, 0.6);
    }
    return p;
  }

  function resize(face) {
    var el = face.canvas;
    var w = el.clientWidth || 300;
    var h = el.clientHeight || w;
    var dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    var bw = Math.max(1, Math.round(w * dpr));
    var bh = Math.max(1, Math.round(h * dpr));
    if (el.width !== bw || el.height !== bh) {
      el.width = bw;
      el.height = bh;
    }
    var scale = (Math.min(w, h) / 300) * dpr;
    face.transform = [scale, (bw - 300 * scale) / 2, (bh - 300 * scale) / 2];
  }

  function paint(face, now) {
    var p = step(face, now);
    var tr = face.transform;
    face.ctx.setTransform(1, 0, 0, 1, 0, 0);
    face.ctx.clearRect(0, 0, face.canvas.width, face.canvas.height);
    face.ctx.setTransform(tr[0], 0, 0, tr[0], tr[1], tr[2]);
    draw(face.ctx, p, now);
  }

  function frame(now) {
    faces.forEach(function (face) {
      if (face.visible && !document.hidden) paint(face, now);
    });
    rafId = faces.length ? window.requestAnimationFrame(frame) : 0;
  }
  var rafId = 0;

  IP.register("coach-face", function (canvas) {
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) return;
    var initial = canvas.getAttribute("data-emotion");
    if (initial && EMOTIONS[initial]) shared.emotion = initial;
    var mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    var now = window.performance ? performance.now() : Date.now();
    var face = {
      canvas: canvas,
      ctx: ctx,
      state: makeState(shared.emotion),
      last: now,
      nextBlink: now + 1200,
      blinkStart: -1000,
      doubleAt: 0,
      nextGlance: now + 1500,
      lookTarget: [0, 0],
      reduced: !!(mq && mq.matches),
      visible: true,
      poke: 0,
      transform: [1, 0, 0],
    };
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", function () {
        face.reduced = mq.matches;
      });
    }
    function refit() {
      resize(face);
      paint(face, window.performance ? performance.now() : Date.now());
    }
    refit(); // first frame right away (RAF may be throttled in background tabs)
    if (window.ResizeObserver) new ResizeObserver(refit).observe(canvas);
    else window.addEventListener("resize", refit);
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        face.visible = entries[entries.length - 1].isIntersecting;
      }).observe(canvas);
    }
    canvas.addEventListener("pointerdown", function () {
      face.poke = window.performance ? performance.now() : Date.now();
      face.blinkStart = face.poke;
    });
    faces.push(face);
    if (!rafId) rafId = window.requestAnimationFrame(frame);
  });

  // test/diagnostic hook (no-op in production paths)
  IP.coachFace._draw = draw;
  IP.coachFace._emotionTable = EMOTIONS;
})();
