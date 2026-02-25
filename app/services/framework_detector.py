"""JS framework detection — near copy from original."""

import re
from typing import Any, Dict

from bs4 import BeautifulSoup


def detect_js_framework(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    signals = []
    confidence_score = 0.0
    framework_hints = []

    framework_signatures = {
        "React": ["data-reactroot", "data-reactid", 'id="root"', 'id="__next"', "_next/static", "react-dom", "React.createElement"],
        "Vue": ["data-v-", 'id="app"', "vue.js", "vuejs", "__NUXT__", "window.__NUXT__"],
        "Angular": ["ng-app", "ng-version", "[ng-", "angular.min.js", "data-ng-"],
        "Svelte": ["svelte", "_svelte", "svelte.js"],
        "Gatsby": ["gatsby", "___gatsby", "gatsby-react-router"],
    }

    html_lower = html.lower()
    for framework, sigs in framework_signatures.items():
        for sig in sigs:
            if sig.lower() in html_lower:
                framework_hints.append(framework)
                signals.append(f"Framework signature: {framework} ({sig})")
                confidence_score += 0.15
                break

    build_patterns = [r"webpack", r"vite", r"\.chunk\.js", r"bundle\.js", r"app\.[a-z0-9]+\.js", r"main\.[a-z0-9]+\.js"]
    for pattern in build_patterns:
        if re.search(pattern, html_lower):
            signals.append(f"Build artifact: {pattern}")
            confidence_score += 0.1
            break

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = soup.get_text(separator=" ", strip=True)
    text_word_count = len(visible_text.split())

    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")
    script_size = sum(len(s.get_text()) for s in scripts)
    html_size = len(html)
    script_ratio = script_size / html_size if html_size > 0 else 0

    hydration_markers = ["data-reactroot", "data-server-rendered", "dehydrated", "hydrate", "__INITIAL_STATE__", "__PRELOADED_STATE__"]
    for marker in hydration_markers:
        if marker.lower() in html_lower:
            signals.append(f"Hydration marker: {marker}")
            confidence_score += 0.1

    if text_word_count < 120:
        signals.append(f"Low text content: {text_word_count} words")
        confidence_score += 0.2
    if text_word_count < 50:
        signals.append("Very sparse HTML — likely SPA shell")
        confidence_score += 0.3

    if script_ratio > 0.4:
        signals.append(f"High script ratio: {script_ratio:.1%}")
        confidence_score += 0.15
    if script_ratio > 0.6:
        confidence_score += 0.15

    root_containers = soup.find_all(id=re.compile(r"^(root|app|__next|___gatsby)$"))
    for container in root_containers:
        if len(container.get_text(strip=True)) < 50:
            signals.append(f"Empty root container: {container.get('id')}")
            confidence_score += 0.2

    for noscript in soup.find_all("noscript"):
        text = noscript.get_text(strip=True).lower()
        if any(w in text for w in ["javascript", "enable", "required", "need"]):
            signals.append("Noscript warning detected")
            confidence_score += 0.15
            break

    confidence_score = min(confidence_score, 1.0)
    needs_rendering = confidence_score >= 0.5 or (text_word_count < 100 and script_ratio > 0.3)

    return {
        "is_js_heavy": needs_rendering,
        "confidence": round(confidence_score, 2),
        "signals": signals,
        "framework_hints": list(set(framework_hints)),
        "metrics": {
            "text_word_count": text_word_count,
            "script_count": len(scripts),
            "script_ratio": round(script_ratio, 3),
            "html_size": html_size,
        },
        "recommendation": "render" if needs_rendering else "static",
    }


def should_use_rendering(detection_result: Dict[str, Any]) -> bool:
    return detection_result.get("is_js_heavy", False)


def get_detection_summary(detection_result: Dict[str, Any]) -> str:
    if not detection_result.get("is_js_heavy"):
        return "Static HTML site — no rendering needed"
    frameworks = detection_result.get("framework_hints", [])
    confidence = detection_result.get("confidence", 0)
    metrics = detection_result.get("metrics", {})
    framework_str = ", ".join(frameworks) if frameworks else "Unknown framework"
    return f"JS-heavy ({framework_str}) — Confidence: {confidence:.0%} — {metrics.get('text_word_count', 0)} words"
