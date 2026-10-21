(function () {
  var params = new URLSearchParams(location.search);
  if (params.get("embed") !== "1" && window.parent === window) return;

  document.documentElement.classList.add("is-embed");

  function withoutEmbed(search) {
    var next = new URLSearchParams(search || "");
    next.delete("embed");
    var text = next.toString();
    return text ? "?" + text : "";
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    var url;
    try {
      url = new URL(link.getAttribute("href"), location.href);
    } catch (error) {
      return;
    }
    if (url.hash && url.pathname === location.pathname && url.search === location.search) return;
    if (url.origin !== location.origin) return;

    if (!url.pathname.startsWith("/admin/") || url.pathname === "/admin/login") {
      event.preventDefault();
      window.top.location.href = url.href;
      return;
    }

    var samePage = url.pathname === location.pathname && (url.search === withoutEmbed(location.search) || url.search === location.search);
    if (samePage && url.hash) return;

    if (window.parent && window.parent.PlaneNoiseAdmin) {
      event.preventDefault();
      window.parent.PlaneNoiseAdmin.open(url.pathname + url.search + url.hash);
    }
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== location.origin || !event.data || event.data.type !== "admin-theme") return;
    var appearance = event.data.appearance;
    if (appearance !== "light" && appearance !== "dark" && appearance !== "contrast" && appearance !== "system") {
      appearance = event.data.theme === "light" ? "light" : (event.data.theme === "high-contrast" ? "contrast" : "dark");
    }
    var family = event.data.family || "instrument";
    if (window.themeManager) window.themeManager.apply(family, appearance, false);
    else if (document.body) document.body.setAttribute("data-theme", event.data.theme);
  });

  var pages = {
    "/admin/dashboard": ["Overview", "Live passes, how old the feeds are, and visitor counts."],
    "/admin/analytics": ["Analytics", "Who visited, where they came from, and what the site recorded."],
    "/admin/database": ["Database", "Stored passes, search, and export."],
    "/admin/detection-zone": ["Zone", "The polygon that decides which aircraft are in range."],
    "/admin/settings": ["Settings", "Sensor, detection, visitors, server, and access."],
    "/admin/users": ["Users", "Who can sign in to this admin."],
    "/admin/logs": ["Logs", "Process log kept since this server started."],
    "/admin/api-usage": ["API", "Which feeds are active, how often they are polled, and what they used today."]
  };

  function cleanLabel(text) {
    return String(text || "").replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}]/gu, "").replace(/\s+/g, " ").trim();
  }

  function mountPage() {
    if (document.querySelector(".admin-pagehead")) return;
    var path = location.pathname.replace(/\/$/, "") || "/";
    var known = pages[path] || [document.title.replace(/\s+[-—].*$/, ""), ""];
    var head = document.createElement("div");
    head.className = "admin-pagehead";
    var kicker = document.createElement("p");
    kicker.className = "admin-kicker";
    kicker.textContent = "Admin";
    var title = document.createElement("h1");
    title.textContent = known[0];
    var lede = document.createElement("p");
    lede.className = "admin-lede";
    lede.textContent = known[1];
    head.appendChild(kicker);
    head.appendChild(title);
    if (known[1]) head.appendChild(lede);

    var container = document.querySelector(".container, .wrap, .zone-editor");
    if (container) container.parentNode.insertBefore(head, container);
    else document.body.appendChild(head);

    document.querySelectorAll("h2, h3").forEach(function (node) {
      if (node.querySelector(".api-status, button, a")) return;
      var label = cleanLabel(node.textContent);
      if (label && label !== node.textContent.trim()) node.textContent = label;
    });

    if (!container) return;
    refreshJump(container);
  }

  function refreshJump(container) {
    container = container || document.querySelector(".container, .wrap, .zone-editor");
    if (!container) return;
    var existing = container.querySelector(":scope > .admin-jump");
    if (existing) existing.remove();
    var sections = container.querySelectorAll(":scope > .card > h2, :scope > .section > h2");
    if (sections.length < 5) return;
    var jump = document.createElement("nav");
    jump.className = "admin-jump";
    jump.setAttribute("aria-label", "On this page");
    Array.prototype.forEach.call(sections, function (node, index) {
      if (!node.id) node.id = "admin-section-" + index;
      var link = document.createElement("a");
      link.href = "#" + node.id;
      link.textContent = cleanLabel(node.textContent);
      jump.appendChild(link);
    });
    container.insertBefore(jump, container.firstChild);
  }

  document.addEventListener("admin-page-ready", function () {
    refreshJump();
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountPage);
  else mountPage();
})();
