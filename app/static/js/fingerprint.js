/*
 * Device fingerprint for exam integrity.
 *
 * Collects stable, non-identifying properties of the browser and hashes them
 * into one opaque value stored in the `nbo_fp` cookie. The server salts and
 * re-hashes it before storage, so the raw components never sit in the database
 * in reversible form.
 *
 * What this is for: catching one person registering several accounts to sit the
 * same exam twice. What it is not: a tracker. Nothing here is sent anywhere
 * except this platform, no advertising identifiers are touched, and the value
 * is useless outside the exam-integrity checks. This is disclosed to
 * participants during registration.
 */
(function () {
  "use strict";

  var COOKIE = "nbo_fp";
  var VERSION = "1";

  function readCookie(name) {
    var match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  function writeCookie(name, value, days) {
    var expires = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie =
      name + "=" + encodeURIComponent(value) +
      ";expires=" + expires + ";path=/;SameSite=Lax";
  }

  /* FNV-1a: short, dependency-free, and good enough for a bucketing hash. */
  function hash(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }

  function canvasSignature() {
    try {
      var canvas = document.createElement("canvas");
      canvas.width = 220;
      canvas.height = 40;
      var ctx = canvas.getContext("2d");
      if (!ctx) return "no-2d";
      ctx.textBaseline = "top";
      ctx.font = "14px 'Arial'";
      ctx.fillStyle = "#f60";
      ctx.fillRect(0, 0, 90, 20);
      ctx.fillStyle = "#069";
      ctx.fillText("NBO биология 2024", 2, 4);
      ctx.fillStyle = "rgba(102,204,0,0.65)";
      ctx.fillText("∂Ω∑∫", 4, 20);
      return hash(canvas.toDataURL());
    } catch (e) {
      return "no-canvas";
    }
  }

  function webglSignature() {
    try {
      var canvas = document.createElement("canvas");
      var gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      if (!gl) return "no-webgl";
      var info = gl.getExtension("WEBGL_debug_renderer_info");
      var vendor = info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
      var renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      return hash(String(vendor) + "|" + String(renderer));
    } catch (e) {
      return "no-webgl";
    }
  }

  function collect() {
    var nav = window.navigator || {};
    var screen = window.screen || {};
    var parts = [
      VERSION,
      nav.userAgent || "",
      nav.platform || "",
      (nav.languages || [nav.language || ""]).join(","),
      String(nav.hardwareConcurrency || 0),
      String(nav.deviceMemory || 0),
      String(nav.maxTouchPoints || 0),
      screen.width + "x" + screen.height + "x" + (screen.colorDepth || 0),
      String(window.devicePixelRatio || 1),
      String(new Date().getTimezoneOffset()),
      (Intl && Intl.DateTimeFormat) ? (Intl.DateTimeFormat().resolvedOptions().timeZone || "") : "",
      canvasSignature(),
      webglSignature()
    ];
    return parts.join("~");
  }

  var existing = readCookie(COOKIE);
  var signature = hash(collect()) + hash(collect().split("").reverse().join(""));

  if (existing !== signature) {
    writeCookie(COOKIE, signature, 365);
  }

  /* Exposed so the exam player can send it as a header on its fetch calls. */
  window.NBO_FINGERPRINT = signature;
  window.NBO_DEVICE = {
    screen: (window.screen || {}).width + "x" + (window.screen || {}).height,
    tzOffset: new Date().getTimezoneOffset()
  };
})();
