// Translation system for Aircraft Noise
// Handles language switching and text translation

class TranslationSystem {
  constructor() {
    this.currentLanguage = 'en';
    // Every dictionary in translations.js; English is always first and the fallback.
    this.supportedLanguages = ['en'].concat(Object.keys(window.Translations || {}).filter(function (k) { return k !== 'en'; }));
    this.translations = window.Translations || {};
    
    // Initialize after a short delay to ensure other scripts have loaded
    setTimeout(() => {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => this.init());
      } else {
        this.init();
      }
    }, 100);
  }
  
  init() {
    console.log('Translation system: Initializing...');
    console.log('Translations available:', this.translations);
    console.log('Translation keys:', Object.keys(this.translations));
    
    // Only initialize if translations are available
    if (!this.translations || Object.keys(this.translations).length === 0) {
      console.warn('Translation system: No translations available, skipping initialization');
      return;
    }
    
    // Detect language from URL parameter or localStorage
    this.detectLanguage();
    console.log('Current language:', this.currentLanguage);
    
    // Apply translations
    this.applyTranslations();
    
    // Set up language switcher
    this.setupLanguageSwitcher();
    
    // Update language switcher UI
    this.updateLanguageSwitcher();
    
    console.log('Translation system: Initialization complete');
  }
  
  detectLanguage() {
    // Check URL parameter first
    const urlParams = new URLSearchParams(window.location.search);
    const langParam = urlParams.get('lang');
    
    if (langParam && this.supportedLanguages.includes(langParam)) {
      this.currentLanguage = langParam;
    } else {
      // Check localStorage
      const storedLang = localStorage.getItem('plane_noise_language');
      const serverLang = document.documentElement.dataset.lang;
      if (storedLang && this.supportedLanguages.includes(storedLang)) {
        this.currentLanguage = storedLang;
      } else if (serverLang && this.supportedLanguages.includes(serverLang)) {
        // The owner's choice for this station, written into the page by the server
        this.currentLanguage = serverLang;
      } else {
        // Check browser language
        const browserLang = navigator.language.split('-')[0];
        if (this.supportedLanguages.includes(browserLang)) {
          this.currentLanguage = browserLang;
        }
      }
    }
    
    // Save to localStorage
    localStorage.setItem('plane_noise_language', this.currentLanguage);
  }
  
  getTranslation(key, params = {}) {
    const translation = this.translations[this.currentLanguage]?.[key] || 
                      this.translations['en']?.[key] || 
                      key;
    
    // Replace parameters in translation
    return this.replaceParams(translation, params);
  }
  
  replaceParams(text, params) {
    return text.replace(/\{(\w+)\}/g, (match, key) => {
      return params[key] !== undefined ? params[key] : match;
    });
  }
  
  applyTranslations() {
    try {
      console.log('Translation system: Applying translations...');
      
      // Update meta tags
      this.updateMetaTags();
      
      // Update page content
      this.updatePageContent();
      
      // Update structured data
      this.updateStructuredData();
      
      // Update language attribute
      document.documentElement.lang = this.currentLanguage;
      
      console.log('Translation system: Translations applied successfully');
    } catch (error) {
      console.warn('Translation system: Error applying translations:', error);
    }
  }
  
  updateMetaTags() {
    const t = this.translations[this.currentLanguage];
    if (!t) return;
    // The server wrote a title and description that name the place in the
    // station's language; those stay, whatever the visitor's interface language.
    if (document.documentElement.dataset.seo === 'server') return;

    if (document.body && document.body.dataset.page === "community") {
      document.title = t.community_title || document.title;
      this.updateMetaContent("description", t.community_description || "");
      this.updateMetaProperty("og:title", t.community_title || document.title);
      this.updateMetaProperty("og:description", t.community_description || "");
      document.documentElement.lang = this.currentLanguage;
      return;
    }
    
    // Title
    document.title = t.title;
    
    // Basic meta tags
    this.updateMetaContent('description', t.description);
    this.updateMetaContent('keywords', t.keywords);
    this.updateMetaContent('author', t.author);
    this.updateMetaContent('geo.placename', t.geo_placename);
    this.updateMetaContent('geo.region', t.geo_region);
    this.updateMetaContent('robots', t.robots);
    this.updateMetaContent('googlebot', t.googlebot);
    this.updateMetaContent('bingbot', t.bingbot);
    
    // Open Graph
    this.updateMetaProperty('og:title', t.og_title);
    this.updateMetaProperty('og:description', t.og_description);
    this.updateMetaProperty('og:site_name', t.og_site_name);
    this.updateMetaProperty('og:type', t.og_type);
    this.updateMetaProperty('og:url', t.og_url);
    if (t.og_image) {
      this.updateMetaProperty('og:image', t.og_image);
      this.updateMetaProperty('og:image:width', t.og_image_width);
      this.updateMetaProperty('og:image:height', t.og_image_height);
      this.updateMetaProperty('og:image:alt', t.og_image_alt);
      this.updateMetaProperty('og:image:type', t.og_image_type);
    }
    this.updateMetaProperty('og:locale', this.currentLanguage === 'nl' ? 'nl_NL' : 'en_US');
    this.updateMetaProperty('fb:app_id', t.fb_app_id);
    
    // Twitter Card
    this.updateMetaContent('twitter:title', t.twitter_title);
    this.updateMetaContent('twitter:description', t.twitter_description);
    this.updateMetaContent('twitter:card', t.twitter_card);
    this.updateMetaContent('twitter:site', t.twitter_site);
    this.updateMetaContent('twitter:creator', t.twitter_creator);
    if (t.twitter_image) {
      this.updateMetaContent('twitter:image', t.twitter_image);
      this.updateMetaContent('twitter:image:alt', t.twitter_image_alt);
    }
  }
  
  updateMetaContent(name, content) {
    if (!content) return;  // keep what the server wrote
    const meta = document.querySelector(`meta[name="${name}"]`);
    if (meta) meta.content = content;
  }
  
  updateMetaProperty(property, content) {
    if (!content) return;  // keep what the server wrote
    const meta = document.querySelector(`meta[property="${property}"]`);
    if (meta) meta.content = content;
  }
  
  updateStructuredData() {
    const t = this.translations[this.currentLanguage];
    if (!t) return;
    
    const script = document.querySelector('script[type="application/ld+json"]');
    if (script) {
      if (document.documentElement.dataset.seo === 'server') return;
      try {
        const data = JSON.parse(script.textContent);
        data.name = t.structured_name;
        data.description = t.structured_description;
        data.featureList = t.structured_features;
        data.inLanguage = [this.currentLanguage, this.currentLanguage === 'en' ? 'nl' : 'en'];
        script.textContent = JSON.stringify(data);
      } catch (e) {
        console.warn('Failed to update structured data:', e);
      }
    }
  }
  
  updatePageContent() {
    const t = this.translations[this.currentLanguage];
    if (!t) return;
    
    // Update elements with data-translate attributes
    document.querySelectorAll('[data-translate]').forEach(element => {
      const key = element.getAttribute('data-translate');
      const params = this.getElementParams(element);
      const translation = this.getTranslation(key, params);
      // Only update if we have a translation (preserve static content as fallback)
      if (translation && translation !== key) {
        element.textContent = translation;
      }
    });
    
    // Attributes: data-translate-title fills title
    document.querySelectorAll('[data-translate-title]').forEach(element => {
      const translation = this.getTranslation(element.getAttribute('data-translate-title'));
      if (translation) element.setAttribute('title', translation);
    });

    // Update elements with data-translate-html attributes
    document.querySelectorAll('[data-translate-html]').forEach(element => {
      const key = element.getAttribute('data-translate-html');
      const params = this.getElementParams(element);
      element.innerHTML = this.getTranslation(key, params);
    });
    
    // Update specific elements by ID
    // Skip site-title as it now contains custom HTML with images
    // this.updateElementById('site-title', t.site_title);
    this.updateElementById(document.getElementById('subtitleText') ? 'subtitleText' : 'subtitle', t.subtitle);
    // Update timezone label with dynamic offset
    this.updateTimezoneLabel();
    this.updateElementById('today-avg-noise', t.today_avg_noise);
    this.updateElementById('closest-approach-today', t.closest_approach_today);
    this.updateElementById('loudest-today', t.loudest_today);
    this.updateElementById('sensor-location-heading', t.sensor_location_aircraft);
    this.updateElementById('noise-level-heading', t.noise_level_spl);
    this.updateElementById('recent-events-heading', t.recent_aircraft_events);
    this.updateElementById('activity-heatmap-heading', t.activity_heatmap);
    this.updateElementById('chart-button', t.chart_button);
    this.updateElementById('table-button', t.table_button);
    this.updateElementById('heatmap-description', t.heatmap_description);
    this.updateElementById('mobile-note', t.mobile_note);
    this.updateElementById('disclaimer-title', t.disclaimer_title);
    
    // Update SEO elements
    
    // Update SEO content page elements
    
    // Update SEO content page specific elements
    
    // Update table headers
    this.updateTableHeaders(t);
    
    // Update day names in heatmap
    this.updateDayNames(t);
    
    // Update radius/altitude info with special handling for polygon area
    this.updateRadiusAltitudeInfo(t);
  }
  
  updateElementById(id, text) {
    const element = document.getElementById(id);
    if (element) element.textContent = text;
  }
  
  updateTimezoneLabel() {
    const element = document.getElementById('netherlands-time');
    if (!element) return;

    const zone = window.SENSOR_TIMEZONE || 'Europe/Amsterdam';
    const place = window.PLANE_PLACE_NAME || 'Netherlands';
    const word = this.currentLanguage === 'nl' ? 'Tijd' : 'Time';
    const now = new Date();
    const utcTime = new Date(now.toLocaleString("en-US", {timeZone: "UTC"}));
    const localTime = new Date(now.toLocaleString("en-US", {timeZone: zone}));
    const offsetHours = Math.round((localTime.getTime() - utcTime.getTime()) / (1000 * 60 * 60));
    const sign = offsetHours >= 0 ? '+' : '';
    element.textContent = place + ' ' + word + ' (UTC' + sign + offsetHours + ')';
  }
  
  updateTableHeaders(t) {
    const headers = {
      'sort-image': t.image,
      'sort-callsign': t.callsign,
      'sort-time': t.time,
      'sort-distance': t.distance,
      'sort-altitude': t.altitude,
      'sort-speed': t.speed,
      'sort-heading': t.heading,
      'sort-noise': t.noise,
      'sort-operator': t.operator,
      'sort-manufacturer': t.aircraft
    };
    
    Object.entries(headers).forEach(([id, text]) => {
      const element = document.getElementById(id);
      if (element) {
        element.textContent = text;
      }
    });
  }
  
  updateDayNames(t) {
    // Update day names in heatmap
    const dayNames = t.days_short;
    if (dayNames && Array.isArray(dayNames)) {
      document.querySelectorAll('.heat-label').forEach((label, index) => {
        if (index < dayNames.length) {
          label.textContent = dayNames[index];
        }
      });
    }
  }
  
  updateRadiusAltitudeInfo(t) {
    const infoElement = document.getElementById('radius-altitude-info');
    if (!infoElement) return;
    
    const translateKey = infoElement.getAttribute('data-translate');
    if (!translateKey) return;
    
    let translatedText = '';
    
    if (translateKey === 'polygon_altitude_info') {
      const area = infoElement.getAttribute('data-area') || '0.0';
      const altitude = infoElement.getAttribute('data-altitude') || '2000';
      
      translatedText = t.polygon_altitude_info
        .replace('{area}', area)
        .replace('{altitude}', altitude);
    } else if (translateKey === 'radius_altitude_info') {
      const radius = infoElement.getAttribute('data-radius') || '8';
      const altitude = infoElement.getAttribute('data-altitude') || '2000';
      
      translatedText = t.radius_altitude_info
        .replace('{radius}', radius)
        .replace('{altitude}', altitude);
    }
    
    if (translatedText) {
      infoElement.textContent = translatedText;
    }
  }
  
  getElementParams(element) {
    const params = {};
    const paramAttrs = element.attributes;
    
    for (let i = 0; i < paramAttrs.length; i++) {
      const attr = paramAttrs[i];
      if (attr.name.startsWith('data-param-')) {
        const key = attr.name.replace('data-param-', '');
        params[key] = attr.value;
      }
    }
    
    return params;
  }
  
  setupLanguageSwitcher() {
    document.querySelectorAll(".lang-link[data-lang]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        this.switchLanguage(link.dataset.lang);
      });
    });
    const enLink = document.querySelector('.lang-link[href="/"]');
    const nlLink = document.querySelector('.lang-link[href="/nl/"]');
    
    if (enLink) {
      enLink.addEventListener('click', (e) => {
        e.preventDefault();
        this.switchLanguage('en');
      });
    }
    
    if (nlLink) {
      nlLink.addEventListener('click', (e) => {
        e.preventDefault();
        this.switchLanguage('nl');
      });
    }
  }
  
  switchLanguage(lang) {
    if (!this.supportedLanguages.includes(lang)) return;
    
    this.currentLanguage = lang;
    localStorage.setItem('plane_noise_language', lang);
    
    // Update URL without page reload
    const url = new URL(window.location);
    url.searchParams.set('lang', lang);
    window.history.replaceState({}, '', url);
    
    // Apply translations
    this.applyTranslations();
    
    // Update language switcher UI
    this.updateLanguageSwitcher();
    
    // Trigger custom event for other components
    window.dispatchEvent(new CustomEvent('languageChanged', { 
      detail: { language: lang } 
    }));
  }
  
  updateLanguageSwitcher() {
    const enLink = document.querySelector('.lang-link[href="/"]');
    const nlLink = document.querySelector('.lang-link[href="/nl/"]');
    
    // Remove active class from all links
    document.querySelectorAll('.lang-link').forEach(link => {
      link.classList.remove('active');
      const code = link.dataset.lang || (link.getAttribute('href') === '/nl/' ? 'nl' : link.getAttribute('href') === '/' ? 'en' : '');
      if (code && code === this.currentLanguage) link.classList.add('active');
    });
    const names = { en: "English", nl: "Nederlands", de: "Deutsch", fr: "Français", es: "Español", it: "Italiano", pt: "Português", pl: "Polski", da: "Dansk", sv: "Svenska", no: "Norsk", fi: "Suomi", cs: "Čeština", el: "Ελληνικά", tr: "Türkçe", hu: "Magyar", ro: "Română" };
    document.querySelectorAll('.theme-lang-select').forEach(select => {
      // A menu drawn before the dictionary loaded lists too few languages: complete it.
      if (select.options.length !== this.supportedLanguages.length) {
        select.innerHTML = this.supportedLanguages.map(code => '<option value="' + code + '">' + (names[code] || code) + '</option>').join('');
      }
      if (select.value !== this.currentLanguage) select.value = this.currentLanguage;
    });
    if (document.querySelector('.lang-link[data-lang]')) return;
    
    // Add active class to current language
    if (this.currentLanguage === 'en' && enLink) {
      enLink.classList.add('active');
    } else if (this.currentLanguage === 'nl' && nlLink) {
      nlLink.classList.add('active');
    }
  }
  
  // Public method to get current language
  getCurrentLanguage() {
    return this.currentLanguage;
  }
  
  // Public method to get translation
  t(key, params = {}) {
    return this.getTranslation(key, params);
  }
}

// Initialize translation system when translations are loaded
function initializeTranslationSystem() {
  if (window.Translations && Object.keys(window.Translations).length > 0) {
    console.log('Initializing translation system with translations:', window.Translations);
    window.translationSystem = new TranslationSystem();
    // Export for use in other scripts
    window.t = (key, params) => window.translationSystem.t(key, params);
  } else {
    console.log('Translations not ready, retrying...');
    // Wait a bit more and try again
    setTimeout(initializeTranslationSystem, 100);
  }
}

// Start initialization immediately
initializeTranslationSystem();

// Also try after DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializeTranslationSystem);
} else {
  initializeTranslationSystem();
}

// Fallback: Apply translations after a longer delay if system didn't initialize
setTimeout(() => {
  if (!window.translationSystem && window.Translations) {
    console.log('Fallback: Applying translations manually');
    const elements = document.querySelectorAll('[data-translate]');
    elements.forEach(element => {
      const key = element.getAttribute('data-translate');
      const translation = window.Translations.en?.[key] || key;
      element.textContent = translation;
    });
  }
}, 3000);
