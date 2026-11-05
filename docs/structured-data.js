// Structured data for answer engines and search (schema.org JSON-LD). Mintlify loads every .js file in the docs root
// on every page and has no per-page head tag, so this builds the data in the browser from what the page shows.
//   /                          SoftwareApplication (name, platform, the three plans)
//   /troubleshooting/<error>   HowTo: the page's title and its numbered Fix list
//   any page with a run GIF    VideoObject per GIF, dated by the page's article:modified_time
// The freshness stamp is the page's own `article:modified_time` frontmatter, which Mintlify emits as a meta tag.
(function (root) {
  "use strict";
  var SITE = "https://docs.mobster.dev";

  function offer(price, per) {
    return { "@type": "Offer", price: price, priceCurrency: "USD", description: per };
  }

  // page: { path, title, description, modified, steps: [string], gifs: [{ src, name }] }
  function build(page) {
    var nodes = [];
    var url = SITE + page.path;
    if (page.path === "/") {
      nodes.push({
        "@type": "SoftwareApplication",
        name: "Mobster",
        url: "https://mobster.dev",
        applicationCategory: "DeveloperApplication",
        operatingSystem: "macOS",
        description: page.description || "Test and automate apps on real iPhones.",
        offers: [offer("12", "A month, every phone included"), offer("99", "A year, every phone included"),
                 offer("149", "Once, every phone included")],
        dateModified: page.modified || undefined,
      });
    }
    if (page.path.indexOf("/troubleshooting/") === 0 && page.steps.length) {
      nodes.push({
        "@type": "HowTo",
        name: "Fix: " + page.title,
        description: page.description || undefined,
        url: url,
        dateModified: page.modified || undefined,
        step: page.steps.map(function (text, index) {
          return { "@type": "HowToStep", position: index + 1, text: text };
        }),
      });
    }
    if (page.modified) {
      page.gifs.forEach(function (gif) {
        nodes.push({
          "@type": "VideoObject",
          name: gif.name || page.title,
          description: page.description || gif.name || page.title,
          thumbnailUrl: new URL(gif.src, SITE).href,
          contentUrl: new URL(gif.src, SITE).href,
          uploadDate: page.modified,
          url: url,
        });
      });
    }
    return nodes.length ? { "@context": "https://schema.org", "@graph": nodes } : null;
  }

  function read(doc, path) {
    var content = doc.querySelector("#content-area") || doc.querySelector("main") || doc.body;
    var meta = doc.querySelector('meta[property="article:modified_time"], meta[name="article:modified_time"]');
    var h1 = doc.querySelector("h1");
    var description = doc.querySelector('meta[name="description"]');
    return {
      path: path,
      title: (h1 ? h1.textContent : doc.title).trim(),
      description: description ? description.getAttribute("content") : "",
      modified: meta ? meta.getAttribute("content") : "",
      steps: Array.prototype.map.call(content.querySelectorAll("ol > li"), function (li) {
        return li.textContent.replace(/\s+/g, " ").trim();
      }).filter(Boolean),
      gifs: Array.prototype.map.call(content.querySelectorAll('img[src$=".gif"]'), function (img) {
        return { src: img.getAttribute("src"), name: img.getAttribute("alt") || "" };
      }),
    };
  }

  function inject(doc, path) {
    var data = build(read(doc, path));
    var old = doc.getElementById("mobster-jsonld");
    if (old) old.remove();
    if (!data) return;
    var script = doc.createElement("script");
    script.type = "application/ld+json";
    script.id = "mobster-jsonld";
    script.text = JSON.stringify(data);
    doc.head.appendChild(script);
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { build: build };
  }
  if (typeof document === "undefined") return;

  var timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      inject(document, location.pathname.replace(/\/$/, "") || "/");
    }, 400);
  }
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  schedule();
})(this);
