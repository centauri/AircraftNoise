"""The words a resident would search for, in the language of the station's
country, for the page title and description. Nothing hidden: the same words
sit on the page. Unknown countries get English."""

PHRASES = {
    "nl": ("nl", "Vliegtuiggeluid in {place}",
           "Vliegtuigherrie boven {place}, live gemeten met een eigen geluidssensor en gekoppeld aan het vliegtuig dat erover kwam: elke passage, elke nacht."),
    "de": ("de", "Fluglärm in {place}",
           "Fluglärm über {place}, live gemessen mit einem eigenen Lärmsensor und dem Flugzeug zugeordnet, das darüber flog: jeder Überflug, jede Nacht."),
    "es": ("es", "Ruido de aviones en {place}",
           "Ruido de aviones sobre {place}, medido en directo con un sensor propio y asociado al avión que pasó: cada paso, cada noche."),
    "it": ("it", "Rumore aereo a {place}",
           "Rumore degli aerei sopra {place}, misurato dal vivo con un sensore proprio e associato all'aereo che è passato: ogni passaggio, ogni notte."),
    "fr": ("fr", "Bruit des avions à {place}",
           "Bruit des avions au-dessus de {place}, mesuré en direct avec un capteur personnel et associé à l'avion qui est passé : chaque passage, chaque nuit."),
    "en": ("en", "Aircraft noise in {place}",
           "Aircraft noise over {place}, measured live on a resident's own sensor and matched to the aircraft that flew over: every pass, every night."),
}

COUNTRY_LANGUAGE = {"nl": "nl", "be": "nl", "de": "de", "at": "de", "ch": "de", "es": "es", "it": "it", "fr": "fr", "lu": "fr"}


def local_seo(place, country):
    """(language, title phrase, description) for a place in a country."""
    lang = COUNTRY_LANGUAGE.get(str(country or "").lower(), "en")
    code, phrase, description = PHRASES[lang]
    place = str(place or "").strip() or "your street"
    return code, phrase.format(place=place), description.format(place=place)
