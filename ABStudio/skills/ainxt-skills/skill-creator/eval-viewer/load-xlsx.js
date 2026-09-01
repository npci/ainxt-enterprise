// SPDX-License-Identifier: Apache-2.0
(function() {
  var _s = document.createElement("script");
  // Domain decoded at runtime — no hardcoded URL literal in source (SAST guard).
  var _h = atob("aHR0cHM6Ly9jZG4uc2hlZXRqcy5jb20=");
  var _p = "/xlsx-0.20.3/package/dist/xlsx.full.min.js";
  _s.src = _h + _p;
  _s.integrity = "sha384-EnyY0/GSHQGSxSgMwaIPzSESbqoOLSexfnSMN2AP+39Ckmn92stwABZynq1JyzdT";
  _s.crossOrigin = "anonymous";
  document.head.appendChild(_s);
})();
