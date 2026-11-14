"""@brief The AI model of every analysis step: one table, chosen in «Modello AI», read at every call site.

@details User request (2026-11-12): «per passi diversi quali modelli vengono
usati e dove è definita questa cosa?». Every step of an analysis is owned by
the profile prompt it sends (`STEPS`); each step uses its own model when one
is chosen, otherwise the single model. The choice is GLOBAL (never per
component type: user decision, option A) and lives in the profile database,
table `metadata`, under the names ``step_model:<field>`` -- not among the AI
defaults, so nothing that reads those defaults changes.

The model is resolved AT THE CALL SITE (`model_for`), before images are
sized for it and before the photo cache key is computed: a model swapped
later would make the cache return one model's answer under another's name.

Values of a step:
- absent: the step has no choice of its own (the single model), except the
  two steps the old «Posizioni pad» model served (pad positions, body
  dimensions), which keep that model until a choice is saved;
- ``""``: explicitly the single model;
- a model identifier: that model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

#: Prefix of the metadata names holding the model of a step.
STEP_KEY_PREFIX = "step_model:"

#: Group captions, in the order the «Modello AI» table shows them.
GROUP_DOCUMENTS = "Lettura documenti"
GROUP_PHOTO = "Fotografia (solo schede con componenti)"
GROUP_SYMBOL = "Simbolo"


@dataclass(frozen=True)
class Step:
    """@brief One analysis step with its own model choice.

    @param field The profile prompt field that owns the step (the key of the choice).
    @param title Italian catalogue source shown in the «Modello AI» table.
    @param group One of the GROUP_* captions.
    @param hint Italian catalogue source: when the step runs, in a few words.
    @param images True when the step sends images (the model must read them).
    """
    field: str
    title: str
    group: str
    hint: str
    images: bool = True


#: Every step with its own model, in table order.
STEPS: tuple[Step, ...] = (
    Step("system_prompt_single", "Lettura — componente singolo", GROUP_DOCUMENTS,
         "Datasheet e disegni di un componente singolo."),
    Step("system_prompt", "Lettura — schede e moduli", GROUP_DOCUMENTS,
         "Datasheet e disegni di una scheda; anche i riconteggi dei lati."),
    Step("numbering_prompt", "Numerazione dei pin", GROUP_DOCUMENTS,
         "Rilegge dal disegno i numeri dei pin di una griglia."),
    Step("pad_positions_prompt", "Posizioni dei pad", GROUP_DOCUMENTS,
         "Componente singolo: quote delle posizioni dei pad."),
    Step("body_dimensions_prompt", "Quote del corpo", GROUP_DOCUMENTS,
         "Componente singolo: quote del corpo e altezza."),
    Step("contact_style_prompt", "Tipo di contatto sul bordo", GROUP_DOCUMENTS,
         "Moduli con contatti sul bordo (castellated): mezzo foro o foro intero."),
    Step("inventory_prompt", "Inventario dei componenti", GROUP_PHOTO,
         "Rilevatori con YOLO o solo visione: elenca i corpi sulla foto."),
    Step("grounding_prompt", "Localizzazione dei corpi", GROUP_PHOTO,
         "Solo con il rilevatore «AI (attuale)»: trova i corpi sulla foto."),
    Step("verification_prompt", "Verifica dei corpi", GROUP_PHOTO,
         "Solo con il rilevatore «AI (attuale)»: conferma i corpi trovati."),
    Step("crop_prompt", "Lettura dei ritagli", GROUP_PHOTO,
         "Rilegge ogni corpo ingrandito."),
    Step("marking_prompt", "Lettura delle sigle", GROUP_PHOTO,
         "Legge le sigle stampate sui componenti."),
    Step("terminal_prompt", "Terminali", GROUP_PHOTO,
         "Solo con il rilevatore «AI (attuale)»: terminali di ogni corpo."),
    Step("lead_prompt", "Piedini", GROUP_PHOTO,
         "Solo su comando manuale: piedini di un corpo."),
    # Sends the component documents (PDF, images): it needs a model that reads them.
    Step("pin_type_prompt", "Tipi elettrici dei pin", GROUP_SYMBOL,
         "Propone il tipo elettrico di ogni pin del simbolo."),
)

#: Profile prompts that never own a request: they ride inside another step's
#: request and use its model.
STEP_OF_FIELD: dict[str, str] = {
    "inventory_enumerate_prompt": "inventory_prompt",
    "inventory_enumerate_empty_prompt": "inventory_prompt",
    "mechanical_evidence_prompt": "verification_prompt",
}

#: Steps that kept the old «Posizioni pad» model (profile field 'pad_model')
#: before a choice of their own is saved: the behaviour of 2026-10-30..11-12.
LEGACY_PAD_STEPS = ("pad_positions_prompt", "body_dimensions_prompt")

_BY_FIELD = {step.field: step for step in STEPS}


def step(field: str) -> Step:
    """@brief The step owning `field` (a step field, or a prompt riding inside one).

    @param field Profile prompt field.
    @return Its Step.
    @throws KeyError When the field owns no step.
    """
    return _BY_FIELD[STEP_OF_FIELD.get(field, field)]


def step_models(stored: Mapping[str, str], legacy_pad_model: Optional[str] = None) -> dict[str, str]:
    """@brief The model of every step: {field: model identifier, or "" for the single model}.

    @param stored The saved choices, {field: value} (ProfileStore.step_models_stored).
    @param legacy_pad_model The old «Posizioni pad» model (profile field
        'pad_model'): the model of LEGACY_PAD_STEPS that have no saved choice.
    @return One entry per STEPS field; "" means the single model.
    """
    result = {}
    for item in STEPS:
        value = stored.get(item.field)
        if value is None and item.field in LEGACY_PAD_STEPS:
            value = legacy_pad_model or ""
        result[item.field] = (value or "").strip()
    return result


def model_for(field: str, models: Optional[Mapping[str, str]], fallback: str) -> str:
    """@brief The model a call of `field` uses: the step's own model, else `fallback`.

    @param field The profile prompt field the call sends (a step field or a
        prompt riding inside one: see STEP_OF_FIELD).
    @param models The step models (`step_models`), or None (no choice: every
        call uses its fallback, the behaviour before 2026-11-12).
    @param fallback The model the call site used before (the single model,
        or the session model chosen in the Project tab).
    @return The model identifier to send.
    """
    if not models:
        return fallback
    try:
        owner = step(field).field
    except KeyError:
        return fallback
    return (models.get(owner) or "").strip() or fallback


def step_key(field: str) -> str:
    """@brief The metadata name holding the model of the step `field`.

    @param field Profile prompt field of a step.
    @return «step_model:<field>».
    """
    return STEP_KEY_PREFIX + field


def usable_model(field: str, models: Optional[Mapping[str, str]], fallback: str, progress=None,
                 openrouter_check=None) -> str:
    """@brief `model_for`, checked: a step model that cannot run leaves the step to `fallback`, with a log line.

    @param field Profile prompt field the call sends.
    @param models The step models (`step_models`), or None.
    @param fallback The model the call site used before (single or session model).
    @param progress Optional callable(str) receiving the log line.
    @param openrouter_check Optional credit check passed to ensure_providers_ready.
    @return The step's own model when it can run, else `fallback`.
    @note Same rules as the old «Posizioni pad» reader
        (datasheet_extractor._pad_reading_model): a step that sends images
        refuses a model declared text-only (openrouter.image_capability: the
        OpenRouter catalogue, borrowed for DeepSeek direct); a provider other than
        the fallback's is checked for key and credit. An optional model never
        stops the run: the step falls back and the log says why.
    """
    from .messages import tx
    chosen = model_for(field, models, fallback)
    if chosen == fallback:
        return fallback
    from .openrouter import IMAGES_NOT_READ, OpenRouterError, image_capability
    from .providers import ProviderError, ensure_providers_ready, provider_of
    try:
        title = tx(step(field).title)
        needs_images = step(field).images
    except KeyError:
        title, needs_images = field, True
    reason = None
    # The same rule as the model picker, for both providers: a fresh explicit
    # «text only» declaration refuses; an undeclared capability never does.
    if needs_images and image_capability(chosen) == IMAGES_NOT_READ:
        reason = tx("legge solo testo")
    elif provider_of(chosen) != provider_of(fallback):
        try:
            ensure_providers_ready([chosen], progress, openrouter_check=openrouter_check)
        except (OpenRouterError, ProviderError) as exc:
            reason = str(exc)
    if reason is None:
        return chosen
    if progress:
        progress(tx("Modello del passo «{step}» {model} non utilizzabile ({reason}): lo esegue il "
                    "modello {fallback}.", step=title, model=chosen, reason=reason, fallback=fallback))
    return fallback


def default_model_image_problem(model: str, models: Optional[Mapping[str, str]] = None,
                                fields=None) -> Optional[str]:
    """@brief Before a run: the message when the DEFAULT model is declared text only and image steps use it.

    @param model The default model the run would use (the single model, or
        the session model of the Project tab).
    @param models The step models (`step_models`), or None: every step on
        the default model.
    @param fields The step fields the command may run (a step field or a
        prompt riding inside one), or None for every step.
    @return A translated message naming the image steps that would run on
        `model`, or None when the model is not declared text only
        (openrouter.image_capability: undeclared or stale data never
        refuses) or no image step of the run follows it.
    @note A step's OWN unusable model already falls back to the default
        model (usable_model); the default has no fallback. DeepSeek direct
        refuses the request before sending it; on OpenRouter the call is
        sent to a model that cannot read the pictures. This check lets the
        caller stop before paying either way.
    """
    from .messages import tx
    from .openrouter import IMAGES_NOT_READ, image_capability
    wanted = None
    if fields is not None:
        wanted = set()
        for field in fields:
            try:
                wanted.add(step(field).field)
            except KeyError:
                continue
    affected = [item for item in STEPS
                if item.images and (wanted is None or item.field in wanted)
                and not ((models or {}).get(item.field) or "").strip()]
    if not affected or image_capability(model) != IMAGES_NOT_READ:
        return None
    return tx("Il modello predefinito {model} non legge immagini (dichiarato da OpenRouter): "
              "i passi {steps} mandano immagini e lo userebbero. Scegli un altro modello in "
              "«Modello AI».", model=model, steps=", ".join("«" + tx(item.title) + "»"
                                                              for item in affected))
