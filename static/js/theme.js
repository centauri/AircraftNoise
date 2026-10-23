/* Named themes, each with a dark and a light appearance. Load themes.js first. */

class ThemeManager {
  constructor() {
    this.policy = null;
    this.signedInAdmin = false;
    this._mounted = false;
    var stored = this.readStored();
    this.family = stored.family;
    this.appearance = stored.appearance;
    this.apply(this.family, this.appearance, false);
    this.watchSystem();
    var self = this;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { self.mount(); });
    } else {
      this.mount();
    }
    this.loadPolicy();
    this.loadSession();
  }

  mount() {
    if (this._mounted) return;
    this._mounted = true;
    var root = document.documentElement;
    if (document.body) {
      ["data-theme", "data-scheme", "data-appearance", "data-family"].forEach(function (name) {
        var value = root.getAttribute(name);
        if (value) document.body.setAttribute(name, value);
      });
    }
    this.createThemeControls();
    this.bindEvents();
    var communityLink = document.querySelector(".header-nav-link");
    if (communityLink && location.pathname.indexOf("/community") === 0) {
      communityLink.setAttribute("aria-current", "page");
    }
  }

  catalog() {
    return window.PlaneNoiseThemes || [];
  }

  appearances() {
    return window.PlaneNoiseAppearances || [];
  }

  find(id) {
    var list = this.catalog();
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  variant(familyId, appearance) {
    var family = this.find(familyId) || this.catalog()[0];
    if (!family) return null;
    return family[appearance] || family.dark || family.light;
  }

  systemScheme() {
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    } catch (error) {}
    return "light";
  }

  resolvedAppearance(appearance) {
    if (appearance === "system") return this.systemScheme();
    return appearance;
  }

  current() {
    return this.variant(this.family, this.resolvedAppearance(this.appearance));
  }

  readStored() {
    // The site's own default is written into the page by the server, so the
    // first paint is right before the policy has been fetched. A visitor's own
    // choice, when there is one, wins over it.
    var root = document.documentElement;
    var family = root.getAttribute("data-site-family") || "instrument";
    var appearance = root.getAttribute("data-site-appearance") || "dark";
    var savedFamily = null;
    var savedAppearance = null;
    var legacy = null;
    try {
      savedFamily = localStorage.getItem("plane-noise-family");
      savedAppearance = localStorage.getItem("plane-noise-appearance");
      legacy = localStorage.getItem("plane-noise-theme");
    } catch (error) {}
    if (savedFamily) family = savedFamily;
    else if (legacy === "early") family = "early";
    if (savedAppearance) appearance = savedAppearance;
    else if (legacy === "light") appearance = "light";
    else if (legacy === "high-contrast") appearance = "contrast";
    if (!this.find(family)) family = root.getAttribute("data-site-family") || "instrument";
    var known = appearance === "dark" || appearance === "light" || appearance === "contrast" || appearance === "system";
    if (!known) appearance = "dark";
    return { family: family, appearance: appearance, explicitFamily: !!savedFamily || legacy === "early", explicitAppearance: !!savedAppearance && known };
  }

  writeStored(family, appearance) {
    try {
      localStorage.setItem("plane-noise-family", family);
      localStorage.setItem("plane-noise-appearance", appearance);
      localStorage.setItem("plane-noise-theme", family + ":" + appearance);
      if (this.policy && this.policy.appearance) {
        localStorage.setItem("plane-noise-appearance-baseline", this.policy.appearance);
      }
    } catch (error) {}
  }

  token(name) {
    var look = this.current();
    if (!look || !look.tokens) return "";
    return look.tokens[name] || "";
  }

  rgba(name, alpha) {
    var hex = this.token(name);
    var value = String(hex || "").replace("#", "");
    if (value.length === 3) value = value.split("").map(function (part) { return part + part; }).join("");
    if (value.length !== 6) return hex;
    var number = parseInt(value, 16);
    var red = (number >> 16) & 255;
    var green = (number >> 8) & 255;
    var blue = number & 255;
    return "rgba(" + red + ", " + green + ", " + blue + ", " + alpha + ")";
  }

  heatColor(ratio) {
    var look = this.current();
    var scale = look && look.heat;
    if (!scale || !scale.length) return this.token("theme-accent") || "#888888";
    var amount = Math.max(0, Math.min(1, ratio || 0));
    var index = amount <= 0 ? 0 : Math.min(scale.length - 1, Math.ceil(amount * (scale.length - 1) - 1e-6));
    return scale[index];
  }

  heatInk(ratio) {
    var look = this.current();
    if (!look) return "#000000";
    return ratio <= look.heatInkAt ? look.inkOnLow : look.inkOnHigh;
  }

  allowedFamilies() {
    if (!this.policy || !this.policy.choice) {
      return this.policy && this.policy.theme ? [this.policy.theme] : this.catalog().map(function (item) { return item.id; });
    }
    var offered = this.policy.offered || this.policy.themes || [];
    return offered.filter(function (id) { return !!this.find(id); }, this);
  }

  watchSystem() {
    if (this._systemWatch || !window.matchMedia) return;
    var query = window.matchMedia("(prefers-color-scheme: dark)");
    var self = this;
    var onChange = function () {
      if (self.appearance === "system") self.apply(self.family, "system", false);
    };
    if (query.addEventListener) query.addEventListener("change", onChange);
    else if (query.addListener) query.addListener(onChange);
    this._systemWatch = true;
  }

  apply(familyId, appearance, persist) {
    if (!this.find(familyId)) familyId = (this.catalog()[0] && this.catalog()[0].id) || "instrument";
    if (appearance !== "dark" && appearance !== "light" && appearance !== "contrast" && appearance !== "system") appearance = "dark";
    var painted = this.resolvedAppearance(appearance);
    var look = this.variant(familyId, painted);
    if (!look) return;
    var root = document.documentElement;
    Object.keys(look.tokens).forEach(function (key) {
      root.style.setProperty("--" + key, look.tokens[key]);
    });
    var themeAttr = painted === "contrast" ? "high-contrast" : painted;
    root.setAttribute("data-theme", themeAttr);
    root.setAttribute("data-scheme", look.scheme || (painted === "light" ? "light" : "dark"));
    root.setAttribute("data-appearance", appearance);
    root.setAttribute("data-family", familyId);
    if (document.body) {
      document.body.setAttribute("data-theme", themeAttr);
      document.body.setAttribute("data-scheme", root.getAttribute("data-scheme"));
      document.body.setAttribute("data-appearance", appearance);
      document.body.setAttribute("data-family", familyId);
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", look.tokens["theme-masthead"] || "#14160f");
    this.family = familyId;
    this.appearance = appearance;
    if (persist !== false) this.writeStored(familyId, appearance);
    this.updateThemeButtons();
    window.dispatchEvent(new CustomEvent("planenoise-theme", {
      detail: { family: familyId, appearance: appearance }
    }));
  }

  createThemeControls() {
    var themeContainer = document.querySelector("#theme-controls-container");
    if (!themeContainer) return;
    var families = this.allowedFamilies();
    var showFamilies = !this.policy || (this.policy.choice && families.length > 1);
    var showAppearance = !this.policy || this.policy.appearance_choice !== false;
    var familyButtons = this.catalog().filter(function (item) {
      return families.indexOf(item.id) !== -1;
    }).map(function (item) {
      return '<button type="button" class="theme-toggle" data-family="' + item.id + '">' + item.name + "</button>";
    }).join("");
    var contrast = showAppearance
      ? '<button type="button" class="theme-toggle" data-appearance="system">System</button><button type="button" class="theme-toggle" data-appearance="contrast">Contrast</button>'
      : "";
    var onAdminPage = location.pathname.indexOf("/admin") === 0;
    var adminLink = this.signedInAdmin && !onAdminPage
      ? '<a class="theme-menu-admin" href="/admin">Admin</a>'
      : "";
    var lang = "en";
    try { lang = localStorage.getItem("plane_noise_language") || "en"; } catch (error) {}
    if (window.translationSystem && window.translationSystem.currentLanguage) {
      lang = window.translationSystem.currentLanguage;
    }
    if (lang !== "nl") lang = "en";
    var languageName = "Language";
    if (window.translationSystem) {
      var translated = window.translationSystem.t("language_label");
      if (translated && translated !== "language_label") languageName = translated;
    }
    var onPublic = !!document.querySelector(".header-banner");
    var names = { en: "English", nl: "Nederlands", de: "Deutsch", fr: "Français", es: "Español", it: "Italiano", pt: "Português", pl: "Polski", da: "Dansk", sv: "Svenska", no: "Norsk", fi: "Suomi", cs: "Čeština", el: "Ελληνικά", tr: "Türkçe", hu: "Magyar", ro: "Română" };
    var available = ["en"].concat(Object.keys(window.Translations || {}).filter(function (k) { return k !== "en"; }));
    var options = available.map(function (code) {
      return '<option value="' + code + '"' + (lang === code ? " selected" : "") + ">" + (names[code] || code) + "</option>";
    }).join("");
    var langMenu = onPublic
      ? '<label class="theme-menu-lang"><span data-translate="language_label">' + languageName + '</span><select class="theme-lang-select" aria-label="' + languageName + '">' + options + "</select></label>"
      : "";
    var menuBody = langMenu + (showFamilies ? familyButtons : "") + contrast + adminLink;
    var sun = '<svg class="theme-icon-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path fill="none" stroke="currentColor" stroke-width="2" d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
    var moon = '<svg class="theme-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M15.5 3.1a8.5 8.5 0 1 0 5.4 14.8A9 9 0 0 1 15.5 3.1z"/></svg>';
    var burger = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" d="M4 7h16M4 12h16M4 17h16"/></svg>';
    themeContainer.innerHTML = "";
    var controls = document.createElement("div");
    controls.className = "theme-controls";
    controls.innerHTML =
      (showAppearance
        ? '<button type="button" class="theme-icon" data-appearance-toggle aria-label="Switch to light">' + sun + moon + "</button>"
        : "") +
      (menuBody
        ? '<div class="theme-menu"><button type="button" class="theme-burger" aria-label="Menu" aria-expanded="false" aria-controls="theme-menu-panel">' + burger + '</button><div id="theme-menu-panel" class="theme-menu-panel" hidden>' + menuBody + "</div></div>"
        : "");
    themeContainer.appendChild(controls);
    this.updateThemeButtons();
  }

  closeMenus() {
    document.querySelectorAll(".theme-menu-panel").forEach(function (panel) {
      panel.hidden = true;
    });
    document.querySelectorAll(".theme-burger").forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
    });
  }

  updateThemeButtons() {
    var self = this;
    document.querySelectorAll(".theme-toggle").forEach(function (button) {
      var family = button.getAttribute("data-family");
      var appearance = button.getAttribute("data-appearance");
      var selected = (family && family === self.family) || (appearance && appearance === self.appearance);
      button.classList.toggle("is-active", !!selected);
      if (selected) button.setAttribute("aria-pressed", "true");
      else button.removeAttribute("aria-pressed");
    });
    var icon = document.querySelector("[data-appearance-toggle]");
    if (icon) {
      var painted = self.resolvedAppearance(self.appearance);
      var toLight = painted !== "light";
      icon.setAttribute("aria-label", toLight ? "Switch to light" : "Switch to dark");
      icon.setAttribute("aria-pressed", painted === "light" ? "true" : "false");
    }
    var currentLang = "en";
    if (window.translationSystem && window.translationSystem.currentLanguage) {
      currentLang = window.translationSystem.currentLanguage;
    } else {
      try { currentLang = localStorage.getItem("plane_noise_language") || "en"; } catch (error) {}
    }
    var known = Object.keys(window.Translations || {});
    if (known.length && known.indexOf(currentLang) < 0) currentLang = "en";
    document.querySelectorAll(".theme-lang-select").forEach(function (select) {
      if (select.value !== currentLang) select.value = currentLang;
    });
  }

  bindEvents() {
    var self = this;
    document.addEventListener("click", function (event) {
      var closest = event.target.closest ? event.target.closest.bind(event.target) : function () { return null; };
      var burger = closest(".theme-burger");
      if (burger) {
        event.preventDefault();
        var panel = burger.parentElement.querySelector(".theme-menu-panel");
        var willOpen = panel && panel.hidden;
        self.closeMenus();
        if (willOpen && panel) {
          panel.hidden = false;
          burger.setAttribute("aria-expanded", "true");
        }
        return;
      }
      var toggle = closest("[data-appearance-toggle]");
      if (toggle) {
        var painted = self.resolvedAppearance(self.appearance);
        self.apply(self.family, painted === "light" ? "dark" : "light", true);
        return;
      }
      var button = closest(".theme-toggle");
      if (button) {
        var family = button.getAttribute("data-family");
        var appearance = button.getAttribute("data-appearance");
        if (family && family !== self.family) self.apply(family, self.appearance, true);
        if (appearance && appearance !== self.appearance) self.apply(self.family, appearance, true);
        self.closeMenus();
        return;
      }
      if (!closest(".theme-menu")) self.closeMenus();
    });
    document.addEventListener("change", function (event) {
      var select = event.target.closest ? event.target.closest(".theme-lang-select") : null;
      if (!select) return;
      var chosen = (window.Translations && window.Translations[select.value]) ? select.value : "en";
      if (window.translationSystem && typeof window.translationSystem.switchLanguage === "function") {
        window.translationSystem.switchLanguage(chosen);
      } else {
        try { localStorage.setItem("plane_noise_language", chosen); } catch (error) {}
      }
      self.closeMenus();
    });
    window.addEventListener("languageChanged", function () {
      self.updateThemeButtons();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") self.closeMenus();
    });
  }

  loadSession() {
    var self = this;
    if (location.pathname.indexOf("/admin") === 0) return Promise.resolve();
    return fetch("/auth/me", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        self.signedInAdmin = !!(data && data.user && data.user.is_admin);
        self.createThemeControls();
      })
      .catch(function () {});
  }

  loadPolicy() {
    var self = this;
    return fetch("/visitor/theme", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (policy) {
        if (!policy) return;
        self.policy = policy;
        var stored = self.readStored();
        var family = stored.family;
        var appearance = stored.appearance;
        var allowed = self.allowedFamilies();
        // No choice of their own yet: the site theme. A choice the site no longer offers: the site theme.
        if (!stored.explicitFamily || allowed.indexOf(family) === -1) family = policy.theme || allowed[0] || family;
        var baseline = null;
        try { baseline = localStorage.getItem("plane-noise-appearance-baseline"); } catch (error) {}
        var siteAppearance = policy.appearance || appearance;
        var visitorStillMatchesThisDefault = stored.explicitAppearance && baseline === siteAppearance;
        if (policy.appearance_choice === false || !visitorStillMatchesThisDefault) appearance = siteAppearance;
        self.apply(family, appearance, false);
        self.createThemeControls();
      })
      .catch(function () {});
  }

  applyTheme(themeId) {
    var appearance = themeId === "light" ? "light" : (themeId === "high-contrast" || themeId === "contrast" ? "contrast" : "dark");
    var family = themeId === "early" ? "early" : this.family;
    this.apply(family, appearance, true);
  }

  getCurrentTheme() {
    return this.family + ":" + this.appearance;
  }
}

(function bootTheme() {
  if (window.themeManager) return;
  window.themeManager = new ThemeManager();
})();
