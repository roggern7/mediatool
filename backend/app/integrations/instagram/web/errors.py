"""Erros do provider web e classificação de respostas HTTP (contrato: web/errors.py).

Nada aqui lê ou devolve valores de cookies/tokens. A classificação olha status, ``Location`` (só o caminho),
nomes de Set-Cookie e, em corpos JSON, apenas campos de topo estruturados. Corpo HTML nunca é varrido em busca de
marcadores (o JavaScript embutido contém palavras como ``feedback_`` e daria falso positivo).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.core.logging import redact
from app.integrations.instagram.downloader import InstagramError

if TYPE_CHECKING:
    from app.integrations.instagram.web.http import WebResponse

_MSG_CONTRACT_CHANGED = "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado."
_MSG_ACCESS_REFUSED = "O Instagram recusou esta consulta. Isso não indica sessão inválida."
_MSG_REQUEST_AUTH_FAILED = ("O Instagram recusou este acesso. O Media Tool aguarda alguns minutos e verifica de "
                            "novo; a sessão salva continua guardada.")

# Stages cujo corpo esperado é HTML (documento); qualquer outro stage espera JSON.
DOCUMENT_STAGES: frozenset[str] = frozenset({"bootstrap", "document"})

_DIAG_STR_MAX = 200
_DIAG_LIST_MAX = 30
_CODE_VALUE_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_RESTRICTION_RE = re.compile(r'feedback_required|"spam"\s*:\s*true|"restriction_enrollment_data"\s*:|"feedback_\w+"\s*:')


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in list(value)[:_DIAG_LIST_MAX]]
    return redact(str(value))[:_DIAG_STR_MAX]


class InstagramWebError(InstagramError):
    """Erro classificado do provider web. ``diagnostic`` só tem dados seguros (status, path, nomes)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, stage: str = "",
                 diagnostic: Mapping[str, object] | None = None) -> None:
        super().__init__(code, message, retryable)
        # InstagramError troca códigos fora de ERROR_CODES por internal_error; os códigos novos do provider web
        # (ex.: contract_changed) são preservados aqui.
        self.code = code
        self.stage = stage
        self.diagnostic: dict = {str(k): _safe_value(v) for k, v in (diagnostic or {}).items()}

    def __repr__(self) -> str:
        return (f"InstagramWebError(code={self.code!r}, stage={self.stage!r}, retryable={self.retryable!r}, "
                f"message={self.message!r})")


# --------------------------------------------------------------------------- auxiliares


def redirect_kind(location_path: str | None) -> str:
    """Classifica o destino de um redirect (só o caminho, já sem query)."""
    if not location_path:
        return "redirect_other"
    path = location_path.lower()
    if "/accounts/login" in path:
        return "redirect_login"
    if "/challenge" in path:
        return "redirect_challenge"
    if "/checkpoint" in path:
        return "redirect_checkpoint"
    if "consent" in path or "/privacy/checks" in path or "/terms/" in path:
        return "redirect_consent"
    if path == "/":
        return "redirect_home"
    return "redirect_other"


def _is_html(content_type: str) -> bool:
    return "html" in (content_type or "").lower()


_ANTI_HIJACK_RE = re.compile(r"\A\s*for\s*\(\s*;\s*;\s*\)\s*;")


def loads_json(text: str) -> object:
    """``json.loads`` tolerando só o prefixo anti-JSON-hijacking ``for (;;);``. ``ValueError`` se inválido."""
    match = _ANTI_HIJACK_RE.match(text or "")
    return json.loads(text[match.end():] if match else text)


def _parse_json(text: str) -> tuple[bool, object]:
    if not text or not text.strip():
        return False, None
    try:
        return True, loads_json(text)
    except ValueError:
        return False, None


def _markers_text(data: dict) -> str:
    return " ".join(str(data.get(k) or "") for k in ("status", "message", "error_type")).lower()


def _base_diagnostic(resp: WebResponse, stage: str) -> dict[str, object]:
    content_type = (resp.content_type or "").split(";", 1)[0].strip().lower()
    diag: dict[str, object] = {
        "stage": stage,
        "status": resp.status,
        "path": getattr(resp, "path", "") or "",
        "content_type": content_type,
        "size": len(resp.text or ""),
    }
    if resp.set_cookie_names:
        diag["set_cookie_names"] = list(resp.set_cookie_names)
    if resp.sessionid_deleted:
        diag["sessionid_deleted"] = True
    return diag


def _json_diagnostic(diag: dict[str, object], data: object) -> None:
    if isinstance(data, dict):
        diag["json_keys"] = sorted(str(k) for k in data)[:_DIAG_LIST_MAX]
        for key in ("status", "error_type"):  # prefixo ig_: não sobrescreve o status HTTP
            value = data.get(key)
            if isinstance(value, str) and _CODE_VALUE_RE.fullmatch(value):
                diag["ig_" + key] = value


# --------------------------------------------------------------------------- classificação


def classify_response(resp: WebResponse, *, stage: str) -> InstagramWebError | None:
    """Classifica a resposta; ``None`` = resposta utilizável.

    Ordem: checkpoint/challenge > 429 > login/require_login/sessionid apagado > feedback/restriction > 3xx >
    401/403 > 5xx > JSON inválido (``contract_changed`` quando o stage espera JSON) > outro status inesperado.

    Seção 7 de docs/BATCH_ROBUSTNESS.md: recusa de autenticação de UM request (401, require_login, login_required,
    redirect para login fora do documento) é ``request_auth_failed`` (pausa de verificação), não sessão inválida.
    Só o documento redirecionando para /accounts/login/ (``session_expired``) e o Set-Cookie apagando o
    ``sessionid`` (``session_invalid``) são evidência forte.
    """
    status = resp.status
    html = _is_html(resp.content_type)
    is_json, data = (False, None) if html else _parse_json(resp.text or "")
    obj = data if isinstance(data, dict) else None
    # Corpo que não é HTML nem JSON: busca textual limitada (sem devolver nada do texto).
    raw = "" if (html or is_json) else (resp.text or "")[:8192].lower()
    markers = _markers_text(obj) if obj is not None else ""
    kind = redirect_kind(resp.location_path) if 300 <= status < 400 else None

    diag = _base_diagnostic(resp, stage)
    if kind is not None:
        diag["redirect"] = kind
        diag["location_path"] = resp.location_path or ""
    if is_json:
        _json_diagnostic(diag, data)

    # Mensagens ao usuário em pt-BR simples e sem jargão; o detalhe técnico vai só em diagnostic["detail"].
    def err(code: str, message: str, detail: str, *, retryable: bool = False) -> InstagramWebError:
        return InstagramWebError(code, message, retryable=retryable, stage=stage,
                                 diagnostic={**diag, "detail": detail})

    # 1. checkpoint / challenge
    checkpoint = (
        kind in ("redirect_challenge", "redirect_checkpoint")
        or (obj is not None and ("checkpoint_url" in obj or "challenge" in obj
                                 or "checkpoint_required" in markers or "challenge_required" in markers))
        or "checkpoint_required" in raw or "challenge_required" in raw
    )
    if checkpoint:
        return err("checkpoint_required",
                   "O Instagram pediu uma verificação de segurança. Abra o Instagram no navegador com esta conta, "
                   "conclua a verificação e salve a sessão de novo em Configurações.",
                   kind if kind in ("redirect_challenge", "redirect_checkpoint") else "checkpoint_marker")

    # 2. 429
    if status == 429:
        return err("rate_limited", "O Instagram limitou os acessos por enquanto. Tente de novo mais tarde.",
                   "http_429", retryable=True)

    # 3. login / require_login / sessionid apagado
    if kind == "redirect_login":
        if stage in DOCUMENT_STAGES:
            return err("session_expired",
                       "O Instagram pediu para entrar de novo ao abrir a página inicial. Entre no Instagram pelo "
                       "navegador e salve a sessão de novo em Configurações, ou teste a sessão.", kind)
        return err("request_auth_failed", _MSG_REQUEST_AUTH_FAILED, kind, retryable=True)
    # 200 com `data` não vazio (dados úteis, talvez com `errors` parcial): só evidência forte de topo
    # (require_login true) conta; marcadores textuais/aninhados não derrubam a sessão.
    useful_data = status == 200 and obj is not None and bool(obj.get("data"))
    if useful_data:
        require_login = obj.get("require_login") is True
    else:
        require_login = (
            (obj is not None and (obj.get("require_login") is True or "login_required" in markers))
            or "login_required" in raw or re.search(r'"require_login"\s*:\s*true', raw) is not None
        )
    if require_login:
        return err("request_auth_failed", _MSG_REQUEST_AUTH_FAILED, "require_login", retryable=True)
    if resp.sessionid_deleted:
        return err("session_invalid",
                   "O Instagram encerrou a sessão salva. Entre no Instagram pelo navegador e salve uma sessão nova "
                   "em Configurações.", "sessionid_deleted")
    if "please wait a few minutes" in markers or "please wait a few minutes" in raw:
        return err("rate_limited", "O Instagram pediu para aguardar alguns minutos. Tente de novo mais tarde.",
                   "please_wait", retryable=True)

    # 4. feedback / restriction (campos explícitos de topo)
    if obj is not None:
        blocked = (obj.get("spam") is True or "feedback_required" in markers
                   or "restriction_enrollment_data" in obj
                   or any(isinstance(k, str) and k.startswith("feedback_") for k in obj))
    else:
        blocked = bool(raw) and _RESTRICTION_RE.search(raw) is not None
    if blocked:
        return err("action_blocked",
                   "O Instagram recusou esta consulta com um aviso de restrição. Isso vale para esta consulta e "
                   "não prova que a conta inteira esteja restrita.", "restriction_fields")

    # 5. outros redirects (sessão não encerrada nesta resposta)
    if kind is not None:
        return err("access_refused", _MSG_ACCESS_REFUSED, kind)

    # 6. 401 / 403
    if status == 401:
        return err("request_auth_failed", _MSG_REQUEST_AUTH_FAILED, "http_401", retryable=True)
    if status == 403:
        return err("access_refused", _MSG_ACCESS_REFUSED, "http_403")

    # 7. 5xx
    if status >= 500:
        return err("network_error", "O Instagram está com problemas para responder agora. Tente de novo mais tarde.",
                   f"http_{status}", retryable=True)

    # 8. sucesso: JSON inválido quando o stage espera JSON
    if 200 <= status < 300:
        if stage not in DOCUMENT_STAGES and not is_json:
            return err("contract_changed", _MSG_CONTRACT_CHANGED, "invalid_json")
        return None

    # 9. qualquer outro status (ex.: 400, 404) não previsto pelo contrato da consulta
    return err("contract_changed", _MSG_CONTRACT_CHANGED, f"http_{status}")


_MSG_POST_NOT_FOUND = "A publicação não existe mais ou não está disponível."


def classify_public_document(resp: WebResponse, *, stage: str = "post_document") -> InstagramWebError | None:
    """Resposta de um GET ANÔNIMO de documento (sem cookies do dono): nenhum código de sessão é possível aqui.

    ``None`` = documento HTML utilizável. Nunca devolve ``session_*``, ``request_auth_failed``,
    ``checkpoint_required`` nem ``action_blocked``: não há conta nem sessão nesta requisição. Redirect NUNCA é
    seguido: qualquer 3xx é ``access_refused`` (detail = tipo do destino; o caminho do ``Location`` não entra no
    diagnóstico, pode carregar o shortcode). O corpo HTML nunca é varrido.

    Verificado no real (2026-10-10, Reel público): só o 200 HTML sem redirect. As demais linhas (3xx, 401/403, 404,
    429, 5xx) NÃO foram observadas neste caminho: seguem o comportamento HTTP padrão e falham de forma segura."""
    status = resp.status
    html = _is_html(resp.content_type)
    diag = _base_diagnostic(resp, stage)
    diag.pop("sessionid_deleted", None)   # sem sessão nesta requisição: o sinal não significa nada aqui

    def err(code: str, message: str, detail: str, *, retryable: bool = False) -> InstagramWebError:
        return InstagramWebError(code, message, retryable=retryable, stage=stage,
                                 diagnostic={**diag, "detail": detail})

    if 300 <= status < 400:
        kind = redirect_kind(resp.location_path)
        diag["redirect"] = kind
        return err("access_refused", _MSG_ACCESS_REFUSED, kind)
    if status == 429:
        return err("rate_limited", "O Instagram limitou os acessos por enquanto. Tente de novo mais tarde.",
                   "http_429", retryable=True)
    # Corpo que não é HTML: busca textual limitada (sem devolver nada do texto).
    if not html and "please wait a few minutes" in (resp.text or "")[:8192].lower():
        return err("rate_limited", "O Instagram pediu para aguardar alguns minutos. Tente de novo mais tarde.",
                   "please_wait", retryable=True)
    if status in (401, 403):
        return err("access_refused", _MSG_ACCESS_REFUSED, f"http_{status}")
    if status == 404:
        return err("content_unavailable", _MSG_POST_NOT_FOUND, "post_http_404")
    if status >= 500:
        return err("network_error", "O Instagram está com problemas para responder agora. Tente de novo mais tarde.",
                   f"http_{status}", retryable=True)
    if 200 <= status < 300:
        return None if html else err("contract_changed", _MSG_CONTRACT_CHANGED, "document_not_html")
    return err("contract_changed", _MSG_CONTRACT_CHANGED, f"http_{status}")


def classify_session_document(resp: WebResponse, *, stage: str = "session_document") -> InstagramWebError | None:
    """Resposta do GET do documento de UMA publicação COM a sessão do dono (2a tentativa de um link restrito no
    documento público). Mesma classificação de ``classify_response`` (a da importação por perfil): checkpoint/
    challenge -> ``checkpoint_required``; 429/"please wait" -> ``rate_limited``; redirect para login, 401 e
    ``require_login`` -> ``request_auth_failed`` (o stage não é de bootstrap: não é evidência forte de sessão
    expirada); ``Set-Cookie`` apagando o ``sessionid`` -> ``session_invalid``; restrição -> ``action_blocked``; outro
    redirect e 403 -> ``access_refused``; 5xx -> ``network_error``. Diferenças: 2xx HTML é o documento utilizável
    (``None``) e 2xx não-HTML (inclusive JSON válido) é ``contract_changed`` ``document_not_html``; 404 é ``content_unavailable``
    ``post_http_404``. O caminho do ``Location`` não entra no diagnóstico (pode carregar o shortcode). O corpo HTML
    nunca é varrido. NÃO VERIFICADO no real: nenhuma destas respostas foi observada com a sessão."""
    error = classify_response(resp, stage=stage)
    if error is None:
        # 2xx que passou na classificação (ex.: JSON válido): o documento esperado é HTML.
        if _is_html(resp.content_type):
            return None
        return InstagramWebError("contract_changed", _MSG_CONTRACT_CHANGED, stage=stage,
                                 diagnostic={**_base_diagnostic(resp, stage), "detail": "document_not_html"})
    error.diagnostic.pop("location_path", None)
    detail = error.diagnostic.get("detail")
    if detail == "invalid_json" and 200 <= resp.status < 300:
        if _is_html(resp.content_type):
            return None
        error.diagnostic["detail"] = "document_not_html"
    elif detail == "http_404":
        return InstagramWebError("content_unavailable", _MSG_POST_NOT_FOUND, stage=stage,
                                 diagnostic={**error.diagnostic, "detail": "post_http_404"})
    return error
