#!/usr/bin/env python3
"""Test : Qwen3 comprend-il une scène dictée en darija et la transforme-t-il en storyboard ?"""
import json
import sys
import time
import urllib.request

SYSTEM = """Tu es « le studio magique », un assistant pour un enfant de 8 ans qui parle surtout darija marocaine.
L'enfant DICTE sa scène d'un seul coup. Ne pose AUCUNE question. Respecte fidèlement son imagination
(tout est permis tant que c'est adapté à un enfant : pas de violence graphique, pas de contenu adulte).
Ta mission :
1. Comprendre la dictée (darija en lettres latines ou arabes, français, anglais ou mélange).
2. Écrire ce que tu as compris en français simple.
3. Écrire un récapitulatif très court en darija (lettres latines), pour que l'enfant vérifie.
4. Découper la scène en 2 à 4 plans de 3 secondes. Pour chaque plan :
   - image_prompt : description visuelle détaillée EN ANGLAIS, style film d'animation 3D mignon, format vertical
   - motion_prompt : le mouvement à animer EN ANGLAIS (personnages + caméra)
   - narration_fr : une phrase courte de voix off en français, simple et joyeuse.
Réponds uniquement en JSON."""

SCHEMA = {
    "type": "object",
    "properties": {
        "compris_fr": {"type": "string"},
        "recap_darija": {"type": "string"},
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_prompt": {"type": "string"},
                    "motion_prompt": {"type": "string"},
                    "narration_fr": {"type": "string"},
                },
                "required": ["image_prompt", "motion_prompt", "narration_fr"],
            },
        },
    },
    "required": ["compris_fr", "recap_darija", "plans"],
}


def ask(dictee: str, model: str = "qwen3:8b") -> dict:
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0.4},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": dictee}],
    }
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    res = json.loads(out["message"]["content"])
    res["_secondes"] = round(time.time() - t, 1)
    return res


if __name__ == "__main__":
    for dictee in sys.argv[1:]:
        print("=" * 70)
        print("DICTÉE :", dictee)
        print(json.dumps(ask(dictee), ensure_ascii=False, indent=2))
