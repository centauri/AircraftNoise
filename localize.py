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
    "pt": ("pt", "Ruído de aviões em {place}",
           "Ruído de aviões sobre {place}, medido ao vivo com um sensor próprio e associado ao avião que passou: cada passagem, cada noite."),
    "pl": ("pl", "Hałas lotniczy w {place}",
           "Hałas samolotów nad {place}, mierzony na żywo własnym czujnikiem i przypisany do samolotu, który przeleciał: każdy przelot, każda noc."),
    "da": ("da", "Flystøj i {place}",
           "Flystøj over {place}, målt live med egen støjsensor og knyttet til det fly, der fløj over: hver passage, hver nat."),
    "sv": ("sv", "Flygbuller i {place}",
           "Flygbuller över {place}, mätt live med egen bullersensor och kopplat till flygplanet som passerade: varje passage, varje natt."),
    "no": ("no", "Flystøy i {place}",
           "Flystøy over {place}, målt live med egen støysensor og knyttet til flyet som passerte: hver passering, hver natt."),
    "fi": ("fi", "Lentomelu paikassa {place}",
           "Lentomelu paikan {place} yllä, mitattu suorana omalla meluanturilla ja yhdistetty ylittäneeseen lentokoneeseen: jokainen ylilento, joka yö."),
    "cs": ("cs", "Hluk z letadel v místě {place}",
           "Hluk letadel nad místem {place}, měřený živě vlastním senzorem a přiřazený k letadlu, které přeletělo: každý přelet, každou noc."),
    "el": ("el", "Θόρυβος αεροσκαφών στο {place}",
           "Θόρυβος αεροσκαφών πάνω από το {place}, μετρημένος ζωντανά με δικό μας αισθητήρα και συνδεδεμένος με το αεροσκάφος που πέρασε: κάθε διέλευση, κάθε νύχτα."),
    "tr": ("tr", "{place} uçak gürültüsü",
           "{place} üzerindeki uçak gürültüsü, kendi gürültü sensörüyle canlı ölçülüp üzerinden geçen uçakla eşleştirilir: her geçiş, her gece."),
    "hu": ("hu", "Repülőgépzaj {place} felett",
           "Repülőgépzaj {place} felett, saját zajérzékelővel élőben mérve és az átrepülő géphez rendelve: minden átrepülés, minden éjszaka."),
    "ro": ("ro", "Zgomotul avioanelor în {place}",
           "Zgomotul avioanelor deasupra localității {place}, măsurat în direct cu un senzor propriu și asociat avionului care a trecut: fiecare trecere, fiecare noapte."),
    "en": ("en", "Aircraft noise in {place}",
           "Aircraft noise over {place}, measured live on a resident's own sensor and matched to the aircraft that flew over: every pass, every night."),
}

COUNTRY_LANGUAGE = {"nl": "nl", "be": "nl", "de": "de", "at": "de", "ch": "de", "es": "es", "it": "it", "fr": "fr", "lu": "fr",
                    "pt": "pt", "br": "pt", "pl": "pl", "dk": "da", "se": "sv", "no": "no", "fi": "fi", "cz": "cs", "gr": "el", "tr": "tr", "hu": "hu", "ro": "ro"}


# Languages the owner can pick, in the order the settings list them.
LANGUAGE_NAMES = [("nl", "Nederlands"), ("de", "Deutsch"), ("en", "English"), ("fr", "Français"), ("es", "Español"), ("it", "Italiano"),
                  ("pt", "Português"), ("pl", "Polski"), ("da", "Dansk"), ("sv", "Svenska"), ("no", "Norsk"), ("fi", "Suomi"),
                  ("cs", "Čeština"), ("el", "Ελληνικά"), ("tr", "Türkçe"), ("hu", "Magyar"), ("ro", "Română")]


def language_for(country, preferred=None):
    """The owner's choice when it is one we have words for, else the country's."""
    preferred = str(preferred or "").strip().lower()
    if preferred in PHRASES:
        return preferred
    return COUNTRY_LANGUAGE.get(str(country or "").lower(), "en")


def local_seo(place, country, preferred=None):
    """(language, title phrase, description) for a place: the owner's language
    when set, else the language of the country."""
    lang = language_for(country, preferred)
    code, phrase, description = PHRASES[lang]
    place = str(place or "").strip() or "your street"
    return code, phrase.format(place=place), description.format(place=place)
