(function () {
  var frame = document.getElementById("admin-frame");
  var navLinks = Array.prototype.slice.call(document.querySelectorAll(".admin-nav .admin-nav-link"));

  function sectionPath(pathname) {
    var path = pathname || "/";
    if (path.length > 1 && path.charAt(path.length - 1) === "/") path = path.slice(0, -1);
    return path;
  }

  function embedSrc(path) {
    var url = new URL(path, location.origin);
    var params = new URLSearchParams(url.search);
    params.delete("embed");
    params.set("embed", "1");
    return url.pathname + "?" + params.toString() + url.hash;
  }

  function publicPath(path) {
    var url = new URL(path, location.origin);
    var params = new URLSearchParams(url.search);
    params.delete("embed");
    var search = params.toString();
    return url.pathname + (search ? "?" + search : "") + url.hash;
  }

  function setCurrent(pathname) {
    var path = sectionPath(pathname);
    navLinks.forEach(function (link) {
      var current = sectionPath(link.getAttribute("href")) === path;
      link.classList.toggle("is-current", current);
      if (current) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function syncTheme() {
    var family = document.body.getAttribute("data-family") || localStorage.getItem("plane-noise-family") || "instrument";
    var theme = document.body.getAttribute("data-theme") || "dark";
    var appearance = document.body.getAttribute("data-appearance") || theme;
    try {
      var body = frame.contentDocument && frame.contentDocument.body;
      if (body) {
        body.setAttribute("data-theme", theme);
        body.setAttribute("data-family", family);
        body.setAttribute("data-appearance", appearance);
      }
      frame.contentWindow.postMessage({ type: "admin-theme", theme: theme, family: family, appearance: appearance }, location.origin);
    } catch (error) {}
  }

  function open(path, replace) {
    var next = publicPath(path);
    frame.src = embedSrc(next);
    setCurrent(new URL(next, location.origin).pathname);
    if (replace) history.replaceState({ admin: next }, "", next);
    else if (publicPath(location.pathname + location.search + location.hash) !== next) history.pushState({ admin: next }, "", next);
  }

  window.PlaneNoiseAdmin = { open: open };

  document.querySelector(".admin-brand").addEventListener("click", function (event) {
    event.preventDefault();
    open("/admin/dashboard");
  });

  navLinks.forEach(function (link) {
    if (link.classList.contains("admin-nav-public")) return;
    link.addEventListener("click", function (event) {
      event.preventDefault();
      open(link.getAttribute("href"));
    });
  });

  document.getElementById("admin-logout").addEventListener("click", function () {
    fetch("/auth/logout", { method: "POST", credentials: "same-origin" })
      .finally(function () { location.href = "/admin/login"; });
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest && (event.target.closest(".theme-toggle") || event.target.closest("[data-appearance-toggle]"))) {
      setTimeout(syncTheme, 0);
    }
  });
  window.addEventListener("planenoise-theme", function () { syncTheme(); });

  window.addEventListener("popstate", function () {
    open(location.pathname + location.search + location.hash, true);
  });

  frame.addEventListener("load", function () {
    try {
      var loc = frame.contentWindow.location;
      var shown = publicPath(loc.pathname + loc.search + loc.hash);
      setCurrent(loc.pathname);
      if (loc.pathname.indexOf("/admin/") === 0 && publicPath(location.pathname + location.search + location.hash) !== shown) {
        history.replaceState({ admin: shown }, "", shown);
      }
      if (frame.contentDocument && frame.contentDocument.title) {
        document.title = frame.contentDocument.title;
      }
    } catch (error) {}
    syncTheme();
  });

  fetch("/auth/me", { credentials: "same-origin" })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (data) {
      var email = data && data.user && data.user.email;
      var slot = document.getElementById("userEmail");
      if (email && slot) slot.textContent = email;
    })
    .catch(function () {});

  var start = location.pathname;
  if (start === "/admin" || start === "/admin/") start = "/admin/dashboard";
  open(start + location.search + location.hash, true);
})();
