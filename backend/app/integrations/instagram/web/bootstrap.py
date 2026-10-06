"""Bootstrap do Instagram Web: GET do documento, extração de fb_dtsg/lsd (e pista de viewer) e cálculo do jazoest.

Contrato: web/bootstrap.py. Valores extraídos ficam só em memória (``WebState`` com repr redigido e não
serializável) e são registrados em ``register_secret``. Logs e diagnósticos usam só nomes de marcadores.

O viewer do HTML inicial (``USER_ID`` etc.) NÃO prova autenticação: num navegador logado o documento inicial veio com
``"USER_ID":"0"`` e mesmo assim a consulta GraphQL respondeu com dados e ``xdt_viewer.user.id`` (a identidade chega
depois, na hidratação/respostas). Por isso o bootstrap não classifica a sessão pelo viewer; ele é só pista.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from app.core.logging import register_secret
from app.integrations.instagram.web.errors import InstagramWebError, classify_response
from app.integrations.instagram.web.http import InstagramWebHttp

_log = logging.getLogger("media_tool.instagram.web.bootstrap")

_MSG_CONTRACT_CHANGED = "O Instagram mudou a forma de carregar a página e o Media Tool precisa ser atualizado."


class WebState:
    """Tokens em memória. Classe com ``__slots__`` (sem ``__dict__``): ``vars``/``dataclasses.asdict`` não a
    expõem; repr redigido; pickle/copy bloqueados."""

    __slots__ = ("fb_dtsg", "lsd", "jazoest", "viewer_id", "app_id", "created_at")

    def __init__(self, fb_dtsg: str, lsd: str, jazoest: str, viewer_id: str | None, app_id: str | None,
                 created_at: float) -> None:
        self.fb_dtsg = fb_dtsg
        self.lsd = lsd
        self.jazoest = jazoest
        self.viewer_id = viewer_id
        self.app_id = app_id
        self.created_at = created_at  # time.monotonic() do momento da extração (base do TTL)

    def __copy__(self) -> object:
        raise TypeError("WebState não é copiável")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("WebState não é copiável")

    def __repr__(self) -> str:
        return f"WebState(<redacted>, html_viewer={self.viewer_id is not None})"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return repr(self)

    def __getstate__(self) -> object:
        raise TypeError("WebState não é serializável")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("WebState não é serializável")

    def __reduce__(self) -> object:
        raise TypeError("WebState não é serializável")


@dataclass
class BootstrapParse:  # seguro para log: só nomes/booleanos
    markers_found: list[str]
    markers_missing: list[str]
    # Pista (não prova) do viewer no HTML inicial: "real" (id != "0"), "zero" ("0") ou "absent". Nunca o valor.
    html_viewer_hint: str


# --------------------------------------------------------------------------- marcadores

# Aspas opcionalmente escapadas (JSON dentro de string JS: \"token\":\"...\").
_Q = r'\\?"'
# Valor: qualquer caractere exceto aspas e barra invertida, aceitando o escape "\/".
_VAL = r'((?:[^"\\]|\\/)+)'
_SEP = r"\s*:\s*"


def _key(name: str) -> str:
    return _Q + re.escape(name) + _Q


def _define(name: str) -> str:
    """``"NAME",[],{ ... "token":"<valor>" ... }`` (ordem das chaves do objeto indiferente)."""
    return (_key(name) + r"\s*,\s*\[\s*\]\s*,\s*\{[^{}]*?" + _key("token") + _SEP + _Q + _VAL + _Q)


_FB_DTSG_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DTSGInitialData", re.compile(_define("DTSGInitialData"))),
    ("DTSGInitData", re.compile(_define("DTSGInitData"))),
    ("fb_dtsg_input", re.compile(
        r"\{[^{}]*?" + _key("name") + _SEP + _key("fb_dtsg") + r"[^{}]*?" + _key("value") + _SEP + _Q + _VAL + _Q)),
    ("fb_dtsg_input", re.compile(
        r"\{[^{}]*?" + _key("value") + _SEP + _Q + _VAL + _Q + r"[^{}]*?" + _key("name") + _SEP + _key("fb_dtsg"))),
    ("dtsg", re.compile(_key("dtsg") + _SEP + r"\{[^{}]*?" + _key("token") + _SEP + _Q + _VAL + _Q)),
)
_LSD_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("LSD", re.compile(_define("LSD"))),
    ("lsd", re.compile(_key("lsd") + _SEP + _Q + _VAL + _Q)),
)
_VIEWER_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("USER_ID", re.compile(_key("USER_ID") + _SEP + _Q + r"(\d+)" + _Q)),
    ("viewerId", re.compile(_key("viewerId") + _SEP + _Q + r"(\d+)" + _Q)),
    ("actorID", re.compile(_key("actorID") + _SEP + _Q + r"(\d+)" + _Q)),
)
_APP_ID_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("APP_ID", re.compile(_key("APP_ID") + _SEP + _Q + r"(\d+)" + _Q)),
    ("appId", re.compile(_key("appId") + _SEP + _Q + r"(\d+)" + _Q)),
)


def _first(html: str, markers: tuple[tuple[str, re.Pattern[str]], ...]) -> tuple[str | None, str | None]:
    for name, pattern in markers:
        match = pattern.search(html)
        if match:
            return name, match.group(1).replace("\\/", "/")
    return None, None


def compute_jazoest(fb_dtsg: str) -> str:
    """``"2"`` + soma dos códigos dos caracteres do fb_dtsg."""
    return "2" + str(sum(ord(ch) for ch in fb_dtsg))


def parse_bootstrap(html: str) -> tuple[WebState | None, BootstrapParse]:
    """Extrai tokens do HTML. ``WebState`` só quando fb_dtsg e lsd existem; viewer "0" ou ausente não impede
    (``viewer_id`` None) e não diz nada sobre a sessão."""
    html = html or ""
    found: list[str] = []
    missing: list[str] = []

    dtsg_marker, fb_dtsg = _first(html, _FB_DTSG_MARKERS)
    lsd_marker, lsd = _first(html, _LSD_MARKERS)
    viewer_marker, viewer = _first(html, _VIEWER_MARKERS)
    app_marker, app_id = _first(html, _APP_ID_MARKERS)
    for group, marker in (("fb_dtsg", dtsg_marker), ("lsd", lsd_marker), ("viewer", viewer_marker),
                          ("app_id", app_marker)):
        if marker:
            found.append(marker)
        elif group != "app_id":
            missing.append(group)

    for value in (fb_dtsg, lsd, viewer):
        register_secret(value)

    viewer_id = viewer if viewer and viewer != "0" else None
    has_tokens = bool(fb_dtsg and lsd)
    hint = "real" if viewer_id is not None else ("zero" if viewer is not None else "absent")
    parse = BootstrapParse(markers_found=found, markers_missing=missing, html_viewer_hint=hint)
    if not has_tokens:
        return None, parse
    jazoest = compute_jazoest(fb_dtsg)
    register_secret(jazoest)
    state = WebState(fb_dtsg=fb_dtsg, lsd=lsd, jazoest=jazoest, viewer_id=viewer_id, app_id=app_id,
                     created_at=time.monotonic())
    return state, parse


# --------------------------------------------------------------------------- cache


class WebBootstrap:
    STAGE = "bootstrap"

    def __init__(self, http: InstagramWebHttp, *, ttl: float = 900.0, path: str = "/") -> None:
        self._http = http
        self._ttl = ttl
        self._path = path
        self._state: WebState | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"WebBootstrap(path={self._path!r}, cached={self._state is not None})"

    def invalidate(self) -> None:
        with self._lock:
            self._state = None

    def get(self, *, force: bool = False) -> WebState:
        """Estado em cache (TTL) ou 1 GET do documento. Levanta ``InstagramWebError`` classificado."""
        with self._lock:
            state = self._state
            if state is not None and not force and time.monotonic() - state.created_at < self._ttl:
                return state
            self._state = None
            resp = self._http.get_document(self._path)
            error = classify_response(resp, stage=self.STAGE)
            if error is not None:
                raise error
            state, parse = parse_bootstrap(resp.text)
            size = len(resp.text or "")
            _log.info("Bootstrap do Instagram Web: status %s, %d bytes, marcadores %s, ausentes %s, "
                      "pista de viewer no HTML %s", resp.status, size, parse.markers_found, parse.markers_missing,
                      parse.html_viewer_hint)
            diagnostic = {"markers_missing": parse.markers_missing, "markers_found": parse.markers_found,
                          "size": size, "html_viewer_hint": parse.html_viewer_hint}
            # Mensagens ao usuário sem jargão; detalhes técnicos só em diagnostic["detail"].
            # Só a falta de fb_dtsg/lsd é erro aqui; o viewer do HTML não classifica a sessão (ver docstring).
            if state is None:
                raise InstagramWebError(
                    "contract_changed", _MSG_CONTRACT_CHANGED, stage=self.STAGE,
                    diagnostic={**diagnostic, "detail": "token_marker_missing"})
            self._state = state
            return state
