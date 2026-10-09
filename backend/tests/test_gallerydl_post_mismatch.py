"""Provider gallery-dl: a renovação interna da URL vencida (dentro de `download`) nunca usa mídia de OUTRA publicação
(shortcode diferente do pedido, inclusive só nas maiúsculas). 100% offline, valores FAKE."""

from __future__ import annotations

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.services.cdn_downloader import DownloadError
from test_gallerydl_downloader import (  # noqa: F401 - fake_popen é fixture
    FIXTURES,
    FakeCdn,
    fake_popen,
    fixture_lines,
    item_from_fixture,
    make_downloader,
)

OLD_URL = "https://scontent.cdninstagram.com/v/vid-b.mp4?efg=1"


def answer(replace: dict[str, str], *, only_line: int | None = None) -> list[bytes]:
    lines = (FIXTURES / "resolve_post.jsonl").read_text(encoding="utf-8").splitlines()
    out = []
    for index, line in enumerate(line for line in lines if line):
        if only_line is None or index == only_line:
            for old, new in replace.items():
                line = line.replace(old, new)
        out.append(line.encode() + b"\n")
    return out


def test_fixture_answer_for_the_right_post_is_still_used(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("resolve_post.jsonl"), [], 0, False)]
    cdn = FakeCdn([DownloadError("forbidden", "expirou"), "ok"])
    assert make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path).exists()
    assert len(cdn.calls) == 2


@pytest.mark.parametrize("lines", [
    answer({"CAROUSEL001": "OUTRAPUBLI99"}),
    answer({"CAROUSEL001": "carousel001"}),
    answer({"CAROUSEL001": "OUTRAPUBLI99"}, only_line=0),
    answer({"CAROUSEL001": "OUTRAPUBLI99"}, only_line=1),
], ids=["outra", "so_maiusculas", "mista_primeiro_item", "mista_segundo_item"])
def test_refresh_inside_download_never_uses_another_post(tmp_path, fake_popen, lines):
    item = item_from_fixture(2)
    assert item.source_post_id == "CAROUSEL001"
    fake_popen.script = [(lines, [], 0, False)]
    cdn = FakeCdn([DownloadError("forbidden", "expirou"), "ok"])
    with pytest.raises(InstagramError) as raised:
        make_downloader(tmp_path, cdn_downloader=cdn).download(item, tmp_path)
    error = raised.value
    assert (error.code, error.retryable, error.diagnostic) == ("contract_changed", False, {"detail": "post_mismatch"})
    assert cdn.calls == [OLD_URL], "nenhum download com URL de outra publicação"
    assert "OUTRAPUBLI99" not in error.message and "CAROUSEL001" not in error.message
