"""
One place where this project talks to Gemini.

DynamicTopicManager (engine.py), which plans a collection cycle, is the only caller.
The dashboard's connection explanations are deterministic and never call the API.
The planner needs four pieces of
defensive plumbing: an optional import, a key check, a model-alias fallback, and a
readable message when the service says no. Those were previously inlined in the
dashboard only, so the engine had no path to the API at all and any fix had to be
made twice.

Design constraints carried over from the dashboard's original implementation
-----------------------------------------------------------------------------
* The SDK is imported INSIDE the call, never at module scope. As a top-level
  import it took every dashboard tab down with ModuleNotFoundError whenever the
  package was absent, not just the one feature that needed it.
* Nothing here raises. Callers get `(text, error)`; the error is a sentence fit to
  print in a terminal or render in a panel. A collection cycle must not die
  because a language model was unavailable.
* Both SDK generations are supported. `google-generativeai` is end-of-life and
  prints a deprecation warning on import; `google-genai` is its replacement. The
  new one is preferred when installed and the legacy one keeps existing
  environments working untouched.
"""

import json
import os
import re
import warnings

import config

# Environment variable holding the API key. GEMINI_KEY is what the dashboard has
# always read; GOOGLE_API_KEY and GEMINI_API_KEY are the names both SDKs pick up
# automatically, so a user who configured either of those is not told the key is
# missing when it plainly is not.
KEY_VARS = ("GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")

# The legacy SDK announces its own end-of-life with a multi-line FutureWarning on
# first import. It is not actionable mid-scan and read like an error in the logs,
# so it is silenced here; the module docstring records the migration path.
warnings.filterwarnings("ignore", category=FutureWarning,
                        message=r"(?s).*google\.generativeai")


def api_key():
    """Returns the first configured key, or None."""
    for var in KEY_VARS:
        value = os.getenv(var)
        if value and value.strip():
            return value.strip()
    return None


def _available_sdk():
    """Returns ('genai'|'legacy'|None) for the best installed SDK generation."""
    try:
        import google.genai  # noqa: F401
        return 'genai'
    except ImportError:
        pass
    try:
        import google.generativeai  # noqa: F401
        return 'legacy'
    except ImportError:
        return None


def status():
    """
    Reports whether a Gemini call can be attempted, without attempting one.

    Returns (ok, message). The message is written for display: it names the exact
    remedy, because "AI unavailable" sent users looking for a bug that was a
    missing environment variable.
    """
    if _available_sdk() is None:
        return False, ("No Google Gemini SDK is installed. Run "
                       "`pip install -r requirements.txt` to enable AI features; "
                       "every other panel works without it.")
    if not api_key():
        return False, (f"No API key found in {' / '.join(KEY_VARS)}, so AI features "
                       "are disabled. Collection, the semantic network and the "
                       "bridge analysis all run without a key.")
    return True, ""


def generate(prompt, temperature=0.4, timeout=None, json_mode=False):
    """
    Sends one prompt and returns (text, error). Exactly one of the two is set.

    Args:
        prompt (str): The full prompt.
        temperature (float): Lower for planning (repeatable query plans), higher
            for narrative briefings.
        timeout (int|None): Seconds before giving up. Used by the planner, which
            sits in front of the whole ingestion cycle.
        json_mode (bool): Asks the API for `application/json`. Supported by both
            SDK generations and far more reliable than asking in the prompt, but
            the caller must still tolerate prose -- see `generate_json`.
    """
    ok, message = status()
    if not ok:
        return None, message

    kind = _available_sdk()
    try:
        if kind == 'genai':
            return _generate_genai(prompt, temperature, timeout, json_mode)
        return _generate_legacy(prompt, temperature, timeout, json_mode)
    except Exception as exc:                      # noqa: BLE001 - see module note
        return None, _humanise(exc)


def _generate_genai(prompt, temperature, timeout, json_mode):
    """Path for the current `google-genai` SDK."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key())
    cfg = types.GenerateContentConfig(temperature=temperature)
    if json_mode:
        cfg.response_mime_type = "application/json"
    if timeout:
        # The SDK takes milliseconds here.
        cfg.http_options = types.HttpOptions(timeout=int(timeout * 1000))

    errors = []
    for name in config.GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=name, contents=prompt, config=cfg
            )
            text = (getattr(response, 'text', '') or '').strip()
            if text:
                return text, None
            errors.append(f"model {name} returned an empty response")
        except Exception as exc:                  # noqa: BLE001
            # A retired alias fails here, not at construction, so the loop must
            # continue past a per-model failure rather than abandoning the call.
            errors.append(str(exc))
    return None, _humanise(_most_relevant(errors))


def _generate_legacy(prompt, temperature, timeout, json_mode):
    """Path for the end-of-life `google-generativeai` SDK."""
    import google.generativeai as genai

    genai.configure(api_key=api_key())
    generation_config = {"temperature": temperature}
    if json_mode:
        generation_config["response_mime_type"] = "application/json"
    request_options = {"timeout": timeout} if timeout else None

    errors = []
    for name in config.GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(name)
            kwargs = {"generation_config": generation_config}
            if request_options:
                kwargs["request_options"] = request_options
            response = model.generate_content(prompt, **kwargs)
            text = (getattr(response, 'text', '') or '').strip()
            if text:
                return text, None
            errors.append(f"model {name} returned an empty response")
        except Exception as exc:                  # noqa: BLE001
            errors.append(str(exc))
    return None, _humanise(_most_relevant(errors))


def _most_relevant(errors):
    """
    Picks the error worth reporting from one attempt per model.

    The last error is usually a retired alias's 404, which hid the real cause --
    a quota or key problem hit by the preferred model first -- and sent users
    hunting for a model-name bug.
    """
    for text in errors:
        if any(marker in text for marker in
               ("429", "quota", "RESOURCE_EXHAUSTED", "403", "PERMISSION_DENIED",
                "API key not valid")):
            return text
    return errors[-1] if errors else "no model produced a response"


def _humanise(exc):
    """Turns an SDK exception into a sentence worth showing a user."""
    text = str(exc)
    if "429" in text or "quota" in text.lower() or "RESOURCE_EXHAUSTED" in text:
        return ("The Gemini API is rate-limited or out of quota right now. "
                "Try again in about a minute.")
    if "403" in text or "PERMISSION_DENIED" in text or "API key not valid" in text:
        return ("Gemini rejected the API key (403). Check that the key is valid "
                "and that the Generative Language API is enabled for it.")
    if "deadline" in text.lower() or "timeout" in text.lower():
        return "The Gemini request timed out."
    return f"Gemini request failed: {text[:300]}"


# JSON responses arrive fenced as often as bare, even in json_mode, because the
# mime-type hint is advisory. Both shapes are handled rather than one.
_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.S)


def generate_json(prompt, temperature=0.2, timeout=None):
    """
    Sends a prompt expecting a JSON object and returns (dict, error).

    Three extraction attempts, cheapest first: parse as-is, unwrap a markdown
    fence, then take the outermost braces. A model that prefixes "Here is the
    JSON:" defeats only the first of the three, and losing a whole cycle's query
    plan to a conversational preamble is not an acceptable failure mode.
    """
    text, error = generate(prompt, temperature=temperature, timeout=timeout,
                           json_mode=True)
    if error:
        return None, error

    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed, None
        # A bare list is a plausible response to a list-shaped request, but the
        # callers here all key on field names, so it is not usable.
    return None, "Gemini returned a response that was not valid JSON."


def _json_candidates(text):
    """Yields progressively more forgiving readings of a maybe-JSON string."""
    text = (text or '').strip()
    if not text:
        return
    yield text
    fenced = _FENCE_RE.search(text)
    if fenced:
        yield fenced.group(1)
    start, end = text.find('{'), text.rfind('}')
    if 0 <= start < end:
        yield text[start:end + 1]
