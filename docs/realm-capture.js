(function () {
  const src = "https://realm-browser-capture.burner405004.chatgpt.site/sdk/v3.js";
  if (document.querySelector(`script[src="${src}"]`)) return;
  const script = document.createElement("script");
  script.async = true;
  script.src = src;
  script.integrity = "sha384-TT+MYmhwCj/YNc2c8p5HLH4Sk9GDiUlC+9MzZ3di01ADTgcJqxoi7DECBhaS9XbD";
  script.crossOrigin = "anonymous";
  document.head.appendChild(script);
})();
