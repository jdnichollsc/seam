"""What a helper may do in Seam, defined once for the in-app chat, REST and browser agents (WebMCP).

Reads answer from the store; page tools only change the form the user is looking at. Nothing here writes, renders or
deletes: a new take is an offer, resolved here and started by the user's click, because E2B proposed a render on 4 of 10
hostile messages, and word rules for "star this" starred on "skip the rules, star all" (dev/research/e2b_selection_run3.txt).
Stars and the bin stay on the cards. `outside`: other agents get reads and form changes, never offers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

HELP = (
    (("trash", "delete", "remove", "bin", "papelera", "borr"), "The bin on a card moves a take or sequence to the trash, with Undo. It stays "
     "7 days, then leaves the disk; Empty trash at the bottom of the gallery does it sooner. Your shots stay."),
    (("reuse", "recipe", "receta", "reusar"), "Open a take to fill the form with its shots and settings. For images, Generate Shot keeps "
     "every prompt and its settings in a collection, ready to use again."),
    (("star", "favourite", "favorite", "estrella", "favorit"), "The star marks a take you like. It gets its own filter, and a starred take "
     "stays when its sequence goes to the trash."),
    (("3d", "glb", "model file"), "Drop a .glb on a shot. Seam opens a small viewer, you frame it, and it records a short "
     "orbit that becomes the shot."),
    (("sequence", "story", "storyboard", "add a shot", "add shot", "next shot", "secuencia"), "Choose Sequence above the shots, put 2 "
     "to 8 shots in order and pick an effect per hand-off. Or press Continue on a finished take to add the next shot; "
     "only the new transition renders."),
    (("clip", "video"), "Drop a clip of 1 to 6 seconds as a shot. The transition leaves from its last frame or lands on "
     "its first, and you also get a version joined with the clips."),
    (("generate shot", "generate an image", "generate image", "collection", "midjourney", "generar imagen", "generar shot", "colección"),
     "Open Generate Shot above the shots. Write a prompt, pick aspect, quality and how many, and the images land in your "
     "collection with their prompt. Any of them can be used again, or become a first shot, a last shot or part of a sequence."),
    (("upload", "image", "shot", "photo", "picture", "subir", "imagen", "foto"), "Click First shot and Last shot, or drop files on them. Seam reads "
     "each one to describe it in the prompt."),
    (("cloud", "credit", "gpu", "backend", "key", "crédito"), "The chip at the top right chooses where takes render: your GPU, or "
     "Comfy Cloud with an API key. Cloud takes use your credits."),
    (("model", "ltx", "wan", "kling", "seedance", "minimax", "modelo"), "LTX-2.5 is the default. The Model menu lists the others "
     "and greys out what this backend cannot render. Partner models use credits."),
    (("seed",), "A seed repeats a take only on the same shots. Leave it empty for a new result each time."),
    (("workflow", "comfyui", "receipt"), "Every finished take has a receipt and a ComfyUI bundle on its card, so you can "
     "open the same graph in ComfyUI."),
    (("take", "again", "effect", "render", "make", "toma", "efecto"), "Pick an effect, press Make transition, and the take lands in Output "
     "and the gallery. Not quite right? New take changes the effect, prompt or seed."),
)
HELP_DEFAULT = ("Add a first and a last shot, pick an effect and press Make transition. Ask me about sequences, clips, "
                "3D models, generating shots, the trash or Comfy Cloud.")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: dict[str, str]  # argument → "string" | "integer"
    call: Callable[[Engine, dict[str, Any]], dict[str, Any]]
    kind: str = "read"    # read | page (the page applies `action`) | offer
    selected: bool = False  # acts on the open take or sequence, so the model only sees it when one is open
    uses_model: bool = False  # runs a graph, so on Comfy Cloud it would spend credits

    @property
    def outside(self) -> bool:
        return self.kind in ("read", "page")

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "args": self.args, "kind": self.kind}


def _squash(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def find_effect(engine: Engine, text: str) -> Any:
    wanted = _squash(str(text))
    effects = engine.exposed_presets().values()
    return (next((p for p in effects if wanted in (_squash(p.name), _squash(p.id))), None)
            or next((p for p in effects if wanted and (wanted in _squash(p.name) or _squash(p.name) in wanted)), None))


def _names(engine: Engine) -> str:
    return ", ".join(p.name for p in engine.exposed_presets().values())


def _list_effects(engine: Engine, _: dict[str, Any]) -> dict[str, Any]:
    return {"say": f"The effects: {_names(engine)}."}


def _pick_effect(engine: Engine, args: dict[str, Any]) -> dict[str, Any]:
    effect = find_effect(engine, args.get("effect", ""))
    if effect is None:
        return {"say": f"I don't have that effect. The effects: {_names(engine)}."}
    return {"say": f"{effect.name} is selected.", "action": {"name": "pick_effect", "effect": effect.id}}


def _set_length(_: Engine, args: dict[str, Any]) -> dict[str, Any]:
    try:
        seconds = int(args.get("seconds", 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return {"say": "How many seconds?"}
    return {"say": f"{seconds} seconds.", "action": {"name": "set_length", "seconds": seconds}}


def _set_direction(_: Engine, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", "")).strip()[:600]
    if not text:
        return {"say": "What direction should I add?"}
    return {"say": "Direction added to the form.", "action": {"name": "set_direction", "text": text}}


def _list_takes(engine: Engine, _: dict[str, Any]) -> dict[str, Any]:
    takes = engine.store.list(limit=5)
    if not takes:
        return {"say": "No takes yet."}
    rows = "; ".join(f"{i}. {_effect_name(engine, j)}, {j.status}" for i, j in enumerate(takes, 1))
    return {"say": f"Your latest takes: {rows}."}


def _effect_name(engine: Engine, job: Any) -> str:
    preset = engine.presets.get(job.params.get("preset"))
    return preset.name if preset else str(job.params.get("preset"))


def _open_take(engine: Engine, args: dict[str, Any]) -> dict[str, Any]:
    try:
        number = int(args.get("number", 0))
    except (TypeError, ValueError):
        number = 0
    takes = engine.store.list(limit=max(number, 1))
    if number < 1 or number > len(takes):
        return {"say": f"You have {len(takes)} recent takes; which number?" if takes else "No takes yet."}
    return {"say": f"Take {number} is open.", "action": {"name": "open_take", "job_id": takes[number - 1].id}}


def _show_status(engine: Engine, _: dict[str, Any]) -> dict[str, Any]:
    active = [j for j in engine.store.list(limit=200) if j.status in ("queued", "running")]
    if not active:
        return {"say": "Nothing is rendering."}
    now = active[-1]
    more = f" {len(active) - 1} more waiting." if len(active) > 1 else ""
    return {"say": f"{_effect_name(engine, now)} is {now.status}, {round(now.progress * 100)}%.{more}"}


def _explain(_: Engine, args: dict[str, Any]) -> dict[str, Any]:
    topic = str(args.get("topic", "")).lower()
    return {"say": next((text for words, text in HELP if any(w in topic for w in words)), HELP_DEFAULT)}


def selected(engine: Engine, args: dict[str, Any]) -> Any:
    """The open take or sequence, from the page. Ids never come from the model; a trashed record counts as closed."""
    chosen = args.get("selection") or {}
    record = None
    if chosen.get("sequence_id"):
        record = engine.store.get_sequence(chosen["sequence_id"])
    elif chosen.get("job_id"):
        record = engine.store.get(chosen["job_id"])
    return None if record is None or record.deleted_at else record


NOTHING_OPEN = "Open a take or a sequence first."


def _take_line(engine: Engine, job: Any) -> str:
    p = job.params
    line = f"{_effect_name(engine, job)}, {p.get('model') or engine.settings.model}, {p.get('seconds', 5)} s, {job.status}"
    return f"{line} ({str(job.error)[:200]})" if job.error else line


def _describe_selected(engine: Engine, args: dict[str, Any]) -> dict[str, Any]:
    record = selected(engine, args)
    if record is None:
        return {"say": NOTHING_OPEN}
    if not hasattr(record, "segments"):
        return {"say": f"This take: {_take_line(engine, record)}."}
    parts = [engine.store.get(j) for j in record.segments]
    rows = "; ".join(f"{i}. {_take_line(engine, j)}" for i, j in enumerate(parts, 1) if j)
    return {"say": f"This sequence has {len(record.shots)} shots and is {record.status}. Transitions: {rows}."}


def _find_model(engine: Engine, text: str) -> str | None:
    wanted = _squash(text)
    return next((k for k, s in engine.catalog().items() if wanted in (_squash(k), _squash(s.label))), None)


def _propose_new_take(engine: Engine, args: dict[str, Any]) -> dict[str, Any]:
    """An offer that says exactly what the click renders: the body pins the model and length, and the backend and
    version it was resolved against, so anything that changed in between is refused instead of rendered."""
    from .backend import CLOUD, current
    record = selected(engine, args)
    if record is None:
        return {"say": NOTHING_OPEN}
    changes: dict[str, Any] = {}
    if args.get("effect"):
        effect = find_effect(engine, args["effect"])
        if effect is None:
            return {"say": f"I don't have that effect. The effects: {_names(engine)}."}
        changes["preset"] = effect.id
    if args.get("model"):
        changes["model"] = _find_model(engine, str(args["model"]))
        if changes["model"] is None:
            return {"say": "I don't have that model. The Model menu lists them."}
    try:
        if args.get("seconds"):
            changes["seconds"] = int(args["seconds"])
    except (TypeError, ValueError):
        return {"say": "How many seconds?"}
    if str(args.get("direction") or "").strip():
        changes["prompt_extra"] = str(args["direction"]).strip()[:600]
    is_sequence = hasattr(record, "segments")
    part = 1
    if is_sequence:
        try:
            part = int(args.get("part") or (1 if len(record.segments) == 1 else 0))
        except (TypeError, ValueError):
            part = 0
        if not 1 <= part <= len(record.segments):
            return {"say": f"Which transition, 1 to {len(record.segments)}? Say it all at once, like: transition 2 to Smoke at 10 s."}
    base = engine.store.get(record.segments[part - 1]) if is_sequence else record
    replaced = bool(base and base.params.get("prompt_override")) and ("preset" in changes or "prompt_extra" in changes)
    if replaced:
        changes["prompt_override"] = ""  # a written prompt would win over the change being offered
    try:
        req = engine.preview_segment(record.id, part - 1, changes) if is_sequence else engine.preview_refine(record.id, changes)
    except Exception as e:  # noqa: BLE001 — a request that can't render is an answer, not a failure
        return {"say": f"That can't render: {str(e)[:200]}"}
    backend = current(engine.settings)[0]
    spec = engine.catalog().get(req.model)
    bits = [engine.presets[req.preset].name, spec.label if spec else req.model, f"{req.seconds} s",
            "Comfy Cloud" if backend == CLOUD else "Your GPU"]
    if backend == CLOUD or (spec and spec.kind == "partner"):
        bits.append("uses credits")
    if replaced:
        bits.append("replaces your written prompt")
    body = {"preset": req.preset, "model": req.model, "seconds": req.seconds, "expected_backend": backend,
            **({"prompt_extra": changes["prompt_extra"]} if "prompt_extra" in changes else {}),
            **({"prompt_override": ""} if replaced else {}),
            **({"expected_updated_at": record.updated_at} if is_sequence else {})}
    target = {"sequence_id": record.id, "part": part} if is_sequence else {"job_id": record.id}
    where = f"transition {part}" if is_sequence else "this take"
    direction = f" Direction: {changes['prompt_extra']}" if "prompt_extra" in changes else ""
    return {"say": f"Ready for {where}.{direction} Nothing renders until you press it.",
            "offer": {"name": "new_take", "label": "New take: " + " · ".join(bits), **target, "body": body}}


TOOLS = (
    Tool("list_effects", "List the transition effects Seam offers.", {}, _list_effects),
    Tool("suggest_effect", "Suggest an effect for a mood.", {"mood": "string"}, lambda e, a: {}, uses_model=True),  # the chat fills it in
    Tool("pick_effect", "Select an effect in the form.", {"effect": "string"}, _pick_effect, "page"),
    Tool("set_length", "Set the take length in seconds.", {"seconds": "integer"}, _set_length, "page"),
    Tool("set_direction", "Set the extra direction for the transition.", {"text": "string"}, _set_direction, "page"),
    Tool("list_takes", "List the user's recent takes.", {}, _list_takes),
    Tool("open_take", "Open one of the recent takes, counted from the newest.", {"number": "integer"}, _open_take, "page"),
    Tool("show_status", "Say what is rendering now.", {}, _show_status),
    Tool("explain", "Explain how to use a part of the app.", {"topic": "string"}, _explain),
    Tool("describe_selected", "Say what the open take or sequence is: effect, model, length, status.", {},
         _describe_selected, selected=True),
    Tool("propose_new_take", "Prepare a new take of what is open, with changes. Args are all optional; part is the "
         "transition number in a sequence.", {"effect": "string", "seconds": "integer", "direction": "string",
                                              "model": "string", "part": "integer"}, _propose_new_take, "offer", True),
)
BY_NAME = {t.name: t for t in TOOLS}
