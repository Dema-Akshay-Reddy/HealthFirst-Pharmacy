"""Medicine categorisation engine.

Rules combine (a) a curated brand -> molecule lookup for the drugs present in the
Zenith-2k25-MedTech dataset and (b) generic keyword rules so that any medicine
name arriving through a daily Excel upload is still categorised automatically.
"""
import re

# brand (normalised) -> metadata
KNOWN_DRUGS = {
    "dolo 650": dict(generic="Paracetamol", category="Analgesic & Antipyretic",
                     form="Tablet", schedule="OTC", storage="Store below 30°C",
                     maker="Micro Labs"),
    "pan 40": dict(generic="Pantoprazole", category="Gastrointestinal",
                   form="Tablet", schedule="Schedule H", storage="Store below 30°C",
                   maker="Aristo Pharmaceuticals"),
    "glycomet 500": dict(generic="Metformin", category="Antidiabetic",
                         form="Tablet", schedule="Schedule H", storage="Store below 30°C",
                         maker="USV Limited"),
    "telma 40": dict(generic="Telmisartan", category="Cardiovascular",
                     form="Tablet", schedule="Schedule H", storage="Store below 30°C",
                     maker="Glenmark Pharmaceuticals"),
    "allegra 120": dict(generic="Fexofenadine", category="Antiallergic",
                        form="Tablet", schedule="OTC", storage="Store below 30°C",
                        maker="Sanofi India"),
    "azithral 500": dict(generic="Azithromycin", category="Antibiotic",
                         form="Tablet", schedule="Schedule H1", storage="Store below 30°C",
                         maker="Alembic Pharmaceuticals"),
}

# keyword rules applied when the brand is unknown -> (category, generic hint)
CATEGORY_RULES = [
    (r"\b(paracetamol|crocin|dolo|calpol|acetaminophen|ibuprofen|diclofenac|nimesulide|aspirin|advil|moov)\b",
     "Analgesic & Antipyretic", "Pain / fever relief"),
    (r"\b(azithromycin|amoxicillin|augmentin|cef|cephal|penicillin|levofloxacin|ciprofloxacin|doxy|metronidazole|clav|monurol)\b",
     "Antibiotic", "Anti-infective"),
    (r"\b(metformin|glycomet|glimepiride|glipizide|insulin|januvia|sitagliptin|sugar|diabet)\b",
     "Antidiabetic", "Blood sugar control"),
    (r"\b(telmisartan|telma|amlodipine|losartan|atenolol|metoprolol|valsartan|hypertens|blood pressure|statin|atorvastatin|rosuvastatin|clopidogrel)\b",
     "Cardiovascular", "Heart / BP"),
    (r"\b(pantoprazole|pan |omeprazole|esomeprazole|pantop|ranitidine|antacid|gerd|acid)\b",
     "Gastrointestinal", "Gut health"),
    (r"\b(fexofenadine|allegra|cetirizine|levocetirizine|chlorpheniramine|allerg|cetirizine|dry cough|dextromethorphan)\b",
     "Antiallergic", "Allergy / cough"),
    (r"\b(atorva|rosuva|coagul|warfarin|heparin)\b", "Hematology", "Blood"),
    (r"\b(sertraline|fluoxetine|escitalopram|alprazolam|zolpidem|anxiet|depress|neuro)\b",
     "CNS / Mental Health", "Neurology"),
    (r"\b(vitamin|calcium|iron|folic|multivitamin|zinc|supplement|nutr)\b",
     "Vitamins & Supplements", "Nutraceutical"),
    (r"\b(enzyme|creon|digest|lactulose|ors|rehydration)\b", "Gastrointestinal", "Gut health"),
    (r"\b(cough|syrup|expectorant|ambroxol|guaifenesin)\b", "Respiratory", "Cough & cold"),
    (r"\b(contracept|progesterone|ovral|fertility)\b", "Women's Health", "Reproductive health"),
    (r"\b(derma|skin|clotrimazole|betnovate|ointment|lotion|neosporin)\b", "Dermatology", "Skin care"),
    (r"\b(eye|ear|drops|otrivin|nasal)\b", "ENT & Ophthalmic", "Sense organs"),
    (r"\b(chemo|onco|supportive)\b", "Oncology", "Cancer care"),
]

FORM_RULES = [
    (r"\bsyrup|suspension|liquid|ml\b", "Syrup"),
    (r"\binjection|vial|ampoule\b", "Injection"),
    (r"\bcapsule|cap\b", "Capsule"),
    (r"\bcream|ointment|gel\b", "Topical"),
    (r"\bdrops\b", "Drops"),
    (r"\bsachet|powder|granules\b", "Powder"),
    (r"\btablet|\d{2,4}\b", "Tablet"),
]

CATEGORY_ORDER = [
    "Analgesic & Antipyretic", "Antibiotic", "Antidiabetic", "Cardiovascular",
    "Gastrointestinal", "Antiallergic", "Respiratory", "Vitamins & Supplements",
    "CNS / Mental Health", "Dermatology", "Hematology", "Women's Health",
    "ENT & Ophthalmic", "Oncology", "Other",
]


def normalise_name(name: str) -> str:
    """Collapse case / hyphen / whitespace noise: 'Dolo-650' -> 'dolo 650'."""
    if name is None:
        return ""
    text = str(name).strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[-_/.,]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def display_name(norm: str) -> str:
    """'dolo 650' -> 'Dolo 650'."""
    parts = norm.split()
    out = []
    for p in parts:
        if p.isalpha() and len(p) > 2:
            out.append(p[0].upper() + p[1:])
        else:
            out.append(p.upper() if p.isalpha() else p)
    return " ".join(out)


def classify(raw_name: str) -> dict:
    """Return {name, norm_name, generic, category, form, schedule, storage}."""
    norm = normalise_name(raw_name)
    meta = KNOWN_DRUGS.get(norm)
    if meta:
        return dict(
            name=display_name(norm), norm_name=norm,
            generic=meta["generic"], category=meta["category"],
            form=meta["form"], schedule=meta["schedule"],
            storage=meta["storage"], maker=meta.get("maker", ""),
            known=True,
        )
    category, generic = "Other", ""
    haystack = " " + norm + " "
    for pattern, cat, hint in CATEGORY_RULES:
        if re.search(pattern, haystack):
            category, generic = cat, hint
            break
    form = "Tablet"
    for pattern, f in FORM_RULES:
        if re.search(pattern, haystack):
            form = f
            break
    return dict(
        name=display_name(norm), norm_name=norm,
        generic=generic, category=category, form=form,
        schedule="Schedule H", storage="Store below 30°C",
        maker="", known=False,
    )


def substitutes(norm_name: str) -> list[dict]:
    """Brands that can stand in for `norm_name`.

    Preference order: same molecule (generic), then same therapeutic category.
    Returns [] when nothing equivalent is catalogued — callers must say so
    explicitly rather than suggesting a wrong-substance replacement.
    """
    meta = KNOWN_DRUGS.get(norm_name)
    if not meta:
        return []
    out = []
    for norm, m in KNOWN_DRUGS.items():
        if norm != norm_name and m["generic"] == meta["generic"]:
            out.append(dict(name=display_name(norm), norm_name=norm, generic=m["generic"],
                            category=m["category"], match="molecule"))
    if not out:
        for norm, m in KNOWN_DRUGS.items():
            if norm != norm_name and m["category"] == meta["category"]:
                out.append(dict(name=display_name(norm), norm_name=norm, generic=m["generic"],
                                category=m["category"], match="category"))
    return out
