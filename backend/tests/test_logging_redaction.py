import hashlib
import io
import logging

import pytest

from app.core import logging as mt_logging
from app.core.logging import RedactingFilter, configure_logging, redact, register_secret

FAKE_SECRET = "FAKE-SESSION-LOG-0000000000"


@pytest.fixture()
def restore_logging():
    root = logging.getLogger()
    handlers, filters, level = list(root.handlers), list(root.filters), root.level
    noisy_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for name, value in noisy_levels.items():
        logging.getLogger(name).setLevel(value)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.filters[:] = filters
    root.setLevel(level)


def test_redact_registered_secret_and_url_variants():
    register_secret("FAKE:SESS%3AION-0000000")
    text = redact("a FAKE:SESS%3AION-0000000 b FAKE:SESS:ION-0000000 c")
    assert "FAKE:SESS" not in text
    assert text.count("<redacted>") == 2


def test_register_ignores_short_or_empty_values():
    register_secret("")
    register_secret(None)
    register_secret("abc")
    assert redact("abc def") == "abc def"


@pytest.mark.parametrize(
    "text,leak",
    [
        ("cookie sessionid=FAKEVALUE0001; path=/", "FAKEVALUE0001"),
        ("sessionid = FAKEVALUE0002", "FAKEVALUE0002"),
        ('{"sessionid": "FAKEVALUE0003", "x": 1}', "FAKEVALUE0003"),
        ("{'sessionid': 'FAKEVALUE0004'}", "FAKEVALUE0004"),
        ("Cookie: sessionid=FAKEVALUE0005; csrftoken=FAKECSRF0005", "FAKEVALUE0005"),
        ("Set-Cookie: sessionid=FAKEVALUE0006; HttpOnly", "FAKEVALUE0006"),
        ("csrftoken=FAKECSRF0007&next=1", "FAKECSRF0007"),
        ("ds_user_id=99887766554", "99887766554"),
        ('{"csrftoken": "FAKECSRF0008"}', "FAKECSRF0008"),
        ("Authorization: Bearer FAKETOKEN0009abcdef", "FAKETOKEN0009abcdef"),
        ("headers={'Authorization': 'Basic RkFLRTpGQUtF0010'}", "RkFLRTpGQUtF0010"),
    ],
)
def test_redact_patterns(text, leak):
    out = redact(text)
    assert leak not in out
    assert "<redacted>" in out


def test_redact_keeps_normal_text():
    assert redact("baixando 10 itens de @perfil") == "baixando 10 itens de @perfil"
    assert redact(None) == ""
    assert redact(123) == "123"


def _capture_logger(name):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler, stream


def test_filter_redacts_msg_args_and_traceback():
    register_secret(FAKE_SECRET)
    logger, handler, stream = _capture_logger("test.redaction.tb")
    try:
        logger.info("sessão %s carregada", FAKE_SECRET)
        logger.info("cookies %r", {"sessionid": "FAKEVALUE-ARGS-0001"})
        try:
            raise RuntimeError(f"falhou com {FAKE_SECRET} e sessionid=FAKEVALUE-EXC-0002")
        except RuntimeError:
            logger.exception("erro ao usar sessão")
        logger.warning("stack", stack_info=True, extra={})
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert FAKE_SECRET not in output
    assert "FAKEVALUE-ARGS-0001" not in output
    assert "FAKEVALUE-EXC-0002" not in output
    assert "Traceback" in output
    assert "RuntimeError" in output


def test_configure_logging_writes_redacted_file(tmp_path, restore_logging):
    configure_logging(tmp_path / "logs", level="DEBUG")
    configure_logging(tmp_path / "logs", level="DEBUG")  # idempotente
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_media_tool_handler", False)]
    assert len(ours) == 2
    register_secret(FAKE_SECRET)
    log = logging.getLogger("app.test.child")
    log.info("valor=%s", FAKE_SECRET)
    try:
        {}["x"]
    except KeyError:
        log.error("falha com Cookie: sessionid=FAKEVALUE-FILE-0003", exc_info=True)
    logging.getLogger().info("raiz %s", FAKE_SECRET)
    for h in ours:
        h.flush()
    content = (tmp_path / "logs" / mt_logging.LOG_FILENAME).read_text(encoding="utf-8")
    assert FAKE_SECRET not in content
    assert "FAKEVALUE-FILE-0003" not in content
    assert "<redacted>" in content
    assert "KeyError" in content


def test_configure_logging_installs_on_existing_named_handlers(tmp_path, restore_logging):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    uv = logging.getLogger("uvicorn.error.testcopy")
    uv.addHandler(handler)
    uv.propagate = False
    try:
        configure_logging(tmp_path / "logs")
        register_secret(FAKE_SECRET)
        uv.error("uvicorn viu %s", FAKE_SECRET)
    finally:
        uv.removeHandler(handler)
    assert FAKE_SECRET not in stream.getvalue()
    assert "<redacted>" in stream.getvalue()


# --------------------------------------------------------------------------- regressão: uvicorn.access


def _access_record(logger_name, path):
    return logging.LogRecord(
        logger_name, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", path, "1.1", 200), None,
    )


def test_filter_keeps_args_structure():
    register_secret(FAKE_SECRET)
    record = _access_record("uvicorn.access", f"/api/x?s={FAKE_SECRET}")
    RedactingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[4] == 200 and isinstance(record.args[4], int)
    assert FAKE_SECRET not in record.args[2]
    mapping = logging.LogRecord("x", logging.INFO, __file__, 1, "%(a)s %(n)d", ({"a": FAKE_SECRET, "n": 3},), None)
    RedactingFilter().filter(mapping)
    assert mapping.getMessage() == "<redacted> 3"


def test_uvicorn_access_formatter_with_filter(capsys):
    from uvicorn.logging import AccessFormatter

    register_secret(FAKE_SECRET)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("uvicorn.access.regression")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET",
                    f"/api/media?q={FAKE_SECRET}&sessionid=FAKEVALUE-QS-0001", "1.1", 200)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    err = capsys.readouterr().err
    assert "Logging error" not in err
    assert '127.0.0.1:50000 - "GET /api/media?q=<redacted>&sessionid=<redacted> HTTP/1.1" 200' in output
    assert FAKE_SECRET not in output and "FAKEVALUE-QS-0001" not in output


def test_configure_logging_wraps_existing_uvicorn_access_handler(tmp_path, restore_logging, capsys):
    from uvicorn.logging import AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger = logging.getLogger("uvicorn.access.regression2")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    err_stream = io.StringIO()
    err_handler = logging.StreamHandler(err_stream)
    err_handler.setFormatter(logging.Formatter("%(message)s"))
    err_logger = logging.getLogger("uvicorn.error.regression2")
    err_logger.addHandler(err_handler)
    err_logger.propagate = False
    try:
        configure_logging(tmp_path / "logs")
        register_secret(FAKE_SECRET)
        logger.handle(_access_record(logger.name, f"/api/{FAKE_SECRET}"))
        # padrão que só aparece juntando template + argumento
        err_logger.error("sessionid=%s", "FAKEVALUE-CROSS-0002")
        err_logger.error("cookies %r", {"sessionid": "FAKEVALUE-CROSS-0003"})
    finally:
        logger.removeHandler(handler)
        err_logger.removeHandler(err_handler)
    output = stream.getvalue()
    err_output = err_stream.getvalue()
    assert "Logging error" not in capsys.readouterr().err
    assert '127.0.0.1:50000 - "GET /api/<redacted> HTTP/1.1" 200' in output
    assert FAKE_SECRET not in output
    assert "sessionid=<redacted>" in err_output
    assert "FAKEVALUE-CROSS-0002" not in err_output
    assert "FAKEVALUE-CROSS-0003" not in err_output


def test_formatter_redacts_even_without_filter():
    from app.core.logging import RedactingFormatter

    register_secret(FAKE_SECRET)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "valor %s", (FAKE_SECRET,), None)
    assert RedactingFormatter("%(message)s").format(record) == "valor <redacted>"
    inner = logging.Formatter("[%(message)s]")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "Cookie: sessionid=%s", ("FAKEVALUE-0004",), None)
    assert RedactingFormatter(inner=inner).format(record) == "[Cookie: <redacted>"  # cabeçalho até o fim da linha



# --------------------------------------------------------------------------- tokens do Facebook/Instagram web


@pytest.mark.parametrize(
    "text,leak",
    [
        ("fb_dtsg=NAFAKEDTSG0001:17:1700000000&x=1", "NAFAKEDTSG0001"),
        ("body: av=1&fb_dtsg=NAFAKEDTSG0002&lsd=FAKELSD0002&jazoest=26123", "NAFAKEDTSG0002"),
        ("body: av=1&fb_dtsg=NAFAKEDTSG0002&lsd=FAKELSD0002&jazoest=26123", "FAKELSD0002"),
        ("body: av=1&fb_dtsg=NAFAKEDTSG0002&lsd=FAKELSD0002&jazoest=26123", "26123"),
        ('{"lsd":"FAKELSD0003","x":1}', "FAKELSD0003"),
        ("{'fb_dtsg': 'NAFAKEDTSG0004'}", "NAFAKEDTSG0004"),
        ('{"jazoest": 26124, "ok": true}', "26124"),
        ("{'jazoest': 26125}", "26125"),
        ("X-FB-LSD: FAKELSD0005", "FAKELSD0005"),
        ("x-csrftoken: FAKECSRF0006", "FAKECSRF0006"),
        ("headers={'X-CSRFToken': 'FAKECSRF0007', 'X-FB-LSD': 'FAKELSD0007'}", "FAKECSRF0007"),
        ("headers={'X-CSRFToken': 'FAKECSRF0007', 'X-FB-LSD': 'FAKELSD0007'}", "FAKELSD0007"),
        ('{"X-FB-LSD": "FAKELSD0008"}', "FAKELSD0008"),
    ],
)
def test_redact_facebook_web_tokens(text, leak):
    out = redact(text)
    assert leak not in out
    assert "<redacted>" in out


@pytest.mark.parametrize(
    "text",
    [
        "lsdx=1 alsd=2 my_lsd_count=3",
        "jazoest calculado a partir do token",
        "campo fb_dtsg ausente na resposta",
        '{"lsd_count": 3, "xlsd": "a"}',
        "X-FB-LSD-Version: 2",
        "my-x-csrftoken-header ok",
    ],
)
def test_new_patterns_do_not_over_redact(text):
    assert redact(text) == text


def test_traceback_with_unregistered_fb_tokens_is_redacted():
    logger, handler, stream = _capture_logger("test.redaction.fbtokens")
    try:
        try:
            raise ValueError("payload fb_dtsg=NAFAKEDTSG-TB-0001&lsd=FAKELSD-TB-0001 e {\"lsd\": \"FAKELSD-TB-0002\"}")
        except ValueError:
            logger.exception("falha no POST")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "Traceback" in output and "ValueError" in output
    for leak in ("NAFAKEDTSG-TB-0001", "FAKELSD-TB-0001", "FAKELSD-TB-0002"):
        assert leak not in output


def test_configure_logging_silences_httpx_request_urls(tmp_path, restore_logging):
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    configure_logging(tmp_path / "logs", level="DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    logging.getLogger("httpx").info('HTTP Request: GET https://scontent.cdninstagram.com/v/x.mp4?oh=FAKESIG0001 "200"')
    logging.getLogger("httpcore.http11").debug("send_request_headers.started FAKESIG0002")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (tmp_path / "logs" / mt_logging.LOG_FILENAME).read_text(encoding="utf-8")
    assert "FAKESIG0001" not in content and "FAKESIG0002" not in content


def test_configure_logging_without_console(tmp_path, restore_logging, monkeypatch):
    monkeypatch.setenv("MEDIA_TOOL_LOG_CONSOLE", "0")
    configure_logging(tmp_path / "logs")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_media_tool_handler", False)]
    assert len(ours) == 1
    assert isinstance(ours[0], logging.handlers.RotatingFileHandler)


def test_job_steps_only_in_debug(tmp_path, restore_logging, monkeypatch):
    monkeypatch.setenv("MEDIA_TOOL_LOG_CONSOLE", "0")
    log = logging.getLogger("media_tool.jobs")
    path = tmp_path / "logs" / mt_logging.LOG_FILENAME
    for level in ("INFO", "DEBUG"):
        configure_logging(tmp_path / "logs", level=level)
        log.info("Job %s iniciado (kind=%s)", 7, "ingest")
        log.info("Job %s: etapa %s", 7, level)
        log.info("Job %s concluído (etapa final %s)", 7, level)
        log.warning("Job %s falhou: %s (%s)", 8, "x", level)
    for h in logging.getLogger().handlers:
        h.flush()
    content = path.read_text(encoding="utf-8")
    assert "Job 7: etapa INFO" not in content
    assert "Job 7: etapa DEBUG" in content
    assert content.count("Job 7 iniciado") == 1
    assert "Job 7 concluído (etapa final INFO)" in content
    assert "Job 8 falhou: x (INFO)" in content


# --------------------------------------------------------------------------- URLs assinadas e cursores


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GET https://scontent-fake.cdninstagram.com/v/a.jpg?oh=FAKE_SIGNED_1&oe=1 falhou",
         "GET <url-midia> falhou"),
        ("baixando https://instagram.fgru5-1.fna.fbcdn.net/v/t51/x.mp4?efg=FAKE_SIGNED_2 agora",
         "baixando <url-midia> agora"),
        ("https://cdninstagram.com/x", "<url-midia>"),
        ("veja https://www.instagram.com/p/ABC123/?igsh=FAKE_SIGNED_3#frag",
         "veja https://www.instagram.com/p/ABC123/"),
        ("http://user:FAKEPASS@example.com:8080/a/b?token=FAKE_SIGNED_4", "http://example.com:8080/a/b"),
        ("erro em HTTPS://WWW.INSTAGRAM.COM/graphql/query/?variables=FAKE_SIGNED_5.",
         "erro em https://www.instagram.com/graphql/query/"),
    ],
)
def test_urls_reduced_and_cdn_removed(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text,leak",
    [
        ("cursor=FAKE_CURSOR_9", "FAKE_CURSOR_9"),
        ("end_cursor=QVFFAKECURSOR0001&first=12", "QVFFAKECURSOR0001"),
        ("after=FAKE-CURSOR-0002", "FAKE-CURSOR-0002"),
        ('{"end_cursor": "QVFFAKECURSOR0003", "has_next_page": true}', "QVFFAKECURSOR0003"),
        ("{'after': 'FAKE-CURSOR-0004'}", "FAKE-CURSOR-0004"),
        ("página com token QVFBakeCursor0005abcdefghijklmnopqrstuvwxyzAB==", "QVFBakeCursor0005"),
        ("next FAKEcursor_0006-abcdefghijklmnopqrstuvwxyz0123456789", "FAKEcursor_0006"),
    ],
)
def test_cursors_and_opaque_tokens_redacted(text, leak):
    out = redact(text)
    assert leak not in out
    assert "<redacted>" in out


@pytest.mark.parametrize(
    "text",
    [
        r"C:\media-tool\storage\originals\2026\10\ab" + "\\" + "ab" * 32 + ".mp4",
        "sha256 " + hashlib.sha256(b"media-tool").hexdigest() + " ok",
        "sha256 " + hashlib.sha256(b"media-tool").hexdigest().upper() + " ok",
        "sha1 " + "a1b2c3d4e5" * 4,
        r"temp C:\media-tool\storage\temp\cdn-" + "f" * 32 + ".part",
        "/tmp/pytest-of-user/pytest-12/test_update_extra_keeps_sessionid_and_lists_only_key_names0/secrets",
        "PolarisProfilePostsTabContentQueryConnection carregado",
        r"C:\Users\Fulano Teste\AppData\Local\Temp\pytest-of-Fulano Teste\pytest-3\x",
        "depois de 3 tentativas (after 3 retries)",
        "cursor ausente na resposta",
        "/api/media/12/thumbnail",
    ],
)
def test_no_over_redaction_of_paths_hashes_and_words(text):
    assert redact(text) == text


def test_traceback_with_signed_url_and_cursor_is_redacted():
    logger, handler, stream = _capture_logger("test.redaction.urls")
    try:
        try:
            raise RuntimeError("GET https://scontent-x.cdninstagram.com/v/a.jpg?oh=FAKE_SIGNED_TB&oe=2 cursor=FAKE_CURSOR_TB")
        except RuntimeError:
            logger.exception("falha %s", "https://www.instagram.com/api/v1/feed/?max_id=FAKE_SIGNED_ARG")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    for leak in ("FAKE_SIGNED_TB", "FAKE_CURSOR_TB", "FAKE_SIGNED_ARG", "cdninstagram", "scontent", "oh=", "oe="):
        assert leak not in output
    assert "https://www.instagram.com/api/v1/feed/" in output


# --------------------------------------------------------------------------- URL de CDN sem esquema e cursor numérico


_CDN_NO_SCHEME = [
    "scontent-gru1-1.cdninstagram.com/v/t51.2885-15/abc.jpg?stp=FAKE_STP&_nc_ht=FAKE_NCHT&oh=FAKEOH01&oe=FAKEOE01",
    "instagram.fgru1-1.fna.fbcdn.net/o1/v/t16/f2/m86/FAKEPATH01.mp4?efg=FAKEEFG01&oh=FAKEOH02&oe=FAKEOE02",
    "//scontent-gru1-1.cdninstagram.com/v/t51.2885-15/abc.jpg?oh=FAKEOH03&oe=FAKEOE03",
    "//instagram.fgru1-1.fna.fbcdn.net/o1/v/t16/FAKEPATH04.mp4",
    "scontent-gru1-1.cdninstagram.com/v/t51.2885-15/FAKEPATH05.jpg",
    "instagram.fgru1-1.fna.fbcdn.net?oh=FAKEOH06",
]


@pytest.mark.parametrize("url", _CDN_NO_SCHEME)
def test_schemeless_cdn_url_removed(url):
    assert redact(f"baixando {url} agora") == "baixando <url-midia> agora"
    assert redact(f"fim em {url}.") != f"fim em {url}."
    assert redact(f'{{"display_url": "{url}"}}') == '{"display_url": "<url-midia>"}'
    assert redact(repr({"src": url})) == "{'src': '<url-midia>'}"


def test_schemeless_cdn_url_with_escaped_json_slashes():
    text = r'{"u":"scontent-gru1-1.cdninstagram.com\/v\/t51.2885-15\/abc.jpg?oh=FAKEOH07&oe=FAKEOE07"}'
    assert redact(text) == '{"u":"<url-midia>"}'
    text = r'{"u":"\/\/instagram.fgru1-1.fna.fbcdn.net\/o1\/v\/FAKEPATH08.mp4"}'
    assert redact(text) == '{"u":"<url-midia>"}'


_NUMERIC_CURSORS = ["3400000000000000000_25025320", "3400000000000000000", "1234567890123456"]


@pytest.mark.parametrize("cursor", _NUMERIC_CURSORS)
def test_numeric_cursor_without_key_redacted(cursor):
    assert redact(f"próxima página {cursor}") == "próxima página <redacted>"
    assert redact(f"próxima página {cursor}.") == "próxima página <redacted>."
    assert redact(f'{{"next_max_id": "{cursor}", "more_available": true}}') == (
        '{"next_max_id": "<redacted>", "more_available": true}'
    )
    assert redact(f'{{"next_max_id":{cursor}}}') == '{"next_max_id":<redacted>}'
    assert redact(repr(("max_id", cursor))) == "('max_id', '<redacted>')"
    assert redact(f"max_id={cursor}&count=12") == "max_id=<redacted>&count=12"


def test_traceback_with_schemeless_cdn_url_and_numeric_cursor_is_redacted():
    logger, handler, stream = _capture_logger("test.redaction.schemeless")
    try:
        try:
            raise RuntimeError(
                "falhou scontent-gru1-1.cdninstagram.com/v/t51.2885-15/abc.jpg?oh=FAKEOH-TB&oe=FAKEOE-TB "
                "max_id 3400000000000000000_25025320"
            )
        except RuntimeError:
            logger.exception("erro %s %r", "//instagram.fgru1-1.fna.fbcdn.net/o1/v/FAKEPATH-ARG.mp4",
                             {"cursor_cru": "3400000000000000001_25025321"})
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "Traceback" in output and "RuntimeError" in output
    for leak in ("FAKEOH-TB", "FAKEOE-TB", "FAKEPATH-ARG", "cdninstagram", "fbcdn", "3400000000000000000",
                 "25025320", "3400000000000000001", "25025321"):
        assert leak not in output
    assert "<url-midia>" in output and "<redacted>" in output


@pytest.mark.parametrize(
    "text",
    [
        "sha256 " + hashlib.sha256(b"cursor").hexdigest() + " ok",
        "sha256 " + "1234567890" * 6 + "abcd ok",
        "job 123456789012 media 42 concluído",
        "id 999999999999",
        "criado em 2026-10-03T12:34:56.123456+00:00",
        "criado em 2026-10-03 12:34:56,789",
        "tamanho 123456789012345 bytes",
        "servidor em 127.0.0.1:8765",
        "valor 0.30000000000000004 e 1.2345678901234567e+20",
        "objeto em 0x000001F2A3B4C5D6",
        r"C:\media-tool\storage\originals\2026\10\3400000000000000000_25025320.mp4",
        r"C:\media-tool\storage\originals\12\1234567890123456789012345678.jpg",
        "/api/v1/media/3400000000000000000_25025320/info/",
        "arquivo 3400000000000000000_25025320.mp4 salvo",
        "nome cdn-3400000000000000000 e v-1234567890123456789",
        "_CDN_HOSTS = ('cdninstagram.com', 'fbcdn.net')",
        "host cdninstagram.com recusou; fbcdn.net também",
        "notcdninstagram.com/v/x e meu-fbcdn.network/a",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
    ],
)
def test_schemeless_and_numeric_rules_do_not_over_redact(text):
    assert redact(text) == text


def test_registered_secret_prefix_does_not_leak_rest_of_value():
    """Regressão (ordem das regras): segredo registrado que é prefixo de um cursor/token não deixa o resto."""
    register_secret("3400000000000")
    out = redact("cursor solto 3400000000000000000_25025320 e token 3400000000000ABCdef123456 fim")
    assert "25025320" not in out and "000000_" not in out and "ABCdef123456" not in out
    assert out.count("<redacted>") == 2 and out.endswith(" fim")


@pytest.mark.parametrize("text", [
    "erro em scontent-gru1-1.cdninstagram.com%2Fv%2Ft51%2Fx.jpg%3Foh%3DFAKEOH%26oe%3DFAKEOE fim",
    "u=https%3A%2F%2Fvideo.fgru1-1.fna.fbcdn.net%2Fo1%2Fx.mp4%3Foh%3DFAKEOH%26oe%3DFAKEOE fim",
])
def test_percent_encoded_cdn_url_is_removed(text):
    out = redact(text)
    assert "FAKEOH" not in out and "FAKEOE" not in out and "x." not in out
    assert "<url-midia>" in out and out.endswith(" fim")


def test_registered_secret_in_url_hostname_is_redacted_despite_lowercasing():
    register_secret("FAKEsecretZ9")
    out = redact("GET https://FAKEsecretZ9.example.com/path falhou")
    assert "fakesecretz9" not in out.lower() and out.endswith(" falhou")
