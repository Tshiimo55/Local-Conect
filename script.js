(function () {
  if (window.LocalConnect) return;

  function inject(src) {
    var script = document.createElement("script");
    script.src = src;
    script.defer = true;
    document.head.appendChild(script);
  }

  inject("assets/localconnect.config.js?v=20260328");
  inject("assets/localconnect.js?v=20260328");
})();
